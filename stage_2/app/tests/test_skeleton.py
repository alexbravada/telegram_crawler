"""Smoke-тест скелета: все модули пакета импортируются."""

import importlib

import pytest

MODULES = [
    "tg_crawler.api",
    "tg_crawler.breaker",
    "tg_crawler.config",
    "tg_crawler.db",
    "tg_crawler.feeder",
    "tg_crawler.fetcher",
    "tg_crawler.models",
    "tg_crawler.names",
    "tg_crawler.outbox",
    "tg_crawler.parser",
    "tg_crawler.protocols",
    "tg_crawler.proxy_pool",
    "tg_crawler.publisher",
    "tg_crawler.repository",
    "tg_crawler.scheduling",
    "tg_crawler.schemas",
    "tg_crawler.service",
    "tg_crawler.worker",
    "tg_crawler.writer",
]


@pytest.mark.parametrize("module", MODULES)
def test_module_imports(module: str) -> None:
    assert importlib.import_module(module) is not None
