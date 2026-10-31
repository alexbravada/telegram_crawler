# tg_crawler: план реализации для агента

Спецификация: `docs/ARCHITECTURE.md`. При расхождении плана и спецификации
следовать спецификации и сообщить о расхождении.

## Правила работы

1. Выполнять задачи строго по порядку. Следующая задача начинается только после того,
   как выполнены все критерии приёмки текущей.
2. После каждой задачи запускать `ruff check`, `ruff format --check`, `mypy tg_crawler`, `pytest`.
   Все проверки должны проходить.
3. Не добавлять функциональность сверх описанной. Не скачивать медиа, не писать реализацию
   Sink, кроме `LoggingSink`, не делать эндпоинты FastAPI.
4. Не применять миграции к общей БД и не удалять данные. Интеграционные тесты работают
   только с временной БД из testcontainers.
5. Секреты (DSN, логины прокси) только через переменные окружения. В логах маскировать
   пароли в URL прокси.
6. Реальные запросы к `t.me` разрешены только в задаче T4 (сбор фикстур, не больше 10 запросов,
   пауза от 5 с) и T14. В остальных тестах HTTP мокается через `respx`.
7. Время в коде, которое влияет на логику (cooldown, `ready_at`, breaker), брать через
   инжектируемые часы (`Callable[[], float]`, по умолчанию `time.monotonic`), чтобы тесты
   не использовали `sleep`.
8. Если требование неоднозначно, остановиться и задать вопрос, а не угадывать.
9. В конце каждой задачи коротко отчитаться: что сделано, какие файлы изменены, результаты проверок.

## Стек

- Python 3.11+ (нужен `asyncio.TaskGroup`)
- `httpx[socks]`, `beautifulsoup4`, `lxml`
- `sqlalchemy[asyncio]` 2.0, `asyncpg`
- `pydantic-settings`
- `structlog` (или стандартный `logging` с JSON-форматтером, если в проекте уже принят он)
- Dev: `pytest`, `pytest-asyncio`, `respx`, `testcontainers[postgres]`, `ruff`, `mypy`

---

## T0. Скелет проекта

**Цель:** пустой пакет, который собирается и проходит проверки.

**Файлы:** `pyproject.toml`, `tg_crawler/__init__.py`, пустые модули из раздела «Структура»,
`tests/conftest.py`.

**Требования:**
- Зависимости из раздела «Стек».
- Настройки `ruff`, `mypy` (strict для `tg_crawler`), `pytest` (`asyncio_mode = "auto"`).

**Приёмка:** `pip install -e .[dev]` работает; все проверки проходят на пустом проекте.

## T1. Конфигурация, модели, протоколы

**Файлы:** `config.py`, `models.py`, `protocols.py`.

**Требования:**
- `Settings` по разделу 12 спецификации, префикс `TG_CRAWLER_`, `proxies` парсится из
  строки через запятую.
- Dataclass-модели из раздела 5.2, enum `Outcome`
  (`OK, NOT_FOUND, RATE_LIMITED, RETRYABLE, LAYOUT_CHANGED, INVALID_NAME`).
- `Sink` Protocol и `LoggingSink`, который пишет пост в лог.
- Функция маскировки пароля в URL прокси.

**Приёмка:** тесты на парсинг `Settings` из окружения и на маскировку URL.

## T2. Валидация имён каналов

**Файлы:** `names.py`, `tests/test_names.py`.

**Требования:** `normalize()` из раздела 7.

**Приёмка:** параметризованные тесты: `@name`, `t.me/name`, `https://t.me/s/name/123`,
`telegram.me/name`, пробелы по краям → `name`; слишком короткое, начинается с цифры,
недопустимые символы, пустая строка → `None`.

## T3. Модель БД и миграция

**Файлы:** `db.py`; миграция (см. требования).

**Требования:**
- Модель `Channel` из раздела 5.1, `DeclarativeBase`.
- Фабрики `create_engine(settings)` и `async_sessionmaker`.
- Если в репозитории есть Alembic, создать ревизию. Иначе положить DDL в
  `migrations/001_channels.sql`. Миграцию не применять.

**Приёмка:** интеграционный тест через testcontainers: таблица создаётся по метаданным,
индекс `channels_due_idx` существует и частичный.

## T4. Парсер

**Файлы:** `parser.py`, `tests/fixtures/*.html`, `tests/test_parser.py`.

**Требования:**
- `parse_latest()` и `_extract_media()` по разделам 3 и 8.
- Исключение `LayoutChanged`.
- Фикстуры: сохранить 4–6 реальных страниц `t.me/s/<channel>` (канал с текстом, с фото,
  с альбомом, с видео, пустой или со служебным последним сообщением). Сохранить и
  синтетическую страницу без шапки. Список каналов для фикстур запросить у пользователя.

**Приёмка:**
- Для каждой фикстуры проверены `title`, `post_id` (максимальный), текст, число и тип медиа.
- Служебное сообщение пропускается.
- Пустой канал → `post=None`, `title` заполнен.
- Страница без шапки → `LayoutChanged`.
- Если реальная вёрстка расходится с селекторами из спецификации, исправить парсер
  и сообщить, какие селекторы изменились.

## T5. ProxyPool

**Файлы:** `proxy_pool.py`, `tests/test_proxy_pool.py`.

**Требования:** раздел 6 спецификации полностью:
- клиент на прокси, режим без прокси;
- куча по `ready_at`, `acquire()` / `release(proxy, outcome, retry_after=None)`;
- cooldown по `Retry-After` или экспонента с jitter, потолок 15 мин;
- AIMD для `min_interval`;
- `dead` после K ошибок, перепроверка раз в M минут;
- `health_check()` при старте;
- `has_alive()` для паузы Feeder;
- `stats()`;
- `aclose()` закрывает все клиенты.

**Приёмка (с фейковыми часами):**
- Один прокси не выдаётся чаще `min_interval`.
- При трёх прокси `acquire()` чередует их.
- После 429 с `Retry-After: 60` прокси не выдаётся 60 с, `min_interval` вырос.
- После серии успехов `min_interval` возвращается к базовому, но не ниже.
- После K ошибок прокси `dead`, `has_alive()` = False при единственном прокси.
- Пустой список прокси → один прямой клиент.

## T6. Fetcher

**Файлы:** `fetcher.py`, `tests/test_fetcher.py`.

**Требования:**
- Одна попытка запроса к `https://t.me/s/<name>`, `follow_redirects=False`, таймауты и
  заголовки из раздела 7.
- Классификация по таблице раздела 7. `LAYOUT_CHANGED` здесь не определяется (это делает парсер).
- Возвращает `FetchResponse(outcome, html, retry_after, error)`. Исключения httpx не выпускать наружу.

**Приёмка (`respx`):** 200 → `OK`; 302 на `/<name>` → `NOT_FOUND`; 429 с заголовком и без →
`RATE_LIMITED` с корректным `retry_after`; 502 → `RETRYABLE`; `ConnectTimeout`,
`ConnectError`, `ReadTimeout` → `RETRYABLE`.

## T7. Планирование

**Файлы:** `scheduling.py`, `tests/test_scheduling.py`.

**Требования:** раздел 9: `next_check_after_ok(last_post_at, now)`,
`retry_delay()`, `error_backoff(fail_count)`, `not_found_delay()`. Jitter через
инжектируемый `random.Random`.

**Приёмка:** интервалы попадают в диапазоны таблицы с учётом jitter;
`error_backoff` растёт и не превышает 6 ч.

## T8. Circuit breaker

**Файлы:** `breaker.py`, `tests/test_breaker.py`.

**Требования:** раздел 10.7: окно, порог, пауза, пробная партия; методы
`record(outcome)`, `is_open()`, `allow_claim(n) -> int` (сколько каналов можно захватить).

**Приёмка (фейковые часы):** порог не превышен → закрыт; превышен → открыт, `allow_claim` = 0
до конца паузы; после паузы разрешается пробная партия; нормальная пробная партия закрывает
breaker, плохая снова открывает.

## T9. Репозиторий и Feeder

**Файлы:** `repository.py`, `feeder.py`, `tests/integration/test_feeder.py`.

**Требования:**
- `claim(batch, lease) -> list[ChannelTask]` по разделу 10.1.
- `release(tasks)` по разделу 10.3, шаг 2 (только при совпадении `lease_token`).
- Каналы с невалидным именем: при захвате проверить `normalize()`, для невалидных
  сразу сформировать результат `INVALID_NAME` для Writer, в очередь задач не класть.
- Feeder: цикл опроса, ограниченная очередь, пауза если `not pool.has_alive()` или
  breaker открыт, учёт `allow_claim`.
- Метод `stop()` останавливает захват и возвращает в БД задачи, оставшиеся в очереди.

**Приёмка (testcontainers):**
- Захватываются только `is_active AND NOT broken_url AND next_check_at <= now()`.
- Два параллельных `claim` не возвращают один канал дважды.
- После захвата `next_check_at` сдвинут на lease, `lease_token` выставлен.
- `release` с чужим токеном строку не меняет.
- `stop()` возвращает незапущенные задачи: у них `next_check_at <= now()`, `lease_token IS NULL`.

## T10. Writer

**Файлы:** `writer.py`, `tests/integration/test_writer.py`.

**Требования:** разделы 10.2, 10.4–10.6, 11:
- группировка по исходам, Core `executemany`, проверка `lease_token`;
- логика `not_found`: интервал от `last_not_found_at`, отличие прокси от `last_not_found_proxy`
  (если прокси больше одного), порог 3 → `broken_url`;
- если breaker открыт, счётчики `not_found_count` и `fail_count` не увеличиваются;
- `last_post_id` через `GREATEST`;
- повтор записи с backoff при ошибке БД, буфер в памяти;
- `flush()` для остановки.

**Приёмка (testcontainers):**
- `OK` обновляет `title`, `last_post_id`, сбрасывает счётчики и `lease_token`.
- `OK` с меньшим `post_id` не уменьшает `last_post_id`.
- Результат с устаревшим токеном игнорируется.
- Три `NOT_FOUND` с нужными интервалами и разными прокси → `broken_url=true`.
- `NOT_FOUND` раньше чем через 6 ч или через тот же прокси не засчитывается.
- `OK` между `NOT_FOUND` сбрасывает счётчик.
- `INVALID_NAME` → `broken_url=true`, `broken_reason=invalid_name`.
- Временная недоступность БД (симулировать исключением) не теряет результаты.

## T11. Worker и CrawlerService

**Файлы:** `worker.py`, `service.py`, `tests/test_worker.py`.

**Требования:**
- Цикл воркера: semaphore → `acquire` → fetch → `release` → парсинг в `to_thread` →
  `emit`, если пост новее `last_post_id` → результат в очередь Writer.
- Любое исключение в обработке задачи превращается в результат `RETRYABLE`, воркер не падает.
- Исход передаётся в breaker.
- `CrawlerService.start()` / `stop()`: `TaskGroup` (Feeder, воркеры, Writer, логирование статистики),
  порядок остановки по разделу 10.3, таймаут `shutdown_timeout`.

**Приёмка (`respx` + фейковые pool/repository):**
- Новый пост уходит в Sink ровно один раз; старый пост не уходит.
- Падение `emit` → результат повтора, `last_post_id` не обновляется.
- Исключение парсера не убивает воркер.
- `stop()` завершается за `shutdown_timeout` даже при зависшем запросе.

## T12. Entrypoint

**Файлы:** `__main__.py`.

**Требования:** `python -m tg_crawler` читает `Settings`, создаёт `LoggingSink`,
запускает `CrawlerService`, обрабатывает SIGTERM и SIGINT через корректную остановку.

**Приёмка:** локальный запуск с пустой таблицей стартует, пишет статистику в лог и
корректно завершается по Ctrl+C.

## T13. End-to-end тест

**Файлы:** `tests/integration/test_e2e.py`.

**Требования:** PG в testcontainers, 50 каналов, `t.me` замокан через `respx`
(часть каналов OK, часть 302, часть 429, часть с таймаутом).

**Приёмка:**
- За ограниченное время каждый канал проверен хотя бы раз.
- Новые посты отправлены в тестовый Sink, без потерь.
- Сценарий «остановка посреди цикла и повторный запуск»: ни один канал не остался
  с `lease_token` и будущим `next_check_at` дольше lease; все каналы снова проверены.
- Ни один канал не помечен `broken_url` по итогам одного прогона.

## T14. Ручной smoke-тест (выполняет пользователь или агент по согласованию)

- 50–100 реальных каналов, 0 прокси, `proxy_min_interval=5`.
- Наблюдать долю 429, корректность `title` и последних постов, работу сторожевого запроса.
- Затем 1 прокси, подбор `min_interval` по метрике 429.
- Результаты и найденные расхождения с вёрсткой записать в `docs/ARCHITECTURE.md`, раздел 15.

---

## Структура пакета

```
tg_crawler/
  __main__.py     # entrypoint, сигналы
  service.py      # CrawlerService, TaskGroup, порядок остановки
  worker.py       # цикл воркера
  feeder.py       # опрос due-каналов, пауза
  writer.py       # пакетные UPDATE с проверкой lease_token
  repository.py   # claim / release / SQL
  breaker.py      # circuit breaker
  proxy_pool.py   # ProxyPool, AIMD, cooldown, health-check
  fetcher.py      # httpx, классификация Outcome
  parser.py       # parse_latest, _extract_media
  scheduling.py   # next_check_at, backoff
  names.py        # normalize
  models.py       # dataclass-модели, Outcome
  db.py           # Channel, engine, sessionmaker
  protocols.py    # Sink, LoggingSink
  config.py       # Settings
tests/
  fixtures/
  integration/
```

