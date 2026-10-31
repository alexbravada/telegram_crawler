"""Тесты ProxyPool на фейковых часах."""

import random
from typing import Any

import httpx
import pytest
import respx

from tg_crawler.models import Outcome
from tg_crawler.proxy_pool import ProxyPool


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now += delay


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def make_pool(
    proxies: list[str],
    clock: FakeClock,
    **kwargs: Any,
) -> ProxyPool:
    defaults: dict[str, Any] = {"min_interval": 4.0, "dead_after": 5, "recheck_interval": 600.0}
    defaults.update(kwargs)
    rnd = defaults.pop("rnd_seed", None)
    return ProxyPool(
        proxies,
        clock=clock,
        sleep=clock.sleep,
        rnd=random.Random(rnd) if rnd is not None else None,
        **defaults,
    )


async def test_single_proxy_respects_min_interval(clock: FakeClock) -> None:
    pool = make_pool(["http://p1:8080"], clock)
    lease1 = await pool.acquire()
    assert lease1.id == "http://p1:8080"
    pool.release(lease1, Outcome.OK)
    before = clock.now
    lease2 = await pool.acquire()
    assert clock.now - before == 4.0
    assert lease2.id == "http://p1:8080"
    await pool.aclose()


async def test_three_proxies_alternate(clock: FakeClock) -> None:
    pool = make_pool(["http://p1:8080", "http://p2:8080", "http://p3:8080"], clock)
    leases = [await pool.acquire() for _ in range(3)]
    assert {lease.id for lease in leases} == {
        "http://p1:8080",
        "http://p2:8080",
        "http://p3:8080",
    }
    for lease in leases:
        pool.release(lease, Outcome.OK)
    await pool.aclose()


async def test_rate_limited_with_retry_after(clock: FakeClock) -> None:
    pool = make_pool(["http://p1:8080"], clock)
    lease = await pool.acquire()
    pool.release(lease, Outcome.RATE_LIMITED, retry_after=60.0)
    before = clock.now
    await pool.acquire()
    assert clock.now - before == 60.0
    stats = pool.stats()["http://p1:8080"]
    assert stats["min_interval"] == 6.0  # 4.0 * 1.5
    await pool.aclose()


async def test_rate_limited_exponential_backoff_with_jitter(clock: FakeClock) -> None:
    pool = make_pool(["http://p1:8080"], clock, rnd_seed=1)
    lease = await pool.acquire()
    pool.release(lease, Outcome.RATE_LIMITED)
    for expected_low, expected_high in [(24.0, 36.0), (48.0, 72.0)]:
        before = clock.now
        lease = await pool.acquire()
        delay = clock.now - before
        pool.release(lease, Outcome.RATE_LIMITED)
        assert expected_low <= delay <= expected_high
    await pool.aclose()


async def test_success_series_returns_min_interval_to_base(clock: FakeClock) -> None:
    pool = make_pool(["http://p1:8080"], clock)
    lease = await pool.acquire()
    pool.release(lease, Outcome.RATE_LIMITED, retry_after=1.0)
    lease = await pool.acquire()
    pool.release(lease, Outcome.RATE_LIMITED, retry_after=1.0)
    assert pool.stats()["http://p1:8080"]["min_interval"] == 9.0
    for _ in range(20):
        lease = await pool.acquire()
        pool.release(lease, Outcome.OK)
    assert pool.stats()["http://p1:8080"]["min_interval"] == 4.0
    await pool.aclose()


async def test_dead_after_k_errors_and_has_alive_false(clock: FakeClock) -> None:
    pool = make_pool(["http://p1:8080"], clock)
    for _ in range(5):
        lease = await pool.acquire()
        pool.release(lease, Outcome.RETRYABLE)
    assert pool.stats()["http://p1:8080"]["status"] == "dead"
    assert pool.has_alive() is False
    clock.now += 600.0
    assert pool.has_alive() is True
    lease = await pool.acquire()
    pool.release(lease, Outcome.OK)
    assert pool.stats()["http://p1:8080"]["status"] == "healthy"
    await pool.aclose()


async def test_dead_proxy_stays_dead_on_failed_recheck(clock: FakeClock) -> None:
    pool = make_pool(["http://p1:8080"], clock)
    for _ in range(5):
        lease = await pool.acquire()
        pool.release(lease, Outcome.RETRYABLE)
    clock.now += 600.0
    lease = await pool.acquire()
    pool.release(lease, Outcome.RETRYABLE)
    assert pool.stats()["http://p1:8080"]["status"] == "dead"
    clock.now += 599.0
    assert pool.has_alive() is False
    clock.now += 1.0
    assert pool.has_alive() is True
    await pool.aclose()


async def test_empty_proxies_single_direct_client(clock: FakeClock) -> None:
    pool = make_pool([], clock)
    lease = await pool.acquire()
    assert lease.id == "direct"
    pool.release(lease, Outcome.OK)
    stats = pool.stats()
    assert set(stats) == {"direct"}
    assert pool.has_alive() is True
    await pool.aclose()


async def test_not_found_and_layout_changed_do_not_hurt_proxy(clock: FakeClock) -> None:
    pool = make_pool(["http://p1:8080"], clock)
    for outcome in (Outcome.NOT_FOUND, Outcome.LAYOUT_CHANGED):
        lease = await pool.acquire()
        pool.release(lease, outcome)
    stats = pool.stats()["http://p1:8080"]
    assert stats["consecutive_errors"] == 0
    await pool.aclose()


@respx.mock
async def test_health_check_marks_dead_on_connect_error() -> None:
    respx.get("https://t.me/").mock(side_effect=httpx.ConnectError("fail"))
    pool = ProxyPool(["http://p1:8080"])
    await pool.health_check()
    assert pool.stats()["http://p1:8080"]["status"] == "dead"
    await pool.aclose()


@respx.mock
async def test_health_check_success_keeps_healthy() -> None:
    respx.get("https://t.me/").mock(return_value=httpx.Response(200))
    pool = ProxyPool(["http://p1:8080", "http://p2:8080"])
    await pool.health_check()
    assert pool.stats()["http://p1:8080"]["status"] == "healthy"
    assert pool.stats()["http://p2:8080"]["status"] == "healthy"
    await pool.aclose()


async def test_aclose_closes_all_clients(clock: FakeClock) -> None:
    pool = make_pool(["http://p1:8080", "http://p2:8080"], clock)
    clients = pool.clients
    await pool.aclose()
    assert all(client.is_closed for client in clients)
