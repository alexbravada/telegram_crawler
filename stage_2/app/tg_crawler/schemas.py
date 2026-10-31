"""Pydantic-модели API (v2 раздел 3)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class WatchItemIn(BaseModel):
    """Элемент POST /watch_channels. channel_id строгий int: строка даёт 422."""

    channel_url: str
    channel_name: str | None = None
    channel_id: int | None = Field(default=None, strict=True)


class WatchAcceptedOut(BaseModel):
    channel_name: str
    channel_id: int | None = None
    created: bool
    warning: str | None = None


class WatchRejectedOut(BaseModel):
    channel_url: str
    channel_name: str | None = None
    reason: str


class WatchResponseOut(BaseModel):
    accepted: list[WatchAcceptedOut]
    rejected: list[WatchRejectedOut]


class ChannelOut(BaseModel):
    """Поля GET /watch_channels (v2 раздел 3.2)."""

    model_config = ConfigDict(from_attributes=True)

    channel_name: str
    channel_id: int | None
    channel_url: str | None
    title: str | None
    is_active: bool
    broken_url: bool
    broken_reason: str | None
    last_post_id: int | None
    last_post_at: datetime | None
    last_checked_at: datetime | None
    next_check_at: datetime
    fail_count: int
    not_found_count: int
