from typing import Protocol

from ..models import Post


class ChannelProvider(Protocol):
    """Source of channel posts.

    Implementations: WebProvider (t.me/s/ scraping), later —
    TelethonProvider (MTProto API). The method takes a channel username
    (without https://t.me/) and returns up to `limit` latest posts.
    Async: suitable for workers under Semaphore/asyncio.gather.
    """

    async def fetch_posts(self, channel: str, limit: int) -> list[Post]: ...
