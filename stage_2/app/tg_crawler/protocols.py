"""Протоколы компонентов."""

from typing import Protocol

from tg_crawler.models import Post, PublishResult


class Publisher(Protocol):
    async def publish(self, post: Post) -> PublishResult: ...


class RawPublisher(Protocol):
    async def publish_raw(self, payload: dict[str, object]) -> PublishResult: ...
