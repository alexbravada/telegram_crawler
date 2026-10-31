# tg_crawler

> English version: [README.md](README.md)

Сервис, который следит за публичными Telegram-каналами через `https://t.me/s/<channel>`,
берёт все посты новее `last_post_id` и публикует их во внешний API (эмулятор,
`POST /messages`).

## Запуск

```bash
cp .env.example .env   # при необходимости поправить значения
docker compose up -d --build
```

Поднимаются сервисы:

| Сервис | Назначение | Порт |
|---|---|---|
| `postgres` | БД | наружу не публикуется |
| `migrate` | `alembic upgrade head`, завершается с кодом 0 | — |
| `emulator` | внешний API, пишет полученные посты в лог | 8001 |
| `api` | управление каналами | 8000 |
| `crawler` | краулер | — |

Проверка готовности:

```bash
curl http://localhost:8000/health
# {"status":"ok","db":"ok"}
```

**Важно:** `crawler` рассчитан на одну реплику — не увеличивайте число реплик
(`docker compose up --scale crawler=2` недопустимо): захват каналов и доставка outbox
не рассчитаны на несколько процессов.

## Архитектура

Подробности: `docs/ARCHITECTURE_v2.md` (актуальная), `docs/ARCHITECTURE.md` (v1).

```
  клиент                      PostgreSQL
    │  POST /watch_channels      ▲   ▲
    ▼                            │   │ claim / запись результатов
┌──────────────┐  INSERT/UPDATE  │   │
│ api          │─────────────────┘   │
│ (FastAPI)    │                     │
└──────────────┘          ┌──────────┴────────────┐
                          │ crawler (1 реплика)   │
                          │ Feeder → Workers →    │
                          │ Writer; Outbox →      │
                          │ Delivery; Publisher   │
                          └──────────┬────────────┘
                                     │ POST /messages
                                     ▼
                          ┌──────────────────────┐
                          │ emulator (внешний    │
                          │ сервис, пишет в лог) │
                          └──────────────────────┘
```

```mermaid
flowchart LR
    client["Клиент"] -->|"POST /watch_channels"| api["api<br/>(FastAPI)"]
    api <-->|"INSERT / UPDATE"| pg[("PostgreSQL<br/>channels, pending_posts")]
    pg <-->|"claim / запись результатов"| crawler["crawler<br/>(1 реплика)<br/>Feeder → Workers → Writer<br/>Outbox → Delivery → Publisher"]
    crawler -->|"POST /messages"| emulator["emulator<br/>(внешний сервис,<br/>пишет в лог)"]
```

API и краулер общаются только через PostgreSQL (таблица `channels`): каналы
добавляются и удаляются без перезапуска, API масштабируется, краулер — нет.

### Поток поста: от fetch до доставки

```mermaid
flowchart TD
    feeder["Feeder: канал пора проверить"] --> w["Worker"]
    w -->|"fetch через прокси"| tme["t.me/s/&lt;channel&gt;"]
    tme -->|"HTML"| parse["Parser: посты с post_id &gt; last_post_id"]
    parse --> outbox[("pending_posts<br/>INSERT ... ON CONFLICT DO NOTHING")]
    outbox --> pub["Publisher: POST /messages"]
    pub -->|"2xx"| ok["status = delivered<br/>Writer сдвигает last_post_id"]
    pub -->|"429 / 5xx / таймаут"| retry["остаётся pending,<br/>экспоненциальный backoff (потолок 30 с)"]
    retry --> pub
    pub -->|"4xx (кроме 429)"| tries["attempts +1"]
    tries -->|"attempts &gt;= outbox_max_attempts"| fail["status = failed<br/>(контент сохранён)"]
    tries -->|"attempts &lt; max"| retry
```

Дубли возможны (at-least-once): публикация выполняется до записи результата;
получатель дедуплицирует по паре `(channel_id, original_message_id)`.

### Конвейер краулера (1 реплика)

| Компонент | Ответственность |
|---|---|
| `CrawlerService` | Запуск Feeder, воркеров, Writer, Outbox Delivery; порядок остановки |
| `Feeder` | Захватывает каналы, у которых наступило `next_check_at` (`FOR UPDATE SKIP LOCKED`), с backpressure |
| `Worker` × N | Прокси из `ProxyPool` → `Fetcher` (запрос к `t.me/s/<channel>`) → `Parser` (все посты с `post_id > last_post_id`) → outbox → результат в `Writer` |
| `ProxyPool` | Темп запросов, cooldown после 429, health-check, AIMD |
| `CircuitBreaker` | Защита от массовых ложных `NOT_FOUND` / `LayoutChanged` |
| `scheduling` | `next_check_at` по активности канала и backoff |
| `Writer` | Батчевая запись результатов с проверкой `lease_token` |
| `Outbox Delivery` | Отдельный цикл: публикует `pending`-посты из `pending_posts` тем же `Publisher` |

Ретраи fetch не делаются циклом внутри воркера: неудачная задача возвращается в БД
с новым `next_check_at`, следующая попытка пойдёт через другой прокси.

### Доставка постов: outbox (at-least-once)

SLA: потери постов ≤ 1/1000. Пост сохраняется в `pending_posts` **до** публикации,
поэтому недоступность получателя, падение процесса и неудачные ретраи не теряют контент.

- Воркер: fetch → parse → `INSERT ... ON CONFLICT DO NOTHING` в `pending_posts` → публикация.
- `2xx` → `delivered`, Writer сдвигает `last_post_id` канала.
- `429`/`5xx`/таймаут → пост остаётся `pending`, ретраи из outbox с экспоненциальным
  backoff (потолок 30 с), без новых запросов к `t.me`.
- `4xx` (кроме `429`) → после `outbox_max_attempts` подряд статус `failed`
  (контент сохранён, `last_post_id` сдвигается).
- Публикация до записи результата → возможен повтор (at-least-once);
  дедуп на получателе по паре `(channel_id, original_message_id)`.
- При первом fetch канала уходит только последний пост (`last_post_id` пуст).

## Управление каналами

Добавить каналы (пакет от 1 до 1000). `channel_id` (`-100…`) необязателен:

```bash
curl -X POST http://localhost:8000/watch_channels \
  -H 'Content-Type: application/json' \
  -d '[
    {"channel_url": "https://t.me/durov", "channel_id": -1001234567890},
    {"channel_url": "t.me/telegram"}
  ]'
```

Список каналов (`active` = `true`/`false`/`all`, `limit` ≤ 1000, `offset`):

```bash
curl 'http://localhost:8000/watch_channels?active=all'
```

Удалить канал (мягко, `is_active = false`, по одному):

```bash
curl -X DELETE http://localhost:8000/watch_channels/durov
```

Изменения подхватываются краулером на следующем цикле Feeder (по умолчанию 5 с),
перезапускать его не нужно.

## Логи эмулятора

Каждый доставленный пост — одна JSON-строка в stdout эмулятора:

```bash
docker compose logs -f emulator
```

## Конфигурация

Основные переменные (см. `.env.example` и `docs/ARCHITECTURE_v2.md` раздел 12):

- `TG_CRAWLER_DATABASE_URL` — строка подключения к PostgreSQL;
- `TG_CRAWLER_PUBLISHER_URL` — URL внешнего API (`http://emulator:8001/messages`);
- `TG_CRAWLER_PROXIES` — список прокси через запятую (0–1 для теста, от 5 для прода).

## Документация

- `docs/PLAN.md` — план задач;
- `docs/ARCHITECTURE_v2.md` — актуальная архитектура (приоритет над v1);
- `docs/ARCHITECTURE.md` — v1: парсер, ProxyPool, Feeder/Writer.

## Настройка темпа одного прокси

Темп запросов через один прокси задаёт `TG_CRAWLER_PROXY_MIN_INTERVAL`
(`tg_crawler/config.py:17`, дефолт `4.0` с). Один прокси даёт `1 / интервал`
запросов в секунду: `4` → 0.25 req/s, `2` → 0.5 req/s.

При снижении интервала:

- ниже интервал — выше риск 429 от `t.me`; при 429 ProxyPool сам ставит прокси
  в cooldown, что снижает полезную пропускную способность;
- поднимите и число воркеров (`TG_CRAWLER_WORKERS`), иначе прокси будут
  простаивать: ориентир — воркеры ≈ (1 / интервал) × средняя задержка запроса
  в секундах;
- рабочий диапазон лучше снять на стенде T18 по доле 429 в `stats`-логах краулера.

## Развёртывание стендов

### Предпосылки

- Тестовый сервер с Docker (24+) и compose-плагином v2.
- **Доступ к `t.me`**: если сеть сервера блокирует Telegram, нужен рабочий прокси
  (для теста — 0–1 адрес, для прод-нагрузки — от 5). Без него краулер будет
  бесконечно ретраить (`outcome: retry` в логах), посты доставляться не будут.
- Код репозитория на сервере (git clone или copy); файл `.env` не коммитится —
  создаётся на сервере из `.env.example`.

### Стенд 1: базовый запуск (проверка живости)

```bash
cp .env.example .env
docker compose up -d --build
curl http://localhost:8000/health   # {"status":"ok","db":"ok"}
```

Смоук-тест одним скриптом (поднимает стек, добавляет канал из
`scripts/smoke_channels.txt`, ждёт доставки постов в эмулятор, проверяет мягкое
удаление канала, гасит стек):

```bash
./scripts/smoke.sh            # после проверки: docker compose down -v
./scripts/smoke.sh --keep     # оставить volume postgres
```

Критерий успеха: `SMOKE OK` и exit code 0. Список каналов для смоука правится в
`scripts/smoke_channels.txt` (формат: `url|name|channel_id`, `channel_id` можно
оставить пустым).

### Стенд 2: нагрузочная проверка (T18)

1. Прокси (если нужны) — в `.env`:

   ```bash
   TG_CRAWLER_PROXIES=scheme://user:pass@host1:port,scheme://user:pass@host2:port
   ```

2. Стек:

   ```bash
   docker compose up -d --build
   ```

3. Каналы: 50–100 публичных каналов одним POST `/watch_channels` (лимит пакета —
   1000). Без прокси на старте — интервал между запросами к одному каналу
   `TG_CRAWLER_PROXY_MIN_INTERVAL=5` (сек), чтобы не ловить 429.

4. Наблюдение:

   ```bash
   docker compose logs -f crawler      # JSON-логи: op/stats, исходы fetch/publish
   docker compose logs -f emulator     # доставленные посты
   curl 'http://localhost:8000/watch_channels?active=all&limit=1000'   # счётчики last_post_id
   ```

5. Чек-лист:
   - посты в эмуляторе соответствуют каналам (`title`, `channel_id` = `channel_name`);
   - доля 429 в логах краулера — при росте поднять `TG_CRAWLER_PROXY_MIN_INTERVAL`
     или добавить прокси;
   - `GET /watch_channels?active=all` — `last_post_id` растёт, ошибок доставки нет;
   - мягкое удаление: `DELETE /watch_channels/<name>` → в списке `active=all`
     канал с `is_active=false`.

6. Остановка/сброс:

   ```bash
   docker compose down          # оставить данные
   docker compose down -v       # снести БД
   ```

### Стенд 3: прод (кратко)

- `.env` с прод-БД, `TG_CRAWLER_PUBLISHER_URL` на реальный API, от 5 прокси;
- пароли прокси задаются в URL и маскируются в логах, секреты не коммитятся;
- `crawler` — строго одна реплика; `api`/`emulator` можно масштабировать.

### Ресурсы для прода (без эмулятора)

Оценки из настроек кода: `proxy_min_interval=4` (один прокси — 0.25 req/s),
интервалы проверки канала (`scheduling.py`): активный (<24 ч с последнего поста) —
раз в 15–30 мин (в среднем 22.5), недавний (до 7 дней) — раз в ~2 ч, спящий —
раз в 6–12 ч. Оценка дана для пессимистичного случая «все каналы активны».

| Показатель | 100 каналов | 1 000 каналов | 10 000 каналов |
|---|---|---|---|
| Запросы к t.me | ~4.4/мин (~6.4 тыс/сут) | ~44/мин (~64 тыс/сут) | ~440/мин (~640 тыс/сут) |
| Прокси (при 4 c/запрос) | 1–5 | 5 | 30–40 |
| Воркеры (дефолт 2×прокси) | 2–4 | 8–10 | 60–80 (`TG_CRAWLER_WORKERS`) |
| `crawler`: CPU / RAM | 0.1 vCPU / 128 МБ | 0.25 vCPU / 256 МБ | 0.5–1 vCPU / 512 МБ |
| `api`: CPU / RAM | 0.05 vCPU / 128 МБ | 0.1 vCPU / 128 МБ | 0.1 vCPU / 256 МБ |
| `postgres`: CPU / RAM | 0.25 vCPU / 256 МБ | 0.25 vCPU / 512 МБ | 0.5–1 vCPU / 1 ГБ |
| Диск postgres (БД) | < 1 ГБ | ~2–5 ГБ/год | ~30–80 ГБ/год |

Пояснения:

- **Узкое место — прокси, не CPU.** Краулер полностью I/O-bound (asyncio),
  парсинг страницы ~десятки мс CPU; даже на 10 000 каналов (~7.4 req/s) хватает
  одного ядра. Растёт в первую очередь число прокси и воркеров.
- **Прокси.** Минимум по темпу: 100 каналов — 1 шт., 1 000 — 3–5, 10 000 — ~30
  (спящие каналы почти не создают трафика; оценки для полностью активных).
  Прокси должны отдавать `t.me` без блокировок (датацентровые IP Telegram часто
  режет — см. T18).
- **Диск postgres** определяется таблицей `pending_posts`: доставленные посты
  остаются в БД (JSONB `payload` ~0.5–2 КБ). При 10 000 активных каналов это
  порядка 70 тыс. постов/сут ≈ 70–150 МБ/сут. Оценки «/год» — с поправкой на
  долю реально активных каналов; при необходимости настройте ротацию
  `delivered`-строк.
- **Сеть/соединения.** 3 процесса, пулы соединений с БД небольшие; исходящий
  трафик — сами страницы `t.me/s/` (~50–200 КБ за fetch) плюс POST /messages.
- Числа — оценка для планирования; фактические значения снимите на стенде T18
  (`stats`-логи краулера раз в `TG_CRAWLER_STATS_LOG_INTERVAL=60` c).
