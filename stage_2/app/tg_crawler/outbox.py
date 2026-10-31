"""Outbox: pending_posts — сохранение постов до публикации и цикл доставки."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import (
    and_,
    case,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tg_crawler.config import Settings
from tg_crawler.db import Channel, PendingPost
from tg_crawler.models import Post, PublishStatus
from tg_crawler.protocols import RawPublisher
from tg_crawler.publisher import build_payload

logger = logging.getLogger(__name__)

BACKOFF_BASE = 1.0
BACKOFF_CAP = 30.0


class InFlightRegistry(Protocol):
    """Ключи постов, которые воркер публикует прямо сейчас."""

    def registered_keys(self) -> set[tuple[str, int]]: ...


class OutboxStore(Protocol):
    """Интерфейс outbox для воркера."""

    async def insert(self, posts: tuple[Post, ...]) -> None: ...
    async def mark_published(self, post: Post) -> None: ...
    async def mark_attempt(self, post: Post, error: str | None) -> str: ...


@dataclass(frozen=True, slots=True)
class PendingRow:
    channel_name: str
    post_id: int
    payload: dict[str, object]
    attempts: int


class PgOutboxStore:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], max_attempts: int
    ) -> None:
        self._session_factory = session_factory
        self._max_attempts = max_attempts

    async def insert(self, posts: tuple[Post, ...]) -> None:
        if not posts:
            return
        rows = [
            {
                "channel_name": post.channel,
                "post_id": post.post_id,
                "payload": build_payload(post),
            }
            for post in posts
        ]
        async with self._session_factory() as session, session.begin():
            stmt = pg_insert(PendingPost).on_conflict_do_nothing(
                index_elements=["channel_name", "post_id"]
            )
            await session.execute(stmt, rows)

    async def mark_published(self, post: Post) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(PendingPost)
                .where(
                    PendingPost.channel_name == post.channel,
                    PendingPost.post_id == post.post_id,
                )
                .values(status="delivered", last_error=None, updated_at=func.now())
            )

    async def mark_attempt(self, post: Post, error: str | None) -> str:
        async with self._session_factory() as session, session.begin():
            row = (
                await session.execute(
                    update(PendingPost)
                    .where(
                        PendingPost.channel_name == post.channel,
                        PendingPost.post_id == post.post_id,
                    )
                    .values(
                        attempts=PendingPost.attempts + 1,
                        last_error=error,
                        updated_at=func.now(),
                        status=case(
                            (
                                and_(
                                    PendingPost.status == "pending",
                                    PendingPost.attempts + 1 >= self._max_attempts,
                                ),
                                "failed",
                            ),
                            else_=PendingPost.status,
                        ),
                    )
                    .returning(PendingPost.status)
                )
            ).one_or_none()
        if row is None:
            return "pending"
        status: str = row[0]
        if status == "failed":
            logger.error(
                "outbox post %s/%s failed after %d attempts: %s",
                post.channel,
                post.post_id,
                self._max_attempts,
                error,
            )
        return status


class OutboxDelivery:
    """Цикл доставки: публикует pending-посты из outbox тем же Publisher."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        publisher: RawPublisher,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        in_flight: InFlightRegistry | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher
        self._settings = settings
        self._clock = clock
        self._sleep: Callable[[float], Awaitable[None]] = sleep or asyncio.sleep
        self._next_try: dict[tuple[str, int], float] = {}
        self._in_flight = in_flight

    async def run(self) -> None:
        while True:
            processed = await self.step()
            if not processed:
                await self._sleep(self._settings.outbox_poll_interval)

    async def step(self) -> int:
        """Один проход по батчу pending-постов; возвращает число обработанных."""
        rows = await self._fetch_batch()
        processed = 0
        for row in rows:
            key = (row.channel_name, row.post_id)
            if self._clock() < self._next_try.get(key, 0.0):
                continue
            processed += 1
            await self._deliver(row)
        return processed

    async def _fetch_batch(self) -> list[PendingRow]:
        # Снимок in-flight до SELECT: строка из снапшота не могла стать «свежей»
        # после него (add делается до INSERT, remove — после записи статуса в БД).
        blocked = self._in_flight.registered_keys() if self._in_flight else set()
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        PendingPost.channel_name,
                        PendingPost.post_id,
                        PendingPost.payload,
                        PendingPost.attempts,
                    )
                    .where(PendingPost.status == "pending")
                    .order_by(PendingPost.attempts, PendingPost.id)
                    .limit(self._settings.outbox_batch)
                )
            ).all()
        return [
            PendingRow(channel_name=row[0], post_id=row[1], payload=row[2], attempts=row[3])
            for row in rows
            if (row[0], row[1]) not in blocked
        ]

    async def _deliver(self, row: PendingRow) -> None:
        key = (row.channel_name, row.post_id)
        try:
            result = await self._publisher.publish_raw(row.payload)
        except Exception as exc:
            logger.exception("outbox delivery crashed for %s/%s", row.channel_name, row.post_id)
            await self._mark_temp_error(
                row.channel_name, row.post_id, str(exc) or type(exc).__name__
            )
            self._next_try[key] = self._clock() + min(BACKOFF_BASE * 2**row.attempts, BACKOFF_CAP)
            return
        if result.status is PublishStatus.DELIVERED:
            await self._mark_delivered(row.channel_name, row.post_id)
            await self._shift_last_post_id(row.channel_name, row.post_id)
            self._next_try.pop(key, None)
        elif result.status is PublishStatus.REJECTED:
            failed = await self._mark_rejected(row.channel_name, row.post_id, result.error)
            if failed:
                await self._shift_last_post_id(row.channel_name, row.post_id)
            self._next_try.pop(key, None)
        else:
            await self._mark_temp_error(row.channel_name, row.post_id, result.error)
            self._next_try[key] = self._clock() + min(BACKOFF_BASE * 2**row.attempts, BACKOFF_CAP)

    async def _mark_delivered(self, channel_name: str, post_id: int) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(PendingPost)
                .where(PendingPost.channel_name == channel_name, PendingPost.post_id == post_id)
                .values(status="delivered", last_error=None, updated_at=func.now())
            )

    async def _mark_rejected(self, channel_name: str, post_id: int, error: str | None) -> bool:
        async with self._session_factory() as session, session.begin():
            row = (
                await session.execute(
                    update(PendingPost)
                    .where(
                        PendingPost.channel_name == channel_name,
                        PendingPost.post_id == post_id,
                    )
                    .values(
                        attempts=PendingPost.attempts + 1,
                        last_error=error,
                        updated_at=func.now(),
                        status=case(
                            (
                                and_(
                                    PendingPost.status == "pending",
                                    PendingPost.attempts + 1 >= self._settings.outbox_max_attempts,
                                ),
                                "failed",
                            ),
                            else_=PendingPost.status,
                        ),
                    )
                    .returning(PendingPost.status)
                )
            ).one_or_none()
        status = str(row[0]) if row is not None else "pending"
        if status == "failed":
            logger.error(
                "outbox post %s/%s failed after %d attempts: %s",
                channel_name,
                post_id,
                self._settings.outbox_max_attempts,
                error,
            )
        return status == "failed"

    async def _mark_temp_error(self, channel_name: str, post_id: int, error: str | None) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(PendingPost)
                .where(PendingPost.channel_name == channel_name, PendingPost.post_id == post_id)
                .values(
                    attempts=PendingPost.attempts + 1,
                    last_error=error,
                    updated_at=func.now(),
                )
            )

    async def _shift_last_post_id(self, channel_name: str, post_id: int) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(Channel)
                .where(Channel.channel_name == channel_name)
                .values(last_post_id=func.greatest(func.coalesce(Channel.last_post_id, 0), post_id))
            )
