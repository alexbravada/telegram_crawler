"""Тесты dataclass-моделей, Outcome, PublishResult."""

import dataclasses
import uuid
from datetime import UTC, datetime

import pytest

from tg_crawler.models import (
    ChannelTask,
    Media,
    Outcome,
    PageResult,
    Post,
    PublishResult,
    PublishStatus,
    Result,
)


def make_post(post_id: int = 10) -> Post:
    return Post(
        channel="durov",
        post_id=post_id,
        title="Durov's Channel",
        text="текст",
        date=datetime(2024, 11, 1, 12, 0, tzinfo=UTC),
        media=(),
        media_not_supported=False,
        url="https://t.me/durov/10",
    )


def test_media_caption_defaults_to_none() -> None:
    m = Media(type="photo", url="https://cdn/x.jpg", thumb_url="https://cdn/t.jpg")
    assert m.caption is None


def test_models_are_frozen() -> None:
    p = make_post()
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.post_id = 11  # type: ignore[misc]
    m = Media(type="photo", url=None, thumb_url=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.url = "x"  # type: ignore[misc]


def test_page_result_holds_multiple_posts() -> None:
    page = PageResult(title="Durov's Channel", posts=(make_post(10), make_post(11)))
    assert page.title == "Durov's Channel"
    assert [p.post_id for p in page.posts] == [10, 11]


def test_channel_task_fields() -> None:
    token = uuid.uuid4()
    task = ChannelTask(id=1, name="durov", last_post_id=None, token=token, channel_id=None)
    assert task.channel_id is None
    task2 = ChannelTask(
        id=2, name="telegram", last_post_id=5, token=token, channel_id=-1001234567890
    )
    assert task2.channel_id == -1001234567890


def test_result_fields() -> None:
    task = ChannelTask(id=1, name="durov", last_post_id=None, token=uuid.uuid4(), channel_id=None)
    page = PageResult(title="t", posts=(make_post(),))
    result = Result(task=task, outcome=Outcome.OK, page=page, proxy_id="p1", error=None)
    assert result.outcome is Outcome.OK
    assert result.page is not None and result.page.posts


def test_outcome_values() -> None:
    assert {o.name for o in Outcome} == {
        "OK",
        "NOT_FOUND",
        "RATE_LIMITED",
        "RETRYABLE",
        "LAYOUT_CHANGED",
        "INVALID_NAME",
    }


def test_publish_result() -> None:
    assert PublishResult(status=PublishStatus.DELIVERED).error is None
    assert PublishResult(status=PublishStatus.REJECTED, error="bad request").error == "bad request"
    assert {s.name for s in PublishStatus} == {"DELIVERED", "REJECTED", "RETRY_LATER"}
