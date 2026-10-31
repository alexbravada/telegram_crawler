"""Circuit breaker против массовых ложных NOT_FOUND / LAYOUT_CHANGED (v1 раздел 10.7)."""

import time
from collections import deque
from collections.abc import Callable

from tg_crawler.models import Outcome

BAD_OUTCOMES = frozenset({Outcome.NOT_FOUND, Outcome.LAYOUT_CHANGED})


class Breaker:
    def __init__(
        self,
        *,
        window_size: int = 100,
        threshold: float = 0.2,
        pause: float = 300.0,
        trial_batch: int = 10,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window_size = window_size
        self._threshold = threshold
        self._pause = pause
        self._trial_batch = trial_batch
        self._clock = clock
        self._window: deque[bool] = deque(maxlen=window_size)
        self._state = "closed"  # closed | open | trial
        self._open_until = 0.0
        self._trial_left = 0

    def _refresh(self) -> None:
        if self._state == "open" and self._clock() >= self._open_until:
            self._state = "trial"
            self._trial_left = self._trial_batch

    def record(self, outcome: Outcome) -> None:
        self._refresh()
        bad = outcome in BAD_OUTCOMES
        if self._state == "trial":
            if bad:
                self._state = "open"
                self._open_until = self._clock() + self._pause
            else:
                self._trial_left -= 1
                if self._trial_left <= 0:
                    self._state = "closed"
                    self._window.clear()
            return
        self._window.append(bad)
        if len(self._window) == self._window_size:
            share = sum(self._window) / len(self._window)
            if share > self._threshold:
                self._state = "open"
                self._open_until = self._clock() + self._pause

    def is_open(self) -> bool:
        self._refresh()
        return self._state == "open"

    def allow_claim(self, count: int) -> int:
        self._refresh()
        if self._state == "open":
            return 0
        if self._state == "trial":
            return min(count, self._trial_batch)
        return count
