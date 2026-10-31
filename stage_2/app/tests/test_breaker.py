"""Тесты circuit breaker на фейковых часах."""

from collections.abc import Iterator

import pytest

from tg_crawler.breaker import Breaker
from tg_crawler.models import Outcome


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Iterator[FakeClock]:
    yield FakeClock()


def make_breaker(clock: FakeClock, **kwargs: object) -> Breaker:
    defaults: dict[str, object] = {
        "window_size": 100,
        "threshold": 0.2,
        "pause": 300.0,
        "trial_batch": 10,
    }
    defaults.update(kwargs)
    return Breaker(clock=clock, **defaults)  # type: ignore[arg-type]


def fill(breaker: Breaker, good: int, bad: int) -> None:
    for _ in range(good):
        breaker.record(Outcome.OK)
    for _ in range(bad):
        breaker.record(Outcome.NOT_FOUND)


def test_under_threshold_closed(clock: FakeClock) -> None:
    breaker = make_breaker(clock)
    fill(breaker, good=80, bad=20)  # ровно 20%, не больше порога
    assert breaker.is_open() is False
    assert breaker.allow_claim(10) == 10


def test_over_threshold_opens(clock: FakeClock) -> None:
    breaker = make_breaker(clock)
    fill(breaker, good=79, bad=21)
    assert breaker.is_open() is True
    assert breaker.allow_claim(10) == 0


def test_after_pause_trial_batch_allowed(clock: FakeClock) -> None:
    breaker = make_breaker(clock)
    fill(breaker, good=79, bad=21)
    clock.now += 300.0
    assert breaker.is_open() is False
    assert breaker.allow_claim(10) == 10
    assert breaker.allow_claim(25) == 10  # пробная партия ограничена


def test_good_trial_closes_breaker(clock: FakeClock) -> None:
    breaker = make_breaker(clock)
    fill(breaker, good=79, bad=21)
    clock.now += 300.0
    assert breaker.allow_claim(10) == 10
    for _ in range(10):
        breaker.record(Outcome.OK)
    assert breaker.is_open() is False
    assert breaker.allow_claim(50) == 50


def test_bad_trial_reopens_breaker(clock: FakeClock) -> None:
    breaker = make_breaker(clock)
    fill(breaker, good=79, bad=21)
    clock.now += 300.0
    assert breaker.allow_claim(10) == 10
    breaker.record(Outcome.LAYOUT_CHANGED)
    assert breaker.is_open() is True
    assert breaker.allow_claim(10) == 0
    clock.now += 300.0
    assert breaker.allow_claim(10) == 10


def test_window_slides_old_records_drop_out(clock: FakeClock) -> None:
    breaker = make_breaker(clock)
    fill(breaker, good=79, bad=21)
    assert breaker.is_open() is True
    clock.now += 300.0
    assert breaker.allow_claim(10) == 10
    for _ in range(10):
        breaker.record(Outcome.OK)
    # пробная партия успешна, breaker закрыт; окно чистится, 79 старых плохих больше не учитываются
    assert breaker.allow_claim(50) == 50
    for _ in range(99):
        breaker.record(Outcome.OK)
    assert breaker.is_open() is False


def test_partial_window_below_threshold(clock: FakeClock) -> None:
    breaker = make_breaker(clock)
    fill(breaker, good=95, bad=5)
    assert breaker.is_open() is False
