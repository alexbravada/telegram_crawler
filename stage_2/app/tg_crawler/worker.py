"""Worker: semaphore → acquire → fetch → release → parse (to_thread) → outbox → publish."""

import asyncio
import logging
import random
from datetime import UTC, datetime, timedelta
from typing import Protocol

from tg_crawler.breaker import Breaker
from tg_crawler.config import Settings
from tg_crawler.fetcher import Fetcher, FetchResponse
from tg_crawler.models import ChannelTask, Outcome, Post, PublishStatus
from tg_crawler.outbox import OutboxStore
from tg_crawler.parser import LayoutChanged, parse_posts
from tg_crawler.protocols import Publisher
from tg_crawler.proxy_pool import ProxyPool
from tg_crawler.scheduling import next_check_after_ok, not_found_delay, retry_delay
from tg_crawler.writer import Op

logger = logging.getLogger(__name__)


class WriterProtocol(Protocol):
    def submit(self, op: Op) -> None: ...


class Worker:
    def __init__(
        self,
        settings: Settings,
        pool: ProxyPool,
        fetcher: Fetcher,
        publisher: Publisher,
        breaker: Breaker,
        writer: WriterProtocol,
        outbox: OutboxStore,
        rnd: random.Random | None = None,
    ) -> None:
        self._settings = settings
        self._pool = pool
        self._fetcher = fetcher
        self._publisher = publisher
        self._breaker = breaker
        self._writer = writer
        self._outbox = outbox
        self._rnd = rnd or random.Random()
        self._sem = asyncio.Semaphore(settings.max_inflight)

    async def process(self, task: ChannelTask) -> None:
        """Обрабатывает одну задачу канала; исключения не выпускает наружу."""
        async with self._sem:
            try:
                await self._process(task)
            except Exception:
                logger.exception("unexpected error processing channel %s", task.name)
                self._writer.submit(
                    Op(
                        kind="retry",
                        task=task,
                        at=datetime.now(UTC),
                        error="unexpected error",
                    )
                )

    async def _process(self, task: ChannelTask) -> None:
        now = datetime.now(UTC)
        lease = await self._pool.acquire()
        response: FetchResponse | None = None
        try:
            response = await self._fetcher.fetch(lease.client, task.name)
        finally:
            if response is not None:
                self._pool.release(lease, response.outcome, response.retry_after)
            else:
                self._pool.release(lease, Outcome.RETRYABLE)

        assert response is not None
        if response.outcome is Outcome.OK:
            await self._handle_ok(task, response, now)
        elif response.outcome is Outcome.NOT_FOUND:
            self._breaker.record(Outcome.NOT_FOUND)
            self._writer.submit(
                Op(
                    kind="not_found",
                    task=task,
                    at=now,
                    next_check_at=now + timedelta(seconds=not_found_delay()),
                    proxy_id=lease.id,
                )
            )
        elif response.outcome is Outcome.INVALID_NAME:
            self._breaker.record(Outcome.INVALID_NAME)
            self._writer.submit(Op(kind="broken", task=task, at=now, reason="invalid_name"))
        else:
            self._writer.submit(
                Op(
                    kind="retry",
                    task=task,
                    at=now,
                    next_check_at=now + timedelta(seconds=retry_delay(self._rnd)),
                    error=response.error,
                )
            )

    async def _handle_ok(self, task: ChannelTask, response: FetchResponse, now: datetime) -> None:
        html = response.html
        assert html is not None
        try:
            page = await asyncio.to_thread(parse_posts, html, task.name, None)
        except LayoutChanged as exc:
            logger.error("layout changed for channel %s: %s", task.name, exc)
            self._breaker.record(Outcome.LAYOUT_CHANGED)
            self._writer.submit(
                Op(kind="layout_error", task=task, at=now, error=str(exc) or "layout changed")
            )
            return
        self._breaker.record(Outcome.OK)

        posts = tuple(
            post
            for post in page.posts
            if task.last_post_id is None or post.post_id > task.last_post_id
        )
        if posts:
            await self._outbox.insert(posts)

        last_post_at = page.posts[-1].date if page.posts else None
        next_at = next_check_after_ok(last_post_at, now, self._rnd)
        shift: int | None = None
        shift_at: datetime | None = None
        for post in posts:
            try:
                result = await self._publisher.publish(post)
            except Exception as exc:
                logger.exception("publisher crashed for %s/%s", post.channel, post.post_id)
                self._writer.submit(
                    Op(
                        kind="retry",
                        task=task,
                        at=now,
                        next_check_at=now + timedelta(seconds=retry_delay(self._rnd)),
                        error=str(exc) or type(exc).__name__,
                    )
                )
                return
            if result.status is PublishStatus.DELIVERED:
                await self._outbox.mark_published(post)
                shift, shift_at = self._bump(shift, shift_at, post)
                continue
            if result.status is PublishStatus.REJECTED:
                logger.error("post %s/%s rejected: %s", post.channel, post.post_id, result.error)
                status = await self._outbox.mark_attempt(post, result.error)
                if status == "failed":
                    shift, shift_at = self._bump(shift, shift_at, post)
            else:
                await self._outbox.mark_attempt(post, result.error)
                self._writer.submit(Op(kind="publish_retry", task=task, at=now, error=result.error))
            break
        self._writer.submit(
            Op(
                kind="ok",
                task=task,
                at=now,
                title=page.title,
                post_id=shift,
                post_at=shift_at,
                next_check_at=next_at,
            )
        )

    @staticmethod
    def _bump(
        shift: int | None, shift_at: datetime | None, post: Post
    ) -> tuple[int, datetime | None]:
        if shift is None or post.post_id > shift:
            return post.post_id, post.date
        return shift, shift_at
