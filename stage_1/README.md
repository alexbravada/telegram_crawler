# post_crawler

CLI utility: takes one or more public Telegram channel links (`https://t.me/<username>`)
and prints their latest posts — text, image and video links.

It works via the `t.me/s/<username>` web preview, so it **does not require api_id/api_hash**
and only supports public channels.

## Requirements

- Python 3.9+
- [httpx](https://www.python-httpx.org/) and [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/bs4/doc/)
  (installed automatically on first run)

## Run with a single command

```sh
./run.sh https://t.me/durov
```

Several posts (newest last):

```sh
./run.sh https://t.me/durov -n 5
```

Multiple channels in parallel:

```sh
./run.sh https://t.me/durov https://t.me/telegram -n 5
```

Without `run.sh` — directly:

```sh
pip install -r requirements.txt
python -m scrapper https://t.me/durov https://t.me/telegram -n 5
```

A plain username works too: `./run.sh durov -n 5`.

## Output format

```
### durov
=== Post 1 ===
[https://t.me/durov/351]
Hi! This is the latest post.
Images:
  https://cdn4.telesco.pe/file/....jpg
Videos:
  https://cdn4.telesco.pe/file/....mp4
```

## Configuration

Settings are read from `default.app.env`; environment variables take priority:

| Variable       | Default | Description                                        |
|----------------|---------|----------------------------------------------------|
| `MAX_WORKERS`  | `5`     | max parallel requests (asyncio.Semaphore)          |
| `PROXIES`      | `[]`    | JSON list of proxies, the first one is used        |
| `TIMEOUT`      | `15`    | HTTP request timeout, seconds                      |
| `MIN_TIMEOUT`  | `1`     | min pause between worker requests, seconds         |
| `MAX_TIMEOUT`  | `3`     | max pause between worker requests, seconds         |
| `RETRIES`      | `3`     | retry attempts for transient failures              |
| `RETRY_BACKOFF`| `1.0`   | base delay for exponential backoff, seconds        |

## Error handling

Errors are split into two classes (`scrapper/errors.py`):

- `ChannelNotFoundError` — fatal (channel does not exist, is private, or HTTP 4xx
  other than 429). **Not retried.**
- `ChannelFetchError` — transient (network failures, HTTP 429/5xx). Retried with
  exponential backoff: `RETRY_BACKOFF * 2^(attempt-1)`.

Failed channels are reported via `logging` to stderr and skipped; the run only
exits with code 1 if no channel yielded any posts. Use `-v` to see debug logs
(including retry attempts).

All user-facing strings live in `scrapper/messages.py` — a single place to
change or localize output text.

## Structure

```
scrapper/
├── cli.py               # argument parsing, parallel channel crawling, output
├── config.py            # loading default.app.env (+ environment overrides)
├── errors.py            # error hierarchy: fatal (ChannelNotFoundError) / retryable (ChannelFetchError)
├── messages.py          # single source of all user-facing strings
├── models.py            # Post dataclass: text, images, videos, url
├── __main__.py          # entry point: python -m scrapper
└── providers/
    ├── base.py          # ChannelProvider interface (Protocol)
    └── web.py           # WebProvider (httpx + BeautifulSoup parser of t.me/s/ HTML, retry with backoff)
```

## Switching to Telethon

Scraping is isolated behind the `ChannelProvider` interface (`scrapper/providers/base.py`).
The interface is async — designed for a service with workers under `asyncio.Semaphore`.
To switch to the MTProto API, write a class with the same method
`async def fetch_posts(channel: str, limit: int) -> list[Post]` and pass it to `scrapper.cli.main(...)`.

## Using in a service (multiple channels)

```python
import asyncio
import httpx

from scrapper.providers.web import WebProvider, ChannelNotFoundError

async def crawl(channels: list, concurrency: int = 5):
    sem = asyncio.Semaphore(concurrency)
    # shared AsyncClient — reuses the connection pool across workers
    async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
        provider = WebProvider(client)

        async def worker(channel: str):
            async with sem:
                try:
                    return await provider.fetch_posts(channel, limit=10)
                except ChannelNotFoundError:
                    return None

        results = await asyncio.gather(*(worker(c) for c in channels))
        return [posts for posts in results if posts is not None]
```
