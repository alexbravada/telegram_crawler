"""Тесты HttpPublisher: контракт, классификация ответов, backoff (respx, фейковый sleep)."""

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from tg_crawler.models import Media, Post, PublishStatus
from tg_crawler.publisher import HttpPublisher

URL = "http://emulator:8001/messages"


class FakeSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def make_post(
    post_id: int = 4821,
    text: str | None = "текст поста",
    media: tuple[Media, ...] = (),
) -> Post:
    return Post(
        channel="durov",
        post_id=post_id,
        title="Durov's Channel",
        text=text,
        date=datetime(2024, 11, 1, 12, 0, tzinfo=UTC),
        media=media,
        media_not_supported=False,
        url=f"https://t.me/durov/{post_id}",
    )


def make_publisher(
    sleep: FakeSleep,
    max_attempts: int = 5,
    client: httpx.AsyncClient | None = None,
) -> HttpPublisher:
    return HttpPublisher(
        url=URL,
        timeout=5.0,
        max_attempts=max_attempts,
        client=client or httpx.AsyncClient(),
        sleep=sleep,
    )


async def test_request_body_matches_contract() -> None:
    with respx.mock:
        route = respx.post(URL).mock(return_value=httpx.Response(201))
        post = make_post(
            media=(
                Media(type="photo", url="https://cdn/a.jpg", thumb_url=None, caption=None),
                Media(type="photo", url="https://cdn/b.jpg", thumb_url=None, caption="подпись"),
            )
        )
        publisher = make_publisher(FakeSleep())
        result = await publisher.publish(post)
        assert result.status is PublishStatus.DELIVERED
        sent = json.loads(route.calls.last.request.content)
        assert sent == {
            "channel_id": "durov",
            "original_message_id": "4821",
            "body": "текст поста",
            "medias": [
                {"id": "m_durov_4821_0"},
                {"id": "m_durov_4821_1", "caption": "подпись"},
            ],
        }


async def test_empty_body_and_caption_keys_absent() -> None:
    with respx.mock:
        route = respx.post(URL).mock(return_value=httpx.Response(201))
        post = make_post(
            text=None,
            post_id=7,
            media=(
                Media(type="photo", url=None, thumb_url=None),
                Media(type="video", url=None, thumb_url="t"),
                Media(type="photo", url=None, thumb_url=None),
            ),
        )
        publisher = make_publisher(FakeSleep())
        await publisher.publish(post)
        sent = json.loads(route.calls.last.request.content)
        assert sent == {
            "channel_id": "durov",
            "original_message_id": "7",
            "body": "",
            "medias": [
                {"id": "m_durov_7_0"},
                {"id": "m_durov_7_1"},
                {"id": "m_durov_7_2"},
            ],
        }


async def test_4xx_is_rejected() -> None:
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(422, text="bad"))
        publisher = make_publisher(FakeSleep())
        result = await publisher.publish(make_post())
        assert result.status is PublishStatus.REJECTED
        assert result.error is not None


async def test_5xx_then_201_delivered_three_requests() -> None:
    with respx.mock:
        route = respx.post(URL).mock(
            side_effect=[httpx.Response(500), httpx.Response(500), httpx.Response(201)]
        )
        sleep = FakeSleep()
        publisher = make_publisher(sleep)
        result = await publisher.publish(make_post())
        assert result.status is PublishStatus.DELIVERED
        assert route.call_count == 3
        assert sleep.delays == [1.0, 2.0]


async def test_exhausted_attempts_retry_later() -> None:
    with respx.mock:
        route = respx.post(URL).mock(return_value=httpx.Response(500))
        sleep = FakeSleep()
        publisher = make_publisher(sleep, max_attempts=3)
        result = await publisher.publish(make_post())
        assert result.status is PublishStatus.RETRY_LATER
        assert route.call_count == 3
        assert sleep.delays == [1.0, 2.0]


async def test_429_is_retryable() -> None:
    with respx.mock:
        respx.post(URL).mock(side_effect=[httpx.Response(429), httpx.Response(201)])
        publisher = make_publisher(FakeSleep())
        result = await publisher.publish(make_post())
        assert result.status is PublishStatus.DELIVERED


async def test_timeout_is_retryable() -> None:
    with respx.mock:
        respx.post(URL).mock(side_effect=[httpx.ReadTimeout("t"), httpx.Response(201)])
        publisher = make_publisher(FakeSleep())
        result = await publisher.publish(make_post())
        assert result.status is PublishStatus.DELIVERED


async def test_backoff_cap_30s() -> None:
    with respx.mock:
        route = respx.post(URL).mock(return_value=httpx.Response(500))
        sleep = FakeSleep()
        publisher = make_publisher(sleep, max_attempts=8)
        result = await publisher.publish(make_post())
        assert result.status is PublishStatus.RETRY_LATER
        assert route.call_count == 8
        assert sleep.delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


async def test_default_sleep_is_asyncio(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[float] = []

    async def fake_sleep(delay: float) -> None:
        called.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(500))
        publisher = HttpPublisher(url=URL, timeout=5.0, max_attempts=1, client=httpx.AsyncClient())
        result = await publisher.publish(make_post())
        assert result.status is PublishStatus.RETRY_LATER
        assert called == []
