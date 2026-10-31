"""Парсер публичных страниц t.me/s/<channel>: чистые функции без ввода-вывода."""

import re
from datetime import datetime

from bs4 import BeautifulSoup, Tag

from tg_crawler.models import Media, PageResult, Post


class LayoutChanged(Exception):
    """В 200-ответе нет шапки канала: вёрстка изменилась или прокси подменил страницу."""


_STYLE_URL_RE = re.compile(r"""url\((['"]?)([^'")]+)\1\)""")


def _style_url(style: str | None) -> str | None:
    if not style:
        return None
    match = _STYLE_URL_RE.search(style)
    return match.group(2) if match else None


def _classes(node: Tag) -> list[str]:
    value = node.get("class")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _extract_media(post_node: Tag) -> tuple[Media, ...]:
    media: list[Media] = []
    for wrap in post_node.select("a.tgme_widget_message_photo_wrap"):
        style = wrap.get("style")
        url = _style_url(style if isinstance(style, str) else None)
        media.append(Media(type="photo", url=url, thumb_url=None, caption=None))
    for video in post_node.select("video.tgme_widget_message_video"):
        src = video.get("src")
        url = src if isinstance(src, str) and src else None
        thumb = None
        thumb_node = post_node.select_one(".tgme_widget_message_video_thumb")
        if thumb_node is not None:
            style = thumb_node.get("style")
            thumb = _style_url(style if isinstance(style, str) else None)
        media.append(Media(type="video", url=url, thumb_url=thumb, caption=None))
    return tuple(media)


def _parse_post(node: Tag, channel: str, title: str) -> Post:
    data_post = node.get("data-post")
    if not isinstance(data_post, str) or "/" not in data_post:
        raise LayoutChanged(f"unexpected data-post: {data_post!r}")
    post_id = int(data_post.split("/")[-1])

    text_node = node.select_one(".tgme_widget_message_text")
    text_value = text_node.get_text("\n", strip=True) if text_node is not None else None

    date_value: datetime | None = None
    url_value: str | None = None
    date_anchor = node.select_one("a.tgme_widget_message_date")
    if date_anchor is not None:
        href = date_anchor.get("href")
        if isinstance(href, str) and href:
            url_value = href
        time_node = date_anchor.select_one("time[datetime]")
        if time_node is not None:
            datetime_attr = time_node.get("datetime")
            if isinstance(datetime_attr, str):
                date_value = datetime.fromisoformat(datetime_attr)

    return Post(
        channel=channel,
        post_id=post_id,
        title=title,
        text=text_value,
        date=date_value,
        media=_extract_media(node),
        media_not_supported=node.select_one(".message_media_not_supported") is not None,
        url=url_value or f"https://t.me/{channel}/{post_id}",
    )


def parse_posts(html: str, channel: str, since_post_id: int | None) -> PageResult:
    """Извлекает все посты с post_id > since_post_id (None — все), по возрастанию post_id."""
    soup = BeautifulSoup(html, "lxml")
    title_node = soup.select_one(".tgme_channel_info_header_title")
    if title_node is None:
        raise LayoutChanged("channel header not found")
    title = title_node.get_text(strip=True)

    posts = [
        _parse_post(node, channel, title)
        for node in soup.select("div.tgme_widget_message[data-post]")
        if "service_message" not in _classes(node)
    ]
    if since_post_id is not None:
        posts = [post for post in posts if post.post_id > since_post_id]
    posts.sort(key=lambda post: post.post_id)
    return PageResult(title=title, posts=tuple(posts))
