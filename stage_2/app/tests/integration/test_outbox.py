"""Интеграционные тесты outbox: PgOutboxStore и OutboxDelivery (testcontainers + respx)."""

import uuid
from datetime import UTC, datetime

import httpx
import respx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tg_crawler.config import Settings
from tg_crawler.models import Media, Post
from tg_crawler.outbox import OutboxDelivery, PgOutboxStore
from tg_crawler.publisher import HttpPublisher

EMULATOR_URL = "http://emulator:8001/messages"
TME_URL = "https://t.me/s/durov"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeSleep:
    async def __call__(self, delay: float) -> None:  # noqa: ARG002
        pass


def make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://u:p@db:5432/tg",
        publisher_url=EMULATOR_URL,
    )


def make_post(post_id: int = 100) -> Post:
    return Post(
        channel="durov",
        post_id=post_id,
        title="Durov's Channel",
        text="привет",
        date=datetime(2024, 11, 1, 12, 0, tzinfo=UTC),
        media=(Media(type="photo", url="https://cdn/a.jpg", thumb_url=None, caption=None),),
        media_not_supported=False,
        url=f"https://t.me/durov/{post_id}",
    )


async def seed_channel(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO channels (id, channel_name, lease_token, next_check_at) "
                "VALUES (1, 'durov', :token, now())"
            ),
            {"token": uuid.uuid4()},
        )


async def fetch_outbox_row(engine: AsyncEngine) -> dict[str, object]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, attempts, last_error FROM pending_posts "
                    "WHERE channel_name = 'durov' AND post_id = 100"
                )
            )
        ).one()
    return {"status": row[0], "attempts": row[1], "last_error": row[2]}


async def fetch_last_post_id(engine: AsyncEngine) -> int | None:
    async with engine.connect() as conn:
        value = (
            await conn.execute(text("SELECT last_post_id FROM channels WHERE id = 1"))
        ).scalar_one()
    return value


def make_delivery(
    clean_db,  # noqa: ANN001
    sleep: FakeSleep,
    clock: FakeClock,
    in_flight: object | None = None,
) -> OutboxDelivery:
    client = httpx.AsyncClient()
    publisher = HttpPublisher(
        url=EMULATOR_URL, timeout=5.0, max_attempts=1, client=client, sleep=sleep
    )
    return OutboxDelivery(
        clean_db,
        publisher,
        make_settings(),
        clock=clock,
        sleep=sleep,
        in_flight=in_flight,  # type: ignore[arg-type]
    )


async def test_insert_on_conflict_no_duplicate(clean_db, engine: AsyncEngine) -> None:  # noqa: ANN001
    store = PgOutboxStore(clean_db, max_attempts=3)
    await store.insert((make_post(),))
    await store.insert((make_post(),))
    async with engine.connect() as conn:
        count = (await conn.execute(text("SELECT count(*) FROM pending_posts"))).scalar_one()
    assert count == 1


async def test_mark_attempt_fails_at_threshold(clean_db, engine: AsyncEngine) -> None:  # noqa: ANN001
    store = PgOutboxStore(clean_db, max_attempts=3)
    await store.insert((make_post(),))
    assert await store.mark_attempt(make_post(), "err1") == "pending"
    assert await store.mark_attempt(make_post(), "err2") == "pending"
    assert await store.mark_attempt(make_post(), "err3") == "failed"
    row = await fetch_outbox_row(engine)
    assert row["status"] == "failed"
    assert row["attempts"] == 3
    assert row["last_error"] == "err3"


@respx.mock
async def test_delivery_retries_then_delivers_without_tme(clean_db, engine: AsyncEngine) -> None:  # noqa: ANN001
    await seed_channel(engine)
    tme_route = respx.get(TME_URL).mock(return_value=httpx.Response(200, text="<html></html>"))
    route = respx.post(EMULATOR_URL).mock(return_value=httpx.Response(500))
    store = PgOutboxStore(clean_db, max_attempts=3)
    await store.insert((make_post(),))

    clock, sleep = FakeClock(), FakeSleep()
    delivery = make_delivery(clean_db, sleep, clock)
    await delivery.step()
    row = await fetch_outbox_row(engine)
    assert row["status"] == "pending"
    assert row["attempts"] == 1

    route.mock(return_value=httpx.Response(201, json={"status": "accepted"}))
    clock.now = 5.0  # backoff 1 с после первой неудачи
    await delivery.step()
    row = await fetch_outbox_row(engine)
    assert row["status"] == "delivered"
    assert await fetch_last_post_id(engine) == 100
    assert tme_route.call_count == 0
    assert route.call_count == 2


@respx.mock
async def test_delivery_backoff_skips_not_due(clean_db, engine: AsyncEngine) -> None:  # noqa: ANN001
    await seed_channel(engine)
    respx.post(EMULATOR_URL).mock(return_value=httpx.Response(500))
    store = PgOutboxStore(clean_db, max_attempts=3)
    await store.insert((make_post(),))

    clock, sleep = FakeClock(), FakeSleep()
    delivery = make_delivery(clean_db, sleep, clock)
    assert await delivery.step() == 1
    # backoff ещё не истёк — пост пропускается
    assert await delivery.step() == 0
    row = await fetch_outbox_row(engine)
    assert row["attempts"] == 1


@respx.mock
async def test_delivery_rejected_final_fails_and_shifts(clean_db, engine: AsyncEngine) -> None:  # noqa: ANN001
    await seed_channel(engine)
    respx.post(EMULATOR_URL).mock(return_value=httpx.Response(422, text="bad"))
    store = PgOutboxStore(clean_db, max_attempts=3)
    await store.insert((make_post(),))

    clock, sleep = FakeClock(), FakeSleep()
    delivery = make_delivery(clean_db, sleep, clock)
    await delivery.step()
    clock.now = 1.0
    await delivery.step()
    clock.now = 2.0
    await delivery.step()
    row = await fetch_outbox_row(engine)
    assert row["status"] == "failed"
    assert row["attempts"] == 3
    assert await fetch_last_post_id(engine) == 100


@respx.mock
async def test_delivery_empty_batch_is_noop(clean_db, engine: AsyncEngine) -> None:  # noqa: ANN001
    respx.post(EMULATOR_URL).mock(return_value=httpx.Response(201))
    clock, sleep = FakeClock(), FakeSleep()
    delivery = make_delivery(clean_db, sleep, clock)
    assert await delivery.step() == 0


@respx.mock
async def test_delivery_skips_in_flight_rows(clean_db, engine: AsyncEngine) -> None:  # noqa: ANN001
    await seed_channel(engine)
    route = respx.post(EMULATOR_URL).mock(return_value=httpx.Response(201))
    store = PgOutboxStore(clean_db, max_attempts=3)
    await store.insert((make_post(),))

    clock, sleep = FakeClock(), FakeSleep()
    registry = FixedRegistry({("durov", 100)})
    delivery = make_delivery(clean_db, sleep, clock, in_flight=registry)
    assert await delivery.step() == 0
    assert route.call_count == 0
    row = await fetch_outbox_row(engine)
    assert row["status"] == "pending"

    registry.keys.clear()
    delivery = make_delivery(clean_db, sleep, clock)
    assert await delivery.step() == 1
    assert route.call_count == 1


class FixedRegistry:
    def __init__(self, keys: set[tuple[str, int]]) -> None:
        self.keys = set(keys)

    def registered_keys(self) -> set[tuple[str, int]]:
        return set(self.keys)
