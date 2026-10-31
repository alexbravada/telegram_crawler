# tg_crawler v2: сервис с API и доставкой постов

Этот документ дополняет и частично заменяет `docs/ARCHITECTURE.md` (v1).
Разделы v1, которых здесь нет, остаются в силе. При расхождении приоритет у v2.

## 1. Что меняется относительно v1

| Раздел v1 | Изменение |
|---|---|
| 1. Назначение | Модуль стал сервисом. Посты публикуются не в лог Sink, а во внешний API (эмулятор) |
| 2. Ключевые решения | Два процесса (API и краулер) в разных контейнерах, один PostgreSQL. Краулер остаётся одной репликой |
| 4. Компоненты | Добавлены `api`, `publisher`, эмулятор. `Sink` заменён на `Publisher` |
| 5.1. Таблица `channels` | Добавлены колонки `channel_id` (Telegram-идентификатор, `-100…`) и `channel_url` (раздел 4) |
| 12. Конфигурация | Добавлены `publisher_*`, `feeder_poll_interval`, `watch_max_batch`, `outbox_*` |
| — (новое в v2) | Outbox: посты сохраняются в БД до публикации, доставка отдельным циклом (раздел 5.1). Требование к надёжности: потери ≤ 1/1000 |
| 15. Что проверить | Дополняется разделом 10 этого документа |

## 2. Общая схема

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

API и краулер не общаются напрямую. Всё общее состояние лежит в таблице `channels`. Это даёт три свойства:
- каналы добавляются и удаляются без перезапуска: краулер перечитывает таблицу на каждом цикле Feeder;
- API можно масштабировать (несколько реплик), краулер масштабировать нельзя;
- перезапуск любого процесса не теряет список каналов.

## 3. API сервиса

Префикс без версии. Аутентификации в MVP нет. Сервис должен быть доступен только во внутренней сети.

### 3.1. `POST /watch_channels`: добавить или обновить каналы

Тело запроса: список от 1 до `watch_max_batch` элементов.

```json
[
  {"channel_url": "https://t.me/durov", "channel_name": "durov", "channel_id": -[jg:education_document_8]},
  {"channel_url": "t.me/telegram"}
]
```

| Поле | Тип | Обязательно | Правила |
|---|---|---|---|
| `channel_url` | `str` | да | Принимаются `https://t.me/x`, `t.me/x`, `telegram.me/x`, `https://t.me/s/x/123`, `@x`, `x`. Имя из URL используется, только если `channel_name` не передан |
| `channel_name` | `str \| null` | нет | **Приоритетный источник имени канала.** Допускается `durov` или `@durov`. Это username канала, он может меняться |
| `channel_id` | `int \| null` | нет | Telegram-идентификатор канала вида `-100…` (число). Неизменяем, в отличие от `channel_name`. **Необязателен (решение зафиксировано)**: сервис не может получить его из публичной страницы `t.me/s/`, поэтому значение принимается от клиента, если оно есть. Схема строгая: строка `"-100…"` даёт `422` |

Ограничения `channel_id`, если он передан: отрицательное число, строковое представление начинается с `-100`,
длина не меньше 13 символов. Иначе элемент попадает в `rejected` с `reason: "invalid_channel_id"`.

#### Определение имени канала

`channel_name` и имя из `channel_url` проходят через `names.normalize()`.
Строки таблицы взаимоисключающие, проверяются сверху вниз.

| # | `channel_name` | Имя из `channel_url` | Результат |
|---|---|---|---|
| 1 | передан, валиден | валидно и совпадает | Используется `channel_name`, `warning: null` |
| 2 | передан, валиден | валидно и отличается | Используется `channel_name`, `warning: "channel_name_differs_from_url"` |
| 3 | передан, валиден | не извлекается | Используется `channel_name`, `warning: "channel_url_unparsed"` |
| 4 | передан, **невалиден** | любое | `rejected`, `reason: "invalid_channel_name"`. Откат на URL **не делается**: клиент явно указал имя, и сервис не должен молча подменить канал |
| 5 | не передан или пустая строка | валидно | Используется имя из URL |
| 6 | не передан или пустая строка | не извлекается | `rejected`, `reason: "invalid_channel_url"` |

`channel_url` сохраняется в БД как есть (колонка `channel_url`) для отладки, даже если имя взято из `channel_name`.

#### Поведение

- Операция идемпотентна. Повторный POST того же канала не создаёт дубликат, ключом служит итоговое `channel_name`.
- Если канал уже есть и неактивен, он реактивируется: `is_active = true`, `next_check_at = now()`.
  Флаги `broken_url` и `broken_reason` при этом **не** сбрасываются, их снимают отдельно.
- Если для существующего канала передан `channel_id`, он обновляется. Если не передан, старое значение сохраняется.
- Канал без `channel_id` принимается и обрабатывается так же, как канал с ним: краулер и контракт эмулятора
  используют `channel_name`. `channel_id` в этом случае хранится как `NULL` и может быть добавлен позже
  повторным `POST` того же канала.
- Дубли внутри одного запроса (одинаковое итоговое имя) обрабатываются один раз. Значения берутся из последнего элемента,
  у остальных в ответе `warning: "duplicate_in_request"`.
- Если `channel_id` уже задан у другого канала (уникальный индекс по `channel_id`), элемент попадает в `rejected`
  с `reason: "channel_id_conflict"`.

Ответ `200`:

```json
{
  "accepted": [
    {"channel_name": "durov", "channel_id": -[jg:education_document_10], "created": true, "warning": null},
    {"channel_name": "telegram", "channel_id": null, "created": false, "warning": null}
  ],
  "rejected": [
    {"channel_url": "t.me/x", "channel_name": "1abc", "reason": "invalid_channel_name"}
  ]
}
```

Код `422` для всего запроса возвращается только при нарушении схемы. Невалидные элементы попадают в `rejected`,
остальные обрабатываются.

### 3.2. `GET /watch_channels`: список

Query-параметры: `active` (`true | false | all`, по умолчанию `true`), `limit` (по умолчанию 100, максимум 1000), `offset`.

Поля ответа: `channel_name`, `channel_id`, `channel_url`, `title`, `is_active`, `broken_url`, `broken_reason`,
`last_post_id`, `last_post_at`, `last_checked_at`, `next_check_at`, `fail_count`, `not_found_count`.

### 3.3. `DELETE /watch_channels/{channel_name}`: убрать канал

Ставит `is_active = false`, `lease_token = NULL`. Строка не удаляется, `last_post_id` сохраняется.
- `200` с `{"channel_name": ..., "is_active": false}`.
- `404`, если канал не найден.
- `200`, если канал уже неактивен (операция идемпотентна).

Пост, который краулер уже забрал и отправляет в эмулятор, может уйти после удаления. Это допустимо.

### 3.4. `GET /health`

`200 {"status": "ok", "db": "ok"}`, если БД доступна, иначе `503`.

## 4. Изменения в таблице `channels`

К колонкам из v1 добавляются:

```python
channel_id: Mapped[int | None] = mapped_column(BigInteger)  # Telegram-id, -100…; BIGINT, т.к. не влезает в int32
channel_url: Mapped[str | None] = mapped_column(Text)       # исходный URL, для отладки
```

- `channel_name` остаётся уникальным ключом строки.
- `channel_id` необязателен (`NULL` допустим) и уникален, если задан. Уникальность обеспечивает индекс
  `UNIQUE (channel_id) WHERE channel_id IS NOT NULL`.
- Индекс `channels_due_idx` не меняется.

## 5. Публикация постов: контракт эмулятора

Краулер отправляет каждый новый пост одним запросом:

```
POST {publisher_url}          # например http://emulator:8001/messages
Content-Type: application/json
```

```json
{
  "channel_id": "durov",
  "original_message_id": "4821",
  "body": "текст поста",
  "medias": [
    {"id": "m_durov_4821_0"},
    {"id": "m_durov_4821_1", "caption": "подпись к фото"}
  ]
}
```

Правила маппинга:

| Поле контракта | Откуда |
|---|---|
| `channel_id` | `channel_name` канала (строка без `@`). Внутренний Telegram-id `channel_id` (`-100…`) в контракт **не** попадает |
| `original_message_id` | `post_id` строкой |
| `body` | `text` поста или `""` |
| `medias[].id` | `m_{channel_name}_{post_id}_{index}`, индекс с нуля |
| `medias[].caption` | `caption` медиа, **только если он задан**. Если `caption` равен `None`, ключ в JSON не передаётся, а не отправляется как `null` |

Фото и видео отправляются одинаково: `id` и, при наличии, `caption`. Превью видео и URL CDN в контракт не входят.

Откуда берётся `caption`: в модели `Media` есть поле `caption: str | None = None`. Парсер `/s/` подпись отдельного медиа
не извлекает, поэтому сейчас `caption` всегда `None`, и ключ в контракте не появляется. Текст поста передаётся в `body`.

### 5.1. Outbox: сохранение и доставка постов

Требование к надёжности: потери постов не более 1 на 1000. Пост сохраняется в БД **до** публикации,
поэтому недоступность эмулятора, падение процесса и неудачные ретраи не приводят к потере контента.

Поток: воркер делает fetch → parse (все посты с `post_id > last_post_id`) → `INSERT ... ON CONFLICT DO NOTHING`
в `pending_posts` → публикация. `last_post_id` канала сдвигается только после `DELIVERED`.

Таблица `pending_posts`:

```python
id: BigInteger, primary key
channel_name: Text, NOT NULL          # уходит в контракт как channel_id
post_id: BigInteger, NOT NULL
payload: JSONB, NOT NULL              # готовое тело запроса: body, medias (с caption при наличии)
status: Text, NOT NULL, default "pending"   # pending | delivered | failed
attempts: SmallInteger, NOT NULL, default 0
last_error: Text, NULL
created_at: DateTime(timezone=True), NOT NULL
updated_at: DateTime(timezone=True), NOT NULL

UNIQUE (channel_name, post_id)
```

Доставка:

- После INSERT воркер сразу пробует опубликовать пост.
- Отдельная asyncio-задача (Outbox Delivery) периодически забирает батч `pending`-постов
  и публикует их тем же `Publisher`.
- Временные ошибки (`429`, `5xx`, таймаут, ошибка соединения) → пост остаётся `pending`,
  `attempts += 1`, следующая попытка с экспоненциальным backoff (потолок 30 с). Ретраи из outbox
  не ограничены числом попыток: пост удаляется из очереди только по терминальному исходу.
- Постоянная ошибка (`4xx`, кроме `429`) → `attempts += 1`; после `outbox_max_attempts` подряд
  статус `failed`, ERROR в лог. Контент сохранён, пост можно переотправить вручную.
- `DELIVERED` → `status = delivered` и сдвиг `last_post_id` канала (через Writer).
- Публикация выполняется **до** записи результата → при падении между отправкой и записью возможен
  повтор (at-least-once); дедуп на получателе по паре `(channel_id, original_message_id)`.

Остаточные пути потери (за пределами SLA 1/1000, зафиксированы сознательно):

- пост удалён из канала до первого успешного fetch;
- канал запостил больше ~20 постов между проверками (страница `/s/` отдаёт только 20 последних).

### Семантика доставки

| Ответ эмулятора | Действие |
|---|---|
| `2xx` | `DELIVERED`. Статус outbox → `delivered`, Writer сдвигает `last_post_id` |
| `4xx`, кроме `429` | `attempts += 1`. После `outbox_max_attempts` подряд статус outbox → `failed`, ERROR в лог, `last_post_id` сдвигается (контент сохранён в outbox) |
| `429`, `5xx`, таймаут, ошибка соединения | Пост остаётся `pending`. Повторы из outbox с экспоненциальным backoff (1 с, 2 с, 4 с, ..., потолок 30 с) без новых запросов к `t.me`. `last_post_id` не сдвигается |

Публикация выполняется **до** записи результата в Writer. Если процесс упадёт между отправкой и записью, пост может уйти
повторно (at-least-once). Получатель при необходимости удаляет дубли по паре `(channel_id, original_message_id)`.

При первой проверке после добавления канала `last_post_id` пуст, поэтому уходит последний пост канала.

## 6. Жизненный цикл канала

```
POST /watch_channels ──► [is_active=true, next_check_at=now()]
                              │
              Feeder claim ───┤  (lease 10 мин)
                              ▼
                        Worker: fetch → parse → publish → Writer
                              │
               next_check_at по активности (раздел 9 v1)
                              │
DELETE /watch_channels/x ─► [is_active=false]  ── Feeder больше не берёт
POST  /watch_channels x  ─► [is_active=true, next_check_at=now()]  (реактивация)
```

Краулер подхватывает изменения на следующем цикле Feeder (интервал `feeder_poll_interval`, по умолчанию 5 с).
Перезапускать его не нужно.

## 7. Развёртывание: docker compose

| Сервис | Образ | Назначение | Порт на хосте |
|---|---|---|---|
| `postgres` | `postgres:16` | БД | — |
| `migrate` | образ проекта | `alembic upgrade head`, завершается с кодом 0 | — |
| `api` | образ проекта | `uvicorn tg_crawler.api:app` | 8000 |
| `crawler` | образ проекта | `python -m tg_crawler`, **одна реплика** | — |
| `emulator` | `emulator/` | Принимает `POST /messages`, пишет в лог | 8001 |

Зависимости запуска:
- `migrate` ждёт, пока `postgres` станет healthy;
- `api` и `crawler` ждут `migrate` в состоянии `completed_successfully` и запуска `emulator`;
- `crawler` не масштабируется: `deploy.replicas` не задаётся, в README указано не увеличивать число реплик.

Эскиз `docker-compose.yml`:

```yaml
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_USER: crawler
      POSTGRES_PASSWORD: crawler
      POSTGRES_DB: tg_crawler
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U crawler -d tg_crawler"]
      interval: 2s
      retries: 30

  migrate:
    build: .
    command: alembic upgrade head
    env_file: .env
    depends_on:
      postgres: {condition: service_healthy}

  emulator:
    build: ./emulator
    ports: ["8001:8001"]

  api:
    build: .
    command: uvicorn tg_crawler.api:app --host [jg:ip_adress_4] --port 8000
    env_file: .env
    ports: ["8000:8000"]
    depends_on:
      migrate: {condition: service_completed_successfully}
      emulator: {condition: service_started}

  crawler:
    build: .
    command: python -m tg_crawler
    env_file: .env
    depends_on:
      migrate: {condition: service_completed_successfully}
      emulator: {condition: service_started}
```

Переменные окружения (`.env.example`):

```
TG_CRAWLER_DATABASE_URL=postgresql+asyncpg://crawler:crawler@postgres:5432/tg_crawler
TG_CRAWLER_PUBLISHER_URL=http://emulator:8001/messages
TG_CRAWLER_PROXIES=
```

Эмулятор (`emulator/`) — небольшое FastAPI-приложение, зависимости только `fastapi` и `uvicorn`:
- на каждый `POST /messages` пишет в stdout одну строку JSON: время и тело запроса;
- валидирует контракт (раздел 5), отвечает `201` или `422`;
- опционально читает `EMULATOR_FAIL_RATE` (0.0–1.0): с такой вероятностью отвечает `500`, чтобы проверять повторы.

Просмотр результата: `docker compose logs -f emulator`.

## 8. Конфигурация

Добавляется к разделу 12 v1:

| Параметр | По умолчанию | Назначение |
|---|---|---|
| `publisher_url` | обязателен | URL эмулятора или внешнего API |
| `publisher_timeout` | 5 с | Таймаут запроса к API |
| `publisher_max_attempts` | 5 | Повторы на временных ошибках |
| `feeder_poll_interval` | 5 с | Как часто Feeder проверяет таблицу |
| `watch_max_batch` | 1000 | Максимум элементов в `POST /watch_channels` |
| `outbox_poll_interval` | 5 с | Как часто цикл доставки проверяет `pending_posts` |
| `outbox_batch` | 50 | Сколько `pending`-постов берётся за раз |
| `outbox_max_attempts` | 3 | Попыток подряд до статуса `failed` при `4xx` |

`pydantic-settings` с префиксом `TG_CRAWLER_`. Настройки API и краулера общие (один класс `Settings`).

## 9. Структура пакета (дополнения к v1)

```
tg_crawler/
  api.py          # FastAPI: /watch_channels (POST/GET/DELETE), /health
  schemas.py      # pydantic-модели запросов и ответов API
  publisher.py    # HttpPublisher: контракт, повторы, классификация ответов
  outbox.py       # модель pending_posts, запись из воркера, цикл доставки
  repository.py   # + upsert_watch, deactivate, list_channels, ping
  names.py        # + resolve_watch_name, validate_channel_id
  __main__.py     # запуск краулера
emulator/
  app.py          # POST /messages, лог в stdout
  Dockerfile
  requirements.txt
docker-compose.yml
Dockerfile        # образ проекта (общий для api, crawler, migrate)
.env.example
alembic/          # миграции
```

## 10. Решения и открытые вопросы

### Принятые решения

- `title` поста = название канала.
- Краулер забирает **все** посты новее `last_post_id` (страница `/s/` отдаёт ~20 последних;
  если канал запостил больше — старые взять неоткуда, остаточный риск зафиксирован в разделе 5.1).
- Посты сохраняются в outbox (`pending_posts`) до публикации; доставка из outbox отдельным циклом.
  Требование к надёжности: потери постов ≤ 1/1000. Пост, удалённый из канала до первого fetch,
  теряется безвозвратно.
- `4xx` (кроме `429`) от эмулятора: после `outbox_max_attempts` попыток пост получает статус `failed`
  (контент сохранён, переотправка вручную).
- В `POST /watch_channels` приоритет у `channel_name`. Имя из `channel_url` используется, только если `channel_name` не передан.
- `channel_id` в `POST /watch_channels` необязателен. Он хранится в БД как внутренний Telegram-идентификатор канала (`-100…`)
  и в контракт эмулятора не передаётся. В поле контракта `channel_id` уходит `channel_name` (раздел 5).
- `caption` у медиа передаётся в контракт, только если он задан.
- Удаление канала мягкое (`is_active = false`), только по одному каналу: `DELETE /watch_channels/{channel_name}`.

### Открытые вопросы

- Как эмулятор или внешний API ведёт себя при повторном `POST` одного поста. Нужен ли заголовок `Idempotency-Key`.
  Сейчас его нет, потому что контракт его не описывает.
- Источник `caption` у медиа: сейчас парсер его не извлекает (раздел 5).
