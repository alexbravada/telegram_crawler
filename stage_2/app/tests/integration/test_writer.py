"""Интеграционные тесты Writer (testcontainers)."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tg_crawler.breaker import Breaker
from tg_crawler.config import Settings
from tg_crawler.models import ChannelTask, Outcome
from tg_crawler.writer import Op, Writer

AT = datetime(2024, 11, 1, 12, 0, tzinfo=UTC)
NEXT = AT + timedelta(minutes=30)


def make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://u:p@db:5432/tg",
        publisher_url="http://emulator:8001/messages",
    )


def make_task(token: uuid.UUID | None = None) -> ChannelTask:
    return ChannelTask(id=1, name="durov", last_post_id=None, token=token or uuid.uuid4())


async def seed_channel(
    engine: AsyncEngine,
    token: uuid.UUID,
    **kwargs: object,
) -> None:
    columns = {
        "id": 1,
        "channel_name": "durov",
        "lease_token": token,
        "next_check_at": AT,
    }
    columns.update(kwargs)
    cols = ", ".join(columns)
    params = {key: value for key, value in columns.items()}
    placeholders = ", ".join(f":{key}" for key in columns)
    async with engine.begin() as conn:
        await conn.execute(text(f"INSERT INTO channels ({cols}) VALUES ({placeholders})"), params)


async def fetch_channel(engine: AsyncEngine) -> dict[str, object]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT title, last_post_id, fail_count, not_found_count, lease_token, "
                "broken_url, broken_reason, last_error, last_not_found_proxy "
                "FROM channels WHERE id = 1"
            )
        )
        row = result.one()
    keys = [
        "title",
        "last_post_id",
        "fail_count",
        "not_found_count",
        "lease_token",
        "broken_url",
        "broken_reason",
        "last_error",
        "last_not_found_proxy",
    ]
    return dict(zip(keys, row, strict=True))


async def test_ok_updates_title_post_id_and_resets(clean_db, engine: AsyncEngine) -> None:
    token = uuid.uuid4()
    await seed_channel(engine, token, title=None, fail_count=2, not_found_count=1, last_error="x")
    writer = Writer(clean_db, Breaker(), make_settings(), proxies_count=1)
    writer.submit(
        Op(
            kind="ok",
            task=make_task(token),
            at=AT,
            title="Durov's Channel",
            post_id=4821,
            post_at=AT,
            next_check_at=NEXT,
        )
    )
    await writer.flush()
    row = await fetch_channel(engine)
    assert row["title"] == "Durov's Channel"
    assert row["last_post_id"] == 4821
    assert row["fail_count"] == 0
    assert row["not_found_count"] == 0
    assert row["lease_token"] is None
    assert row["last_error"] is None


async def test_ok_does_not_decrease_last_post_id(clean_db, engine: AsyncEngine) -> None:
    token = uuid.uuid4()
    await seed_channel(engine, token, last_post_id=5000)
    writer = Writer(clean_db, Breaker(), make_settings(), proxies_count=1)
    writer.submit(
        Op(
            kind="ok",
            task=make_task(token),
            at=AT,
            title="t",
            post_id=4000,
            post_at=AT,
            next_check_at=NEXT,
        )
    )
    await writer.flush()
    row = await fetch_channel(engine)
    assert row["last_post_id"] == 5000


async def test_stale_token_ignored(clean_db, engine: AsyncEngine) -> None:
    token = uuid.uuid4()
    await seed_channel(engine, token)
    writer = Writer(clean_db, Breaker(), make_settings(), proxies_count=1)
    writer.submit(
        Op(
            kind="ok",
            task=make_task(uuid.uuid4()),  # чужой токен
            at=AT,
            title="evil",
            post_id=9,
            post_at=AT,
            next_check_at=NEXT,
        )
    )
    await writer.flush()
    row = await fetch_channel(engine)
    assert row["title"] is None
    assert row["lease_token"] == token


async def test_not_found_three_times_breaks_channel(clean_db, engine: AsyncEngine) -> None:
    token = uuid.uuid4()
    await seed_channel(engine, token)
    writer = Writer(clean_db, Breaker(), make_settings(), proxies_count=2)
    for i in range(3):
        writer.submit(
            Op(
                kind="not_found",
                task=make_task(token),
                at=AT + timedelta(hours=6 * i),
                next_check_at=AT + timedelta(hours=6 * i + 6),
                proxy_id=f"proxy_{i}",
            )
        )
    await writer.flush()
    row = await fetch_channel(engine)
    assert row["broken_url"] is True
    assert row["broken_reason"] == "not_found"
    assert row["not_found_count"] == 3


async def test_not_found_same_proxy_or_short_gap_not_counted(clean_db, engine: AsyncEngine) -> None:
    token = uuid.uuid4()
    await seed_channel(engine, token, last_not_found_at=AT, last_not_found_proxy="p1")
    writer = Writer(clean_db, Breaker(), make_settings(), proxies_count=2)
    # тот же прокси и меньше 6 ч — не засчитывается
    writer.submit(
        Op(
            kind="not_found",
            task=make_task(token),
            at=AT + timedelta(hours=1),
            next_check_at=AT + timedelta(hours=7),
            proxy_id="p1",
        )
    )
    await writer.flush()
    row = await fetch_channel(engine)
    assert row["not_found_count"] == 0
    assert row["broken_url"] is False

    # другой прокси, но снова меньше 6 ч — не засчитывается
    writer.submit(
        Op(
            kind="not_found",
            task=make_task(token),
            at=AT + timedelta(hours=2),
            next_check_at=AT + timedelta(hours=8),
            proxy_id="p2",
        )
    )
    await writer.flush()
    row = await fetch_channel(engine)
    assert row["not_found_count"] == 0


async def test_ok_resets_not_found_counter(clean_db, engine: AsyncEngine) -> None:
    token = uuid.uuid4()
    await seed_channel(
        engine, token, not_found_count=2, last_not_found_at=AT, last_not_found_proxy="p1"
    )
    writer = Writer(clean_db, Breaker(), make_settings(), proxies_count=2)
    writer.submit(
        Op(
            kind="ok",
            task=make_task(token),
            at=AT,
            title="t",
            post_id=1,
            post_at=AT,
            next_check_at=NEXT,
        )
    )
    await writer.flush()
    row = await fetch_channel(engine)
    assert row["not_found_count"] == 0
    assert row["last_not_found_proxy"] == "p1"  # ok не трогает not_found-поля, только счётчик


async def test_broken_invalid_name(clean_db, engine: AsyncEngine) -> None:
    token = uuid.uuid4()
    await seed_channel(engine, token)
    writer = Writer(clean_db, Breaker(), make_settings(), proxies_count=1)
    writer.submit(Op(kind="broken", task=make_task(token), at=AT, reason="invalid_name"))
    await writer.flush()
    row = await fetch_channel(engine)
    assert row["broken_url"] is True
    assert row["broken_reason"] == "invalid_name"


async def test_breaker_open_not_found_not_counted(clean_db, engine: AsyncEngine) -> None:
    token = uuid.uuid4()
    await seed_channel(engine, token)
    breaker = Breaker()
    for _ in range(80):
        breaker.record(Outcome.OK)
    for _ in range(21):
        breaker.record(Outcome.NOT_FOUND)
    assert breaker.is_open() is True
    writer = Writer(clean_db, breaker, make_settings(), proxies_count=2)
    writer.submit(
        Op(
            kind="not_found",
            task=make_task(token),
            at=AT,
            next_check_at=AT + timedelta(minutes=5),
            proxy_id="p9",
        )
    )
    await writer.flush()
    row = await fetch_channel(engine)
    assert row["not_found_count"] == 0
    assert row["broken_url"] is False
    assert row["lease_token"] is None


async def test_layout_error_increments_fail_count(clean_db, engine: AsyncEngine) -> None:
    token = uuid.uuid4()
    await seed_channel(engine, token)
    writer = Writer(clean_db, Breaker(), make_settings(), proxies_count=1)
    writer.submit(
        Op(
            kind="layout_error",
            task=make_task(token),
            at=AT,
            next_check_at=NEXT,
            error="layout",
        )
    )
    await writer.flush()
    row = await fetch_channel(engine)
    assert row["fail_count"] == 1
    assert row["last_error"] == "layout"


async def test_db_down_buffer_kept_then_flushed(clean_db, engine: AsyncEngine) -> None:
    token = uuid.uuid4()
    await seed_channel(engine, token)

    class FailingFactory:
        def __call__(self) -> object:
            raise RuntimeError("db down")

    writer = Writer(FailingFactory(), Breaker(), make_settings(), proxies_count=1)  # type: ignore[arg-type]
    writer.submit(
        Op(
            kind="ok",
            task=make_task(token),
            at=AT,
            title="t",
            post_id=42,
            post_at=AT,
            next_check_at=NEXT,
        )
    )
    await writer.flush()  # не падает, результат остаётся в буфере
    row = await fetch_channel(engine)
    assert row["title"] is None

    writer.set_session_factory(clean_db)
    await writer.flush()
    row = await fetch_channel(engine)
    assert row["title"] == "t"
    assert row["last_post_id"] == 42
