"""Feeder: цикл опроса due-каналов, паузы (прокси/breaker), ограниченная очередь."""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol

from tg_crawler.breaker import Breaker
from tg_crawler.config import Settings
from tg_crawler.models import ChannelTask
from tg_crawler.proxy_pool import ProxyPool
from tg_crawler.worker import WriterProtocol
from tg_crawler.writer import Op

logger = logging.getLogger(__name__)


class RepositoryProtocol(Protocol):
    async def claim(
        self, batch: int, lease: float
    ) -> tuple[list[ChannelTask], list[ChannelTask]]: ...

    async def release(self, tasks: Sequence[ChannelTask]) -> None: ...


class Feeder:
    def __init__(
        self,
        repository: RepositoryProtocol,
        pool: ProxyPool,
        breaker: Breaker,
        writer: WriterProtocol,
        settings: Settings,
        rnd: random.Random | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._repository = repository
        self._pool = pool
        self._breaker = breaker
        self._writer = writer
        self._settings = settings
        self._rnd = rnd or random.Random()
        self._sleep: Callable[[float], Awaitable[None]] = sleep or asyncio.sleep
        self._queue: asyncio.Queue[ChannelTask] = asyncio.Queue(maxsize=settings.queue_size)
        self._stopping = False

    @property
    def queue(self) -> asyncio.Queue[ChannelTask]:
        return self._queue

    async def run(self) -> None:
        while not self._stopping:
            if not self._pool.has_alive():
                logger.error("no alive proxies, feeder paused")
                await self._sleep(self._settings.feeder_poll_interval)
                continue
            if self._breaker.is_open():
                logger.error(
                    "circuit breaker open, feeder paused for %s s", self._settings.breaker_pause
                )
                await self._sleep(self._settings.breaker_pause)
                continue
            free = self._queue.maxsize - self._queue.qsize()
            batch = self._breaker.allow_claim(min(free, self._settings.claim_batch))
            if batch > 0:
                await self._claim_batch(batch)
            await self._sleep(self._settings.feeder_poll_interval)

    async def _claim_batch(self, batch: int) -> None:
        valid, invalid = await self._repository.claim(batch, self._settings.lease)
        for task in invalid:
            logger.error("channel %r has invalid name, marking broken", task.name)
            self._writer.submit(
                Op(kind="broken", task=task, at=datetime.now(UTC), reason="invalid_name")
            )
        for index, task in enumerate(valid):
            try:
                self._queue.put_nowait(task)
            except asyncio.QueueFull:
                await self._repository.release(valid[index:])
                return

    async def stop(self) -> None:
        """Останавливает захват и возвращает незапущенные задачи в БД."""
        self._stopping = True
        tasks: list[ChannelTask] = []
        while True:
            try:
                tasks.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if tasks:
            await self._repository.release(tasks)
