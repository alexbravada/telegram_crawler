"""Entrypoint: python -m tg_crawler — читает Settings, запускает CrawlerService.

SIGTERM/SIGINT → корректная остановка (v1 раздел 10.3), exit code 0.
"""

import asyncio
import logging
import signal
import sys

import httpx
from sqlalchemy.ext.asyncio import create_async_engine

from tg_crawler.config import Settings
from tg_crawler.db import create_sessionmaker
from tg_crawler.publisher import HttpPublisher
from tg_crawler.service import CrawlerService, JsonFormatter

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])


async def amain() -> int:
    settings = Settings()  # type: ignore[call-arg]  # значения из окружения/.env
    setup_logging()
    engine = create_async_engine(settings.database_url)
    session_factory = create_sessionmaker(engine)
    client = httpx.AsyncClient(timeout=settings.publisher_timeout)
    publisher = HttpPublisher(
        settings.publisher_url,
        settings.publisher_timeout,
        settings.publisher_max_attempts,
        client,
    )
    service = CrawlerService(settings, session_factory, publisher)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, service.request_stop)
    try:
        await service.run()
    finally:
        await client.aclose()
        await engine.dispose()
    logger.info("service stopped")
    return 0


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    raise SystemExit(main())
