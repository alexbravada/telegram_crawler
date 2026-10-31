import asyncio
import logging
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from .. import messages
from ..errors import ChannelFetchError, ChannelNotFoundError
from ..models import Post

logger = logging.getLogger(__name__)

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
from ..config import load_config


def _extract_style_url(style: str) -> Optional[str]:
    idx = style.find("url(")
    if idx == -1:
        return None
    rest = style[idx + 4 :].lstrip("'\"")
    end = rest.find(")")
    return rest[:end].rstrip("'\"") or None


def parse_posts(html: str) -> list[Post]:
    """Extracts posts from the https://t.me/s/<username> HTML page."""
    soup = BeautifulSoup(html, "html.parser")
    posts: list[Post] = []

    for message in soup.select("div.tgme_widget_message"):
        post = Post()

        data_post = message.get("data-post")
        if data_post:
            channel, _, raw_post_id = str(data_post).rpartition("/")
            post.channel_id = channel
            if raw_post_id.isdigit():
                post.post_id = int(raw_post_id)
                post.url = f"https://t.me/{channel}/{raw_post_id}"

        text_div = message.select_one("div.tgme_widget_message_text")
        if text_div is not None:
            for br in text_div.find_all("br"):
                br.replace_with("\n")
            post.text = text_div.get_text().strip()

        for photo in message.select(
            "a.tgme_widget_message_photo_wrap, "
            "i.tgme_widget_message_photo_wrap, "
            "img.tgme_widget_message_image"
        ):
            url = _extract_style_url(str(photo.get("style") or ""))
            if url:
                post.images.append(url)
            src = photo.get("src")
            if src:
                post.images.append(str(src))

        for video in message.select("video[src]"):
            post.videos.append(str(video["src"]))
        for source in message.select("source[src]"):
            post.videos.append(str(source["src"]))

        post.images = list(dict.fromkeys(post.images))
        post.videos = list(dict.fromkeys(post.videos))
        posts.append(post)
    #posts = posts[:-1]
    return posts


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


class WebProvider:
    """ChannelProvider implementation based on scraping t.me/s/<username> web preview.

    Works without api_id/api_hash, public channels only.
    To switch to Telethon, just implement the same interface
    (see scrapper/providers/base.py) and pass it to cli.main().

    Async: many fetch_posts calls can run in parallel,
    with concurrency limited via asyncio.Semaphore.
    An external httpx.AsyncClient can be passed to reuse
    the connection pool across workers.

    Timeout, proxies, retry count and backoff are taken from
    default.app.env (scrapper/config.py) by default.

    Errors: ChannelNotFoundError (fatal, no retry) and
    ChannelFetchError (retryable: network failures, HTTP 429/5xx).
    Transient failures are retried with exponential backoff.
    """

    def __init__(
        self,
        client: Optional[httpx.AsyncClient] = None,
        timeout: Optional[float] = None,
        proxies: Optional[list] = None,
    ) -> None:
        from ..config import load_config

        config = load_config()
        self._client = client
        self._timeout = timeout if timeout is not None else config.timeout
        self._proxies = proxies if proxies is not None else list(config.proxies)
        self._retries = config.retries
        self._retry_backoff = config.retry_backoff

    def _make_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            follow_redirects=True,
            timeout=self._timeout,
            proxy=self._proxies[0] if self._proxies else None,
            headers={"User-Agent": USER_AGENT},
        )

    async def fetch_posts(self, channel: str, limit: int = 1) -> list[Post]:
        url = f"https://t.me/s/{channel}"
        if self._client is not None:
            return await self._fetch_with_retries(self._client, channel, limit, url)
        async with self._make_client() as client:
            return await self._fetch_with_retries(client, channel, limit, url)

    async def _fetch_with_retries(
        self, client, channel: str, limit: int, url: str
    ) -> list[Post]:
        last_error: Optional[Exception] = None
        for attempt in range(1, self._retries + 1):
            try:
                return await self._fetch_once(client, channel, limit, url)
            except (ChannelFetchError, httpx.HTTPError) as exc:
                last_error = exc
                if attempt >= self._retries:
                    break
                delay = self._retry_backoff * 2 ** (attempt - 1)
                logger.debug(
                    messages.fetch_retry(channel, attempt, self._retries, str(exc))
                )
                await asyncio.sleep(delay)
        raise ChannelFetchError(channel, str(last_error))

    async def _fetch_once(
        self, client, channel: str, limit: int, url: str
    ) -> list[Post]:
        last = url + "?last"
        #before = url + "?before=400"
        #after = url + "?after=400"
        #response = await client.get(url)
        response = await client.get(last)
        if response.status_code == 404:
            raise ChannelNotFoundError(channel, status_code=404)
        if response.status_code in RETRYABLE_STATUSES:
            raise ChannelFetchError(channel, f"HTTP {response.status_code}")
        if response.status_code != 200:
            raise ChannelNotFoundError(channel, status_code=response.status_code)
        posts = parse_posts(response.text)
        if not posts:
            raise ChannelNotFoundError(channel)

        #return posts[post_len - limit:]
        return posts[-limit:]
