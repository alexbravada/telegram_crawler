"""Тесты валидации имён каналов и channel_id."""

import pytest

from tg_crawler.names import normalize, resolve_watch_name, validate_channel_id


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("@name", "name"),
        ("name", "name"),
        ("t.me/name", "name"),
        ("https://t.me/s/name/123", "name"),
        ("telegram.me/name", "name"),
        ("  @name  ", "name"),
        ("https://t.me/name", "name"),
    ],
)
def test_normalize_valid(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["ab", "1abc", "name!", "имя", "", "   ", "@", "t.me/", "a" * 33],
)
def test_normalize_invalid(raw: str) -> None:
    assert normalize(raw) is None


def test_resolve_name_matches() -> None:
    r = resolve_watch_name("durov", "https://t.me/durov")
    assert r.name == "durov"
    assert r.warning is None
    assert r.reject_reason is None


def test_resolve_name_differs_from_url() -> None:
    r = resolve_watch_name("durov", "t.me/telegram")
    assert r.name == "durov"
    assert r.warning == "channel_name_differs_from_url"
    assert r.reject_reason is None


def test_resolve_name_url_unparsed() -> None:
    r = resolve_watch_name("@durov", "мусор")
    assert r.name == "durov"
    assert r.warning == "channel_url_unparsed"
    assert r.reject_reason is None


def test_resolve_invalid_name_rejected() -> None:
    r = resolve_watch_name("1abc", "t.me/durov")
    assert r.name is None
    assert r.reject_reason == "invalid_channel_name"


@pytest.mark.parametrize("channel_name", [None, ""])
def test_resolve_name_from_url(channel_name: str | None) -> None:
    r = resolve_watch_name(channel_name, "t.me/durov")
    assert r.name == "durov"
    assert r.warning is None
    assert r.reject_reason is None


@pytest.mark.parametrize("channel_name", [None, ""])
def test_resolve_no_name_bad_url_rejected(channel_name: str | None) -> None:
    r = resolve_watch_name(channel_name, "мусор")
    assert r.name is None
    assert r.reject_reason == "invalid_channel_url"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, True),
        (-1001234567890, True),
        (-1009999999999, True),
        (1234567890, False),
        (-100, False),
        (-1002, False),
        (0, False),
        (1001234567890, False),
    ],
)
def test_validate_channel_id(value: int | None, expected: bool) -> None:
    assert validate_channel_id(value) is expected
