"""E2E: сервис целиком против замоканного t.me (respx) и эмулятора (ASGITransport)."""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Sized
from typing import cast

import httpx
import pytest
import respx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from emulator import app as emulator_module
from tg_crawler.api import create_app
from tg_crawler.config import Settings
from tg_crawler.publisher import HttpPublisher
from tg_crawler.service import CrawlerService

OK_CH = [f"okch{i}" for i in range(10)]
NF_CH = [f"nfch{i}" for i in range(3)]
RL_CH = [f"rlch{i}" for i in range(3)]
TO_CH = [f"toch{i}" for i in range(4)]
ALL_CH = OK_CH + NF_CH + TO_CH + RL_CH
SLOW_CH = "okch5"
EMULATOR_BASE = "http://emulator.test"


def make_settings(pg_url: str, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": pg_url,
        "publisher_url": f"{EMULATOR_BASE}/messages",
        "proxies": [],
        "workers": 4,
        "queue_size": 50,
        "claim_batch": 20,
        "lease": 10.0,
        "shutdown_timeout": 1.0,
        "feeder_poll_interval": 0.05,
        "writer_flush_interval": 0.05,
        "stats_log_interval": 60.0,
        "outbox_poll_interval": 0.05,
        "publisher_timeout": 2.0,
        "publisher_max_attempts": 1,
        "proxy_min_interval": 0.01,
        "proxy_dead_after": 100,
        "proxy_recheck": 0.5,
        "max_inflight": 10,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def make_html(name: str, ids: tuple[int, ...] = (100, 101)) -> str:
    posts = "".join(
        f'<div class="tgme_widget_message" data-post="{name}/{pid}">'
        f'<div class="tgme_widget_message_text js-message_text">Текст {pid}</div>'
        f'<a class="tgme_widget_message_date" href="https://t.me/{name}/{pid}">'
        f'<time datetime="2024-11-01T12:00:00+00:00">Nov 1</time></a></div>'
        for pid in ids
    )
    return (
        "<!DOCTYPE html><html><head><title>T</title></head><body>"
        f'<div class="tgme_channel_info_header_title">{name} title</div>'
        f"{posts}</body></html>"
    )


def make_channel_items(names: list[str] = ALL_CH) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for index, name in enumerate(names):
        item: dict[str, object] = {"channel_url": f"t.me/{name}"}
        if index < len(names) // 2:
            item["channel_id"] = -1000000000000 - index
        items.append(item)
    return items


def tme_handler(calls: dict[str, int]) -> Callable[[httpx.Request], Awaitable[httpx.Response]]:
    async def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        calls[name] = calls.get(name, 0) + 1
        if name == SLOW_CH:
            await asyncio.sleep(1.2)
        if name in OK_CH:
            return httpx.Response(200, text=make_html(name))
        if name in NF_CH:
            return httpx.Response(302, headers={"location": f"/{name}"})
        if name in RL_CH:
            return httpx.Response(429, headers={"retry-after": "0.1"})
        raise httpx.ConnectTimeout("timed out", request=request)

    return handler


async def wait_until(predicate: Callable[[], bool], timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


async def wait_until_async(predicate: Callable[[], Awaitable[bool]], timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(0.05)
    return await predicate()


async def db_rows(engine: AsyncEngine, columns: str) -> list[dict[str, object]]:
    async with engine.connect() as conn:
        rows = await conn.execute(text(f"SELECT {columns} FROM channels ORDER BY channel_name"))
        return [dict(row._mapping) for row in rows.all()]


async def db_scalar(engine: AsyncEngine, query: str) -> object:
    async with engine.connect() as conn:
        return (await conn.execute(text(query))).scalar()


def pending_predicate(engine: AsyncEngine) -> Callable[[], Awaitable[bool]]:
    async def predicate() -> bool:
        value = await db_scalar(
            engine, "SELECT count(*) FROM pending_posts WHERE status = 'pending'"
        )
        return cast("int", value) > 0

    return predicate


async def api_request(pg_url: str, method: str, path: str, json: object = None) -> httpx.Response:
    api_app = create_app(make_settings(pg_url))
    async with api_app.router.lifespan_context(api_app):
        transport = httpx.ASGITransport(app=api_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as http:
            return await http.request(method, path, json=json)


class RecordingApp:
    """ASGI-обёртка эмулятора: складывает в sink тела успешных (2xx) POST /messages."""

    def __init__(self, app: object, sink: list[dict[str, object]]) -> None:
        self._app = app
        self._sink = sink

    async def __call__(self, scope: object, receive: object, send: object) -> None:
        http_scope = cast("dict[str, object]", scope)
        if http_scope.get("path") != "/messages":
            await self._app(scope, receive, send)  # type: ignore[operator]
            return
        chunks: list[bytes] = []
        status = {"code": 0}

        async def recv() -> dict[str, object]:
            message = await cast("Callable[[], Awaitable[dict[str, object]]]", receive)()
            if message.get("type") == "http.request":
                chunks.append(cast("bytes", message.get("body", b"")))
            return message

        async def snd(message: dict[str, object]) -> None:
            if message.get("type") == "http.response.start":
                status["code"] = cast("int", message["status"])
            await cast("Callable[[dict[str, object]], Awaitable[None]]", send)(message)

        await self._app(scope, recv, snd)  # type: ignore[operator]
        if 200 <= status["code"] < 300 and chunks:
            self._sink.append(cast("dict[str, object]", json.loads(b"".join(chunks))))


def make_service(
    settings: Settings,
    session_factory: async_sessionmaker,
) -> tuple[CrawlerService, list[dict[str, object]]]:
    delivered: list[dict[str, object]] = []
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=RecordingApp(emulator_module.app, delivered)),
        base_url=EMULATOR_BASE,
    )
    publisher = HttpPublisher(
        settings.publisher_url,
        settings.publisher_timeout,
        settings.publisher_max_attempts,
        client,
    )
    service = CrawlerService(settings, session_factory, publisher)  # type: ignore[arg-type]
    return service, delivered


def start(service: CrawlerService) -> asyncio.Task[None]:
    return asyncio.create_task(service.run())


def all_names_called(calls: dict[str, int], names: list[str]) -> bool:
    return all(calls.get(name, 0) >= 1 for name in names)


def len_at_least(items: Sized, size: int) -> bool:
    return len(items) >= size


def sum_at_least(calls: dict[str, int], total: int) -> bool:
    return sum(calls.values()) >= total


@respx.mock
async def test_full_run_delivery(
    pg_url: str,
    clean_db: async_sessionmaker,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = await api_request(pg_url, "POST", "/watch_channels", make_channel_items())
    assert response.status_code == 200
    body = response.json()
    assert len(body["accepted"]) == 20
    assert body["rejected"] == []

    calls: dict[str, int] = {}
    respx.get(host="t.me", path__startswith="/s/").mock(side_effect=tme_handler(calls))
    service, delivered = make_service(make_settings(pg_url), clean_db)
    runner = start(service)
    try:
        assert await wait_until(lambda: all_names_called(calls, ALL_CH))
        assert await wait_until(lambda: len_at_least(delivered, 20))
    finally:
        await service.stop()
        await asyncio.wait_for(runner, timeout=10.0)

    assert len(delivered) == 20
    keys = {(m["channel_id"], m["original_message_id"]) for m in delivered}
    assert len(keys) == 20
    assert all(m["channel_id"] in ALL_CH for m in delivered)
    rows = await db_rows(engine, "channel_name, is_active, broken_url, last_post_id")
    assert len(rows) == 20
    assert all(row["is_active"] and not row["broken_url"] for row in rows)
    assert all(row["last_post_id"] == 101 for row in rows if str(row["channel_name"]) in OK_CH)
    assert all(calls[name] >= 1 for name in ALL_CH)


@respx.mock
async def test_emulator_500_then_recover(
    pg_url: str,
    clean_db: async_sessionmaker,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMULATOR_FAIL_RATE", "1")
    channels = ["okch0", "okch5"]
    response = await api_request(pg_url, "POST", "/watch_channels", make_channel_items(channels))
    assert response.status_code == 200

    calls: dict[str, int] = {}
    respx.get(host="t.me", path__startswith="/s/").mock(side_effect=tme_handler(calls))
    service, delivered = make_service(make_settings(pg_url), clean_db)
    runner = start(service)
    try:
        assert await wait_until(lambda: all_names_called(calls, channels))
        assert await wait_until_async(pending_predicate(engine), timeout=10.0)
        assert delivered == []

        monkeypatch.setenv("EMULATOR_FAIL_RATE", "0")
        assert await wait_until(lambda: len_at_least(delivered, 4), timeout=60.0)
        snapshot = dict(calls)
        await service.stop()
        await asyncio.wait_for(runner, timeout=10.0)
    finally:
        if not runner.done():
            await service.stop()
            await asyncio.wait_for(runner, timeout=10.0)

    assert calls == snapshot
    assert len(delivered) == 4
    keys = {(m["channel_id"], m["original_message_id"]) for m in delivered}
    assert len(keys) == 4
    rows = await db_rows(engine, "channel_name, last_post_id")
    last_post = {str(row["channel_name"]): row["last_post_id"] for row in rows}
    assert last_post["okch0"] == 101
    assert last_post["okch5"] == 101


@respx.mock
async def test_stop_and_restart(
    pg_url: str,
    clean_db: async_sessionmaker,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = await api_request(pg_url, "POST", "/watch_channels", make_channel_items())
    assert response.status_code == 200

    calls: dict[str, int] = {}
    respx.get(host="t.me", path__startswith="/s/").mock(side_effect=tme_handler(calls))
    settings = make_settings(pg_url)
    service, delivered = make_service(settings, clean_db)
    runner = start(service)
    assert await wait_until(lambda: sum_at_least(calls, 5))
    await service.stop()
    await asyncio.wait_for(runner, timeout=10.0)

    await asyncio.sleep(10.5)

    service2, delivered2 = make_service(settings, clean_db)
    runner2 = start(service2)
    try:
        assert await wait_until(lambda: len_at_least(delivered + delivered2, 20), timeout=45.0)
        assert await wait_until(lambda: all_names_called(calls, ALL_CH), timeout=45.0)
    finally:
        await service2.stop()
        await asyncio.wait_for(runner2, timeout=10.0)

    all_delivered = delivered + delivered2
    keys = [(m["channel_id"], m["original_message_id"]) for m in all_delivered]
    assert len(keys) == len(set(keys)) == 20
    rows = await db_rows(
        engine, "channel_name, lease_token, last_checked_at, broken_url, is_active"
    )
    assert len(rows) == 20
    assert all(row["lease_token"] is None for row in rows)
    assert all(row["last_checked_at"] is not None for row in rows)
    assert all(not row["broken_url"] and row["is_active"] for row in rows)


@respx.mock
async def test_delete_channel_mid_work(
    pg_url: str,
    clean_db: async_sessionmaker,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = await api_request(pg_url, "POST", "/watch_channels", make_channel_items())
    assert response.status_code == 200

    calls: dict[str, int] = {}
    respx.get(host="t.me", path__startswith="/s/").mock(side_effect=tme_handler(calls))
    service, delivered = make_service(make_settings(pg_url), clean_db)
    runner = start(service)
    try:
        assert await wait_until(lambda: calls.get("okch0", 0) >= 1)
        delete_response = await api_request(pg_url, "DELETE", "/watch_channels/okch0")
        assert delete_response.status_code == 200
        assert delete_response.json() == {"channel_name": "okch0", "is_active": False}

        calls_after_delete = calls["okch0"]
        await asyncio.sleep(0.7)
        assert calls["okch0"] == calls_after_delete
        rows = await db_rows(engine, "channel_name, is_active")
        active = {str(row["channel_name"]): row["is_active"] for row in rows}
        assert active["okch0"] is False
    finally:
        await service.stop()
        await asyncio.wait_for(runner, timeout=10.0)
