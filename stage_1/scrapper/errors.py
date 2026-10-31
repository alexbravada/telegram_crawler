"""Exception hierarchy for channel fetching.

Errors carry structured data (channel, status, reason) while the human-readable
text is rendered in one place — scrapper/messages.py.
"""

from typing import Optional

from . import messages


class ChannelError(Exception):
    """Base class for channel fetching errors."""

    def __init__(self, channel: str) -> None:
        self.channel = channel
        super().__init__(self.message)

    @property
    def message(self) -> str:
        raise NotImplementedError


class ChannelNotFoundError(ChannelError):
    """Fatal error: channel does not exist or is private. Not retried."""

    def __init__(self, channel: str, status_code: Optional[int] = None) -> None:
        self.status_code = status_code
        super().__init__(channel)

    @property
    def message(self) -> str:
        if self.status_code is not None:
            return messages.channel_not_found(self.channel, self.status_code)
        return messages.channel_empty(self.channel)


class ChannelFetchError(ChannelError):
    """Retryable error: network failure or transient HTTP status (429, 5xx)."""

    def __init__(self, channel: str, reason: str) -> None:
        self.reason = reason
        super().__init__(channel)

    @property
    def message(self) -> str:
        return messages.channel_fetch_failed(self.channel, self.reason)
