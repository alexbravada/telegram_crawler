"""Тесты CrawlerService: жизненный цикл start/stop, порядок остановки, логи."""

import asyncio
import io
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from tg_crawler.config import Settings
from tg_crawler.models import ChannelTask
from tg_crawler.service import CrawlerService, InFlightPosts, JsonFormatter, LoggingWriter
from tg_crawler.writer import Op

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker


class FakeRepository:
    def __init__(self, tasks: list[ChannelTask] | None = None) -> None:
        self._tasks = list(tasks or [])
        self.claim_calls = 0
        self.released: list[ChannelTask] = []

    async def claim(self, batch: int, lease: float) -> tuple[list[ChannelTask], list[ChannelTask]]:
        self.claim_calls += 1
        taken, self._tasks = self._tasks[:batch], self._tasks[batch:]
        return taken, []

    async def release(self, tasks):  # noqa: ANN001, ANN202
        self.released.extend(tasks)


class FakePublisher:
    async def publish(self, post):  # noqa: ANN001, ANN202
        raise AssertionError("not expected in service tests")

    async def publish_raw(self, payload):  # noqa: ANN001, ANN202
        raise AssertionError("not expected in service tests")


class FakeDelivery:
    def __init__(self) -> None:
        self.stopped = False

    async def run(self) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.stopped = True
            raise


class HungFetcher:
    def __init__(self) -> None:
        self.in_flight = 0

    async def fetch(self, client, name):  # noqa: ANN001, ANN202
        self.in_flight += 1
        try:
            await asyncio.Event().wait()
        finally:
            self.in_flight -= 1


class RecordingWriter:
    def __init__(self) -> None:
        self.ops: list[Op] = []

    def submit(self, op: Op) -> None:
        self.ops.append(op)


def make_settings(**overrides) -> Settings:  # noqa: ANN003
    defaults: dict = {
        "database_url": "postgresql+asyncpg://user:pass@localhost:5432/db",
        "publisher_url": "http://localhost:9/",
        "workers": 1,
        "queue_size": 2,
        "claim_batch": 3,
        "feeder_poll_interval": 0.02,
        "writer_flush_interval": 0.02,
        "stats_log_interval": 0.02,
        "shutdown_timeout": 0.2,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def make_task(name: str, index: int = 0) -> ChannelTask:
    return ChannelTask(id=index, name=name, last_post_id=None, token=uuid.uuid4())


def make_service(
    settings: Settings,
    repository: FakeRepository,
    *,
    fetcher=None,  # noqa: ANN001
    delivery=None,  # noqa: ANN001
) -> CrawlerService:
    # session_factory не используется: writer с пустым буфером не ходит в БД,
    # delivery подменён фейком.
    return CrawlerService(
        settings,
        session_factory=cast("async_sessionmaker", object()),
        publisher=FakePublisher(),
        repository=repository,
        fetcher=fetcher,
        delivery=delivery,
    )


async def test_start_stop_with_empty_table() -> None:
    settings = make_settings()
    repo = FakeRepository()
    delivery = FakeDelivery()
    service = make_service(settings, repo, delivery=delivery)
    runner = asyncio.create_task(service.run())
    await asyncio.sleep(0.1)
    await service.stop()
    await asyncio.wait_for(runner, timeout=2.0)
    assert repo.claim_calls >= 1
    assert delivery.stopped


async def test_stop_releases_queued_tasks() -> None:
    settings = make_settings()
    tasks = [make_task(f"ch{i}", i) for i in range(3)]
    repo = FakeRepository(tasks)
    service = make_service(settings, repo, fetcher=HungFetcher(), delivery=FakeDelivery())
    runner = asyncio.create_task(service.run())
    await asyncio.sleep(0.2)
    await service.stop()
    await asyncio.wait_for(runner, timeout=2.0)
    assert len(repo.released) >= 1
    assert repo.released[0].name == "ch1"


async def test_stop_within_timeout_with_hung_request() -> None:
    settings = make_settings()
    repo = FakeRepository([make_task("alpha")])
    service = make_service(settings, repo, fetcher=HungFetcher(), delivery=FakeDelivery())
    runner = asyncio.create_task(service.run())
    await asyncio.sleep(0.2)
    start = time.monotonic()
    await service.stop()
    await asyncio.wait_for(runner, timeout=2.0)
    assert time.monotonic() - start < 2.0


async def test_stats_logged(caplog) -> None:  # noqa: ANN001
    settings = make_settings()
    repo = FakeRepository()
    service = make_service(settings, repo, delivery=FakeDelivery())
    runner = asyncio.create_task(service.run())
    with caplog.at_level(logging.INFO, logger="tg_crawler.service"):
        await asyncio.sleep(0.15)
        await service.stop()
    await asyncio.wait_for(runner, timeout=2.0)
    assert any("stats" in record.message for record in caplog.records)


async def test_logging_writer_json_fields(caplog) -> None:  # noqa: ANN001
    inner = RecordingWriter()
    lw = LoggingWriter(inner)
    op = Op(
        kind="not_found",
        task=make_task("alpha"),
        at=datetime.now(UTC),
        proxy_id="direct",
    )
    with caplog.at_level(logging.INFO, logger="tg_crawler.service"):
        lw.submit(op)
    assert inner.ops == [op]
    record = next(r for r in caplog.records if hasattr(r, "channel"))
    assert record.channel == "alpha"
    assert record.outcome == "not_found"
    assert record.proxy == "direct"
    assert record.post_id is None


def test_json_formatter() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("test.jsonformatter")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        logger.info("op processed", extra={"channel": "alpha", "outcome": "ok", "post_id": 5})
    finally:
        logger.removeHandler(handler)
    data = json.loads(stream.getvalue())
    assert data["message"] == "op processed"
    assert data["level"] == "INFO"
    assert data["channel"] == "alpha"
    assert data["outcome"] == "ok"
    assert data["post_id"] == 5
    assert "proxy" not in data or data["proxy"] is None


def test_in_flight_registry_ttl() -> None:
    ticks = {"t": 100.0}

    def clock() -> float:
        return ticks["t"]

    registry = InFlightPosts(ttl=30.0, clock=clock)
    registry.add(("durov", 100))
    assert registry.is_in_flight(("durov", 100))
    assert not registry.is_in_flight(("other", 1))
    ticks["t"] = 129.0
    assert registry.is_in_flight(("durov", 100))
    ticks["t"] = 131.0
    assert not registry.is_in_flight(("durov", 100))
    registry.add(("durov", 100))
    registry.remove(("durov", 100))
    assert not registry.is_in_flight(("durov", 100))


def test_in_flight_registry_registered_keys() -> None:
    ticks = {"t": 0.0}

    def clock() -> float:
        return ticks["t"]

    registry = InFlightPosts(ttl=30.0, clock=clock)
    registry.add(("a", 1))
    ticks["t"] = 10.0
    registry.add(("b", 2))
    assert registry.registered_keys() == {("a", 1), ("b", 2)}
    ticks["t"] = 39.0
    assert registry.registered_keys() == {("b", 2)}
