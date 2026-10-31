"""Эмулятор внешнего API: принимает POST /messages, пишет в stdout, отвечает 201/422."""

import json
import os
import random
import time

from fastapi import FastAPI, Response
from pydantic import BaseModel, ConfigDict, Field

app = FastAPI()


class MediaIn(BaseModel):
    id: str = Field(min_length=1)
    caption: str | None = None


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(min_length=1)
    original_message_id: str = Field(min_length=1)
    body: str = ""
    medias: list[MediaIn] = []


def _fail_rate() -> float:
    raw = os.getenv("EMULATOR_FAIL_RATE", "0")
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        return 0.0


def _log(message: MessageIn) -> None:
    print(
        json.dumps(
            {"ts": time.time(), "path": "/messages", "body": message.model_dump()},
            ensure_ascii=False,
        ),
        flush=True,
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/messages", status_code=201)
async def messages(message: MessageIn) -> Response:
    _log(message)
    if random.random() < _fail_rate():
        return Response(status_code=500)
    return {"status": "accepted"}
