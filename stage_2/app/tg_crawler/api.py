"""FastAPI: /watch_channels (POST/GET/DELETE), /health (v2 раздел 3)."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tg_crawler.config import Settings
from tg_crawler.db import create_sessionmaker
from tg_crawler.names import normalize, resolve_watch_name, validate_channel_id
from tg_crawler.repository import Repository, WatchItem
from tg_crawler.schemas import (
    ChannelOut,
    WatchAcceptedOut,
    WatchItemIn,
    WatchRejectedOut,
    WatchResponseOut,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = create_async_engine(app.state.settings.database_url, pool_pre_ping=True)
    app.state.session_factory = create_sessionmaker(engine)
    app.state.engine = engine
    yield
    await engine.dispose()


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="tg_crawler", lifespan=lifespan)
    app.state.settings = settings
    app.include_router(router)
    return app


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_repository(request: Request) -> Repository:
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return Repository(session_factory)


@router.post("/watch_channels", response_model=WatchResponseOut)
async def post_watch_channels(
    items: list[WatchItemIn],
    request: Request,
    repo: Repository = Depends(get_repository),
) -> WatchResponseOut:
    settings = get_settings(request)
    if not items or len(items) > settings.watch_max_batch:
        raise HTTPException(status_code=422, detail="items length out of range")

    resolved: list[tuple[WatchItem, str | None]] = []
    rejected: list[WatchRejectedOut] = []
    winners: dict[str, WatchItem] = {}
    for item in items:
        resolution = resolve_watch_name(item.channel_name, item.channel_url)
        if resolution.name is None or resolution.reject_reason is not None:
            reason = resolution.reject_reason or "invalid_channel_url"
            rejected.append(
                WatchRejectedOut(
                    channel_url=item.channel_url,
                    channel_name=item.channel_name,
                    reason=reason,
                )
            )
            continue
        if not validate_channel_id(item.channel_id):
            rejected.append(
                WatchRejectedOut(
                    channel_url=item.channel_url,
                    channel_name=item.channel_name,
                    reason="invalid_channel_id",
                )
            )
            continue
        watch_item = WatchItem(
            channel_name=resolution.name,
            channel_url=item.channel_url,
            channel_id=item.channel_id,
        )
        resolved.append((watch_item, resolution.warning))
        winners[resolution.name] = watch_item

    # дубли: значения берутся из последнего элемента, у остальных warning
    seen_after: set[str] = set()
    marked: list[tuple[WatchItem, str | None]] = []
    for watch_item, warning in reversed(resolved):
        if watch_item.channel_name in seen_after:
            warning = "duplicate_in_request"
        else:
            seen_after.add(watch_item.channel_name)
        marked.append((watch_item, warning))
    marked.reverse()

    upsert_results = await repo.upsert_watch(list(winners.values()))
    result_map = {result.channel_name: result for result in upsert_results}
    accepted: list[WatchAcceptedOut] = []
    for watch_item, warning in marked:
        result = result_map[watch_item.channel_name]
        if result.reject_reason is not None:
            rejected.append(
                WatchRejectedOut(
                    channel_url=watch_item.channel_url,
                    channel_name=watch_item.channel_name,
                    reason=result.reject_reason,
                )
            )
            continue
        accepted.append(
            WatchAcceptedOut(
                channel_name=watch_item.channel_name,
                channel_id=watch_item.channel_id,
                created=result.created,
                warning=warning,
            )
        )
    return WatchResponseOut(accepted=accepted, rejected=rejected)


@router.get("/watch_channels", response_model=list[ChannelOut])
async def get_watch_channels(
    repo: Repository = Depends(get_repository),
    active: Literal["true", "false", "all"] = "true",
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ChannelOut]:
    infos = await repo.list_channels(active, limit, offset)
    return [ChannelOut.model_validate(info) for info in infos]


@router.delete("/watch_channels/{channel_name}")
async def delete_watch_channel(
    channel_name: str, repo: Repository = Depends(get_repository)
) -> JSONResponse:
    name = normalize(channel_name)
    if name is None or not await repo.deactivate(name):
        raise HTTPException(status_code=404)
    return JSONResponse({"channel_name": name, "is_active": False})


@router.get("/health")
async def health(repo: Repository = Depends(get_repository)) -> JSONResponse:
    if await repo.ping():
        return JSONResponse({"status": "ok", "db": "ok"})
    return JSONResponse({"status": "error", "db": "unavailable"}, status_code=503)
