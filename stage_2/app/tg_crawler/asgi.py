"""ASGI-entrypoint для uvicorn: app = create_app(Settings()) из окружения."""

from tg_crawler.api import create_app
from tg_crawler.config import Settings

app = create_app(Settings())  # type: ignore[call-arg]
