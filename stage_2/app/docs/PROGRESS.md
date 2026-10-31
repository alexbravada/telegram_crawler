# Прогресс

## T17. Smoke-тест compose

- **Статус:** заблокирован (скрипт готов, фактический прогон отложен — см. расхождения).
- **Файлы:** `scripts/smoke.sh` (новый), `scripts/smoke_channels.txt` (новый: 1 канал
  `t.me/telegram`, без `channel_id` — по решению пользователя).
- **Проверки:** полный прогон не выполнен. Частично проверено на живом стеке при отладке:
  `docker compose up -d --build` поднимает все сервисы, `migrate` отрабатывает,
  `/health` → ok, POST `/watch_channels` (списком) принимает канал, краулер стартует
  цикл по каналу. Доставка постов не удалась: `t.me` заблокирован в сети (IPv6 —
  network unreachable, IPv4 — timeout), рабочего прокси нет. Стек снят (`down -v`).
- **Расхождения со спекой:**
  - спека T17 предполагает реальные запросы к `t.me` — прогон отложен до появления
    рабочего прокси (по решению пользователя);
  - `smoke.sh` совместим с bash 3.2 (macOS): без `mapfile`/ассоциативных массивов;
  - ожидание постов: до 180s по логам эмулятора; после проверки — DELETE первого
    канала с проверкой `is_active=false` (GET `?active=all`), затем `down -v`
    (или `down` при `--keep`).

## T16. Docker compose

- **Статус:** выполнено.
- **Файлы:** `docker-compose.yml`, `README.md` (новый), `Dockerfile`
  (+`alembic.ini`, `alembic/` — нужны сервису `migrate`), `tg_crawler/asgi.py` (новый:
  module-level `app = create_app(Settings())` для `uvicorn tg_crawler.asgi:app`).
- **Проверки:** `docker compose config` ✅; `docker compose up -d --build` — все сервисы
  подняты, `migrate` exit code 0, миграции 0001/0002 применены,
  `curl localhost:8000/health` → `{"status":"ok","db":"ok"}`; POST/GET/DELETE
  `/watch_channels` проверены curl'ом на живом стеке; после проверки стек снят
  (`docker compose down -v`). `ruff`/`mypy`/`pytest` ✅ (217 passed).
- **Расхождения со спекой / уточнения:**
  - в эскизе v2 `uvicorn tg_crawler.api:app` — в api.py только фабрика `create_app(settings)`,
    поэтому добавлен `tg_crawler/asgi.py` с module-level `app`;
  - `.env.example` уже соответствовал v2 раздел 7 — не менялся; `.env` в git не попадает
    (в `.gitignore`);
  - сервис `crawler` без `deploy.replicas`, в README указано не масштабировать.

## T15. End-to-end тест без compose

- **Статус:** выполнено.
- **Файлы:** `tests/integration/test_e2e.py` (новый), `tg_crawler/outbox.py`
  (+`InFlightRegistry`, `in_flight` у `OutboxDelivery`), `tg_crawler/service.py`
  (+`InFlightPosts`, `TrackedOutboxStore`, передача настроек прокси в `ProxyPool`).
- **Проверки:** `ruff check` ✅, `ruff format --check` ✅, `mypy tg_crawler` (strict) ✅,
  `pytest` ✅ (217 passed); стресс полный прогон ×6 — без дублей.
- **Покрытые сценарии:** полный прогон 20 каналов (10 OK, 3×302, 3×429, 4×timeout; половина
  без `channel_id`) — доставка 20 постов без потерь/дублей, `channel_id` в контракте =
  `channel_name`, ни один канал не `broken_url`; «эмулятор 500 → 2xx» — посты остаются
  `pending` и доставляются из outbox, повторных запросов к `t.me` нет, `last_post_id` сдвинут;
  «остановка посреди цикла и рестарт» — после рестарта нет каналов с `lease_token`, все
  проверены заново, дублей нет; `DELETE /watch_channels` посреди работы — новых запросов к
  `t.me` нет, `is_active` не возвращается в `true`.
- **Расхождения со спекой / уточнения:**
  - гонка «воркер публикует пост ↔ Outbox Delivery публикует ту же строку» давала дубли в рамках
    прогона (спека v2 §5.1 допускает at-least-once, но приёмка T15 требует их отсутствия без
    падений). Добавлен реестр `InFlightPosts` (TTL 30 с, self-healing) + обёртка
    `TrackedOutboxStore`: воркер помечает пост in-flight до INSERT и снимает метку после записи
    результата в БД; Delivery пропускает in-flight строки. Изменение схемы не требуется;
  - ключевой момент корректности: `_fetch_batch` снимает копию `registered_keys()` **до** SELECT
    и фильтрует снапшот по ней. По-ключевая проверка после SELECT гонку не устраняет: между
    SELECT и фильтром воркер успевает закоммитить `delivered` и снять метку — строка из снапшота
    уходила в повторную публикацию;
  - `ProxyPool` в сервисе теперь создаётся с `proxy_min_interval`/`proxy_dead_after`/
    `proxy_recheck` из настроек (ранее использовались дефолты);
  - тестовые имена каналов ≥ 4 символов (ограничение `normalize()`), `lease` в тестах 10 с —
    больше полного цикла обработки, иначе истечение lease даёт легитимный повтор (at-least-once);
  -Recorder: успешные POST /messages захватываются ASGI-обёрткой (только 2xx).

## T14. CrawlerService и entrypoint

- **Статус:** выполнено.
- **Файлы:** `tg_crawler/service.py`, `tg_crawler/__main__.py`, `tests/test_service.py` (новый),
  `alembic/env.py` (`fileConfig(..., disable_existing_loggers=False)` — запуск миграций в процессе
  ранее отключал уже созданные логгеры `tg_crawler.*`).
- **Проверки:** `ruff check` ✅, `ruff format --check` ✅, `mypy tg_crawler` (strict) ✅,
  `pytest` ✅ (210 passed).
- **Расхождения со спекой / уточнения:**
  - `run()` держит TaskGroup: feeder, N воркеров, outbox-delivery, writer-цикл (флаш по
    `writer_flush_interval`), stats-цикл (по `stats_log_interval`: очередь, прокси, буфер writer);
  - остановка по v1 §10.3: отмена feeder/delivery/writer/stats → `feeder.stop()` (задачи из
    очереди возвращаются в БД) → ожидание воркеров с таймаутом `shutdown_timeout`, зависшие
    отменяются → `writer.flush()` → `pool.aclose()`; `engine.dispose()` делает `__main__`;
  - `request_stop()` — синхронный (для обработчиков SIGTERM/SIGINT), `stop()` ждёт завершения;
  - `LoggingWriter` оборачивает Writer и пишет JSON-лог по каждому опу (channel/outcome/proxy/post_id);
    `JsonFormatter` выводит строки в JSON (поля добавляются, если заданы);
  - `__main__`: Settings из окружения/.env, HttpPublisher с собственным httpx-клиентом,
    обработчики SIGTERM/SIGINT, exit code 0; приёмка «запуск с пустой таблицей и SIGTERM» —
    в T17/T18 (нужен реальный PostgreSQL);
  - сервисные компоненты инъектируются (repository/pool/fetcher/breaker/writer/delivery) —
    юнит-тесты на фейках без БД.

## T13. HTTP API

- **Статус:** выполнено.
- **Файлы:** `tg_crawler/api.py`, `tg_crawler/schemas.py`, `tests/integration/test_api.py` (новый),
  `pyproject.toml` (per-file-ignores: `B008` для `tg_crawler/api.py` — идиоматический FastAPI `Depends`).
- **Проверки:** `ruff check` ✅, `ruff format --check` ✅, `mypy tg_crawler` (strict) ✅,
  `pytest` ✅ (204 passed, включая 17 API-тестов на testcontainers).
- **Расхождения со спекой / уточнения:**
  - дубликаты в одном запросе: значения берутся из последнего элемента, у остальных
    `warning: "duplicate_in_request"`; победитель обрабатывается как обычный элемент и сохраняет
    собственное предупреждение разрешения имени (например, `channel_name_differs_from_url`) —
    по v2 §3.1 правило дубликатов переопределяет warning только у проигравших;
  - `channel_id_conflict` из `upsert_watch` маппится в `rejected` по `reject_reason`;
  - lifespan создаёт engine/session_factory, `dispose()` при остановке; тесты запускают
    `app.router.lifespan_context(app)` (ASGITransport lifespan не выполняет);
  - `limit` ≤ 1000 через `Query(ge=1, le=1000)`, батч POST ≤ `watch_max_batch` → 422;
  - DELETE нормализует имя (`%40durov` → `durov`), 404 для отсутствующего/невалидного;
  - `/health`: ping БД → 200 `{"status": "ok", "db": "ok"}` / 503.

## T12. Repository и Feeder

- **Статус:** выполнено.
- **Файлы:** `tg_crawler/repository.py`, `tg_crawler/feeder.py`,
  `tests/integration/test_repository.py` (новый), `tests/test_feeder.py` (новый).
- **Проверки:** `ruff check` ✅, `ruff format --check` ✅, `mypy tg_crawler` (strict) ✅,
  `pytest` ✅ (187 passed).
- **Расхождения со спекой / уточнения:**
  - `claim()` возвращает `(valid, invalid)`: каналы с невалидным именем тоже захватываются
    (lease_token выставлен), чтобы Writer принял `broken`-исход с проверкой токена; в очередь
    не попадают, broken-оп отправляет Feeder;
  - `upsert_watch` выполняется поэлементно внутри `begin_nested` (savepoint): конфликт
    `channel_id` возвращает `reject_reason="channel_id_conflict"` для элемента, не роняя батч;
  - признак `created` — через `RETURNING (xmax = 0)`;
  - `list_channels` возвращает `ChannelInfo` (поля v2 §3.2), сортировка по `channel_name`;
  - DML в репозитории — Core-таблица `Channel.__table__` (ORM-entity `update` с параметрами
    включает bulk-стратегию SQLAlchemy 2.0 и требует `synchronize_session`);
  - при переполнении очереди Feeder возвращает остаток захваченных задач в БД через
    `release()` (остаток из той же порции claim, тем же токеном);
  - тесты Feeder — на фейковых repository/pool/writer, без БД; инъекция `sleep` и `rnd`.

## T11. Worker и Writer

- **Статус:** выполнено.
- **Файлы:** `tg_crawler/worker.py`, `tg_crawler/writer.py`, `tg_crawler/outbox.py`,
  `tg_crawler/publisher.py` (рефакторинг: `publish_raw(payload)`, общий `_send`),
  `tg_crawler/protocols.py` (+`RawPublisher`), `tests/test_worker.py`,
  `tests/integration/conftest.py` (сессия-фикстура `pg_url`, `clean_db`; не попал в коммит T3),
  `tests/integration/test_writer.py`, `tests/integration/test_outbox.py` (новый).
- **Проверки:** `ruff check` ✅, `ruff format --check` ✅, `mypy tg_crawler` (strict) ✅,
  `pytest` ✅ (168 passed, включая writer/outbox на testcontainers).
- **Расхождения со спекой / уточнения:**
  - пакетная публикация в воркере останавливается на первом не-`DELIVERED` результате
    (REJECTED/RETRY_LATER/исключение): остальные посты остаются `pending` в outbox, их доставит
    цикл доставки — без этого нарушалась семантика «публикация до записи результата»;
  - исключение publisher → исход `retry` (lease освобождается, `last_post_id` не сдвигается);
  - `mark_attempt` (интерфейс из тестов) применяет порог `outbox_max_attempts` к любому типу
    ошибки; цикл доставки для 429/5xx/сетевых ошибок использует отдельный метод без порога
    (ретраи не ограничены, по v2 §5.1). Пограничный случай: смешанная последовательность
    «временные ошибки, затем 4xx» в прямом пути воркера может дать `failed` раньше — принят;
  - сдвиг `last_post_id` из цикла доставки делается прямым `UPDATE ... GREATEST` по
    `channel_name` (вне lease; монотонность гарантирует GREATEST), а не через Writer-оп `ok`,
    который требует lease_token;
  - в `not_found`/`layout_error` обработка построчная; если в одном флеше несколько операторов
    одного канала (один lease), проверка токена пропускается для последующих (lease уже отпущен
    первой операцией того же воркера);
  - breaker записывает исходы fetch (OK/NOT_FOUND/LAYOUT_CHANGED/INVALID_NAME); RATE_LIMITED/
    RETRYABLE/ошибки публикации в breaker не идут (вина прокси/эмулятора, v1 §10.8);
  - backoff доставки по временным ошибкам — в памяти процесса (`2^attempts`, потолок 30 с),
    инжектируемые часы; при рестарте процесса backoff сбрасывается.

## T10. Эмулятор

- **Статус:** выполнено.
- **Файлы:** `emulator/app.py`, `emulator/Dockerfile`, `emulator/requirements.txt`,
  `emulator/tests/test_app.py`, `emulator/__init__.py`, корневой `conftest.py` (импорт пакета в тестах),
  `pyproject.toml` (testpaths += emulator/tests).
- **Проверки:** `ruff check` ✅, `ruff format --check` ✅, `mypy tg_crawler` ✅, `pytest` ✅ (142 passed),
  `docker build ./emulator` ✅.
- **Расхождения со спекой:**
  - JSON-лог пишется и при fail-rate срабатывании (в спеке «на каждый запрос одна строка»);
  - `EMULATOR_FAIL_RATE` читается из окружения на каждый запрос (удобно для тестов через monkeypatch);
  - исправлено по ходу TDD: статус 201 через `status_code=201`, fail-ветка возвращает явный
    `Response(status_code=500)`.

## T9. Publisher

- **Статус:** выполнено.
- **Файлы:** `tg_crawler/publisher.py`, `tests/test_publisher.py`.
- **Проверки:** `ruff check` ✅, `ruff format --check` ✅, `mypy tg_crawler` (strict) ✅, `pytest` ✅ (134 passed).
- **Расхождения со спекой:**
  - маппинг контракта вынесен в чистую функцию `build_payload(post)` — переиспользуется воркером
    при записи payload в outbox;
  - ERROR при REJECTED пишется стандартным `logging` (структурные JSON-логи — в T14).

## T8. Circuit breaker

- **Статус:** выполнено.
- **Файлы:** `tg_crawler/breaker.py`, `tests/test_breaker.py`.
- **Проверки:** `ruff check` ✅, `ruff format --check` ✅, `mypy tg_crawler` (strict) ✅, `pytest` ✅ (125 passed).
- **Расхождения со спекой / уточнения:**
  - порог оценивается только по заполненному окну (100 ответов): до заполнения окна breaker закрыт —
    одиночный NOT_FOUND сразу после старта не открывает breaker;
  - состояния: closed → open (пауза 5 мин) → trial (пробная партия ≤ 10) → closed при успехе,
    bad-исход в trial снова открывает breaker;
  - успешная пробная партия очищает окно (старые ложные NOT_FOUND не переоткрывают breaker).

## T7. Планирование

- **Статус:** выполнено.
- **Файлы:** `tg_crawler/scheduling.py`, `tests/test_scheduling.py`.
- **Проверки:** `ruff check` ✅, `ruff format --check` ✅, `mypy tg_crawler` (strict) ✅, `pytest` ✅ (118 passed).
- **Расхождения со спекой / уточнения:**
  - `next_check_after_ok` возвращает `datetime` (now + delay); интервалы: <24 ч → uniform(15–30 мин),
    1–7 дней → 2 ч ±10%, >7 дней / нет постов → uniform(6–12 ч);
  - наивный `last_post_at` трактуется как UTC;
  - база экспоненты `error_backoff` = 5 мин (в спеке база не задана), потолок 6 ч — детерминированная,
    без rnd.

## T6. Fetcher

- **Статус:** выполнено.
- **Файлы:** `tg_crawler/fetcher.py`, `tests/test_fetcher.py`.
- **Проверки:** `ruff check` ✅, `ruff format --check` ✅, `mypy tg_crawler` (strict) ✅, `pytest` ✅ (110 passed).
- **Расхождения со спекой:**
  - `Fetcher` — класс с методом `fetch(client, name) -> FetchResponse` (клиент берётся из lease
    ProxyPool, таймауты/заголовки применяются на запрос);
  - `302` в `NOT_FOUND` только при `location` = `/{name}`; прочие 3xx → `RETRYABLE`;
  - невалидный `Retry-After` → `retry_after=None`;
  - `FetchResponse` определён в `fetcher.py` (fetcher-специфичный тип).

## T5. ProxyPool

- **Статус:** выполнено.
- **Файлы:** `tg_crawler/proxy_pool.py`, `tests/test_proxy_pool.py`.
- **Проверки:** `ruff check` ✅, `ruff format --check` ✅, `mypy tg_crawler` (strict) ✅, `pytest` ✅ (98 passed).
- **Расхождения со спекой / уточнения:**
  - `acquire()` возвращает `ProxyLease` (id + клиент), `release(lease, outcome, retry_after=None)`;
  - шаг AIMD (аддитивное уменьшение) = 0.5 с на успех, не ниже базового `min_interval`;
  - экспонента cooldown: 30·2^n с jitter ±20% (инжектируемый `random.Random`), потолок 15 мин;
  - success-исходы для прокси: `OK`, `NOT_FOUND`, `LAYOUT_CHANGED`, `INVALID_NAME` (прокси не виноват);
  - при пустом списке прокси — один прямой клиент с id `direct`;
  - перепроверка dead-прокси: `has_alive()` снова True, когда наступило `next_recheck` (иначе Feeder
    с одним dead-прокси встал бы навсегда), сам проверочный запрос делает `acquire()`;
  - два теста писались с ошибками в самом тесте (неосвобождённый lease, сброс экспоненты успешным
    release) — исправлены тесты, не код.

## T4. Парсер

- **Статус:** выполнено.
- **Файлы:** `tg_crawler/parser.py`, `tests/test_parser.py` (фикстуры созданы ранее в T0).
- **Проверки:** `ruff check` ✅, `ruff format --check` ✅, `mypy tg_crawler` (strict) ✅, `pytest` ✅ (86 passed).
- **Расхождения со спекой:** нет. Селекторы v1 §3 совпали с фикстурами, исправлений селекторов не потребовалось.
  `media_not_supported` проверяется inline-HTML (отдельная фикстура не создавалась).
  Реальные запросы к `t.me` не выполнялись — фикстуры синтетические (решение пользователя).

## T3. Модель БД и миграции

- **Статус:** выполнено.
- **Файлы:** `tg_crawler/db.py`, `alembic.ini`, `alembic/env.py`,
  `alembic/versions/0001_channels.py`, `alembic/versions/0002_pending_posts.py`,
  `tests/integration/test_db.py`.
- **Проверки:** `ruff check` ✅, `ruff format --check` ✅, `mypy tg_crawler` (strict) ✅,
  `pytest` ✅ (75 passed, включая 7 интеграционных на testcontainers: upgrade/downgrade,
  уникальность `channel_id`, NULL-значения, BIGINT без переполнения, дедуп outbox, дефолты статуса).
- **Расхождения со спекой:**
  - `alembic.command` синхронный и вызывает `asyncio.run` внутри — тест downgrade сделан синхронным,
    проверки через `asyncio.run`;
  - DeprecationWarning `testcontainers.postgres → testcontainers.community.postgres`
    (в v4 API работает, оставлено как есть до обновления testcontainers).

## T2. Валидация и определение имени канала

- **Статус:** выполнено.
- **Файлы:** `tg_crawler/names.py`, `tests/test_names.py`.
- **Проверки:** `ruff check` ✅, `ruff format --check` ✅, `mypy tg_crawler` (strict) ✅, `pytest` ✅ (68 passed).
- **Расхождения со спекой:** нет. TDD: тесты написаны первыми, RED подтверждён; один тест изначально
  содержал ошибку в ожидании (28 символов проходит regex `{3,31}`) — исправлен тест, не код.

## T1. Конфигурация, модели, протоколы

- **Статус:** выполнено.
- **Файлы:** `tg_crawler/config.py`, `tg_crawler/models.py`, `tg_crawler/protocols.py`,
  `tests/test_config.py`, `tests/test_models.py`.
- **Проверки:** `ruff check` ✅, `ruff format --check` ✅, `mypy tg_crawler` (strict) ✅, `pytest` ✅ (36 passed).
- **Расхождения со спекой:**
  - `proxies` размечен `Annotated[list[str], NoDecode]`, иначе pydantic-settings пытается JSON-парсить строку
    до валидатора;
  - `workers: int | None = None`, дефолт `max(1, len(proxies)) * 2` считается в `model_validator`
    (дефолт по другим полям в pydantic-settings недоступен на этапе полей);
  - `Outcome`/`PublishStatus` — `StrEnum` (требование ruff UP042, py3.11);
  - маскировка пароля — функция `mask_proxy_url()` в `config.py`.

## T0. Скелет проекта

- **Статус:** выполнено.
- **Файлы:** `pyproject.toml`, `tg_crawler/*` (20 модулей-заглушек), `tests/conftest.py`, `tests/test_skeleton.py`,
  `Dockerfile`, `.dockerignore`, `.env.example`, `.gitignore`.
- **Проверки:** `pip install -e .[dev]` ✅, `ruff check` ✅, `ruff format --check` ✅, `mypy tg_crawler` ✅
  (strict), `pytest` ✅ (19 passed), `docker build .` ✅.
- **Расхождения со спекой:**
  - `docs/` исключён из `ruff format` (иначе форматтер переписывает python-блоки в markdown-доках);
  - добавлен `tests/test_skeleton.py` (импорт всех модулей), чтобы `pytest` был зелёным без тестов;
  - синтетические фикстуры парсера для T4 созданы заранее в `tests/fixtures/` (7 файлов,
    реальные запросы к `t.me` не выполнялись).

## Ожидает решения

- Рабочий прокси для реального прогона smoke-теста T17 и ручной проверки T18
  (`t.me` заблокирован в сети — фикстуры T4 заменены синтетикой, реальный прогон отложен).
