# TODO

## Quality

- [ ] Add pytest test suite (parser fixtures, config, CLI, retry logic)
- [ ] Add `pyproject.toml` with pinned dependencies
- [ ] Add linting/typecheck tooling (ruff, mypy) + pre-commit hooks
- [ ] Use strict typing (`crawl` return type, bs4 attribute casts)

## Reliability

- [ ] Adaptive rate: slow down all workers on HTTP 429 (`Retry-After`)
- [ ] Distinguish retryable/fatal errors in service consumers (already done in provider)

## Features

- [ ] Rate limiter: token bucket (`aiolimiter` or custom) + `REQUESTS_PER_SECOND` in `default.app.env`
- [ ] Move pause out of semaphore slot (semaphore = concurrency, limiter = rate)
- [ ] Per-host limits for multiple sources
- [ ] Telethon provider implementing `ChannelProvider`
- [ ] Handle `parse_env_file` edge cases: quotes, `export` prefix, inline comments
