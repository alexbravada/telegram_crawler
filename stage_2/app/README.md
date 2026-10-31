# tg_crawler

> Русская версия: [README_RU.md](README_RU.md)

A service that watches public Telegram channels via `https://t.me/s/<channel>`,
takes all posts newer than `last_post_id`, and publishes them to an external API
(emulator, `POST /messages`).

## Getting started

```bash
cp .env.example .env   # adjust values if needed
docker compose up -d --build
```

Services started:

| Service | Purpose | Port |
|---|---|---|
| `postgres` | database | not exposed |
| `migrate` | `alembic upgrade head`, exits with code 0 | — |
| `emulator` | external API, logs received posts | 8001 |
| `api` | channel management | 8000 |
| `crawler` | the crawler | — |

Readiness check:

```bash
curl http://localhost:8000/health
# {"status":"ok","db":"ok"}
```

**Important:** `crawler` is designed for a single replica — do not increase the
replica count (`docker compose up --scale crawler=2` is not allowed): channel
claiming and outbox delivery are not designed for multiple processes.

## Architecture

Details: `docs/ARCHITECTURE_v2.md` (current), `docs/ARCHITECTURE.md` (v1).

```
  client                      PostgreSQL
    │  POST /watch_channels      ▲   ▲
    ▼                            │   │ claim / write results
┌──────────────┐  INSERT/UPDATE  │   │
│ api          │─────────────────┘   │
│ (FastAPI)    │                     │
└──────────────┘          ┌──────────┴────────────┐
                          │ crawler (1 replica)   │
                          │ Feeder → Workers →    │
                          │ Writer; Outbox →      │
                          │ Delivery; Publisher   │
                          └──────────┬────────────┘
                                     │ POST /messages
                                     ▼
                          ┌──────────────────────┐
                          │ emulator (external   │
                          │ service, logs posts) │
                          └──────────────────────┘
```

```mermaid
flowchart LR
    client["Client"] -->|"POST /watch_channels"| api["api<br/>(FastAPI)"]
    api <-->|"INSERT / UPDATE"| pg[("PostgreSQL<br/>channels, pending_posts")]
    pg <-->|"claim / write results"| crawler["crawler<br/>(1 replica)<br/>Feeder → Workers → Writer<br/>Outbox → Delivery → Publisher"]
    crawler -->|"POST /messages"| emulator["emulator<br/>(external service,<br/>logs posts)"]
```

The API and the crawler communicate only through PostgreSQL (the `channels`
table): channels are added and removed without restarts, the API scales
horizontally, the crawler does not.

### Post flow: fetch to delivery

```mermaid
flowchart TD
    feeder["Feeder: channel due for check"] --> w["Worker"]
    w -->|"fetch via proxy"| tme["t.me/s/&lt;channel&gt;"]
    tme -->|"HTML"| parse["Parser: posts with post_id &gt; last_post_id"]
    parse --> outbox[("pending_posts<br/>INSERT ... ON CONFLICT DO NOTHING")]
    outbox --> pub["Publisher: POST /messages"]
    pub -->|"2xx"| ok["status = delivered<br/>Writer advances last_post_id"]
    pub -->|"429 / 5xx / timeout"| retry["stays pending,<br/>exponential backoff (cap 30 s)"]
    retry --> pub
    pub -->|"4xx (except 429)"| tries["attempts +1"]
    tries -->|"attempts &gt;= outbox_max_attempts"| fail["status = failed<br/>(content preserved)"]
    tries -->|"attempts &lt; max"| retry
```

Duplicates are possible (at-least-once): publishing happens before the result is
recorded; the receiver deduplicates by `(channel_id, original_message_id)`.

### Crawler pipeline (1 replica)

| Component | Responsibility |
|---|---|
| `CrawlerService` | Starts the Feeder, workers, Writer, Outbox Delivery; shutdown ordering |
| `Feeder` | Claims channels whose `next_check_at` is due (`FOR UPDATE SKIP LOCKED`), with backpressure |
| `Worker` × N | Proxy from `ProxyPool` → `Fetcher` (request to `t.me/s/<channel>`) → `Parser` (all posts with `post_id > last_post_id`) → outbox → result to `Writer` |
| `ProxyPool` | Request pacing, cooldown after 429, health checks, AIMD |
| `CircuitBreaker` | Protection against mass false `NOT_FOUND` / `LayoutChanged` |
| `scheduling` | `next_check_at` based on channel activity and backoff |
| `Writer` | Batched result writes with `lease_token` validation |
| `Outbox Delivery` | Separate loop: publishes `pending` posts from `pending_posts` via the same `Publisher` |

Fetch retries are not done in a loop inside the worker: a failed task is returned
to the DB with a new `next_check_at`, and the next attempt will most likely go
through a different proxy.

### Post delivery: outbox (at-least-once)

SLA: post loss ≤ 1/1000. A post is saved to `pending_posts` **before** being
published, so receiver unavailability, a process crash, or failed retries never
lose content.

- Worker: fetch → parse → `INSERT ... ON CONFLICT DO NOTHING` into `pending_posts` → publish.
- `2xx` → `delivered`; the Writer advances the channel's `last_post_id`.
- `429`/`5xx`/timeout → the post stays `pending`; retries from the outbox with
  exponential backoff (capped at 30 s), no new requests to `t.me`.
- `4xx` (except `429`) → after `outbox_max_attempts` consecutive failures the
  status becomes `failed` (content preserved, `last_post_id` advanced).
- Publishing happens before the result is recorded → duplicates are possible
  (at-least-once); the receiver deduplicates by `(channel_id, original_message_id)`.
- On a channel's first fetch, only its latest post is delivered (`last_post_id` is empty).

## Channel management

Add channels (batch of 1 to 1000). `channel_id` (`-100…`) is optional:

```bash
curl -X POST http://localhost:8000/watch_channels \
  -H 'Content-Type: application/json' \
  -d '[
    {"channel_url": "https://t.me/durov", "channel_id": -1001234567890},
    {"channel_url": "t.me/telegram"}
  ]'
```

List channels (`active` = `true`/`false`/`all`, `limit` ≤ 1000, `offset`):

```bash
curl 'http://localhost:8000/watch_channels?active=all'
```

Delete a channel (soft, `is_active = false`, one at a time):

```bash
curl -X DELETE http://localhost:8000/watch_channels/durov
```

The crawler picks up changes on the next Feeder cycle (5 s by default);
no restart needed.

## Emulator logs

Each delivered post is one JSON line on the emulator's stdout:

```bash
docker compose logs -f emulator
```

## Configuration

Main variables (see `.env.example` and `docs/ARCHITECTURE_v2.md` section 12):

- `TG_CRAWLER_DATABASE_URL` — PostgreSQL connection string;
- `TG_CRAWLER_PUBLISHER_URL` — external API URL (`http://emulator:8001/messages`);
- `TG_CRAWLER_PROXIES` — comma-separated proxy list (0–1 for testing, 5+ for production).

## Documentation

- `docs/PLAN.md` — task plan;
- `docs/ARCHITECTURE_v2.md` — current architecture (takes priority over v1);
- `docs/ARCHITECTURE.md` — v1: parser, ProxyPool, Feeder/Writer.

## Tuning a single proxy's throughput

The per-proxy request pacing is set by `TG_CRAWLER_PROXY_MIN_INTERVAL`
(`tg_crawler/config.py:17`, default `4.0` seconds). One proxy yields
`1 / interval` requests per second: `4` → 0.25 req/s, `2` → 0.5 req/s.

When lowering it:

- a lower interval raises the risk of 429s from `t.me`; on a 429 the ProxyPool
  puts the proxy into cooldown itself, which reduces effective throughput;
- raise the worker count as well (`TG_CRAWLER_WORKERS`), otherwise proxies idle:
  a rule of thumb is workers ≈ (1 / interval) × average request latency in seconds;
- the practical range is best measured on the T18 stand via the share of 429s
  in the crawler's `stats` logs.

## Deployment guides

### Prerequisites

- A test server with Docker (24+) and the compose v2 plugin.
- **Access to `t.me`**: if the server's network blocks Telegram, you need a working
  proxy (0–1 addresses for testing, 5+ for production load). Without one the crawler
  will retry forever (`outcome: retry` in the logs) and no posts will be delivered.
- Repository code on the server (git clone or copy); the `.env` file is not committed —
  create it on the server from `.env.example`.

### Stand 1: basic run (liveness check)

```bash
cp .env.example .env
docker compose up -d --build
curl http://localhost:8000/health   # {"status":"ok","db":"ok"}
```

One-script smoke test (brings up the stack, adds a channel from
`scripts/smoke_channels.txt`, waits for posts to be delivered to the emulator,
verifies soft channel deletion, tears the stack down):

```bash
./scripts/smoke.sh            # after the check: docker compose down -v
./scripts/smoke.sh --keep     # keep the postgres volume
```

Success criterion: `SMOKE OK` and exit code 0. The smoke-test channel list is edited
in `scripts/smoke_channels.txt` (format: `url|name|channel_id`; `channel_id` may be
left empty).

### Stand 2: load check (T18)

1. Proxies (if needed) — in `.env`:

   ```bash
   TG_CRAWLER_PROXIES=scheme://user:pass@host1:port,scheme://user:pass@host2:port
   ```

2. Stack:

   ```bash
   docker compose up -d --build
   ```

3. Channels: 50–100 public channels in a single POST `/watch_channels` (batch limit
   is 1000). Without proxies initially, set `TG_CRAWLER_PROXY_MIN_INTERVAL=5` (sec)
   — the interval between requests to one channel — to avoid 429s.

4. Observation:

   ```bash
   docker compose logs -f crawler      # JSON logs: op/stats, fetch/publish outcomes
   docker compose logs -f emulator     # delivered posts
   curl 'http://localhost:8000/watch_channels?active=all&limit=1000'   # last_post_id counters
   ```

5. Checklist:
   - posts in the emulator match the channels (`title`, `channel_id` = `channel_name`);
   - share of 429s in crawler logs — if it grows, raise `TG_CRAWLER_PROXY_MIN_INTERVAL`
     or add proxies;
   - `GET /watch_channels?active=all` — `last_post_id` is advancing, no delivery errors;
   - soft deletion: `DELETE /watch_channels/<name>` → in the `active=all` list the
     channel shows `is_active=false`.

6. Shutdown/reset:

   ```bash
   docker compose down          # keep data
   docker compose down -v       # wipe the database
   ```

### Stand 3: production (briefly)

- `.env` with the production DB, `TG_CRAWLER_PUBLISHER_URL` pointing to the real API,
  5+ proxies;
- proxy passwords are set in the URL and masked in logs; secrets are never committed;
- `crawler` — strictly one replica; `api`/`emulator` can be scaled.

### Production resource estimates (emulator excluded)

Estimates derived from the code's settings: `proxy_min_interval=4` (one proxy —
0.25 req/s), channel check intervals (`scheduling.py`): active (<24 h since last
post) — every 15–30 min (22.5 on average), recent (up to 7 days) — every ~2 h,
dormant — every 6–12 h. Estimates assume the pessimistic case where all channels
are active.

| Metric | 100 channels | 1,000 channels | 10,000 channels |
|---|---|---|---|
| Requests to t.me | ~4.4/min (~6.4k/day) | ~44/min (~64k/day) | ~440/min (~640k/day) |
| Proxies (at 4 s/request) | 1–5 | 5 | 30–40 |
| Workers (default 2× proxies) | 2–4 | 8–10 | 60–80 (`TG_CRAWLER_WORKERS`) |
| `crawler`: CPU / RAM | 0.1 vCPU / 128 MB | 0.25 vCPU / 256 MB | 0.5–1 vCPU / 512 MB |
| `api`: CPU / RAM | 0.05 vCPU / 128 MB | 0.1 vCPU / 128 MB | 0.1 vCPU / 256 MB |
| `postgres`: CPU / RAM | 0.25 vCPU / 256 MB | 0.25 vCPU / 512 MB | 0.5–1 vCPU / 1 GB |
| Postgres disk (DB) | < 1 GB | ~2–5 GB/year | ~30–80 GB/year |

Notes:

- **The bottleneck is proxies, not CPU.** The crawler is fully I/O-bound (asyncio);
  parsing a page takes tens of ms of CPU, so even 10,000 channels (~7.4 req/s) fit
  in a single core. What grows first is the number of proxies and workers.
- **Proxies.** Minimum by pacing: 100 channels — 1, 1,000 — 3–5, 10,000 — ~30
  (dormant channels generate almost no traffic; estimates assume all channels active).
  Proxies must serve `t.me` without blocks (Telegram often blocks datacenter IPs —
  see T18).
- **Postgres disk** is driven by the `pending_posts` table: delivered posts stay in
  the DB (JSONB `payload` ~0.5–2 KB). With 10,000 active channels that is roughly
  70k posts/day ≈ 70–150 MB/day. The "/year" figures are adjusted for the share of
  genuinely active channels; set up rotation of `delivered` rows if needed.
- **Network/connections.** 3 processes with small DB connection pools; outbound
  traffic is the `t.me/s/` pages themselves (~50–200 KB per fetch) plus POST /messages.
- These figures are planning estimates; take actual measurements on the T18 stand
  (crawler `stats` logs every `TG_CRAWLER_STATS_LOG_INTERVAL=60` s).
