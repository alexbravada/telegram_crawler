"""Тесты Fetcher: классификация ответов t.me (respx)."""

from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from tg_crawler.fetcher import Fetcher, FetchResponse
from tg_crawler.models import Outcome


def make_fetcher() -> Fetcher:
    return Fetcher()


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as c:
        yield c


@respx.mock
async def test_200_ok(client: httpx.AsyncClient) -> None:
    route = respx.get("https://t.me/s/durov").mock(
        return_value=httpx.Response(200, text="<html></html>")
    )
    response = await make_fetcher().fetch(client, "durov")
    assert response.outcome is Outcome.OK
    assert response.html == "<html></html>"
    assert response.retry_after is None
    assert response.error is None
    assert route.called
    request = route.calls.last.request
    assert request.headers["user-agent"]
    assert request.headers["accept"]


@respx.mock
async def test_302_to_channel_is_not_found(client: httpx.AsyncClient) -> None:
    respx.get("https://t.me/s/durov").mock(
        return_value=httpx.Response(302, headers={"location": "/durov"})
    )
    response = await make_fetcher().fetch(client, "durov")
    assert response.outcome is Outcome.NOT_FOUND
    assert response.html is None


@respx.mock
async def test_302_to_other_path_is_retryable(client: httpx.AsyncClient) -> None:
    respx.get("https://t.me/s/durov").mock(
        return_value=httpx.Response(302, headers={"location": "/other"})
    )
    response = await make_fetcher().fetch(client, "durov")
    assert response.outcome is Outcome.RETRYABLE


@respx.mock
async def test_429_with_retry_after(client: httpx.AsyncClient) -> None:
    respx.get("https://t.me/s/durov").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "60"})
    )
    response = await make_fetcher().fetch(client, "durov")
    assert response.outcome is Outcome.RATE_LIMITED
    assert response.retry_after == 60.0


@respx.mock
async def test_429_without_retry_after(client: httpx.AsyncClient) -> None:
    respx.get("https://t.me/s/durov").mock(return_value=httpx.Response(429))
    response = await make_fetcher().fetch(client, "durov")
    assert response.outcome is Outcome.RATE_LIMITED
    assert response.retry_after is None


@respx.mock
async def test_429_invalid_retry_after_is_none(client: httpx.AsyncClient) -> None:
    respx.get("https://t.me/s/durov").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "abc"})
    )
    response = await make_fetcher().fetch(client, "durov")
    assert response.outcome is Outcome.RATE_LIMITED
    assert response.retry_after is None


@respx.mock
async def test_502_retryable(client: httpx.AsyncClient) -> None:
    respx.get("https://t.me/s/durov").mock(return_value=httpx.Response(502))
    response = await make_fetcher().fetch(client, "durov")
    assert response.outcome is Outcome.RETRYABLE


@respx.mock
async def test_other_codes_retryable(client: httpx.AsyncClient) -> None:
    respx.get("https://t.me/s/durov").mock(return_value=httpx.Response(404))
    response = await make_fetcher().fetch(client, "durov")
    assert response.outcome is Outcome.RETRYABLE


@pytest.mark.parametrize(
    "exception",
    [
        httpx.ConnectTimeout("t"),
        httpx.ConnectError("e"),
        httpx.ReadTimeout("t"),
    ],
)
@respx.mock
async def test_network_errors_retryable(
    client: httpx.AsyncClient, exception: httpx.HTTPError
) -> None:
    respx.get("https://t.me/s/durov").mock(side_effect=exception)
    response: FetchResponse = await make_fetcher().fetch(client, "durov")
    assert response.outcome is Outcome.RETRYABLE
    assert response.error is not None
    assert response.html is None


@respx.mock
async def test_no_redirect_following(client: httpx.AsyncClient) -> None:
    respx.get("https://t.me/s/durov").mock(
        return_value=httpx.Response(302, headers={"location": "/durov"})
    )
    respx.get("https://t.me/durov").mock(return_value=httpx.Response(200, text="target"))
    response = await make_fetcher().fetch(client, "durov")
    assert response.outcome is Outcome.NOT_FOUND
    assert not respx.get("https://t.me/durov").called
