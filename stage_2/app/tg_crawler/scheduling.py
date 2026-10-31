"""Расчёт next_check_at: интервалы по активности, backoff, jitter."""

import random
from datetime import UTC, datetime, timedelta

MIN_15 = 15 * 60
MIN_30 = 30 * 60
H_2 = 2 * 3600
H_6 = 6 * 3600
H_12 = 12 * 3600
CAP_6H = 6 * 3600
BACKOFF_BASE = 5 * 60

RETRY_MIN = 60.0
RETRY_MAX = 300.0


def next_check_after_ok(
    last_post_at: datetime | None, now: datetime, rnd: random.Random
) -> datetime:
    """Интервал проверки по активности канала (v1 раздел 9)."""
    if last_post_at is None:
        delay = rnd.uniform(H_6, H_12)
    else:
        posted_at = last_post_at
        if posted_at.tzinfo is None:
            posted_at = posted_at.replace(tzinfo=UTC)
        age = now - posted_at
        if age < timedelta(hours=24):
            delay = rnd.uniform(MIN_15, MIN_30)
        elif age < timedelta(days=7):
            delay = rnd.uniform(H_2 * 0.9, H_2 * 1.1)
        else:
            delay = rnd.uniform(H_6, H_12)
    return now + timedelta(seconds=delay)


def retry_delay(rnd: random.Random) -> float:
    """Повтор после RATE_LIMITED / RETRYABLE: 1–5 мин со случайным разбросом."""
    return float(rnd.uniform(RETRY_MIN, RETRY_MAX))


def error_backoff(fail_count: int) -> float:
    """Экспонента по fail_count (LAYOUT_CHANGED, ошибки парсера), потолок 6 ч."""
    return float(min(BACKOFF_BASE * 2 ** max(0, fail_count - 1), CAP_6H))


def not_found_delay() -> float:
    """После NOT_FOUND следующая проверка через 6 ч (v1 раздел 10.6)."""
    return CAP_6H
