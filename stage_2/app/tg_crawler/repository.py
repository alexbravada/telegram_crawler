"""Repository: claim/release задач, upsert_watch, deactivate, list_channels, ping."""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, cast

from sqlalchemy import (
    Table,
    Uuid,
    bindparam,
    func,
    literal_column,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import UpdateBase

from tg_crawler.db import Channel
from tg_crawler.models import ChannelTask
from tg_crawler.names import normalize

logger = logging.getLogger(__name__)

_t: Table = cast(Table, Channel.__table__)


@dataclass(frozen=True, slots=True)
class WatchItem:
    """Канал для upsert: имя уже разрешено через resolve_watch_name."""

    channel_name: str
    channel_url: str
    channel_id: int | None = None


@dataclass(frozen=True, slots=True)
class UpsertResult:
    channel_name: str
    created: bool
    reject_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ChannelInfo:
    """Строка канала для GET /watch_channels (v2 раздел 3.2)."""

    channel_name: str
    channel_id: int | None
    channel_url: str | None
    title: str | None
    is_active: bool
    broken_url: bool
    broken_reason: str | None
    last_post_id: int | None
    last_post_at: datetime | None
    last_checked_at: datetime | None
    next_check_at: datetime
    fail_count: int
    not_found_count: int


class SessionFactory(Protocol):
    def __call__(self) -> AsyncSession: ...


_RELEASE_STMT = (
    update(_t)
    .where(
        _t.c.id == bindparam("b_id"),
        _t.c.lease_token == bindparam("b_token", type_=Uuid()),
    )
    .values(next_check_at=func.now(), lease_token=None)
)


class Repository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def claim(self, batch: int, lease: float) -> tuple[list[ChannelTask], list[ChannelTask]]:
        """Захватывает due-каналы (FOR UPDATE SKIP LOCKED), lease на lease секунд.

        Возвращает (валидные, невалидные имена). Невалидные тоже захватываются:
        lease_token выставлен, чтобы Writer принял исход invalid_name.
        """
        token = uuid.uuid4()
        due = (
            select(Channel.id)
            .where(
                Channel.is_active.is_(True),
                Channel.broken_url.is_(False),
                Channel.next_check_at <= func.now(),
            )
            .order_by(Channel.next_check_at)
            .limit(batch)
            .with_for_update(skip_locked=True)
        )
        stmt = (
            update(Channel)
            .where(Channel.id.in_(due))
            .values(next_check_at=func.now() + timedelta(seconds=lease), lease_token=token)
            .returning(Channel.id, Channel.channel_name, Channel.last_post_id, Channel.channel_id)
        )
        async with self._session_factory() as session, session.begin():
            rows = (await session.execute(stmt)).all()
        valid: list[ChannelTask] = []
        invalid: list[ChannelTask] = []
        for row in rows:
            task = ChannelTask(
                id=row[0], name=row[1], last_post_id=row[2], token=token, channel_id=row[3]
            )
            if normalize(row[1]) is None:
                logger.error("channel %r has invalid name, will be marked broken", row[1])
                invalid.append(task)
            else:
                valid.append(task)
        return valid, invalid

    async def release(self, tasks: Sequence[ChannelTask]) -> None:
        """Возвращает незапущенные задачи: next_check_at = now(), lease_token = NULL."""
        if not tasks:
            return
        params = [{"b_id": task.id, "b_token": task.token} for task in tasks]
        async with self._session_factory() as session, session.begin():
            await session.execute(_RELEASE_STMT, params)

    async def upsert_watch(self, items: Sequence[WatchItem]) -> list[UpsertResult]:
        results: list[UpsertResult] = []
        async with self._session_factory() as session, session.begin():
            for item in items:
                excluded = pg_insert(_t).excluded
                insert_stmt = pg_insert(_t).values(
                    channel_name=item.channel_name,
                    channel_url=item.channel_url,
                    channel_id=item.channel_id,
                    is_active=True,
                    next_check_at=func.now(),
                )
                stmt: UpdateBase = insert_stmt.on_conflict_do_update(
                    index_elements=[_t.c.channel_name],
                    set_={
                        _t.c.is_active: True,
                        _t.c.next_check_at: func.now(),
                        _t.c.channel_url: excluded.channel_url,
                        _t.c.channel_id: func.coalesce(excluded.channel_id, _t.c.channel_id),
                    },
                ).returning(literal_column("xmax = 0").label("created"))
                try:
                    async with session.begin_nested():
                        created = (await session.execute(stmt)).scalar_one()
                except IntegrityError:
                    logger.error("channel_id conflict for %r, item rejected", item.channel_name)
                    results.append(UpsertResult(item.channel_name, False, "channel_id_conflict"))
                    continue
                results.append(UpsertResult(item.channel_name, bool(created), None))
        return results

    async def deactivate(self, channel_name: str) -> bool:
        async with self._session_factory() as session, session.begin():
            row = (
                await session.execute(
                    update(_t)
                    .where(_t.c.channel_name == channel_name)
                    .values(is_active=False, lease_token=None)
                    .returning(_t.c.id)
                )
            ).one_or_none()
        return row is not None

    async def list_channels(self, active: str, limit: int, offset: int) -> list[ChannelInfo]:
        stmt = select(
            Channel.channel_name,
            Channel.channel_id,
            Channel.channel_url,
            Channel.title,
            Channel.is_active,
            Channel.broken_url,
            Channel.broken_reason,
            Channel.last_post_id,
            Channel.last_post_at,
            Channel.last_checked_at,
            Channel.next_check_at,
            Channel.fail_count,
            Channel.not_found_count,
        )
        if active == "true":
            stmt = stmt.where(Channel.is_active.is_(True))
        elif active == "false":
            stmt = stmt.where(Channel.is_active.is_(False))
        stmt = stmt.order_by(Channel.channel_name).limit(limit).offset(offset)
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).all()
        return [
            ChannelInfo(
                channel_name=row[0],
                channel_id=row[1],
                channel_url=row[2],
                title=row[3],
                is_active=row[4],
                broken_url=row[5],
                broken_reason=row[6],
                last_post_id=row[7],
                last_post_at=row[8],
                last_checked_at=row[9],
                next_check_at=row[10],
                fail_count=row[11],
                not_found_count=row[12],
            )
            for row in rows
        ]

    async def ping(self) -> bool:
        try:
            async with self._session_factory() as session:
                await session.execute(text("SELECT 1"))
        except Exception:
            logger.exception("ping failed")
            return False
        return True
