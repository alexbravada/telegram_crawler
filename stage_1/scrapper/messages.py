"""Single source of all user-facing strings: CLI output, errors, logs."""

from typing import Optional


def invalid_link(source: str) -> str:
    return f"Not a Telegram channel link: '{source}'"


def channel_not_found(channel: str, status_code: Optional[int] = None) -> str:
    if status_code is not None:
        return f"Channel '{channel}' not found or unavailable (HTTP {status_code})"
    return f"Channel '{channel}' not found"


def channel_empty(channel: str) -> str:
    return f"Channel '{channel}' is empty, private, or does not exist"


def channel_fetch_failed(channel: str, reason: str) -> str:
    return f"Failed to fetch channel '{channel}': {reason}"


def channel_skipped(channel: str, error: str) -> str:
    return f"[{channel}] skipped: {error}"


def fetch_retry(channel: str, attempt: int, retries: int, error: str) -> str:
    return f"[{channel}] attempt {attempt}/{retries} failed ({error}), retrying..."


def no_posts() -> str:
    return "No posts were fetched for any channel"
