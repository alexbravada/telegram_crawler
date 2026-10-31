"""Интеграционные тесты HTTP API: /watch_channels POST/GET/DELETE, /health (testcontainers)."""

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tg_crawler.api import create_app, get_repository
from tg_crawler.config import Settings


def make_settings(pg_url: str, **kwargs: object) -> Settings:
    base = {
        "database_url": pg_url,
        "publisher_url": "http://emulator:8001/messages",
    }
    base.update(kwargs)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture
async def client(clean_db, pg_url: str, engine: AsyncEngine):  # noqa: ANN001
    app = create_app(make_settings(pg_url))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http, engine


async def fetch_channel(engine: AsyncEngine, name: str) -> dict[str, object]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT channel_name, channel_id, channel_url, is_active, title "
                    "FROM channels WHERE channel_name = :name"
                ),
                {"name": name},
            )
        ).one_or_none()
    if row is None:
        return {}
    keys = ["channel_name", "channel_id", "channel_url", "is_active", "title"]
    return dict(zip(keys, row, strict=True))


async def test_post_name_differs_warning(client) -> None:  # noqa: ANN001
    http, engine = client
    response = await http.post(
        "/watch_channels",
        json=[{"channel_url": "t.me/telegram", "channel_name": "durov"}],
    )
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == [
        {
            "channel_name": "durov",
            "channel_id": None,
            "created": True,
            "warning": "channel_name_differs_from_url",
        }
    ]
    assert body["rejected"] == []
    row = await fetch_channel(engine, "durov")
    assert row["channel_url"] == "t.me/telegram"


async def test_post_unparsed_url_warning(client) -> None:  # noqa: ANN001
    http, _ = client
    response = await http.post(
        "/watch_channels", json=[{"channel_url": "мусор", "channel_name": "durov"}]
    )
    assert response.status_code == 200
    assert response.json()["accepted"][0]["warning"] == "channel_url_unparsed"


async def test_post_invalid_name_rejected(client) -> None:  # noqa: ANN001
    http, engine = client
    response = await http.post(
        "/watch_channels", json=[{"channel_url": "t.me/durov", "channel_name": "1abc"}]
    )
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == []
    assert body["rejected"] == [
        {"channel_url": "t.me/durov", "channel_name": "1abc", "reason": "invalid_channel_name"}
    ]
    assert (await fetch_channel(engine, "durov")) == {}


async def test_post_url_only_accepted_with_null_channel_id(client) -> None:  # noqa: ANN001
    http, engine = client
    response = await http.post("/watch_channels", json=[{"channel_url": "t.me/durov"}])
    assert response.status_code == 200
    accepted = response.json()["accepted"]
    assert accepted[0]["channel_name"] == "durov"
    assert accepted[0]["created"] is True
    row = await fetch_channel(engine, "durov")
    assert row["channel_id"] is None


async def test_post_repeat_created_false_and_channel_id_set(client) -> None:  # noqa: ANN001
    http, engine = client
    await http.post("/watch_channels", json=[{"channel_url": "t.me/durov"}])
    response = await http.post(
        "/watch_channels",
        json=[{"channel_url": "t.me/durov", "channel_id": -1001234567890}],
    )
    body = response.json()
    assert body["accepted"][0]["created"] is False
    row = await fetch_channel(engine, "durov")
    assert row["channel_id"] == -1001234567890


async def test_post_invalid_url_rejected(client) -> None:  # noqa: ANN001
    http, _ = client
    response = await http.post("/watch_channels", json=[{"channel_url": "мусор"}])
    assert response.json()["rejected"] == [
        {"channel_url": "мусор", "channel_name": None, "reason": "invalid_channel_url"}
    ]


async def test_post_bigint_channel_id_persisted(client) -> None:  # noqa: ANN001
    http, engine = client
    response = await http.post(
        "/watch_channels",
        json=[{"channel_url": "t.me/durov", "channel_id": -1009876543210}],
    )
    assert response.status_code == 200
    row = await fetch_channel(engine, "durov")
    assert row["channel_id"] == -1009876543210


async def test_post_string_channel_id_422(client) -> None:  # noqa: ANN001
    http, _ = client
    response = await http.post(
        "/watch_channels",
        json=[{"channel_url": "t.me/durov", "channel_id": "-1001234567890"}],
    )
    assert response.status_code == 422


async def test_post_small_channel_id_rejected(client) -> None:  # noqa: ANN001
    http, _ = client
    response = await http.post(
        "/watch_channels", json=[{"channel_url": "t.me/durov", "channel_id": 123}]
    )
    assert response.json()["rejected"][0]["reason"] == "invalid_channel_id"


async def test_post_channel_id_conflict(client) -> None:  # noqa: ANN001
    http, engine = client
    await http.post(
        "/watch_channels",
        json=[{"channel_url": "t.me/taken", "channel_id": -1001234567890}],
    )
    response = await http.post(
        "/watch_channels",
        json=[{"channel_url": "t.me/durov", "channel_id": -1001234567890}],
    )
    body = response.json()
    assert body["rejected"] == [
        {"channel_url": "t.me/durov", "channel_name": "durov", "reason": "channel_id_conflict"}
    ]
    row = await fetch_channel(engine, "taken")
    assert row["channel_url"] == "t.me/taken"


async def test_delete_idempotent_and_readd(client) -> None:  # noqa: ANN001
    http, engine = client
    await http.post("/watch_channels", json=[{"channel_url": "t.me/durov"}])
    assert (await http.delete("/watch_channels/durov")).status_code == 200
    assert (await http.delete("/watch_channels/durov")).status_code == 200
    assert (await http.delete("/watch_channels/ghost")).status_code == 404
    listing = (await http.get("/watch_channels", params={"active": "all"})).json()
    assert listing[0]["is_active"] is False

    response = await http.post("/watch_channels", json=[{"channel_url": "t.me/durov"}])
    assert response.json()["accepted"][0]["created"] is False
    row = await fetch_channel(engine, "durov")
    assert row["is_active"] is True


async def test_delete_normalizes_name(client) -> None:  # noqa: ANN001
    http, _ = client
    await http.post("/watch_channels", json=[{"channel_url": "t.me/durov"}])
    response = await http.delete("/watch_channels/%40durov")
    assert response.status_code == 200
    assert response.json() == {"channel_name": "durov", "is_active": False}


async def test_duplicates_in_request(client) -> None:  # noqa: ANN001
    http, engine = client
    response = await http.post(
        "/watch_channels",
        json=[
            {"channel_url": "t.me/durov"},
            {"channel_url": "t.me/other", "channel_name": "durov"},
        ],
    )
    body = response.json()
    assert [item["warning"] for item in body["accepted"]] == [
        "duplicate_in_request",
        "channel_name_differs_from_url",
    ]
    async with engine.connect() as conn:
        count = (await conn.execute(text("SELECT count(*) FROM channels"))).scalar_one()
    assert count == 1
    row = await fetch_channel(engine, "durov")
    assert row["channel_url"] == "t.me/other"  # значения последнего элемента


async def test_get_filters_and_paging(client) -> None:  # noqa: ANN001
    http, _ = client
    await http.post(
        "/watch_channels",
        json=[{"channel_url": "t.me/alpha"}, {"channel_url": "t.me/bravo"}],
    )
    await http.delete("/watch_channels/bravo")
    active = (await http.get("/watch_channels")).json()
    assert [row["channel_name"] for row in active] == ["alpha"]
    all_rows = (await http.get("/watch_channels", params={"active": "all"})).json()
    assert [row["channel_name"] for row in all_rows] == ["alpha", "bravo"]
    inactive = (await http.get("/watch_channels", params={"active": "false"})).json()
    assert inactive[0]["is_active"] is False
    paged = (
        await http.get("/watch_channels", params={"active": "all", "limit": 1, "offset": 1})
    ).json()
    assert [row["channel_name"] for row in paged] == ["bravo"]
    fields = set(all_rows[0])
    assert fields == {
        "channel_name",
        "channel_id",
        "channel_url",
        "title",
        "is_active",
        "broken_url",
        "broken_reason",
        "last_post_id",
        "last_post_at",
        "last_checked_at",
        "next_check_at",
        "fail_count",
        "not_found_count",
    }


async def test_schema_errors_422(pg_url: str, clean_db) -> None:  # noqa: ANN001
    app = create_app(make_settings(pg_url, watch_max_batch=2))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            assert (await http.post("/watch_channels", json=[])).status_code == 422
            assert (await http.post("/watch_channels", json=[{}])).status_code == 422
            long_list = [{"channel_url": "t.me/x"} for _ in range(3)]
            assert (await http.post("/watch_channels", json=long_list)).status_code == 422


async def test_health_ok(client) -> None:  # noqa: ANN001
    http, _ = client
    response = await http.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok"}


async def test_health_db_down_503(pg_url: str, clean_db) -> None:  # noqa: ANN001
    app = create_app(make_settings(pg_url))

    class DownRepo:
        async def ping(self) -> bool:
            return False

    app.dependency_overrides[get_repository] = lambda: DownRepo()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.get("/health")
    assert response.status_code == 503
