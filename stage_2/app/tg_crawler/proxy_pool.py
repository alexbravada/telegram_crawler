"""ProxyPool: темп на прокси, cooldown после 429, AIMD, dead-статус и перепроверка."""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final

from httpx import AsyncClient

from tg_crawler.config import mask_proxy_url
from tg_crawler.models import Outcome

PROXY_SUCCESS_OUTCOMES: Final[frozenset[Outcome]] = frozenset(
    {Outcome.OK, Outcome.NOT_FOUND, Outcome.LAYOUT_CHANGED, Outcome.INVALID_NAME}
)
AIMD_STEP: Final = 0.5
COOLDOWN_BASE: Final = 30.0
COOLDOWN_CAP: Final = 900.0
JITTER: Final = 0.2
IDLE_WAIT: Final = 0.5


@dataclass(slots=True)
class _ProxyState:
    id: str
    client: AsyncClient
    ready_at: float
    min_interval: float
    base_interval: float
    consecutive_errors: int = 0
    cooldown_step: int = 0
    status: str = "healthy"  # healthy | cooldown | dead
    next_recheck: float = 0.0
    in_flight: bool = False


@dataclass(frozen=True, slots=True)
class ProxyLease:
    id: str
    client: AsyncClient
    _state: _ProxyState


class ProxyPool:
    def __init__(
        self,
        proxies: Sequence[str],
        *,
        min_interval: float = 4.0,
        dead_after: int = 5,
        recheck_interval: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        rnd: random.Random | None = None,
    ) -> None:
        self._clock = clock
        self._sleep: Callable[[float], Awaitable[None]] = sleep or asyncio.sleep
        self._rnd = rnd or random.Random()
        self._dead_after = dead_after
        self._recheck_interval = recheck_interval
        now = clock()
        self._states: list[_ProxyState] = []
        if proxies:
            for url in proxies:
                self._states.append(
                    _ProxyState(
                        id=mask_proxy_url(url),
                        client=AsyncClient(proxy=url),
                        ready_at=now,
                        min_interval=min_interval,
                        base_interval=min_interval,
                    )
                )
        else:
            self._states.append(
                _ProxyState(
                    id="direct",
                    client=AsyncClient(),
                    ready_at=now,
                    min_interval=min_interval,
                    base_interval=min_interval,
                )
            )

    @property
    def clients(self) -> tuple[AsyncClient, ...]:
        return tuple(state.client for state in self._states)

    async def acquire(self) -> ProxyLease:
        while True:
            now = self._clock()
            candidates = [
                state
                for state in self._states
                if not state.in_flight and (state.status != "dead" or now >= state.next_recheck)
            ]
            if not candidates:
                await self._sleep(IDLE_WAIT)
                continue
            state = min(candidates, key=lambda item: item.ready_at)
            delay = state.ready_at - now
            if delay > 0:
                await self._sleep(delay)
            state.in_flight = True
            return ProxyLease(id=state.id, client=state.client, _state=state)

    def release(
        self,
        lease: ProxyLease,
        outcome: Outcome,
        retry_after: float | None = None,
    ) -> None:
        state = lease._state
        state.in_flight = False
        now = self._clock()
        if outcome in PROXY_SUCCESS_OUTCOMES:
            state.consecutive_errors = 0
            state.cooldown_step = 0
            state.min_interval = max(state.base_interval, state.min_interval - AIMD_STEP)
            state.status = "healthy"
            state.ready_at = now + state.min_interval
            return
        if outcome is Outcome.RATE_LIMITED:
            if retry_after is not None:
                delay = float(retry_after)
            else:
                delay = min(COOLDOWN_BASE * 2**state.cooldown_step, COOLDOWN_CAP)
                delay *= self._rnd.uniform(1 - JITTER, 1 + JITTER)
                state.cooldown_step += 1
            state.ready_at = now + delay
            state.min_interval *= 1.5
            state.status = "cooldown"
            return
        state.consecutive_errors += 1
        if state.status == "dead" or state.consecutive_errors >= self._dead_after:
            state.status = "dead"
            state.next_recheck = now + self._recheck_interval
            state.ready_at = state.next_recheck
        else:
            state.ready_at = now + state.min_interval

    def has_alive(self) -> bool:
        now = self._clock()
        return any(state.status != "dead" or now >= state.next_recheck for state in self._states)

    def stats(self) -> dict[str, dict[str, object]]:
        return {
            state.id: {
                "status": state.status,
                "min_interval": state.min_interval,
                "consecutive_errors": state.consecutive_errors,
            }
            for state in self._states
        }

    async def health_check(self) -> None:
        for state in self._states:
            try:
                response = await state.client.get("https://t.me/")
                if response.status_code < 500:
                    state.status = "healthy"
                else:
                    state.status = "dead"
            except Exception:
                state.status = "dead"
            if state.status == "dead":
                state.next_recheck = self._clock() + self._recheck_interval
                state.ready_at = state.next_recheck

    async def aclose(self) -> None:
        for state in self._states:
            await state.client.aclose()
