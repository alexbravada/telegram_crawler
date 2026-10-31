"""Тесты эмулятора: контракт, лог в stdout, fail rate."""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from emulator.app import app


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_valid_request_201_and_logged(
    client: AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {
        "channel_id": "durov",
        "original_message_id": "10",
        "body": "текст",
        "medias": [{"id": "m_durov_10_0", "caption": "подпись"}],
    }
    response = await client.post("/messages", json=payload)
    assert response.status_code == 201
    assert response.json() == {"status": "accepted"}
    captured = capsys.readouterr().out
    assert captured

    line = json.loads(captured.strip().splitlines()[-1])
    assert line["path"] == "/messages"
    assert line["body"] == payload
    assert "ts" in line


async def test_caption_key_optional(client: AsyncClient) -> None:
    payload = {
        "channel_id": "durov",
        "original_message_id": "7",
        "body": "",
        "medias": [{"id": "m_durov_7_0"}],
    }
    response = await client.post("/messages", json=payload)
    assert response.status_code == 201


async def test_invalid_channel_id_422(client: AsyncClient) -> None:
    response = await client.post(
        "/messages",
        json={"channel_id": "", "original_message_id": "1", "body": "", "medias": []},
    )
    assert response.status_code == 422


async def test_empty_body_allowed(client: AsyncClient) -> None:
    response = await client.post(
        "/messages",
        json={"channel_id": "x", "original_message_id": "1"},
    )
    assert response.status_code == 201


async def test_unknown_extra_field_422(client: AsyncClient) -> None:
    response = await client.post(
        "/messages",
        json={
            "channel_id": "x",
            "original_message_id": "1",
            "body": "",
            "medias": [],
            "extra": 1,
        },
    )
    assert response.status_code == 422


async def test_media_without_id_422(client: AsyncClient) -> None:
    response = await client.post(
        "/messages",
        json={
            "channel_id": "x",
            "original_message_id": "1",
            "body": "",
            "medias": [{"caption": "no id"}],
        },
    )
    assert response.status_code == 422


async def test_fail_rate_one_gives_500(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EMULATOR_FAIL_RATE", "1")
    response = await client.post(
        "/messages",
        json={"channel_id": "x", "original_message_id": "1", "body": "", "medias": []},
    )
    assert response.status_code == 500


async def test_health(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
