"""Конфигурация приложения: pydantic-settings с префиксом TG_CRAWLER_."""

from typing import Annotated, Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TG_CRAWLER_", env_file=".env", extra="ignore")

    database_url: str
    publisher_url: str

    proxies: Annotated[list[str], NoDecode] = []
    proxy_min_interval: float = 4.0
    max_inflight: int = 10
    workers: int | None = None
    queue_size: int = 50
    claim_batch: int = 20
    lease: float = 600.0
    shutdown_timeout: float = 30.0
    writer_flush_interval: float = 1.5
    writer_batch: int = 100
    breaker_window: int = 100
    breaker_threshold: float = 0.2
    breaker_pause: float = 300.0
    not_found_threshold: int = 3
    not_found_min_gap: float = 21600.0
    proxy_dead_after: int = 5
    proxy_recheck: float = 600.0
    stats_log_interval: float = 60.0
    publisher_timeout: float = 5.0
    publisher_max_attempts: int = 5
    feeder_poll_interval: float = 5.0
    watch_max_batch: int = 1000
    outbox_poll_interval: float = 5.0
    outbox_batch: int = 50
    outbox_max_attempts: int = 3

    @field_validator("proxies", mode="before")
    @classmethod
    def _parse_proxies(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _default_workers(self) -> "Settings":
        if self.workers is None:
            object.__setattr__(self, "workers", max(1, len(self.proxies)) * 2)
        return self


def mask_proxy_url(url: str) -> str:
    """Маскирует пароль в URL прокси для безопасного логирования."""
    parts = urlsplit(url)
    if parts.password is None:
        return url
    hostname = parts.hostname or ""
    host = f"[{hostname}]" if ":" in hostname else hostname
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    username = f"{parts.username}:" if parts.username is not None else ""
    netloc = f"{username}***@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
