"""Fetcher: одна попытка запроса к t.me/s/<name> и классификация ответа."""

from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from tg_crawler.models import Outcome

TIMEOUT = httpx.Timeout(connect=5, read=15, write=5, pool=5)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass(frozen=True, slots=True)
class FetchResponse:
    outcome: Outcome
    html: str | None
    retry_after: float | None
    error: str | None


class Fetcher:
    async def fetch(self, client: httpx.AsyncClient, name: str) -> FetchResponse:
        url = f"https://t.me/s/{name}"
        try:
            response = await client.get(
                url, headers=HEADERS, timeout=TIMEOUT, follow_redirects=False
            )
        except httpx.HTTPError as exc:
            detail = str(exc) or type(exc).__name__
            return FetchResponse(Outcome.RETRYABLE, None, None, detail)

        if response.status_code == 200:
            return FetchResponse(Outcome.OK, response.text, None, None)

        if response.status_code == 429:
            raw = response.headers.get("Retry-After")
            retry_after: float | None = None
            if raw is not None:
                try:
                    retry_after = float(raw)
                except ValueError:
                    retry_after = None
            return FetchResponse(Outcome.RATE_LIMITED, None, retry_after, None)

        if 300 <= response.status_code < 400:
            location = response.headers.get("location", "")
            path = urlsplit(location).path
            if path == f"/{name}":
                return FetchResponse(Outcome.NOT_FOUND, None, None, None)

        return FetchResponse(Outcome.RETRYABLE, None, None, f"status {response.status_code}")
