"""Интеграционные тесты модели БД и миграций (testcontainers)."""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer

from alembic import command

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def pg_url() -> Iterator[str]:
    with PostgresContainer("postgres:16", driver="asyncpg") as postgres:
        url = postgres.get_connection_url()
        old_dsn = os.environ.get("TG_CRAWLER_DATABASE_URL")
        old_pub = os.environ.get("TG_CRAWLER_PUBLISHER_URL")
        os.environ["TG_CRAWLER_DATABASE_URL"] = url
        if old_pub is None:
            os.environ["TG_CRAWLER_PUBLISHER_URL"] = "http://emulator:8001/messages"
        try:
            cfg = Config(str(ROOT / "alembic.ini"))
            cfg.set_main_option("script_location", str(ROOT / "alembic"))
            command.upgrade(cfg, "head")
            yield url
        finally:
            if old_dsn is None:
                os.environ.pop("TG_CRAWLER_DATABASE_URL", None)
            else:
                os.environ["TG_CRAWLER_DATABASE_URL"] = old_dsn
            if old_pub is None:
                os.environ.pop("TG_CRAWLER_PUBLISHER_URL", None)
            else:
                os.environ["TG_CRAWLER_PUBLISHER_URL"] = old_pub


@pytest.fixture
async def engine(pg_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(pg_url)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _table_exists(engine: AsyncEngine, name: str) -> bool:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT 1 FROM information_schema.tables WHERE table_name = :name"),
            {"name": name},
        )
        return result.scalar() is not None


async def _index_exists(engine: AsyncEngine, name: str) -> bool:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT 1 FROM pg_indexes WHERE indexname = :name"),
            {"name": name},
        )
        return result.scalar() is not None


async def test_upgrade_creates_tables_and_indexes(engine: AsyncEngine) -> None:
    assert await _table_exists(engine, "channels")
    assert await _table_exists(engine, "pending_posts")
    assert await _index_exists(engine, "channels_due_idx")
    assert await _index_exists(engine, "channels_channel_id_uq")
    assert await _index_exists(engine, "pending_posts_channel_post_uq")


async def test_duplicate_channel_id_rejected(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO channels (channel_name, channel_id) VALUES ('a', -1001234567890)")
        )
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO channels (channel_name, channel_id) VALUES ('b', -1001234567890)")
            )


async def test_multiple_null_channel_ids_allowed(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO channels (channel_name) VALUES ('n1')"))
        await conn.execute(text("INSERT INTO channels (channel_name) VALUES ('n2')"))


async def test_bigint_channel_id_no_overflow(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO channels (channel_name, channel_id) VALUES ('big', -100123456789012)")
        )
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT channel_id FROM channels WHERE channel_name = 'big'")
        )
        assert result.scalar_one() == -100123456789012


async def test_pending_posts_unique_dedup(engine: AsyncEngine) -> None:
    payload = '{"channel_id": "durov", "original_message_id": "10", "body": "t", "medias": []}'
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO pending_posts (channel_name, post_id, payload) "
                "VALUES ('durov', 10, :payload)"
            ),
            {"payload": payload},
        )
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO pending_posts (channel_name, post_id, payload) "
                    "VALUES ('durov', 10, :payload)"
                ),
                {"payload": payload},
            )


async def test_pending_posts_defaults(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO pending_posts (channel_name, post_id, payload) "
                "VALUES ('telegram', 5, '{}')"
            )
        )
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT status, attempts FROM pending_posts WHERE channel_name = 'telegram'")
        )
        row = result.one()
    assert row.status == "pending"
    assert row.attempts == 0


async def _check_after_downgrade(pg_url: str) -> None:
    engine = create_async_engine(pg_url)
    try:
        assert not await _table_exists(engine, "channels")
        assert not await _table_exists(engine, "pending_posts")
        assert not await _index_exists(engine, "channels_due_idx")
    finally:
        await engine.dispose()


def test_downgrade_base_drops_everything(pg_url: str) -> None:
    old_dsn = os.environ.get("TG_CRAWLER_DATABASE_URL")
    os.environ["TG_CRAWLER_DATABASE_URL"] = pg_url
    try:
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "alembic"))
        command.downgrade(cfg, "base")
        asyncio.run(_check_after_downgrade(pg_url))
        command.upgrade(cfg, "head")
    finally:
        if old_dsn is None:
            os.environ.pop("TG_CRAWLER_DATABASE_URL", None)
        else:
            os.environ["TG_CRAWLER_DATABASE_URL"] = old_dsn
