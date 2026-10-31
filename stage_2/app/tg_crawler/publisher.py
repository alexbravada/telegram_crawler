"""HttpPublisher: контракт эмулятора, классификация ответов, повторы с backoff."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx

from tg_crawler.models import Post, PublishResult, PublishStatus

logger = logging.getLogger(__name__)

BACKOFF_BASE = 1.0
BACKOFF_CAP = 30.0


def build_payload(post: Post) -> dict[str, object]:
    """Маппинг Post в контракт эмулятора (v2 раздел 5)."""
    medias: list[dict[str, object]] = []
    for index, media in enumerate(post.media):
        item: dict[str, object] = {"id": f"m_{post.channel}_{post.post_id}_{index}"}
        if media.caption is not None:
            item["caption"] = media.caption
        medias.append(item)
    return {
        "channel_id": post.channel,
        "original_message_id": str(post.post_id),
        "body": post.text or "",
        "medias": medias,
    }


class HttpPublisher:
    def __init__(
        self,
        url: str,
        timeout: float,
        max_attempts: int,
        client: httpx.AsyncClient,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._url = url
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._client = client
        self._sleep: Callable[[float], Awaitable[None]] = sleep or asyncio.sleep

    async def publish(self, post: Post) -> PublishResult:
        """Публикация поста: тело запроса собирается из Post."""
        return await self._send(build_payload(post), f"{post.channel}/{post.post_id}")

    async def publish_raw(self, payload: dict[str, object]) -> PublishResult:
        """Публикация готового тела запроса (пост из outbox)."""
        label = f"{payload.get('channel_id')}/{payload.get('original_message_id')}"
        return await self._send(payload, label)

    async def _send(self, payload: dict[str, object], label: str) -> PublishResult:
        for attempt in range(self._max_attempts):
            last_error: str
            try:
                response = await self._client.post(self._url, json=payload, timeout=self._timeout)
            except httpx.HTTPError as exc:
                last_error = str(exc) or type(exc).__name__
            else:
                if 200 <= response.status_code < 300:
                    return PublishResult(status=PublishStatus.DELIVERED, error=None)
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    body = response.text[:500]
                    logger.error(
                        "publisher rejected post %s: %s %s",
                        label,
                        response.status_code,
                        body,
                    )
                    return PublishResult(
                        status=PublishStatus.REJECTED,
                        error=f"status {response.status_code}: {body}",
                    )
                last_error = f"status {response.status_code}"

            if attempt == self._max_attempts - 1:
                return PublishResult(status=PublishStatus.RETRY_LATER, error=last_error)
            await self._sleep(min(BACKOFF_BASE * 2**attempt, BACKOFF_CAP))

        return PublishResult(status=PublishStatus.RETRY_LATER, error="unreachable")
