"""Валидация имён каналов и Telegram-идентификаторов."""

import re
from dataclasses import dataclass

USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_URL_PREFIX_RE = re.compile(r"^(https?://)?(t\.me|telegram\.me)/(s/)?")


@dataclass(frozen=True, slots=True)
class NameResolution:
    name: str | None
    warning: str | None
    reject_reason: str | None


def normalize(raw: str) -> str | None:
    name = raw.strip().removeprefix("@")
    name = _URL_PREFIX_RE.sub("", name).split("/")[0]
    return name if USERNAME_RE.match(name) else None


def resolve_watch_name(channel_name: str | None, channel_url: str) -> NameResolution:
    """Определяет итоговое имя канала по таблице v2 раздел 3.1.

    Приоритет у channel_name; при невалидном channel_name откат на URL не делается.
    Пустая строка channel_name считается «не передан».
    """
    normalized_name = normalize(channel_name) if channel_name else None
    if channel_name and normalized_name is None:
        return NameResolution(name=None, warning=None, reject_reason="invalid_channel_name")

    url_name = normalize(channel_url)

    if normalized_name is not None:
        warning: str | None
        if url_name is None:
            warning = "channel_url_unparsed"
        elif url_name != normalized_name:
            warning = "channel_name_differs_from_url"
        else:
            warning = None
        return NameResolution(name=normalized_name, warning=warning, reject_reason=None)

    if url_name is None:
        return NameResolution(name=None, warning=None, reject_reason="invalid_channel_url")
    return NameResolution(name=url_name, warning=None, reject_reason=None)


def validate_channel_id(value: int | None) -> bool:
    """None допустим (поле необязательное); иначе -100…, минимум 13 символов."""
    if value is None:
        return True
    text = str(value)
    return value < 0 and text.startswith("-100") and len(text) >= 13
