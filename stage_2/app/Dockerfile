FROM python:3.11-slim

RUN useradd --create-home appuser
WORKDIR /app

COPY pyproject.toml alembic.ini ./
COPY tg_crawler ./tg_crawler
COPY alembic ./alembic
RUN pip install --no-cache-dir .

USER appuser
