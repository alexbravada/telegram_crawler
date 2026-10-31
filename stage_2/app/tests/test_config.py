"""Тесты Settings: парсинг из окружения и дефолты."""

import pytest
from pydantic import ValidationError

from tg_crawler.config import Settings, mask_proxy_url


def setup_env(monkeypatch: pytest.MonkeyPatch, **overrides: str | None) -> None:
    env: dict[str, str | None] = {
        "TG_CRAWLER_DATABASE_URL": "postgresql+asyncpg://u:p@db:5432/tg",
        "TG_CRAWLER_PUBLISHER_URL": "http://emulator:8001/messages",
    }
    env.update(overrides)
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def test_required_fields_and_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    setup_env(monkeypatch)
    s = Settings(_env_file=None)
    assert s.database_url == "postgresql+asyncpg://u:p@db:5432/tg"
    assert s.publisher_url == "http://emulator:8001/messages"
    assert s.proxies == []
    assert s.proxy_min_interval == 4.0
    assert s.max_inflight == 10
    assert s.workers == 2  # max(1, len([])) * 2
    assert s.queue_size == 50
    assert s.claim_batch == 20
    assert s.lease == 600.0
    assert s.shutdown_timeout == 30.0
    assert s.writer_flush_interval == 1.5
    assert s.writer_batch == 100
    assert s.breaker_window == 100
    assert s.breaker_threshold == 0.2
    assert s.breaker_pause == 300.0
    assert s.not_found_threshold == 3
    assert s.not_found_min_gap == 21600.0
    assert s.proxy_dead_after == 5
    assert s.proxy_recheck == 600.0
    assert s.stats_log_interval == 60.0
    assert s.publisher_timeout == 5.0
    assert s.publisher_max_attempts == 5
    assert s.feeder_poll_interval == 5.0
    assert s.watch_max_batch == 1000
    assert s.outbox_poll_interval == 5.0
    assert s.outbox_batch == 50
    assert s.outbox_max_attempts == 3


def test_workers_computed_from_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    setup_env(
        monkeypatch, TG_CRAWLER_PROXIES="http://a:1,http://b:2,http://c:3,http://d:4,http://e:5"
    )
    s = Settings(_env_file=None)
    assert s.proxies == ["http://a:1", "http://b:2", "http://c:3", "http://d:4", "http://e:5"]
    assert s.workers == 10


def test_proxies_empty_string_is_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    setup_env(monkeypatch, TG_CRAWLER_PROXIES="")
    s = Settings(_env_file=None)
    assert s.proxies == []


def test_proxies_whitespace_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    setup_env(monkeypatch, TG_CRAWLER_PROXIES=" http://a:1 , http://b:2 ")
    s = Settings(_env_file=None)
    assert s.proxies == ["http://a:1", "http://b:2"]


def test_workers_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    setup_env(monkeypatch, TG_CRAWLER_WORKERS="7")
    s = Settings(_env_file=None)
    assert s.workers == 7


@pytest.mark.parametrize("missing", ["TG_CRAWLER_DATABASE_URL", "TG_CRAWLER_PUBLISHER_URL"])
def test_missing_required_field_fails(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    setup_env(monkeypatch, **{missing: None})
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_mask_proxy_url_http() -> None:
    assert mask_proxy_url("http://user:secret@1.2.3.4:8080") == "http://user:***@1.2.3.4:8080"


def test_mask_proxy_url_socks() -> None:
    assert mask_proxy_url("socks5://user:secret@5.6.7.8:1080") == "socks5://user:***@5.6.7.8:1080"


def test_mask_proxy_url_without_password_unchanged() -> None:
    assert mask_proxy_url("http://1.2.3.4:8080") == "http://1.2.3.4:8080"
    assert mask_proxy_url("http://user@1.2.3.4:8080") == "http://user@1.2.3.4:8080"
