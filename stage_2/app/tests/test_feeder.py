"""Тесты Feeder: паузы, allow_claim, ограниченная очередь, stop() (фейки, без БД)."""

import asyncio
import random
import uuid

from tg_crawler.breaker import Breaker
from tg_crawler.config import Settings
from tg_crawler.models import ChannelTask, Outcome
from tg_crawler.writer import Op


def make_settings(**kwargs: object) -> Settings:
    base = {
        "database_url": "postgresql+asyncpg://u:p@db:5432/tg",
        "publisher_url": "http://emulator:8001/messages",
        "queue_size": 2,
        "claim_batch": 10,
        "feeder_poll_interval": 0.0,
    }
    base.update(kwargs)
    return Settings(**base)  # type: ignore[arg-type]


class FakeRepository:
    def __init__(
        self,
        claim_result: tuple[list[ChannelTask], list[ChannelTask]],
        *,
        respect_batch: bool = True,
    ) -> None:
        self.claim_result = claim_result
        self.respect_batch = respect_batch
        self.claim_calls: list[int] = []
        self.released: list[list[ChannelTask]] = []

    async def claim(self, batch: int, lease: float) -> tuple[list[ChannelTask], list[ChannelTask]]:
        self.claim_calls.append(batch)
        valid, invalid = self.claim_result
        self.claim_result = ([], [])
        if self.respect_batch:
            return valid[:batch], invalid[:batch]
        return valid, invalid

    async def release(self, tasks: list[ChannelTask]) -> None:
        self.released.append(tasks)


class FakeWriter:
    def __init__(self) -> None:
        self.ops: list[Op] = []

    def submit(self, op: Op) -> None:
        self.ops.append(op)


class FakePool:
    def __init__(self, alive: bool) -> None:
        self._alive = alive

    def has_alive(self) -> bool:
        return self._alive


def make_task(name: str = "durov") -> ChannelTask:
    return ChannelTask(id=1, name=name, last_post_id=None, token=uuid.uuid4(), channel_id=None)


def make_feeder(
    repo: FakeRepository,
    settings: Settings | None = None,
    pool: object | None = None,
    breaker: Breaker | None = None,
    writer: FakeWriter | None = None,
) -> object:
    from tg_crawler.feeder import Feeder

    return Feeder(
        repository=repo,  # type: ignore[arg-type]
        pool=pool or FakePool(True),
        breaker=breaker or Breaker(),
        writer=writer or FakeWriter(),  # type: ignore[arg-type]
        settings=settings or make_settings(),
        rnd=random.Random(1),
    )


async def run_until(feeder: object, rounds: int = 5, *, stop: bool = True) -> None:
    runner = asyncio.create_task(feeder.run())  # type: ignore[attr-defined]
    for _ in range(rounds):
        await asyncio.sleep(0)
    if stop:
        await feeder.stop()  # type: ignore[attr-defined]
    runner.cancel()
    try:
        await runner
    except asyncio.CancelledError:
        pass


async def test_claim_and_queue_fill() -> None:
    tasks = [make_task("a"), make_task("b")]
    repo = FakeRepository((tasks, []))
    feeder = make_feeder(repo)
    await run_until(feeder, stop=False)
    assert repo.claim_calls and repo.claim_calls[0] == 2  # min(free, claim_batch)
    assert feeder.queue.qsize() == 2  # type: ignore[attr-defined]
    assert repo.released == []


async def test_invalid_name_goes_to_writer_not_queue() -> None:
    writer = FakeWriter()
    invalid = [make_task("1abc")]
    repo = FakeRepository(([], invalid))
    feeder = make_feeder(repo, writer=writer)
    await run_until(feeder)
    assert [op.kind for op in writer.ops] == ["broken"]
    assert writer.ops[0].reason == "invalid_name"
    assert feeder.queue.qsize() == 0  # type: ignore[attr-defined]


async def test_queue_full_releases_remaining() -> None:
    tasks = [make_task("a"), make_task("b"), make_task("c")]
    settings = make_settings(queue_size=1, claim_batch=10)
    repo = FakeRepository((tasks, []), respect_batch=False)
    feeder = make_feeder(repo, settings=settings)
    await run_until(feeder, stop=False)
    assert feeder.queue.qsize() == 1  # type: ignore[attr-defined]
    released = [task.name for batch in repo.released for task in batch]
    assert released == ["b", "c"]


async def test_pause_when_no_alive_proxies() -> None:
    repo = FakeRepository(([make_task()], []))
    feeder = make_feeder(repo, pool=FakePool(False))
    await run_until(feeder)
    assert repo.claim_calls == []


async def test_pause_when_breaker_open() -> None:
    breaker = Breaker()
    for _ in range(100):
        breaker.record(Outcome.OK)
    for _ in range(21):
        breaker.record(Outcome.NOT_FOUND)
    assert breaker.is_open() is True
    repo = FakeRepository(([make_task()], []))
    feeder = make_feeder(repo, breaker=breaker)
    await run_until(feeder)
    assert repo.claim_calls == []


async def test_allow_claim_limits_batch() -> None:
    breaker = Breaker(trial_batch=5, pause=0.0, clock=lambda: 1000.0)
    for _ in range(100):
        breaker.record(Outcome.OK)
    for _ in range(21):
        breaker.record(Outcome.NOT_FOUND)
    # после открытия с pause=0 наступает trial: is_open False, лимит trial_batch
    assert breaker.is_open() is False
    tasks = [make_task(f"task{i:04d}") for i in range(12)]
    repo = FakeRepository((tasks, []))
    settings = make_settings(queue_size=10, claim_batch=10)
    feeder = make_feeder(repo, settings=settings, breaker=breaker)
    await run_until(feeder, stop=False)
    assert repo.claim_calls[0] == 5  # allow_claim(min(10, 10)) → trial_batch 5
    assert feeder.queue.qsize() == 5  # type: ignore[attr-defined]


async def test_stop_returns_queued_tasks() -> None:
    tasks = [make_task("a"), make_task("b")]
    repo = FakeRepository((tasks, []))
    feeder = make_feeder(repo)
    runner = asyncio.create_task(feeder.run())  # type: ignore[attr-defined]
    for _ in range(3):
        await asyncio.sleep(0)
    assert feeder.queue.qsize() == 2  # type: ignore[attr-defined]
    await feeder.stop()  # type: ignore[attr-defined]
    runner.cancel()
    try:
        await runner
    except asyncio.CancelledError:
        pass
    assert feeder.queue.qsize() == 0  # type: ignore[attr-defined]
    released = [task.name for batch in repo.released for task in batch]
    assert sorted(released) == ["a", "b"]
