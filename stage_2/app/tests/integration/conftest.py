"""Общие фикстуры интеграционных тестов: контейнер Postgres + миграции."""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

from alembic import command

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
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


@pytest.fixture
async def clean_db(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker]:
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE channels, pending_posts RESTART IDENTITY"))
    from tg_crawler.db import create_sessionmaker

    yield create_sessionmaker(engine)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE channels, pending_posts RESTART IDENTITY"))
