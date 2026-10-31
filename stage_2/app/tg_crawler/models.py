"""Внутренние модели данных, исходы fetch и результаты публикации."""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal


class Outcome(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    RETRYABLE = "retryable"
    LAYOUT_CHANGED = "layout_changed"
    INVALID_NAME = "invalid_name"


class PublishStatus(StrEnum):
    DELIVERED = "delivered"
    REJECTED = "rejected"
    RETRY_LATER = "retry_later"


@dataclass(frozen=True, slots=True)
class Media:
    type: Literal["photo", "video"]
    url: str | None
    thumb_url: str | None
    caption: str | None = None


@dataclass(frozen=True, slots=True)
class Post:
    channel: str
    post_id: int
    title: str
    text: str | None
    date: datetime | None
    media: tuple[Media, ...]
    media_not_supported: bool
    url: str


@dataclass(frozen=True, slots=True)
class PageResult:
    title: str
    posts: tuple[Post, ...]


@dataclass(frozen=True, slots=True)
class ChannelTask:
    id: int
    name: str
    last_post_id: int | None
    token: uuid.UUID
    channel_id: int | None = None


@dataclass(frozen=True, slots=True)
class Result:
    task: ChannelTask
    outcome: Outcome
    page: PageResult | None
    proxy_id: str | None
    error: str | None


@dataclass(frozen=True, slots=True)
class PublishResult:
    status: PublishStatus
    error: str | None = field(default=None)
