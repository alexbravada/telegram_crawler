"""Тесты парсера на синтетических фикстурах."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tg_crawler.models import PageResult
from tg_crawler.parser import LayoutChanged, parse_posts

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_text_fixture_two_posts() -> None:
    page = parse_posts(load("text.html"), "durov", None)
    assert isinstance(page, PageResult)
    assert page.title == "Durov's Channel"
    assert [p.post_id for p in page.posts] == [100, 101]
    first = page.posts[0]
    assert first.channel == "durov"
    assert first.title == "Durov's Channel"
    assert first.text == "Первый текстовый пост"
    assert first.date == datetime(2024, 11, 1, 12, 0, tzinfo=UTC)
    assert first.media == ()
    assert first.media_not_supported is False
    assert first.url == "https://t.me/durov/100"


def test_text_fixture_since_filters_older_posts() -> None:
    page = parse_posts(load("text.html"), "durov", 100)
    assert [p.post_id for p in page.posts] == [101]


def test_text_fixture_since_equal_excludes_post() -> None:
    page = parse_posts(load("text.html"), "durov", 101)
    assert page.posts == ()


def test_photo_fixture() -> None:
    page = parse_posts(load("photo.html"), "durov", None)
    assert len(page.posts) == 1
    post = page.posts[0]
    assert post.post_id == 500
    assert post.text == "Пост с фото"
    assert len(post.media) == 1
    media = post.media[0]
    assert media.type == "photo"
    assert media.url == "https://cdn4.telesco.pe/file/photo_500.jpg"
    assert media.caption is None


def test_album_fixture_three_photos() -> None:
    page = parse_posts(load("album.html"), "durov", None)
    post = page.posts[0]
    assert post.post_id == 600
    assert len(post.media) == 3
    assert all(m.type == "photo" for m in post.media)
    urls = [m.url for m in post.media]
    assert "https://cdn4.telesco.pe/file/album_600_2.jpg" in urls


def test_video_fixture_with_and_without_src() -> None:
    page = parse_posts(load("video.html"), "durov", None)
    assert [p.post_id for p in page.posts] == [700, 701]
    video = page.posts[0].media[0]
    assert video.type == "video"
    assert video.url == "https://cdn4.telesco.pe/file/video_700.mp4"
    assert video.thumb_url == "https://cdn4.telesco.pe/file/video_700_thumb.jpg"
    video_no_src = page.posts[1].media[0]
    assert video_no_src.type == "video"
    assert video_no_src.url is None
    assert video_no_src.thumb_url == "https://cdn4.telesco.pe/file/video_701_thumb.jpg"


def test_service_message_skipped() -> None:
    page = parse_posts(load("service_message.html"), "durov", None)
    assert [p.post_id for p in page.posts] == [800]


def test_empty_channel() -> None:
    page = parse_posts(load("empty.html"), "durov", None)
    assert page.title == "Durov's Channel"
    assert page.posts == ()


def test_no_header_raises_layout_changed() -> None:
    with pytest.raises(LayoutChanged):
        parse_posts(load("no_header.html"), "durov", None)


def test_posts_sorted_ascending_even_if_dom_reversed() -> None:
    html = load("text.html").replace(
        '<div class="tgme_widget_message" data-post="durov/100">',
        '<div class="tgme_widget_message" data-post="durov/102">',
    )
    page = parse_posts(html, "durov", None)
    assert [p.post_id for p in page.posts] == [101, 102]


def test_media_not_supported_flag() -> None:
    html = """
    <div class="tgme_channel_info_header_title">Durov's Channel</div>
    <div class="tgme_widget_message" data-post="durov/110">
      <div class="message_media_not_supported">Unsupported</div>
      <a class="tgme_widget_message_date" href="https://t.me/durov/110">
        <time datetime="2024-11-07T10:00:00+00:00">Nov 7</time>
      </a>
    </div>
    """
    page = parse_posts(html, "durov", None)
    post = page.posts[0]
    assert post.media_not_supported is True
    assert post.media == ()
    assert post.text is None
