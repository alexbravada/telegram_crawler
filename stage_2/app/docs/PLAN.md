# tg_crawler: план реализации для агента (v2)

Спецификации: `docs/ARCHITECTURE.md` (v1) и `docs/ARCHITECTURE_v2.md` (v2). При расхождении приоритет у v2.
Если расхождение существенное, остановиться и спросить.

## Правила работы

1. Задачи выполнять строго по порядку. Следующая задача начинается, только когда выполнены все критерии приёмки текущей.
2. После каждой задачи запускать `ruff check`, `ruff format --check`, `mypy tg_crawler`, `pytest`. Все проверки должны проходить.
3. Не добавлять функциональность сверх описанной. Не скачивать медиа. Не добавлять аутентификацию в API.
4. Миграции создаются через Alembic. Интеграционные тесты работают только с временной БД из testcontainers. Общие БД не трогать.
5. Секреты (DSN, логины прокси) только через переменные окружения. В логах маскировать пароли в URL прокси.
6. Реальные запросы к `t.me` разрешены только:
   - в T4 (сбор фикстур, не больше 10 запросов, пауза не меньше 5 с);
   - в T17 (smoke compose, не больше 5 каналов);
   - в T18 (ручная проверка).

   В остальных тестах HTTP мокается через `respx`. Эмулятор в тестах тоже заменяется `respx`-моком.
7. Время, влияющее на логику (cooldown, `ready_at`, breaker, lease, backoff), берётся через инжектируемые часы
   (`Callable[[], float]`, по умолчанию `time.monotonic`). Тесты не используют `sleep`.
8. Если требование неоднозначно, остановиться и задать вопрос. Не угадывать.
9. В конце каждой задачи коротко отчитаться: что сделано, какие файлы изменены, результаты проверок, расхождения со спецификацией.

## Стек

- Python 3.11+ (нужен `asyncio.TaskGroup`)
- `fastapi`, `uvicorn`, `pydantic`, `pydantic-settings`
- `httpx[socks]`, `beautifulsoup4`, `lxml`
- `sqlalchemy[asyncio]` 2.0, `asyncpg`, `alembic`
- `structlog` (или `logging` с JSON-форматтером, если он уже принят в проекте)
- Dev: `pytest`, `pytest-asyncio`, `respx`, `testcontainers[postgres]`, `ruff`, `mypy`
- Docker, docker compose v2

---

## T0. Скелет проекта

**Файлы:** `pyproject.toml`, `tg_crawler/__init__.py`, пустые модули из раздела «Структура», `tests/conftest.py`,
`Dockerfile`, `.dockerignore`, `.env.example`, `.gitignore`.

**Требования:**
- Зависимости из раздела «Стек», dev-зависимости в группе `[dev]`.
- Настроены `ruff`, `mypy` (strict для `tg_crawler`), `pytest` (`asyncio_mode = "auto"`).
- `Dockerfile`: `python:3.11-slim`, non-root пользователь, установка через `pip install .`.
- `.env` в `.gitignore`.

**Приёмка:** `pip install -e .[dev]` работает, все проверки проходят, `docker build .` проходит.

## T1. Конфигурация, модели, протоколы

**Файлы:** `config.py`, `models.py`, `protocols.py`.

**Требования:**
- `Settings` по v1 раздел 12 и v2 раздел 8, префикс `TG_CRAWLER_`. `proxies` парсится из строки через запятую,
  пустая строка даёт `[]`.
- Dataclass-модели из v1 раздел 5.2. Изменения:
  - `Media` получает поле `caption: str | None = None`;
  - `ChannelTask` получает поле `channel_id: int | None` (Telegram-id, `-100…`, может быть `None`);
  - `PageResult` содержит `posts: tuple[Post, ...]` (все посты новее `since_post_id`) вместо единственного `post`.
- Enum `Outcome`: `OK, NOT_FOUND, RATE_LIMITED, RETRYABLE, LAYOUT_CHANGED, INVALID_NAME`.
- Протокол `Publisher`: `async def publish(post: Post) -> PublishResult`. `post.channel` содержит `channel_name`,
  он и уходит в контракт как `channel_id`.
- `PublishResult`: enum `DELIVERED | REJECTED | RETRY_LATER` плюс текст ошибки.
- Протокол `Sink` из v1 не создаётся.
- Функция маскировки пароля в URL прокси.

**Приёмка:** тесты на парсинг `Settings` из окружения (включая пустой список прокси) и на маскировку URL.

## T2. Валидация и определение имени канала

**Файлы:** `names.py`, `tests/test_names.py`.

**Требования:**
- `normalize(raw: str) -> str | None` из v1 раздел 7. Поддерживаемые формы: `@x`, `x`, `t.me/x`,
  `https://t.me/s/x/123`, `telegram.me/x`.
- `resolve_watch_name(channel_name: str | None, channel_url: str) -> NameResolution`, где
  `NameResolution(name: str | None, warning: str | None, reject_reason: str | None)`.
  Логика строго по таблице v2 раздел 3.1 «Определение имени канала». Приоритет у `channel_name`,
  при невалидном `channel_name` откат на URL не делается. Пустая строка в `channel_name` считается «не передан».
- `validate_channel_id(value: int | None) -> bool`: `None` допустим (поле необязательное); иначе значение отрицательное,
  строковое представление начинается с `-100`, длина не меньше 13 символов.

**Приёмка:**
- Параметризованные тесты `normalize()`: `@name`, `t.me/name`, `https://t.me/s/name/123`, `telegram.me/name`,
  пробелы по краям → `name`. Слишком короткое имя, имя с цифры, недопустимые символы, пустая строка → `None`.
- Тест `resolve_watch_name()` на каждую из 6 строк таблицы v2 раздел 3.1:
  1. `("durov", "https://t.me/durov")` → `durov`, без warning;
  2. `("durov", "t.me/telegram")` → `durov`, `channel_name_differs_from_url`;
  3. `("@durov", "мусор")` → `durov`, `channel_url_unparsed`;
  4. `("1abc", "t.me/durov")` → reject `invalid_channel_name`;
  5. `(None, "t.me/durov")` и `("", "t.me/durov")` → `durov`;
  6. `(None, "мусор")` → reject `invalid_channel_url`.
- `validate_channel_id`: `-[jg:education_document_11]` → True; `None` → True; `1234567890` → False; `-100` → False;
  `-1002` (меньше 13 символов) → False.

## T3. Модель БД и миграции

**Файлы:** `db.py`, `outbox.py`, `alembic.ini`, `alembic/env.py`, `alembic/versions/0001_channels.py`,
`alembic/versions/0002_pending_posts.py`.

**Требования:**
- Модель `Channel` из v1 раздел 5.1 плюс колонки v2 раздел 4: `channel_id BIGINT NULL`, `channel_url TEXT NULL`.
  `DeclarativeBase`.
- Модель `PendingPost` по v2 раздел 5.1: `id`, `channel_name`, `post_id`, `payload JSONB`, `status`
  (`pending | delivered | failed`), `attempts`, `last_error`, `created_at`, `updated_at`,
  уникальность по `(channel_name, post_id)`.
- `create_engine(settings)`, `async_sessionmaker`.
- Alembic настроен на async-движок (`env.py` с `run_sync`), DSN берётся из `Settings`.
- Миграция `0001`:
  - создаёт таблицу и частичный индекс `channels_due_idx` (`WHERE is_active AND NOT broken_url`);
  - создаёт уникальный частичный индекс `channels_channel_id_uq` на `channel_id` (`WHERE channel_id IS NOT NULL`);
  - миграция обратима (`downgrade`).
- Миграция `0002`: таблица `pending_posts` с уникальным индексом по `(channel_name, post_id)`, обратима.

**Приёмка (testcontainers):**
- `alembic upgrade head` создаёт таблицу `channels`, оба индекса и `pending_posts`; `downgrade base` удаляет всё.
- Вставка двух строк с одинаковым `channel_id` падает с `IntegrityError`.
- Вставка нескольких строк с `channel_id = NULL` проходит.
- `channel_id = -[jg:education_document_12]` сохраняется без переполнения.
- Повторный INSERT в `pending_posts` с тем же `(channel_name, post_id)` не создаёт дубликат
  (`ON CONFLICT DO NOTHING` затрагивает 0 строк).

## T4. Парсер

**Файлы:** `parser.py`, `tests/fixtures/*.html`, `tests/test_parser.py`.

**Требования:**
- `parse_posts(html, channel, since_post_id)` и `_extract_media()` по v1 разделы 3 и 8. Изменение относительно v1:
  возвращаются **все** посты с `post_id > since_post_id` (отсортированы по возрастанию `post_id`), а не только последний.
  `since_post_id = None` означает «взять все посты со страницы» (первая проверка канала).
- Исключение `LayoutChanged`.
- `Media.caption` всегда `None`: подпись отдельного медиа в `/s/` не извлекается. Не добавлять эвристику без отдельного решения.
- Фикстуры: 4–6 реальных страниц `t.me/s/<channel>` (текст, фото, альбом, видео, пустой канал, канал со служебным
  последним сообщением) и синтетическая страница без шапки. Список каналов для фикстур запросить у пользователя
  до начала задачи.

**Приёмка:**
- Для каждой фикстуры проверены `title`, список `post_id` (только посты новее `since_post_id`, по возрастанию),
  текст, число и тип медиа.
- `since_post_id` между двумя существующими постами отрезает старые.
- Служебное сообщение пропускается.
- Пустой канал → `posts=()`, `title` заполнен.
- Страница без шапки → `LayoutChanged`.
- Если вёрстка расходится с селекторами из v1, исправить парсер и перечислить изменённые селекторы в отчёте.

## T5. ProxyPool

**Файлы:** `proxy_pool.py`, `tests/test_proxy_pool.py`.

**Требования:** v1 раздел 6 полностью:
- отдельный клиент на каждый прокси, режим без прокси (один прямой клиент);
- куча по `ready_at`, `acquire()` / `release(proxy, outcome, retry_after=None)`;
- cooldown по `Retry-After` или экспонента с jitter, потолок 15 мин;
- AIMD для `min_interval`;
- статус `dead` после K ошибок подряд, перепроверка раз в M минут;
- `health_check()` при старте;
- `has_alive()` для паузы Feeder;
- `stats()`;
- `aclose()` закрывает все клиенты.

**Приёмка (фейковые часы):**
- Один прокси не выдаётся чаще `min_interval`.
- При трёх прокси `acquire()` чередует их.
- После 429 с `Retry-After: 60` прокси не выдаётся 60 с, `min_interval` вырос.
- После серии успехов `min_interval` возвращается к базовому, но не ниже.
- После K ошибок прокси получает статус `dead`; при единственном прокси `has_alive()` = False.
- Пустой список прокси → один прямой клиент.

## T6. Fetcher

**Файлы:** `fetcher.py`, `tests/test_fetcher.py`.

**Требования:**
- Одна попытка запроса к `https://t.me/s/<name>`, `follow_redirects=False`, таймауты и заголовки из v1 раздел 7.
- Классификация по таблице v1 раздел 7. `LAYOUT_CHANGED` здесь не определяется, это делает парсер.
- Возвращает `FetchResponse(outcome, html, retry_after, error)`. Исключения httpx наружу не выпускаются.

**Приёмка (`respx`):**
- 200 → `OK`;
- 302 на `/<name>` → `NOT_FOUND`;
- 429 с `Retry-After` и без → `RATE_LIMITED` с корректным `retry_after`;
- 502 → `RETRYABLE`;
- `ConnectTimeout`, `ConnectError`, `ReadTimeout` → `RETRYABLE`.

## T7. Планирование

**Файлы:** `scheduling.py`, `tests/test_scheduling.py`.

**Требования:** v1 раздел 9: `next_check_after_ok(last_post_at, now, rnd)`, `retry_delay(rnd)`,
`error_backoff(fail_count)`, `not_found_delay()`. Jitter через инжектируемый `random.Random`.

**Приёмка:** интервалы попадают в диапазоны таблицы с учётом jitter; `error_backoff` растёт и не превышает 6 ч.

## T8. Circuit breaker

**Файлы:** `breaker.py`, `tests/test_breaker.py`.

**Требования:** v1 раздел 10.7. Методы `record(outcome)`, `is_open()`, `allow_claim(n) -> int`.

**Приёмка (фейковые часы):**
- Порог не превышен → breaker закрыт.
- Порог превышен → breaker открыт, `allow_claim` = 0 до конца паузы.
- После паузы разрешается пробная партия.
- Нормальная пробная партия закрывает breaker, плохая снова открывает.

## T9. Publisher

**Файлы:** `publisher.py`, `tests/test_publisher.py`.

**Требования:**
- `HttpPublisher(url, timeout, max_attempts, client, sleep=asyncio.sleep)` реализует протокол `Publisher`.
- Маппинг `Post` в контракт эмулятора по v2 раздел 5:
  - `channel_id` = `post.channel` (`channel_name`, строка). Telegram-id в контракт не передаётся;
  - `original_message_id` = `str(post.post_id)`;
  - `body` = `post.text or ""`;
  - `medias[].id` = `m_{channel_name}_{post_id}_{index}`, индекс с нуля;
  - `medias[].caption` добавляется **только если у медиа `caption` не `None`**, иначе ключа нет в JSON.
- Классификация ответов:
  - `2xx` → `DELIVERED`;
  - `4xx`, кроме `429`, → `REJECTED` (ERROR в лог с телом запроса);
  - `429`, `5xx`, таймаут, ошибка соединения → повторы с экспоненциальным backoff (1 с, 2 с, 4 с, потолок 30 с),
    после `max_attempts` → `RETRY_LATER`.

**Приёмка (`respx`, фейковый `sleep`):**
- Тело запроса точно соответствует контракту (сравнение словаря целиком).
- Для канала `durov` в теле `"channel_id": "durov"`, Telegram-id в теле отсутствует.
- Пост без текста с тремя фото без подписей → `body == ""`, три элемента `medias` только с `id`, ключа `caption` нет.
- Медиа с `caption="подпись"` → в JSON `"caption": "подпись"`; соседнее медиа без подписи → ключа нет.
- 201 → `DELIVERED`; 422 → `REJECTED`.
- 500, 500, затем 201 → `DELIVERED`, ровно 3 запроса.
- 500 при `max_attempts=3` → `RETRY_LATER`, ровно 3 запроса.

## T10. Эмулятор

**Файлы:** `emulator/app.py`, `emulator/Dockerfile`, `emulator/requirements.txt`, `emulator/tests/test_app.py`.

**Требования:**
- FastAPI, `POST /messages` по контракту v2 раздел 5: pydantic-модель с валидацией, `extra="forbid"` для корня.
  - `channel_id`: непустая строка (это `channel_name`, формат `-100` не проверяется);
  - `original_message_id`: непустая строка;
  - `body`: строка, может быть пустой;
  - `medias`: список, `id` обязателен, `caption` опциональный (`str | None`, по умолчанию отсутствует).
- На каждый запрос одна строка JSON в stdout с полями `ts`, `path`, `body` (`print(json.dumps(...), flush=True)`
  или `logging` в stdout).
- Ответ `201 {"status": "accepted"}` при валидном запросе, `422` при невалидном.
- Переменная `EMULATOR_FAIL_RATE` (float, по умолчанию 0): с этой вероятностью отвечать `500`.
- `GET /health` → `200`.
- Образ на `python:3.11-slim`, зависимости только `fastapi` и `uvicorn`.

**Приёмка:** тесты через `httpx.AsyncClient` + `ASGITransport`:
- валидный запрос → 201 и строка в stdout (`capsys`);
- невалидный → 422;
- `EMULATOR_FAIL_RATE=1` → 500.

`docker build ./emulator` проходит.

## T11. Worker и Writer

**Файлы:** `worker.py`, `writer.py`, `tests/test_worker.py`, `tests/integration/test_writer.py`.

**Требования к Writer** (v1 разделы 10.2, 10.4–10.6, 11):
- группировка по исходам `ok`, `retry`, `not_found`, `broken`, `layout_error`, `publish_retry`;
- Core `executemany` с проверкой `lease_token`;
- `GREATEST` для `last_post_id`;
- логика `not_found`: интервал от `last_not_found_at`, другой прокси, если прокси больше одного, порог 3 → `broken_url`;
- если breaker открыт, `not_found_count` и `fail_count` не увеличиваются;
- повтор записи с backoff при ошибке БД, буфер в памяти;
- `flush()` для остановки.

**Требования к Worker** (v1 раздел 4, v2 разделы 5 и 5.1):
- Последовательность: semaphore → `acquire` → fetch → `release` → парсинг в `to_thread`.
- Каждый спарсенный пост с `post_id > last_post_id` канала пишется в outbox
  (`INSERT ... ON CONFLICT DO NOTHING`, payload — готовое тело запроса), затем воркер публикует его.
- Для каждого поста:
  - `DELIVERED` → исход `ok`: статус outbox `delivered`, Writer сдвигает `last_post_id` на `GREATEST`-максимум
    доставленных постов;
  - `REJECTED` → ERROR в лог, `attempts += 1`; если достигнут `outbox_max_attempts`, статус `failed`,
    `last_post_id` сдвигается (контент сохранён в outbox);
  - `RETRY_LATER` → пост остаётся `pending` (`attempts += 1`), `last_post_id` не сдвигается.
- Любое исключение при обработке задачи превращается в исход `retry`, воркер не падает.
- Исход передаётся в breaker.

**Требования к Outbox Delivery** (v2 раздел 5.1):
- Отдельная asyncio-задача: раз в `outbox_poll_interval` берёт батч `pending`-постов (по `attempts`, потом `id`)
  и публикует через тот же `Publisher` (экземпляр и семантика повторов — как в воркере).
- Временные ошибки → пост остаётся `pending`, следующая попытка с экспоненциальным backoff (потолок 30 с).
- `DELIVERED` → статус `delivered`, результат в Writer для сдвига `last_post_id`.
- `REJECTED` → `attempts += 1`, порог `outbox_max_attempts` → `failed`, ERROR в лог.
- Модель `PendingPost` и запись/чтение — в `outbox.py`.

**Приёмка (testcontainers для Writer; `respx` и фейковые pool/publisher для Worker):**
- `ok` обновляет `title`, `last_post_id`, сбрасывает счётчики и `lease_token`.
- `ok` с меньшим `post_id` не уменьшает `last_post_id`.
- Результат с устаревшим токеном игнорируется.
- Три `not_found` с нужными интервалами и разными прокси → `broken_url=true`.
- `not_found` раньше чем через 6 ч или через тот же прокси не засчитывается.
- `ok` между `not_found` сбрасывает счётчик.
- `invalid_name` → `broken_url=true`, `broken_reason=invalid_name`.
- Недоступность БД (симуляцией исключением) не теряет результаты.
- Новые посты публикуются ровно один раз; старые не публикуются; два новых поста за один фетч публикуются оба.
- `RETRY_LATER` → `last_post_id` не меняется, пост остаётся `pending` в outbox; после
  изменения ответа эмулятора на `2xx` цикл доставки публикует его без нового запроса к `t.me`.
- `REJECTED` `outbox_max_attempts` раз подряд → статус `failed`, `last_post_id` сдвинут.
- Исключение парсера не убивает воркер.

## T12. Repository и Feeder

**Файлы:** `repository.py`, `feeder.py`, `tests/integration/test_feeder.py`, `tests/integration/test_repository.py`.

**Требования к краулерной части:**
- `claim(batch, lease)` по v1 раздел 10.1. Выбираются строки с `is_active AND NOT broken_url AND next_check_at <= now()`.
  `RETURNING` включает `channel_id` и `channel_name`.
- `release(tasks)` по v1 раздел 10.3, шаг 2 (только при совпадении `lease_token`).
- Каналы с невалидным именем при захвате: проверяется `normalize()`, для невалидных формируется исход `invalid_name`
  для Writer, в очередь задача не попадает.
- Feeder: цикл опроса раз в `feeder_poll_interval`, ограниченная очередь, пауза, если `not pool.has_alive()`
  или breaker открыт, учёт `allow_claim`.
- `stop()` останавливает захват и возвращает в БД задачи из очереди.

**Требования к методам для API** (используются в T13):
- `upsert_watch(items) -> list[UpsertResult]`. На вход подаются уже разрешённые имена (после `resolve_watch_name`).
  - Реализация через `INSERT ... ON CONFLICT (channel_name) DO UPDATE`.
  - Неактивные каналы реактивируются: `is_active=true`, `next_check_at=now()`.
  - `broken_url` и `broken_reason` не меняются.
  - `channel_id = COALESCE(EXCLUDED.channel_id, channels.channel_id)`: отсутствие `channel_id` в запросе не затирает
    сохранённое значение.
  - `channel_url` обновляется.
  - Конфликт по `channel_id` с другой строкой возвращается как `reject_reason = "channel_id_conflict"`, а не падает исключением.
  - Признак `created` определяется через `RETURNING (xmax = 0)`.
- `deactivate(channel_name) -> bool`: `is_active=false`, `lease_token=NULL`. `True`, если канал найден
  (был активен или уже неактивен), `False`, если не найден.
- `list_channels(active, limit, offset)`.
- `ping()` для `/health`.

**Приёмка (testcontainers):**
- Захватываются только строки по условию выше.
- Два параллельных `claim` не возвращают один канал дважды.
- После захвата `next_check_at` сдвинут на lease, `lease_token` выставлен.
- `release` с чужим токеном строку не меняет.
- `stop()` возвращает незапущенные задачи: `next_check_at <= now()`, `lease_token IS NULL`.
- `upsert_watch` дважды для одного канала → одна строка, `created=true` только в первый раз.
- `upsert_watch` для неактивного канала → `is_active=true`, `next_check_at <= now()`.
- `upsert_watch` без `channel_id` → строка создаётся с `channel_id IS NULL`.
- `upsert_watch` без `channel_id` не затирает уже сохранённый `channel_id`.
- `upsert_watch` с `channel_id`, уже занятым другим каналом → `channel_id_conflict`, обе строки не меняются.
- `upsert_watch` для канала с `broken_url=true` оставляет `broken_url=true`.
- `deactivate` активного канала → `True`, `is_active=false`, `lease_token IS NULL`.
- `deactivate` неактивного → `True`, строка не меняется; несуществующего → `False`.

## T13. HTTP API

**Файлы:** `api.py`, `schemas.py`, `tests/integration/test_api.py`.

**Требования** (v2 раздел 3 полностью):
- `POST /watch_channels`:
  - тело `list[WatchItem]`, `WatchItem(channel_url: str, channel_name: str | None = None, channel_id: int | None = None)`;
  - `channel_id` **необязателен**; если передан, объявлен как `int` со `strict=True`: строка даёт `422`;
  - длина списка от 1 до `watch_max_batch`;
  - имя определяется через `resolve_watch_name()`, приоритет у `channel_name`;
  - `channel_id` проверяется через `validate_channel_id()`; неверный формат → `rejected`, `invalid_channel_id`;
  - дубли внутри запроса по итоговому имени: обрабатывается последний элемент, у остальных `warning: "duplicate_in_request"`;
  - ответ содержит `accepted` и `rejected` по формату v2 раздел 3.1.
- `GET /watch_channels` с параметрами `active`, `limit`, `offset`, поля ответа по v2 раздел 3.2.
- `DELETE /watch_channels/{channel_name}`: имя нормализуется через `normalize()` (`@durov` → `durov`);
  `200` при успехе и при повторе, `404`, если канала нет.
- `GET /health`: проверка БД через `repository.ping()`, при ошибке `503`.
- Сессия БД через зависимость FastAPI. Engine создаётся в `lifespan`, при остановке вызывается `engine.dispose()`.

**Приёмка (testcontainers, `httpx.AsyncClient` + `ASGITransport`):**
- `POST` с `{"channel_url": "t.me/telegram", "channel_name": "durov"}` → в БД канал `durov`,
  `channel_url = "t.me/telegram"`, `warning: "channel_name_differs_from_url"`.
- `POST` с `{"channel_url": "мусор", "channel_name": "durov"}` → принят, `warning: "channel_url_unparsed"`.
- `POST` с `{"channel_url": "t.me/durov", "channel_name": "1abc"}` → `rejected`, `invalid_channel_name`, канал `durov` не создан.
- `POST` с `{"channel_url": "t.me/durov"}` → принят как `durov`, в БД `channel_id IS NULL`.
- Повторный `POST` того же канала с `channel_id` → значение записано, `created=false`.
- `POST` с `{"channel_url": "мусор"}` → `rejected`, `invalid_channel_url`.
- `POST` с `{"channel_url": "t.me/durov", "channel_id": -[jg:education_document_13]}` → в БД `channel_id = -[jg:education_document_14]` (BIGINT).
- `POST` с `"channel_id": "-[jg:education_document_15]"` (строка) → `422`.
- `POST` с `"channel_id": 123` → `rejected`, `invalid_channel_id`.
- `POST` с тем же `channel_id` для другого канала → `rejected`, `channel_id_conflict`.
- Повторный `POST` → `created=false`; `POST` после `DELETE` → канал снова активен.
- Два элемента с одинаковым итоговым именем в одном запросе → одна строка в БД, warning `duplicate_in_request`.
- `DELETE /watch_channels/durov` → `200`; повтор → `200`; несуществующий канал → `404`.
- `GET` после `DELETE` при `active=all` показывает `is_active=false`.
- Ошибки схемы → `422`: пустой список; список длиннее `watch_max_batch`; нет `channel_url`.
- `/health` → `200`; при недоступной БД → `503` (через подмену зависимости).

## T14. CrawlerService и entrypoint

**Файлы:** `service.py`, `__main__.py`, `tests/test_service.py`.

**Требования:**
- `CrawlerService.start()` / `stop()`: `TaskGroup` (Feeder, воркеры, Outbox Delivery, Writer, статистика в лог),
  порядок остановки по v1 раздел 10.3, таймаут `shutdown_timeout`.
- `__main__.py`: `python -m tg_crawler` читает `Settings`, создаёт `HttpPublisher`, запускает сервис,
  обрабатывает SIGTERM и SIGINT через корректную остановку.
- Логи в JSON с полями `channel`, `outcome`, `proxy`, `post_id`.

**Приёмка:**
- `stop()` завершается за `shutdown_timeout` даже при зависшем запросе.
- Процесс запускается с пустой таблицей, пишет статистику и завершается по SIGTERM с кодом 0.

## T15. End-to-end тест без compose

**Файлы:** `tests/integration/test_e2e.py`.

**Требования:**
- PostgreSQL в testcontainers.
- Эмулятор как ASGI-приложение через `respx` (или отдельный процесс на случайном порту).
- `t.me` замокан через `respx`: 20 каналов, из них 10 отвечают OK с постами, 3 отвечают 302, 3 — 429, 4 — таймаутом.
- Половина из 20 каналов добавляется без `channel_id`. Для них посты доставляются так же, как для остальных.
- Сервис запущен с маленькими интервалами из тестовых `Settings`.

**Приёмка:**
- 20 каналов добавлены через API. За ограниченное время каждый канал проверен хотя бы раз.
- Все новые посты доставлены в эмулятор без потерь и без дублей в рамках прогона; в поле `channel_id` приходит `channel_name`
  (и для каналов с `channel_id`, и без него).
- Сценарий «эмулятор сначала отвечает 500, затем 2xx»: посты остаются `pending` и доставляются
  из outbox после восстановления, без повторных запросов к `t.me`.
- Сценарий «остановка посреди цикла и повторный запуск»: ни один канал не остался с `lease_token` и будущим
  `next_check_at` дольше lease. Все каналы снова проверены.
- Ни один канал не помечен `broken_url` после одного прогона.
- `DELETE /watch_channels/{name}` посреди работы: после ответа API и одного цикла Feeder новых запросов к `t.me`
  по этому каналу нет. Результат, пришедший от воркера после деактивации, не возвращает `is_active=true`.

## T16. Docker compose

**Файлы:** `docker-compose.yml`, `.env.example`, `README.md` (раздел «Запуск»).

**Требования** (v2 раздел 7):
- Сервисы `postgres`, `migrate`, `emulator`, `api`, `crawler`.
- `migrate` завершается с кодом 0, `api` и `crawler` стартуют после него.
- `crawler` не масштабируется: `replicas` в compose не задаётся, в README указано не увеличивать число реплик.
- Порты: `api` на `8000`, `emulator` на `8001`. Postgres наружу не публикуется.
- `.env.example` содержит переменные из v2 раздел 7. Реальный `.env` в git не попадает.
- В README: запуск, примеры `curl` для `POST`, `GET`, `DELETE /watch_channels` (в том числе `POST` без `channel_id`),
  просмотр логов эмулятора.

**Приёмка:**
- `docker compose config` проходит валидацию.
- `docker compose up -d --build` поднимает все сервисы, `migrate` завершается с кодом 0,
  `curl localhost:8000/health` возвращает `ok`.

## T17. Smoke-тест compose (автоматический)

**Файлы:** `scripts/smoke.sh`, `scripts/smoke_channels.txt`.

**Формат `smoke_channels.txt`:** `channel_url|channel_name|channel_id`, один канал на строку.
`channel_id` необязателен: строка вида `t.me/durov|durov|` допустима, и тогда поле не передаётся в `POST`.
Список каналов запросить у пользователя (до 5 штук).

**Шаги скрипта:**
1. `docker compose up -d --build`, ожидание `/health` (таймаут 120 с).
2. `POST /watch_channels` с каналами из файла.
3. Ожидание до 3 минут, пока в логах эмулятора (`docker compose logs emulator`) появится хотя бы один пост
   по каждому каналу: поиск JSON-строк с `channel_id`, равным `channel_name` канала.
4. Вывод сводки: сколько постов доставлено по каждому каналу.
5. Деактивация одного канала через `DELETE /watch_channels/{channel_name}` и проверка, что
   `GET /watch_channels?active=all` показывает у него `is_active=false`.
6. `docker compose down`. С флагом `--keep` volume не удаляется.

**Приёмка:** при доступе к `t.me` скрипт завершается с кодом 0, у каждого канала из файла есть хотя бы один пост
в логе эмулятора. Каналов не больше 5, пауза между запросами задаётся `ProxyPool` (без прокси 5 с).

## T18. Ручная проверка на большем наборе (пользователь или агент по согласованию)

- 50–100 реальных каналов, 0 прокси, `TG_CRAWLER_PROXY_MIN_INTERVAL=5`.
- Наблюдать долю 429, корректность `title` и последних постов, ответы `GET /watch_channels`.
- Затем подключить 1 прокси и подобрать `min_interval` по доле 429.
- Найденные расхождения с вёрсткой и контрактом записать в `docs/ARCHITECTURE.md` (раздел 15)
  и `docs/ARCHITECTURE_v2.md` (раздел 10).

---

## Решения и открытые вопросы

**Принятые решения** (не переспрашивать):
- `channel_id` в `POST /watch_channels` необязателен; в контракт эмулятора уходит `channel_name`.
- `caption` у медиа передаётся, только если задан.
- Удаление канала только по одному: `DELETE /watch_channels/{channel_name}`.
- Outbox (`pending_posts`): посты сохраняются в БД до публикации, доставка отдельным циклом.
  Требование к надёжности: потери ≤ 1/1000.
- Парсятся все посты новее `last_post_id`, а не только последний.
- `4xx` (кроме `429`) после `outbox_max_attempts` попыток → статус `failed` в outbox (контент сохранён).

**Запросить у пользователя:**
- Список каналов для фикстур T4 (4–6 штук: текст, фото, альбом, видео, пустой канал или канал со служебным сообщением).
- Список каналов для smoke-теста T17 (до 5 штук, `channel_id` по желанию).

---

## Структура пакета (итоговая)

```
tg_crawler/
  __main__.py     # запуск краулера, сигналы
  api.py          # FastAPI: /watch_channels (POST/GET/DELETE), /health
  schemas.py      # pydantic-модели API
  service.py      # CrawlerService, TaskGroup, порядок остановки
  worker.py       # цикл воркера
  feeder.py       # опрос due-каналов, пауза
  writer.py       # пакетные UPDATE с проверкой lease_token
  repository.py   # claim / release / upsert_watch / deactivate / list_channels / ping
  publisher.py    # HttpPublisher, контракт эмулятора, повторы
  outbox.py       # PendingPost, запись из воркера, цикл доставки
  breaker.py      # circuit breaker
  proxy_pool.py   # ProxyPool, AIMD, cooldown, health-check
  fetcher.py      # httpx, классификация Outcome
  parser.py       # parse_latest, _extract_media
  scheduling.py   # next_check_at, backoff
  names.py        # normalize, resolve_watch_name, validate_channel_id
  models.py       # dataclass-модели, Outcome, PublishResult
  db.py           # Channel, engine, sessionmaker
  protocols.py    # Publisher
  config.py       # Settings
alembic/
  env.py
  versions/0001_channels.py
alembic.ini
emulator/
  app.py
  Dockerfile
  requirements.txt
  tests/test_app.py
scripts/
  smoke.sh
  smoke_channels.txt
tests/
  fixtures/
  integration/
Dockerfile
docker-compose.yml
.env.example
README.md
```
