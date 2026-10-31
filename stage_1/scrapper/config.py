"""Loads configuration from default.app.env with environment variable overrides."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

ENV_FILE = Path(__file__).resolve().parent.parent / "default.app.env"


def parse_env_file(path: Path) -> dict:
    """Reads KEY=VALUE lines, skipping blank lines and # comments."""
    values = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


@dataclass
class Config:
    max_workers: int = 5
    proxies: List[str] = field(default_factory=list)
    timeout: float = 15.0
    min_timeout: float = 1.0
    max_timeout: float = 3.0
    retries: int = 3
    retry_backoff: float = 1.0


def load_config(path: Path = ENV_FILE) -> Config:
    raw = parse_env_file(path)
    # environment variables take priority over the file
    raw.update({key: value for key, value in os.environ.items() if key in raw})
    proxies = json.loads(raw.get("PROXIES", "[]"))
    return Config(
        max_workers=int(raw.get("MAX_WORKERS", "5")),
        proxies=[str(p) for p in proxies],
        timeout=float(raw.get("TIMEOUT", "15")),
        min_timeout=float(raw.get("MIN_TIMEOUT", "1")),
        max_timeout=float(raw.get("MAX_TIMEOUT", "3")),
        retries=int(raw.get("RETRIES", "3")),
        retry_backoff=float(raw.get("RETRY_BACKOFF", "1.0")),
    )
