"""Тесты планирования next_check_at и backoff (инжектируемый random.Random)."""

import random
from datetime import UTC, datetime, timedelta

from tg_crawler.scheduling import (
    error_backoff,
    next_check_after_ok,
    not_found_delay,
    retry_delay,
)

NOW = datetime(2024, 11, 1, 12, 0, tzinfo=UTC)


def delays(last_post_at: datetime | None, trials: int = 200) -> list[float]:
    rnd = random.Random(42)
    return [
        (next_check_after_ok(last_post_at, NOW, rnd) - NOW).total_seconds() for _ in range(trials)
    ]


def test_recent_channel_interval_15_30_min() -> None:
    recent = NOW - timedelta(hours=1)
    values = delays(recent)
    assert all(900 <= v <= 1800 for v in values)
    assert max(values) - min(values) > 60  # jitter реально присутствует


def test_week_old_channel_interval_about_2h() -> None:
    week = NOW - timedelta(days=3)
    values = delays(week)
    assert all(6480 <= v <= 7920 for v in values)  # 2 ч ± 10%


def test_old_channel_interval_6_12h() -> None:
    old = NOW - timedelta(days=10)
    values = delays(old)
    assert all(21600 <= v <= 43200 for v in values)


def test_no_posts_interval_6_12h() -> None:
    values = delays(None)
    assert all(21600 <= v <= 43200 for v in values)


def test_naive_last_post_at_treated_as_utc() -> None:
    naive = datetime(2024, 11, 1, 11, 0)  # час назад, без tzinfo
    values = delays(naive, trials=20)
    assert all(900 <= v <= 1800 for v in values)


def test_retry_delay_1_5_min() -> None:
    rnd = random.Random(7)
    values = [retry_delay(rnd) for _ in range(200)]
    assert all(60 <= v <= 300 for v in values)


def test_error_backoff_grows_and_capped() -> None:
    assert error_backoff(1) == 300.0
    assert error_backoff(2) == 600.0
    assert error_backoff(3) == 1200.0
    assert error_backoff(4) > error_backoff(3)
    assert error_backoff(100) == 21600.0  # потолок 6 ч
    for n in range(1, 30):
        assert error_backoff(n + 1) >= error_backoff(n)
        assert error_backoff(n) <= 21600.0


def test_not_found_delay_is_6h() -> None:
    assert not_found_delay() == 21600.0
