"""Интеграционные тесты Repository: claim/release/upsert/deactivate/list (testcontainers)."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tg_crawler.models import ChannelTask
from tg_crawler.repository import Repository, UpsertResult, WatchItem


async def seed_channel(engine: AsyncEngine, name: str, **kwargs: object) -> None:
    columns: dict[str, object] = {
        "channel_name": name,
        "next_check_at": datetime.now(UTC) - timedelta(seconds=1),
    }
    columns.update(kwargs)
    cols = ", ".join(columns)
    params = {f"p{i}": value for i, value in enumerate(columns.values())}
    placeholders = ", ".join(f":p{i}" for i in range(len(columns)))
    async with engine.begin() as conn:
        await conn.execute(  # noqa: S608
            text(f"INSERT INTO channels ({cols}) VALUES ({placeholders})"), params
        )


async def fetch_channel(engine: AsyncEngine, name: str) -> dict[str, object]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT is_active, next_check_at, lease_token, channel_id, channel_url, "
                "broken_url, broken_reason FROM channels WHERE channel_name = :name"
            ),
            {"name": name},
        )
        row = result.one()
    keys = [
        "is_active",
        "next_check_at",
        "lease_token",
        "channel_id",
        "channel_url",
        "broken_url",
        "broken_reason",
    ]
    return dict(zip(keys, row, strict=True))


async def test_claim_skips_not_due_broken_inactive(clean_db, engine: AsyncEngine) -> None:  # noqa: ANN001
    await seed_channel(engine, "due1")
    await seed_channel(engine, "future1", next_check_at=datetime.now(UTC) + timedelta(hours=1))
    await seed_channel(engine, "broken", broken_url=True, broken_reason="not_found")
    await seed_channel(engine, "inactive", is_active=False)
    repo = Repository(clean_db)
    valid, invalid = await repo.claim(batch=10, lease=600.0)
    assert [task.name for task in valid] == ["due1"]
    assert invalid == []
    row = await fetch_channel(engine, "due1")
    assert row["lease_token"] is not None
    assert row["next_check_at"] > datetime.now(UTC)


async def test_claim_returns_channel_id_and_disjoint_parallel(
    clean_db, engine: AsyncEngine
) -> None:  # noqa: ANN001
    await seed_channel(engine, "alpha", channel_id=-1001234567890)
    await seed_channel(engine, "bravo")
    await seed_channel(engine, "charli")
    repo = Repository(clean_db)
    first, second = await asyncio.gather(
        repo.claim(batch=10, lease=600.0),
        repo.claim(batch=10, lease=600.0),
    )
    names = [task.name for task in first[0] + second[0]]
    assert sorted(names) == ["alpha", "bravo", "charli"]
    task_a = next(task for task in first[0] + second[0] if task.name == "alpha")
    assert task_a.channel_id == -1001234567890
    assert task_a.last_post_id is None


async def test_claim_invalid_names_separated(clean_db, engine: AsyncEngine) -> None:  # noqa: ANN001
    await seed_channel(engine, "durov")
    await seed_channel(engine, "1abc")
    repo = Repository(clean_db)
    valid, invalid = await repo.claim(batch=10, lease=600.0)
    assert [task.name for task in valid] == ["durov"]
    assert [task.name for task in invalid] == ["1abc"]
    # невалидный тоже захвачен (lease выставлен), чтобы broken-исход прошёл проверку токена
    row = await fetch_channel(engine, "1abc")
    assert row["lease_token"] is not None


async def test_release_token_guard(clean_db, engine: AsyncEngine) -> None:  # noqa: ANN001
    await seed_channel(engine, "durov")
    repo = Repository(clean_db)
    valid, _ = await repo.claim(batch=10, lease=600.0)
    task = valid[0]

    stranger = ChannelTask(
        id=task.id,
        name=task.name,
        last_post_id=task.last_post_id,
        token=uuid.uuid4(),
        channel_id=task.channel_id,
    )
    await repo.release([stranger])
    row = await fetch_channel(engine, "durov")
    assert row["lease_token"] is not None

    await repo.release([task])
    row = await fetch_channel(engine, "durov")
    assert row["lease_token"] is None
    assert row["next_check_at"] <= datetime.now(UTC) + timedelta(seconds=1)


async def test_upsert_watch_created_and_idempotent(clean_db, engine: AsyncEngine) -> None:  # noqa: ANN001
    repo = Repository(clean_db)
    results = await repo.upsert_watch(
        [WatchItem(channel_name="durov", channel_url="t.me/telegram", channel_id=None)]
    )
    assert results == [UpsertResult(channel_name="durov", created=True, reject_reason=None)]
    row = await fetch_channel(engine, "durov")
    assert row["channel_url"] == "t.me/telegram"
    assert row["is_active"] is True

    results2 = await repo.upsert_watch(
        [WatchItem(channel_name="durov", channel_url="t.me/durov", channel_id=None)]
    )
    assert results2[0].created is False
    async with engine.connect() as conn:
        count = (await conn.execute(text("SELECT count(*) FROM channels"))).scalar_one()
    assert count == 1
    row = await fetch_channel(engine, "durov")
    assert row["channel_url"] == "t.me/durov"


async def test_upsert_watch_reactivates_inactive(clean_db, engine: AsyncEngine) -> None:  # noqa: ANN001
    await seed_channel(
        engine, "durov", is_active=False, next_check_at=datetime.now(UTC) + timedelta(hours=2)
    )
    repo = Repository(clean_db)
    results = await repo.upsert_watch(
        [WatchItem(channel_name="durov", channel_url="t.me/durov", channel_id=None)]
    )
    assert results[0].created is False
    row = await fetch_channel(engine, "durov")
    assert row["is_active"] is True
    assert row["next_check_at"] <= datetime.now(UTC)


async def test_upsert_watch_channel_id_none_preserved(clean_db, engine: AsyncEngine) -> None:  # noqa: ANN001
    repo = Repository(clean_db)
    await repo.upsert_watch(
        [WatchItem(channel_name="durov", channel_url="t.me/durov", channel_id=None)]
    )
    results = await repo.upsert_watch(
        [WatchItem(channel_name="durov", channel_url="t.me/durov", channel_id=-1001234567890)]
    )
    assert results[0].reject_reason is None
    row = await fetch_channel(engine, "durov")
    assert row["channel_id"] == -1001234567890

    # повторный upsert без channel_id не затирает сохранённое значение
    await repo.upsert_watch(
        [WatchItem(channel_name="durov", channel_url="t.me/durov", channel_id=None)]
    )
    row = await fetch_channel(engine, "durov")
    assert row["channel_id"] == -1001234567890


async def test_upsert_watch_channel_id_conflict(clean_db, engine: AsyncEngine) -> None:  # noqa: ANN001
    await seed_channel(engine, "taken", channel_id=-1001234567890, channel_url="t.me/taken")
    repo = Repository(clean_db)
    results = await repo.upsert_watch(
        [WatchItem(channel_name="durov", channel_url="t.me/durov", channel_id=-1001234567890)]
    )
    assert results[0].reject_reason == "channel_id_conflict"
    assert results[0].created is False
    async with engine.connect() as conn:
        created = (
            await conn.execute(text("SELECT count(*) FROM channels WHERE channel_name = 'durov'"))
        ).scalar_one()
    assert created == 0  # строка не создана
    row = await fetch_channel(engine, "taken")
    assert row["channel_url"] == "t.me/taken"  # занятая строка не тронута


async def test_upsert_watch_keeps_broken(clean_db, engine: AsyncEngine) -> None:  # noqa: ANN001
    await seed_channel(engine, "durov", broken_url=True, broken_reason="not_found")
    repo = Repository(clean_db)
    results = await repo.upsert_watch(
        [WatchItem(channel_name="durov", channel_url="t.me/durov", channel_id=None)]
    )
    assert results[0].created is False
    row = await fetch_channel(engine, "durov")
    assert row["broken_url"] is True
    assert row["broken_reason"] == "not_found"


async def test_deactivate(clean_db, engine: AsyncEngine) -> None:  # noqa: ANN001
    await seed_channel(engine, "durov")
    await seed_channel(engine, "idle", is_active=False)
    repo = Repository(clean_db)
    assert await repo.deactivate("durov") is True
    row = await fetch_channel(engine, "durov")
    assert row["is_active"] is False
    assert row["lease_token"] is None

    assert await repo.deactivate("idle") is True
    assert await repo.deactivate("durov") is True  # идемпотентно
    assert await repo.deactivate("ghost") is False


async def test_list_channels_filters(clean_db, engine: AsyncEngine) -> None:  # noqa: ANN001
    await seed_channel(engine, "alpha")
    await seed_channel(engine, "bravo", is_active=False)
    repo = Repository(clean_db)
    active = await repo.list_channels(active="true", limit=10, offset=0)
    all_rows = await repo.list_channels(active="all", limit=10, offset=0)
    inactive = await repo.list_channels(active="false", limit=10, offset=0)
    assert [row.channel_name for row in active] == ["alpha"]
    assert [row.channel_name for row in all_rows] == ["alpha", "bravo"]
    assert [row.channel_name for row in inactive] == ["bravo"]
    paged = await repo.list_channels(active="all", limit=1, offset=1)
    assert [row.channel_name for row in paged] == ["bravo"]
    info = all_rows[0]
    assert info.title is None
    assert info.is_active is True


async def test_ping(clean_db) -> None:  # noqa: ANN001
    repo = Repository(clean_db)
    assert await repo.ping() is True
