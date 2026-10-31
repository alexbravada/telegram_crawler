"""CrawlerService: TaskGroup (Feeder, воркеры, Outbox Delivery, Writer, статистика).

Порядок остановки по v1 раздел 10.3:
1. Feeder перестаёт захватывать каналы; незапущенные задачи из очереди возвращаются в БД.
2. Воркеры дорабатывают текущие запросы (таймаут `shutdown_timeout`, зависшие отменяются).
3. Writer записывает весь буфер.
4. Закрываются httpx-клиенты (engine.dispose() делает вызывающий код).
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tg_crawler.breaker import Breaker
from tg_crawler.config import Settings
from tg_crawler.feeder import Feeder
from tg_crawler.fetcher import Fetcher
from tg_crawler.models import Post
from tg_crawler.outbox import OutboxDelivery, OutboxStore, PgOutboxStore
from tg_crawler.protocols import Publisher, RawPublisher
from tg_crawler.proxy_pool import ProxyPool
from tg_crawler.repository import Repository
from tg_crawler.worker import Worker, WriterProtocol
from tg_crawler.writer import Op, Writer

logger = logging.getLogger(__name__)

IN_FLIGHT_TTL = 30.0


class InFlightPosts:
    """Реестр постов, которые воркер публикует прямо сейчас (защита от дублей).

    Запись протухает через ttl — зависший/упавший воркер не блокирует доставку
    из outbox навсегда.
    """

    def __init__(self, ttl: float = IN_FLIGHT_TTL, clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl
        self._clock = clock
        self._registered: dict[tuple[str, int], float] = {}

    def add(self, key: tuple[str, int]) -> None:
        self._registered[key] = self._clock()

    def remove(self, key: tuple[str, int]) -> None:
        self._registered.pop(key, None)

    def is_in_flight(self, key: tuple[str, int]) -> bool:
        registered_at = self._registered.get(key)
        if registered_at is None:
            return False
        if self._clock() - registered_at > self._ttl:
            del self._registered[key]
            return False
        return True

    def registered_keys(self) -> set[tuple[str, int]]:
        now = self._clock()
        expired = [key for key, at in self._registered.items() if now - at > self._ttl]
        for key in expired:
            del self._registered[key]
        return set(self._registered)


class TrackedOutboxStore:
    """OutboxStore для воркера: помечает посты in-flight на время прямой публикации."""

    def __init__(self, inner: OutboxStore, registry: InFlightPosts) -> None:
        self._inner = inner
        self._registry = registry

    async def insert(self, posts: tuple[Post, ...]) -> None:
        for post in posts:
            self._registry.add((post.channel, post.post_id))
        await self._inner.insert(posts)

    async def mark_published(self, post: Post) -> None:
        await self._inner.mark_published(post)
        self._registry.remove((post.channel, post.post_id))

    async def mark_attempt(self, post: Post, error: str | None) -> str:
        status = await self._inner.mark_attempt(post, error)
        self._registry.remove((post.channel, post.post_id))
        return status


class ServicePublisher(Publisher, RawPublisher, Protocol):
    """Publisher, пригодный и для прямого пути, и для цикла доставки."""


class LoggingWriter:
    """Обёртка над Writer: JSON-лог по каждой операции (channel/outcome/proxy/post_id)."""

    def __init__(self, inner: WriterProtocol) -> None:
        self._inner = inner

    def submit(self, op: Op) -> None:
        logger.info(
            "op",
            extra={
                "channel": op.task.name,
                "outcome": op.kind,
                "proxy": op.proxy_id,
                "post_id": op.post_id,
            },
        )
        self._inner.submit(op)


class JsonFormatter(logging.Formatter):
    """JSON-логи; поля channel/outcome/proxy/post_id добавляются, если заданы."""

    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, object] = {
            "time": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("channel", "outcome", "proxy", "post_id"):
            value = record.__dict__.get(key)
            if value is not None:
                data[key] = value
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


class CrawlerService:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        publisher: ServicePublisher,
        *,
        repository: Repository | None = None,
        pool: ProxyPool | None = None,
        fetcher: Fetcher | None = None,
        breaker: Breaker | None = None,
        writer: Writer | None = None,
        delivery: OutboxDelivery | None = None,
    ) -> None:
        self._settings = settings
        self._publisher = publisher
        self._pool = (
            pool
            if pool is not None
            else ProxyPool(
                settings.proxies,
                min_interval=settings.proxy_min_interval,
                dead_after=settings.proxy_dead_after,
                recheck_interval=settings.proxy_recheck,
            )
        )
        self._fetcher = fetcher if fetcher is not None else Fetcher()
        self._breaker = (
            breaker
            if breaker is not None
            else Breaker(
                window_size=settings.breaker_window,
                threshold=settings.breaker_threshold,
                pause=settings.breaker_pause,
            )
        )
        self._writer = (
            writer
            if writer is not None
            else Writer(
                session_factory,
                self._breaker,
                settings,
                proxies_count=max(1, len(settings.proxies)),
            )
        )
        self._logging_writer = LoggingWriter(self._writer)
        self._repository = repository if repository is not None else Repository(session_factory)
        self._in_flight = InFlightPosts(ttl=IN_FLIGHT_TTL)
        outbox_store = TrackedOutboxStore(
            PgOutboxStore(session_factory, settings.outbox_max_attempts), self._in_flight
        )
        self._delivery = (
            delivery
            if delivery is not None
            else OutboxDelivery(
                session_factory,
                publisher,
                settings,
                in_flight=self._in_flight,
            )
        )
        self._feeder = Feeder(
            self._repository, self._pool, self._breaker, self._logging_writer, settings
        )
        self._workers = [
            Worker(
                settings,
                self._pool,
                self._fetcher,
                publisher,
                self._breaker,
                self._logging_writer,
                outbox_store,
            )
            for _ in range(self._settings.workers or 1)
        ]
        self._stop_event: asyncio.Event | None = None
        self._run_task: asyncio.Task[None] | None = None

    def request_stop(self) -> None:
        """Синхронный запрос остановки (для обработчиков сигналов)."""
        if self._stop_event is not None:
            self._stop_event.set()

    async def stop(self) -> None:
        """Запрашивает остановку и ждёт завершения run()."""
        if self._stop_event is None:
            return
        self._stop_event.set()
        if self._run_task is not None:
            await self._run_task

    async def run(self) -> None:
        stop_event = asyncio.Event()
        self._stop_event = stop_event
        self._run_task = asyncio.current_task()
        try:
            async with asyncio.TaskGroup() as tg:
                feeder_task = tg.create_task(self._feeder.run(), name="feeder")
                delivery_task = tg.create_task(self._delivery.run(), name="outbox-delivery")
                writer_task = tg.create_task(self._writer_loop(), name="writer")
                stats_task = tg.create_task(self._stats_loop(), name="stats")
                worker_tasks = [
                    tg.create_task(self._worker_loop(worker), name=f"worker-{index}")
                    for index, worker in enumerate(self._workers)
                ]
                await stop_event.wait()
                await self._shutdown(
                    feeder_task, delivery_task, writer_task, stats_task, worker_tasks
                )
        finally:
            self._run_task = None

    async def _shutdown(
        self,
        feeder_task: asyncio.Task[None],
        delivery_task: asyncio.Task[None],
        writer_task: asyncio.Task[None],
        stats_task: asyncio.Task[None],
        worker_tasks: list[asyncio.Task[None]],
    ) -> None:
        # 1. Feeder перестаёт захватывать; задачи из очереди возвращаются в БД.
        feeder_task.cancel()
        delivery_task.cancel()
        writer_task.cancel()
        stats_task.cancel()
        await asyncio.gather(
            feeder_task, delivery_task, writer_task, stats_task, return_exceptions=True
        )
        await self._feeder.stop()
        # 2. Воркеры дорабатывают текущие запросы, таймаут shutdown_timeout.
        if worker_tasks:
            _, pending = await asyncio.wait(worker_tasks, timeout=self._settings.shutdown_timeout)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        # 3. Writer записывает весь буфер.
        await self._writer.flush()
        # 4. Закрываются httpx-клиенты.
        await self._pool.aclose()

    async def _wait_or_stop(self, seconds: float) -> None:
        assert self._stop_event is not None
        try:
            await asyncio.wait_for(self._stop_event.wait(), seconds)
        except TimeoutError:
            pass

    async def _writer_loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            await self._wait_or_stop(self._settings.writer_flush_interval)
            await self._writer.flush()

    async def _stats_loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            await self._wait_or_stop(self._settings.stats_log_interval)
            if self._stop_event.is_set():
                return
            logger.info(
                "stats",
                extra={
                    "queue": self._feeder.queue.qsize(),
                    "proxies": self._pool.stats(),
                    "writer_buffer": len(self._writer._buffer),  # noqa: SLF001
                },
            )

    async def _worker_loop(self, worker: Worker) -> None:
        assert self._stop_event is not None
        queue = self._feeder.queue
        while not self._stop_event.is_set():
            try:
                task = await asyncio.wait_for(queue.get(), timeout=0.5)
            except TimeoutError:
                continue
            await worker.process(task)
