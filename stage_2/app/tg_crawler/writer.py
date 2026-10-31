"""Writer: пакетная запись результатов воркеров в БД с проверкой lease_token."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from sqlalchemy import (
    BigInteger,
    ColumnElement,
    DateTime,
    Table,
    Text,
    Uuid,
    bindparam,
    case,
    func,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tg_crawler.breaker import Breaker
from tg_crawler.config import Settings
from tg_crawler.db import Channel
from tg_crawler.models import ChannelTask
from tg_crawler.scheduling import error_backoff

logger = logging.getLogger(__name__)

OpKind = Literal["ok", "retry", "not_found", "broken", "layout_error", "publish_retry"]


@dataclass(frozen=True, slots=True)
class Op:
    kind: OpKind
    task: ChannelTask
    at: datetime
    title: str | None = None
    post_id: int | None = None
    post_at: datetime | None = None
    next_check_at: datetime | None = None
    proxy_id: str | None = None
    reason: str | None = None
    error: str | None = None


_t = cast(Table, Channel.__table__)

_B_ID = bindparam("b_id", type_=BigInteger)
_B_TOKEN = bindparam("b_token", type_=Uuid())
_B_AT = bindparam("b_at", type_=DateTime(timezone=True))
_B_NEXT = bindparam("b_next", type_=DateTime(timezone=True))
_B_TITLE = bindparam("b_title", type_=Text)
_B_POST_ID = bindparam("b_post_id", type_=BigInteger)
_B_POST_AT = bindparam("b_post_at", type_=DateTime(timezone=True))
_B_ERROR = bindparam("b_error", type_=Text)
_B_REASON = bindparam("b_reason", type_=Text)

_LEASE_WHERE = (_t.c.id == _B_ID) & (_t.c.lease_token == _B_TOKEN)

_OK_SHIFT_STMT = (
    update(_t)
    .where(_LEASE_WHERE)
    .values(
        title=_B_TITLE,
        last_post_id=func.greatest(func.coalesce(_t.c.last_post_id, 0), _B_POST_ID),
        last_post_at=case(
            (_B_POST_AT.is_(None), _t.c.last_post_at),
            else_=func.greatest(func.coalesce(_t.c.last_post_at, _B_POST_AT), _B_POST_AT),
        ),
        last_checked_at=_B_AT,
        next_check_at=_B_NEXT,
        fail_count=0,
        not_found_count=0,
        lease_token=None,
        last_error=None,
    )
)

_OK_NO_SHIFT_STMT = (
    update(_t)
    .where(_LEASE_WHERE)
    .values(
        title=_B_TITLE,
        last_checked_at=_B_AT,
        next_check_at=_B_NEXT,
        fail_count=0,
        not_found_count=0,
        lease_token=None,
        last_error=None,
    )
)

_RETRY_STMT = (
    update(_t)
    .where(_LEASE_WHERE)
    .values(
        last_checked_at=_B_AT,
        next_check_at=_B_NEXT,
        lease_token=None,
        last_error=_B_ERROR,
    )
)

_BROKEN_STMT = (
    update(_t)
    .where(_LEASE_WHERE)
    .values(
        broken_url=True,
        broken_reason=_B_REASON,
        last_checked_at=_B_AT,
        lease_token=None,
    )
)

_NF_SUPPRESSED_STMT = (
    update(_t)
    .where(_LEASE_WHERE)
    .values(
        last_checked_at=_B_AT,
        next_check_at=_B_NEXT,
        lease_token=None,
    )
)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class Writer:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        breaker: Breaker,
        settings: Settings,
        *,
        proxies_count: int,
    ) -> None:
        self._session_factory = session_factory
        self._breaker = breaker
        self._settings = settings
        self._proxies_count = proxies_count
        self._buffer: list[Op] = []

    def set_session_factory(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    def submit(self, op: Op) -> None:
        self._buffer.append(op)

    async def flush(self) -> None:
        """Пишет весь буфер; при ошибке БД оставляет данные в буфере."""
        while self._buffer:
            if not await self._flush_step():
                return

    async def _flush_step(self) -> bool:
        ops = self._buffer
        self._buffer = []
        try:
            await self._write(ops)
        except Exception:
            logger.exception("writer flush failed, %d ops kept in buffer", len(ops))
            self._buffer = ops + self._buffer
            return False
        return True

    async def _write(self, ops: list[Op]) -> None:
        groups: dict[str, list[Op]] = {}
        for op in ops:
            groups.setdefault(op.kind, []).append(op)
        async with self._session_factory() as session:
            async with session.begin():
                if "ok" in groups:
                    await self._write_ok(session, groups["ok"])
                for kind in ("retry", "publish_retry"):
                    if kind in groups:
                        await session.execute(_RETRY_STMT, self._retry_params(groups[kind]))
                if "not_found" in groups:
                    await self._write_not_found(session, groups["not_found"])
                if "layout_error" in groups:
                    await self._write_layout_error(session, groups["layout_error"])
                if "broken" in groups:
                    await session.execute(_BROKEN_STMT, self._broken_params(groups["broken"]))

    async def _write_ok(self, session: AsyncSession, ops: list[Op]) -> None:
        shift: list[dict[str, object]] = []
        no_shift: list[dict[str, object]] = []
        for op in ops:
            row: dict[str, object] = {
                "b_id": op.task.id,
                "b_token": op.task.token,
                "b_title": op.title,
                "b_at": op.at,
                "b_next": op.next_check_at,
            }
            if op.post_id is not None:
                row["b_post_id"] = op.post_id
                row["b_post_at"] = op.post_at
                shift.append(row)
            else:
                no_shift.append(row)
        if shift:
            await session.execute(_OK_SHIFT_STMT, shift)
        if no_shift:
            await session.execute(_OK_NO_SHIFT_STMT, no_shift)

    @staticmethod
    def _retry_params(ops: list[Op]) -> list[dict[str, object]]:
        return [
            {
                "b_id": op.task.id,
                "b_token": op.task.token,
                "b_at": op.at,
                "b_next": op.next_check_at,
                "b_error": op.error,
            }
            for op in ops
        ]

    @staticmethod
    def _broken_params(ops: list[Op]) -> list[dict[str, object]]:
        return [
            {
                "b_id": op.task.id,
                "b_token": op.task.token,
                "b_at": op.at,
                "b_reason": op.reason,
            }
            for op in ops
        ]

    async def _write_not_found(self, session: AsyncSession, ops: list[Op]) -> None:
        if self._breaker.is_open():
            await session.execute(_NF_SUPPRESSED_STMT, self._retry_params(ops))
            return
        released: set[int] = set()
        for op in ops:
            row = (
                await session.execute(
                    select(
                        _t.c.not_found_count,
                        _t.c.last_not_found_at,
                        _t.c.last_not_found_proxy,
                        _t.c.lease_token,
                    ).where(_t.c.id == op.task.id)
                )
            ).one_or_none()
            if row is None or (row[3] != op.task.token and op.task.id not in released):
                continue
            count: int = row[0]
            last_at: datetime | None = row[1]
            last_proxy: str | None = row[2]
            gap_ok = last_at is None or (op.at - _aware(last_at)).total_seconds() >= (
                self._settings.not_found_min_gap
            )
            proxy_ok = self._proxies_count <= 1 or op.proxy_id != last_proxy
            where = self._op_where(op, released)
            if not (gap_ok and proxy_ok):
                await session.execute(
                    update(_t)
                    .where(*where)
                    .values(
                        last_checked_at=op.at,
                        next_check_at=op.next_check_at,
                        lease_token=None,
                    )
                )
                continue
            new_count = count + 1
            values: dict[str, object] = {
                "not_found_count": new_count,
                "last_checked_at": op.at,
                "next_check_at": op.next_check_at,
                "lease_token": None,
            }
            if new_count >= self._settings.not_found_threshold:
                values["broken_url"] = True
                values["broken_reason"] = "not_found"
            else:
                values["last_not_found_at"] = op.at
                values["last_not_found_proxy"] = op.proxy_id
            await session.execute(update(_t).where(*where).values(**values))
            released.add(op.task.id)

    async def _write_layout_error(self, session: AsyncSession, ops: list[Op]) -> None:
        breaker_open = self._breaker.is_open()
        released: set[int] = set()
        for op in ops:
            row = (
                await session.execute(
                    select(_t.c.fail_count, _t.c.lease_token).where(_t.c.id == op.task.id)
                )
            ).one_or_none()
            if row is None or (row[1] != op.task.token and op.task.id not in released):
                continue
            fail_count: int = row[0]
            increment = 0 if breaker_open else 1
            next_at = op.next_check_at
            if next_at is None:
                delay = error_backoff(fail_count + increment)
                next_at = op.at + timedelta(seconds=delay)
            await session.execute(
                update(_t)
                .where(*self._op_where(op, released))
                .values(
                    fail_count=fail_count + increment,
                    last_error=op.error,
                    last_checked_at=op.at,
                    next_check_at=next_at,
                    lease_token=None,
                )
            )
            released.add(op.task.id)

    @staticmethod
    def _op_where(op: Op, released: set[int]) -> tuple[ColumnElement[bool], ...]:
        if op.task.id in released:
            # в этом же флеше lease уже отпущен первой операцией того же воркера
            return (_t.c.id == op.task.id,)
        return (_t.c.id == op.task.id, _t.c.lease_token == op.task.token)
