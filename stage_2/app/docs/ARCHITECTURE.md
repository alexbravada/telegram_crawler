# tg_crawler: архитектура

## 1. Назначение и границыы

Модуль периодически опрашивает публичные превью Telegram-каналов `https://t.me/s/<channel>`
и для каждого канала извлекает **только последний пост**:

> **Устарело (v2):** берутся все посты новее `last_post_id`; посты сохраняются в outbox до публикации.
> См. `docs/ARCHITECTURE_v2.md`, разделы 5 и 5.1.

- `title` — название канала;
- `post_id`;
- `text`;
- медиа (фото, видео): URL файлов и превью;
- дата поста и ссылка на пост.

Новый пост передаётся в `Sink` (интерфейс). Реализация Sink (очередь, HTTP и т.п.)
вне рамок модуля; в MVP используется Sink, который пишет в лог.

Файлы медиа модуль **не скачивает**, только собирает URL.

## 2. Ключевые решения

| Вопрос                                          | Решение                                                                                                                              |
| ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| HTTP-клиент                                     | `httpx.AsyncClient`, по одному клиенту на прокси                                                                   |
| Парсинг                                        | BeautifulSoup4 + `lxml`, в `asyncio.to_thread`                                                                                         |
| БД                                                  | PostgreSQL, SQLAlchemy 2.0 async, драйвер `asyncpg`                                                                                |
| Источник каналов и состояние | Одна таблица `channels`, ей владеет проект FastAPI                                                              |
| Запуск                                          | Отдельный процесс `python -m tg_crawler`, **одна реплика**                                               |
| Прокси                                          | Датацентровые, по умолчанию HTTP (CONNECT); SOCKS5 поддерживается через `httpx[socks]`         |
| Количество прокси                     | Тест: 0 или 1. Прод: от 5                                                                                                      |
| Режим без прокси                        | Пул из одного прямого клиента, код воркеров не меняется                                       |
| Гарантия доставки                     | At-least-once: дубли возможны, потери нет. Получатель удаляет дубли по `(channel, post_id)` |

Почему отдельный процесс: при `uvicorn --workers N` краулер внутри `lifespan` запустился бы
N раз, и у каждого был бы свой ProxyPool, то есть лимиты прокси нарушались бы в N раз.

## 3. Источник данных: страница `t.me/s/<channel>`

| Что                                      | Где в HTML                                                                                                    |
| ------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| Название канала               | `.tgme_channel_info_header_title`                                                                               |
| Пост                                    | `div.tgme_widget_message[data-post="channel/123"]`                                                              |
| Служебное сообщение       | у блока поста есть класс `service_message`, пропускаем                            |
| Текст                                  | `.tgme_widget_message_text`                                                                                     |
| Дата                                    | `time[datetime]` (ISO 8601)                                                                                     |
| Фото                                    | `a.tgme_widget_message_photo_wrap`, URL в `style="background-image:url('...')"`                              |
| Видео                                  | `video.tgme_widget_message_video[src]`; превью в `.tgme_widget_message_video_thumb` (URL в `style`) |
| Неподдерживаемое медиа | `.message_media_not_supported`                                                                                  |
| Альбом                                | несколько `photo_wrap` / видео внутри одного поста                               |

Правила:

- Страница отдаёт около 20 последних постов, порядок от старых к новым.
  Последний пост определяется по **максимальному `post_id`**, а не по позиции в DOM.
- Канала нет или превью закрыто: ответ `302` на `https://t.me/<channel>`.
  Запросы делаются с `follow_redirects=False`.
- Пустой канал: шапка есть, постов нет. Это нормальный ответ, не ошибка.
- Нет шапки канала в ответе 200: вёрстка изменилась или прокси подменил страницу.
- Большие видео на `/s/` могут не иметь `src`. Тогда сохраняется только превью (`url=None`).
- Ссылки CDN Telegram (`cdn*.telesco.pe`), вероятно, со временем перестают работать.
  Это нужно проверить; если подтвердится, медиа надо скачивать вскоре после парсинга (вне этого модуля).

## 4. Компоненты и поток данных

```
                 PostgreSQL (channels)
                   ▲              │
     batch UPDATE  │              │ claim: SELECT ... FOR UPDATE SKIP LOCKED
                   │              ▼
┌──────────────┐  results  ┌──────────────┐   tasks   ┌─────────────────┐
│   Writer     │◀──────────│  Workers × N │◀──────────│     Feeder      │
│ (батчами)    │  Queue    │              │  bounded  │ (выборка каналов│
└──────────────┘           │ acquire proxy│   Queue   │  по расписанию) │
                           │ fetch, parse │           └─────────────────┘
┌──────────────┐ новый пост│              │                  ▲
│ Sink         │◀──────────│ ProxyPool    │                  │ пауза
│ (Protocol)   │           └──────────────┘           ┌──────┴──────────┐
└──────────────┘                                      │ Circuit breaker │
                                                      └─────────────────┘
```

| Компонент | Ответственность                                                                                                                                                                                                                                                                                                                                                                              |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `CrawlerService` | Запуск Feeder, воркеров и Writer в `asyncio.TaskGroup`; порядок остановки                                                                                                                                                                                                                                                                                                 |
| `Feeder`         | Раз в несколько секунд захватывает пачку каналов, у которых `next_check_at <= now()`, кладёт в очередь ограниченного размера. Если очередь полна, ждёт (backpressure). Встаёт на паузу, если нет живых прокси или сработал предохранитель |
| `Worker`         | Берёт задачу, получает прокси, делает запрос, парсит, вызывает Sink, отдаёт результат Writer                                                                                                                                                                                                                                              |
| `ProxyPool`      | Выдаёт прокси с учётом темпа, cooldown после 429, AIMD, health-check                                                                                                                                                                                                                                                                                                           |
| `Fetcher`        | Одна попытка запроса и классификация ответа (`Outcome`)                                                                                                                                                                                                                                                                                                             |
| `Parser`         | Чистая функция `parse_latest(html, channel) -> PageResult` без ввода-вывода                                                                                                                                                                                                                                                                                                    |
| `Writer`         | Пакетная запись результатов в БД с проверкой `lease_token`                                                                                                                                                                                                                                                                                                          |
| `CircuitBreaker` | Защита от массовых ложных `NOT_FOUND` / `LayoutChanged`                                                                                                                                                                                                                                                                                                                           |
| `scheduling`     | Расчёт `next_check_at`: по активности канала и backoff                                                                                                                                                                                                                                                                                                                           |

Ретраи не делаются циклом внутри воркера. Неудачная задача возвращается в БД с новым
`next_check_at`, и следующая попытка пойдёт, скорее всего, через другой прокси.

## 5. Модель данных

### 5.1. Таблица `channels`

```python
class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    channel_name: Mapped[str] = mapped_column(Text, unique=True)  # username без @ и t.me/
    is_active: Mapped[bool] = mapped_column(default=True)
    broken_url: Mapped[bool] = mapped_column(default=False)
    broken_reason: Mapped[str | None]          # invalid_name | not_found
    title: Mapped[str | None]
    last_post_id: Mapped[int | None]
    last_post_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_check_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lease_token: Mapped[uuid.UUID | None]
    fail_count: Mapped[int] = mapped_column(SmallInteger, default=0)
    not_found_count: Mapped[int] = mapped_column(SmallInteger, default=0)
    last_not_found_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_not_found_proxy: Mapped[str | None]   # id прокси последнего 302
    last_error: Mapped[str | None]

    __table_args__ = (
        Index("channels_due_idx", "next_check_at",
              postgresql_where=text("is_active AND NOT broken_url")),
    )
```

| Колонка                                                       | Зачем                                                                                               |
| -------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `last_post_id`                                                     | Дедупликация: в Sink уходит только пост новее сохранённого |
| `next_check_at`                                                    | Расписание канала, отложенные повторы, lease                            |
| `lease_token`                                                      | Защита от записи запоздалого результата поверх свежего   |
| `not_found_count`, `last_not_found_at`, `last_not_found_proxy` | Защита от ложного `broken_url`                                                          |
| `fail_count`                                                       | Backoff для каналов, которые стабильно дают ошибку парсера    |

### 5.2. Внутренние модели (dataclass, `frozen=True, slots=True`)

```python
class Media:            type: Literal["photo", "video"]; url: str | None; thumb_url: str | None
class Post:             channel: str; post_id: int; title: str; text: str | None;
                        date: datetime | None; media: tuple[Media, ...];
                        media_not_supported: bool; url: str
class PageResult:       title: str; post: Post | None
class ChannelTask:      id: int; name: str; last_post_id: int | None; token: UUID
class Result:           task: ChannelTask; outcome: Outcome; page: PageResult | None;
                        proxy_id: str | None; error: str | None
```

### 5.3. Sink

```python
class Sink(Protocol):
    async def emit(self, post: Post) -> None: ...
```

## 6. ProxyPool

- На каждый прокси свой `httpx.AsyncClient` (keep-alive, свои лимиты соединений, свои cookies).
- Схема прокси берётся из URL:
  - `http://user:pass@[jg:ip_adress_1]:8080` — HTTP CONNECT (рекомендуется);
  - `socks5://user:pass@[jg:ip_adress_2]:1080` — нужен extra `httpx[socks]`.
- Используется параметр `proxy=` (httpx >= 0.26; `proxies=` удалён в 0.28).
- Пустой список прокси означает один прямой клиент.

Состояние прокси: `ready_at`, `min_interval`, `consecutive_errors`, `successes_since_slowdown`,
статус `healthy | cooldown | dead`.

| Событие             | Действие                                                                                                                                                                                                                                         |
| -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `acquire()`              | Из кучи по `ready_at` берётся прокси, который освободится раньше всех; при необходимости ждём до его `ready_at`. После выдачи `ready_at = now + min_interval` |
| Успех                 | `consecutive_errors = 0`; после серии успехов `min_interval` уменьшается на шаг, но не ниже базового (аддитивное уменьшение)                                                    |
| 429                        | Cooldown на `Retry-After`, если заголовок есть, иначе экспонента с jitter (30 → 60 → 120 с, потолок 15 мин); `min_interval *= 1.5` (мультипликативное увеличение)         |
| Timeout, ConnectError, 5xx | `consecutive_errors += 1`; после K подряд (по умолчанию 5) статус `dead`                                                                                                                                                 |
| `dead`                   | Раз в M минут (по умолчанию 10) проверочный запрос; при успехе снова `healthy`                                                                                                                      |
| Старт пула        | Health-check каждого прокси одним запросом                                                                                                                                                                                     |

Базовый `min_interval`: 4 с на прокси в проде, 5 с без прокси. Уменьшать только по метрикам 429.

Рекомендации по закупке: IP из разных подсетей /24; заранее проверить, что IP не забанены на `t.me`.

Ограничение нагрузки:

| Уровень       | Механизм                                                                                          |
| -------------------- | --------------------------------------------------------------------------------------------------------- |
| Глобальный | `asyncio.Semaphore(max_inflight)`                                                                       |
| На прокси    | `min_interval` + `ready_at`                                                                           |
| На канал      | Канал захвачен одним lease, в очереди не больше одной задачи |

Число воркеров: примерно `max(1, число_прокси) × 2`.

## 7. Fetcher и классификация ответов

Таймауты: `httpx.Timeout(connect=5, read=15, write=5, pool=5)`.
Заголовки как у браузера: `User-Agent`, `Accept`, `Accept-Language`.

| Ответ                                      | Outcome            | Действие с каналом                                                              | Действие с прокси |
| ----------------------------------------------- | ------------------ | ----------------------------------------------------------------------------------------------- | -------------------------------- |
| Имя не прошло валидацию     | `INVALID_NAME`   | Сразу `broken_url=true`, `broken_reason=invalid_name`, **без запроса** | —                               |
| 200, есть шапка                        | `OK`             | Парсим, сбрасываем `fail_count`, `not_found_count`                          | успех                       |
| 302 на `/<channel>`                         | `NOT_FOUND`      | См. раздел 10.6                                                                         | успех                       |
| 429                                             | `RATE_LIMITED`   | Повтор через 1–5 мин, `fail_count` не растёт                           | cooldown                         |
| 5xx, timeout, ошибка соединения | `RETRYABLE`      | Повтор через 1–5 мин, `fail_count` не растёт                           | ошибка                     |
| 200 без шапки (`LayoutChanged`)       | `LAYOUT_CHANGED` | `fail_count += 1`, backoff, лог                                                            | —                               |
| Прочие коды                           | `RETRYABLE`      | как выше                                                                                 | ошибка                     |

Валидация имени:

```python
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")

def normalize(raw: str) -> str | None:
    name = raw.strip().removeprefix("@")
    name = re.sub(r"^(https?://)?(t\.me|telegram\.me)/(s/)?", "", name).split("/")[0]
    return name if USERNAME_RE.match(name) else None
```

## 8. Parser

- `parse_latest(html: str, channel: str) -> PageResult`, чистая функция.
- Вызывается через `asyncio.to_thread`.
- Нет `.tgme_channel_info_header_title` → исключение `LayoutChanged`.
- Служебные сообщения пропускаются.
- Последний пост = максимальный `post_id` среди `data-post`.
- `text`: `get_text("\n", strip=True)`.
- `date`: `datetime.fromisoformat(time["datetime"])`.
- Медиа: все фото и видео поста (альбом), превью видео, флаг `media_not_supported`.
- Тесты на сохранённых HTML-фикстурах.

## 9. Планирование проверок (`next_check_at`)

Пропускная способность: `число_прокси / min_interval` запросов в секунду.

| Каналов | 0–1 прокси (5 с) | 5 прокси (4 с) |
| -------------- | ------------------------ | --------------------- |
| 100            | ~8 мин                | ~1,5 мин           |
| 1 000          | ~1,4 ч                  | ~13 мин            |
| 10 000         | ~14 ч                   | ~2,2 ч               |

Поэтому интервал проверки зависит от активности канала (`last_post_at`):

| Последний пост             | Следующая проверка |
| --------------------------------------- | ----------------------------------- |
| < 24 ч назад                      | 15–30 мин                       |
| 1–7 дней                           | 2 ч                                |
| > 7 дней или нет постов | 6–12 ч                            |

К каждому интервалу добавляется jitter ±10%, чтобы проверки не сбивались в пики.

Backoff при ошибках:

- `RATE_LIMITED`, `RETRYABLE`: 1–5 мин со случайным разбросом;
- `LAYOUT_CHANGED` и ошибки парсера: экспонента по `fail_count`, потолок 6 ч.

## 10. Защита от потерь каналов

«Потеря» означает одно из: канал навсегда выпал из обхода, канал ошибочно помечен `broken_url`,
пост не ушёл в Sink, состояние канала откатилось назад.

### 10.1. Падение процесса после захвата

Feeder при захвате ставит `next_check_at = now() + lease` (10 мин) и новый `lease_token`.
Если процесс упал, канал снова попадёт в выборку после истечения lease.

```sql
UPDATE channels SET next_check_at = now() + :lease, lease_token = :token
WHERE id IN (
    SELECT id FROM channels
    WHERE is_active AND NOT broken_url AND next_check_at <= now()
    ORDER BY next_check_at
    LIMIT :batch
    FOR UPDATE SKIP LOCKED
)
RETURNING id, channel_name, last_post_id;
```

Lease должен быть намного больше времени ожидания в очереди (очередь 50 задач при 1,25 запроса/с ≈ 40 с).

### 10.2. Запоздалый результат

Writer обновляет строку только при `WHERE id = :id AND lease_token = :token`.
`last_post_id` пишется через `GREATEST`, поэтому не может уменьшиться.

### 10.3. Штатная остановка (SIGTERM)

1. Feeder перестаёт захватывать каналы.
2. Задачи, которые лежат в очереди и не начаты, возвращаются в БД:
   `next_check_at = now()`, `lease_token = NULL` (только при совпадении токена).
3. Воркеры дорабатывают текущие запросы, таймаут 30 с. Отменённые задачи вернутся по истечении lease.
4. Writer записывает весь буфер.
5. Закрываются httpx-клиенты, `engine.dispose()`.

### 10.4. Пост не ушёл в Sink

`emit` вызывается до записи результата. Если `emit` упал, результат = повтор,
`last_post_id` не обновляется, пост будет отправлен снова.

### 10.5. БД недоступна при записи

Writer повторяет запись с backoff и держит буфер в памяти. Если процесс упадёт
с непустым буфером, lease истечёт и каналы проверятся заново. Худший исход: повторная отправка поста.

### 10.6. Ложный `broken_url`

- `broken_url=true`, `broken_reason=not_found` ставится только после **3 подряд** `NOT_FOUND`.
- Между засчитанными `NOT_FOUND` не меньше 6 ч (`last_not_found_at`). После `NOT_FOUND`
  следующая проверка назначается через 6 ч.
- Если прокси больше одного, засчитывается только `NOT_FOUND` через прокси,
  отличный от `last_not_found_proxy`. В режиме 0–1 прокси проверяется только интервал.
- Любой `OK` сбрасывает `not_found_count`.
- Строки не удаляются никогда; флаг снимается вручную через FastAPI.

### 10.7. Массовые ложные срабатывания (circuit breaker)

Скользящее окно последних 100 ответов. Если доля `NOT_FOUND` + `LAYOUT_CHANGED` больше 20%:

- `not_found_count` и `fail_count` не увеличиваются, `broken_url` не ставится;
- Feeder встаёт на паузу на 5 мин, в лог пишется ошибка уровня ERROR;
- после паузы пропускается небольшая пробная партия (10 каналов); если доля в норме, работа возобновляется.

### 10.8. Ошибки прокси не наказывают канал

429, таймауты и ошибки соединения засчитываются прокси. Канал откладывается на 1–5 мин
без роста `fail_count`. Если живых прокси нет, Feeder на паузе и не захватывает каналы.

### 10.9. Канал со стабильной ошибкой парсера

`fail_count` растёт, backoff до 6 ч. В `broken_url` автоматически **не** переводится.

### 10.10. Сторожевой запрос

```sql
SELECT count(*) FROM channels
WHERE is_active AND NOT broken_url
  AND (last_checked_at IS NULL OR last_checked_at < now() - interval '24 hours');
```

Растущее значение означает, что каналы выпадают из обхода или не хватает прокси.

## 11. Writer

- Буфер сбрасывается раз в 1–2 с или при 100 результатах.
- Результаты группируются по типу исхода: `ok`, `retry`, `not_found`, `broken`, `layout_error`.
  На каждую группу один `executemany`.
- Для `executemany` с собственным `WHERE` использовать Core-таблицу `Channel.__table__`
  и `bindparam`, а не ORM `update(Channel)`: ORM bulk update в SQLAlchemy 2.0 требует
  первичный ключ в параметрах и работает иначе.
- Отдельный `async_sessionmaker` / engine с пулом 5 соединений.

Пример для группы `ok`:

```python
t = Channel.__table__
OK_STMT = (
    update(t)
    .where(t.c.id == bindparam("b_id"), t.c.lease_token == bindparam("b_token"))
    .values(
        title=bindparam("b_title"),
        last_post_id=func.greatest(func.coalesce(t.c.last_post_id, 0), bindparam("b_post_id")),
        last_post_at=func.greatest(t.c.last_post_at, bindparam("b_post_at")),
        last_checked_at=func.now(),
        next_check_at=bindparam("b_next"),
        fail_count=0, not_found_count=0, lease_token=None, last_error=None,
    )
)
```

## 12. Конфигурация (pydantic-settings, префикс `TG_CRAWLER_`)

| Параметр                                               | По умолчанию                                  |
| -------------------------------------------------------------- | -------------------------------------------------------- |
| `database_url`                                               | обязателен, `postgresql+asyncpg://...`       |
| `proxies`                                                    | `[]`                                                   |
| `proxy_min_interval`                                         | 4.0 (без прокси рекомендуется 5.0) |
| `max_inflight`                                               | 10                                                       |
| `workers`                                                    | `max(1, len(proxies)) * 2`                             |
| `queue_size`                                                 | 50                                                       |
| `claim_batch`                                                | 20                                                       |
| `lease`                                                      | 10 мин                                                |
| `shutdown_timeout`                                           | 30 с                                                    |
| `writer_flush_interval` / `writer_batch`                   | 1.5 с / 100                                             |
| `breaker_window` / `breaker_threshold` / `breaker_pause` | 100 / 0.2 / 5 мин                                     |
| `not_found_threshold` / `not_found_min_gap`                | 3 / 6 ч                                                 |
| `proxy_dead_after` / `proxy_recheck`                       | 5 / 10 мин                                            |
| `stats_log_interval`                                         | 60 с                                                    |

## 13. Наблюдаемость

- Структурированные логи (JSON).
- Раз в `stats_log_interval` в лог пишется статистика: по каждому прокси (успехи, доля 429,
  средняя латентность, статус), размер очередей, число отправленных постов, состояние breaker.
- FastAPI получает состояние каналов запросами к таблице (включая сторожевой запрос 10.10).

## 14. Ограничения

- Одна реплика краулера. Две реплики потребуют разделения прокси между ними:
  `SKIP LOCKED` защищает только от дублей каналов, не от двойной нагрузки на прокси.
- Массовый сбор может противоречить условиям использования Telegram. Сдержанный темп
  и соблюдение `Retry-After` снижают этот риск.

## 15. Что проверить на практике

- Безопасный `min_interval` для датацентровых прокси (по метрике 429).
- Время жизни ссылок CDN на медиа.
- Актуальность селекторов из раздела 3 на реальных страницах.
