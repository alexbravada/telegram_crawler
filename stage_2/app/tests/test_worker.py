"""Тесты Worker: фейковые pool/publisher/outbox, t.me через respx."""

import random
import uuid
from pathlib import Path

import httpx
import respx

from tg_crawler.breaker import Breaker
from tg_crawler.config import Settings
from tg_crawler.fetcher import Fetcher
from tg_crawler.models import ChannelTask, Post, PublishResult, PublishStatus
from tg_crawler.proxy_pool import ProxyPool
from tg_crawler.worker import Worker
from tg_crawler.writer import Op

FIXTURES = Path(__file__).parent / "fixtures"


def make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://u:p@db:5432/tg",
        publisher_url="http://emulator:8001/messages",
    )


class FakeWriter:
    def __init__(self) -> None:
        self.ops: list[Op] = []

    def submit(self, op: Op) -> None:
        self.ops.append(op)


class FakeOutbox:
    def __init__(self, max_attempts: int = 3) -> None:
        self.max_attempts = max_attempts
        self.inserted: list[int] = []
        self.statuses: dict[int, str] = {}
        self.attempts: dict[int, int] = {}

    async def insert(self, posts: tuple[Post, ...]) -> None:
        for post in posts:
            if post.post_id not in self.statuses:
                self.statuses[post.post_id] = "pending"
                self.attempts[post.post_id] = 0
            self.inserted.append(post.post_id)

    async def mark_published(self, post: Post) -> None:
        self.statuses[post.post_id] = "delivered"

    async def mark_attempt(self, post: Post, error: str | None) -> str:
        self.attempts[post.post_id] = self.attempts.get(post.post_id, 0) + 1
        if self.attempts[post.post_id] >= self.max_attempts:
            self.statuses[post.post_id] = "failed"
        return self.statuses[post.post_id]


class FakePublisher:
    def __init__(self, results: list[PublishResult] | Exception) -> None:
        self.results = results
        self.calls: list[int] = []

    async def publish(self, post: Post) -> PublishResult:
        if isinstance(self.results, Exception):
            raise self.results
        self.calls.append(post.post_id)
        return self.results.pop(0)


def make_worker(
    writer: FakeWriter,
    outbox: FakeOutbox,
    publisher: FakePublisher,
    breaker: Breaker | None = None,
) -> Worker:
    return Worker(
        settings=make_settings(),
        pool=ProxyPool([], min_interval=0.0),
        fetcher=Fetcher(),
        publisher=publisher,
        breaker=breaker or Breaker(),
        writer=writer,  # type: ignore[arg-type]
        outbox=outbox,  # type: ignore[arg-type]
        rnd=random.Random(1),
    )


def make_task(last_post_id: int | None = None) -> ChannelTask:
    return ChannelTask(
        id=1,
        name="durov",
        last_post_id=last_post_id,
        token=uuid.uuid4(),
        channel_id=None,
    )


def fixture_page() -> str:
    return (FIXTURES / "text.html").read_text(encoding="utf-8")


@respx.mock
async def test_new_post_published_once() -> None:
    respx.get("https://t.me/s/durov").mock(return_value=httpx.Response(200, text=fixture_page()))
    publisher = FakePublisher([PublishResult(status=PublishStatus.DELIVERED) for _ in range(2)])
    writer, outbox = FakeWriter(), FakeOutbox()
    worker = make_worker(writer, outbox, publisher)
    await worker.process(make_task(last_post_id=None))
    assert publisher.calls == [100, 101]
    assert outbox.statuses[100] == "delivered"
    ok = writer.ops[0]
    assert ok.kind == "ok"
    assert ok.post_id == 101


@respx.mock
async def test_old_posts_not_published() -> None:
    respx.get("https://t.me/s/durov").mock(return_value=httpx.Response(200, text=fixture_page()))
    publisher = FakePublisher([])
    writer, outbox = FakeWriter(), FakeOutbox()
    worker = make_worker(writer, outbox, publisher)
    await worker.process(make_task(last_post_id=101))
    assert publisher.calls == []
    assert writer.ops[0].kind == "ok"
    assert writer.ops[0].post_id is None


@respx.mock
async def test_two_new_posts_both_published() -> None:
    respx.get("https://t.me/s/durov").mock(return_value=httpx.Response(200, text=fixture_page()))
    publisher = FakePublisher([PublishResult(status=PublishStatus.DELIVERED) for _ in range(2)])
    writer, outbox = FakeWriter(), FakeOutbox()
    worker = make_worker(writer, outbox, publisher)
    await worker.process(make_task(last_post_id=99))
    assert publisher.calls == [100, 101]
    assert outbox.inserted == [100, 101]
    assert writer.ops[0].post_id == 101


@respx.mock
async def test_retry_later_keeps_post_pending() -> None:
    respx.get("https://t.me/s/durov").mock(return_value=httpx.Response(200, text=fixture_page()))
    publisher = FakePublisher([PublishResult(status=PublishStatus.RETRY_LATER, error="x")])
    writer, outbox = FakeWriter(), FakeOutbox()
    worker = make_worker(writer, outbox, publisher)
    await worker.process(make_task(last_post_id=None))
    assert outbox.statuses[100] == "pending"
    assert outbox.attempts[100] == 1
    assert writer.ops[0].kind == "publish_retry"
    assert writer.ops[0].post_id is None


@respx.mock
async def test_rejected_below_threshold_stays_pending() -> None:
    respx.get("https://t.me/s/durov").mock(return_value=httpx.Response(200, text=fixture_page()))
    publisher = FakePublisher([PublishResult(status=PublishStatus.REJECTED, error="bad")])
    writer, outbox = FakeWriter(), FakeOutbox(max_attempts=3)
    worker = make_worker(writer, outbox, publisher)
    await worker.process(make_task(last_post_id=None))
    assert outbox.statuses[100] == "pending"
    assert outbox.attempts[100] == 1
    assert writer.ops[0].kind == "ok"
    assert writer.ops[0].post_id is None


@respx.mock
async def test_rejected_final_marks_failed_and_shifts() -> None:
    respx.get("https://t.me/s/durov").mock(return_value=httpx.Response(200, text=fixture_page()))
    publisher = FakePublisher([PublishResult(status=PublishStatus.REJECTED, error="bad")])
    writer, outbox = FakeWriter(), FakeOutbox(max_attempts=3)
    outbox.statuses[100] = "pending"
    outbox.attempts[100] = 2  # уже 2 попытки, эта станет третьей
    worker = make_worker(writer, outbox, publisher)
    await worker.process(make_task(last_post_id=None))
    assert outbox.statuses[100] == "failed"
    assert writer.ops[0].kind == "ok"
    assert writer.ops[0].post_id == 100


@respx.mock
async def test_parser_error_becomes_layout_error_op() -> None:
    respx.get("https://t.me/s/durov").mock(return_value=httpx.Response(200, text="<html></html>"))
    publisher = FakePublisher([])
    writer, outbox = FakeWriter(), FakeOutbox()
    breaker = Breaker()
    worker = make_worker(writer, outbox, publisher, breaker=breaker)
    await worker.process(make_task(last_post_id=None))
    assert writer.ops[0].kind == "layout_error"
    assert publisher.calls == []
    assert breaker.is_open() is False


@respx.mock
async def test_publisher_exception_becomes_retry_op() -> None:
    respx.get("https://t.me/s/durov").mock(return_value=httpx.Response(200, text=fixture_page()))
    publisher = FakePublisher(RuntimeError("boom"))
    writer, outbox = FakeWriter(), FakeOutbox()
    worker = make_worker(writer, outbox, publisher)
    await worker.process(make_task(last_post_id=None))
    assert writer.ops[0].kind == "retry"
    assert outbox.statuses[100] == "pending"  # пост сохранён, доставка позже


@respx.mock
async def test_not_found_op_with_proxy() -> None:
    respx.get("https://t.me/s/durov").mock(
        return_value=httpx.Response(302, headers={"location": "/durov"})
    )
    publisher = FakePublisher([])
    writer, outbox = FakeWriter(), FakeOutbox()
    worker = make_worker(writer, outbox, publisher)
    await worker.process(make_task(last_post_id=None))
    op = writer.ops[0]
    assert op.kind == "not_found"
    assert op.proxy_id == "direct"


@respx.mock
async def test_worker_survives_repeated_processing() -> None:
    respx.get("https://t.me/s/durov").mock(return_value=httpx.Response(200, text=fixture_page()))
    publisher = FakePublisher(
        [
            PublishResult(status=PublishStatus.DELIVERED),
            PublishResult(status=PublishStatus.DELIVERED),
        ]
    )
    writer, outbox = FakeWriter(), FakeOutbox()
    worker = make_worker(writer, outbox, publisher)
    await worker.process(make_task(last_post_id=None))
    await worker.process(make_task(last_post_id=101))
    assert len(writer.ops) == 2
    assert writer.ops[1].post_id is None
