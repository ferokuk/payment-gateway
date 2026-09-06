# Payment Gateway

Платёжный шлюз с REST API для создания платежей и частичных возвратов.
Поддерживает идемпотентные операции, обработку callbacks от платёжных провайдеров
и фоновую сверку статусов.

**Стек:** Python 3.14, FastAPI, PostgreSQL, SQLAlchemy, Alembic, Dishka.

## Целевая архитектура

[![Архитектура Payment Gateway](assets/payment-gateway.svg)](assets/payment-gateway.svg)

## Запуск

Требования: Docker Compose. Команды — для PowerShell.

```powershell
if (!(Test-Path .env)) { Copy-Item .env.example .env }
docker compose up --build -d
```

API: `http://localhost:8000`. PostgreSQL: `localhost:5432`.
Конфигурация: [.env.example](.env.example).
Тестовый провайдер: `provider_id=1`; автоматические callbacks: `FAKE_PROVIDER_MODE=auto`.

## API

[Swagger](http://localhost:8000/docs) · [OpenAPI](http://localhost:8000/openapi.json)

| Метод | Эндпоинт | Назначение |
| --- | --- | --- |
| POST | `/payments` | Создание платежа |
| GET | `/payments/{payment_id}` | Статус платежа |
| POST | `/payments/{payment_id}/refunds` | Создание возврата |
| GET | `/payments/{payment_id}/refunds` | Возвраты платежа |
| GET | `/refunds/{refund_id}` | Статус возврата |
| POST | `/callbacks/payments` | Обработка результата платежа |
| POST | `/callbacks/refunds` | Обработка результата возврата |
| GET | `/health` | Проверка доступности сервиса и БД |

Авторизация: `X-API-Key` для клиентских запросов, `X-Callback-Secret` для callbacks.
Идемпотентность создания платежей и возвратов: `Idempotency-Key`.

Каждый API-ключ принадлежит мерчанту. Платежи, возвраты и ключи идемпотентности
изолированы по мерчанту; чужой ID возвращает тот же 404, что отсутствующий.
Одинаковый `Idempotency-Key` у разных мерчантов независим. Несколько API-ключей
одного мерчанта используют общую историю идемпотентности, в том числе после ротации.

## Мерчанты и API-ключи

После миграций создайте мерчанта и выдайте ключ через операторский CLI:

```powershell
docker compose exec app python -m src.contexts.merchants create --name "My shop"
docker compose exec app python -m src.contexts.merchants issue-key <merchant-uuid> --label "Backend"
```

Первая команда выводит UUID мерчанта, вторая — API-ключ **один раз** после фиксации
в БД. Сохраните его в хранилище секретов и передавайте в `X-API-Key`. В БД хранится
только SHA-256 от ключа; новый ключ содержит 256 случайных бит. Для notebook
передайте выданный ключ в `GatewayConfig(api_key=...)`.

```powershell
docker compose exec app python -m src.contexts.merchants issue-key <merchant-uuid> --label "Rotation" --expires-at "2027-01-01T00:00:00+00:00"
docker compose exec app python -m src.contexts.merchants revoke-key <key-uuid>
docker compose exec app python -m src.contexts.merchants deactivate <merchant-uuid>
docker compose exec app python -m src.contexts.merchants activate <merchant-uuid>
```

UUID ключа — часть между `pg_` и точкой. Ротация: выдать новый ключ, переключить
клиента, отозвать прежний. Отзыв необратим; просроченные/отозванные ключи и ключи
неактивного мерчанта дают 401 на следующем запросе. Уже принятые операции
продолжают обрабатываться callbacks и reconciler. CLI требует привилегированного
доступа к БД; публичного API управления мерчантами нет. Не сохраняйте вывод выдачи
ключей в общедоступных логах; внешний HTTP-доступ должен проходить через TLS.

## Миграции

Миграции применяются при запуске Compose. Отдельный запуск:

```powershell
docker compose build migrate
docker compose run --rm migrate
```

При переходе с глобального ключа остановите API и reconciler, сделайте резервную
копию, соберите новый образ и примените миграции. Существующие данные получают
владельца `00000000-0000-4000-8000-000000000001` (legacy merchant), сохраняя суммы,
статусы и снимки идемпотентности. До запуска новых процессов импортируйте прежний
ключ через скрытый интерактивный ввод:

```powershell
docker compose stop app reconciler
docker compose build
docker compose run --rm migrate
docker compose run --rm --no-deps app python -m src.contexts.merchants import-legacy-key
docker compose up -d app reconciler
```

Импорт разрешён один раз и создаёт обычную отзываемую credential-запись для
legacy merchant. Переменная `API_KEY` больше не авторизует запросы. После импорта
прежние клиенты могут использовать свой ключ без изменений; затем его следует
ротировать. Одновременная работа старого и нового кода не поддерживается.
Downgrade запрещён при новых мерчантах/ключах, деактивации, отзыве или сроке
действия legacy-ключа: старый код не умеет сохранять эти ограничения доступа.
Подробности и границы безопасности:
[merchant isolation design spec](docs/architecture/merchant-isolation.md).

## Разработка

Требования: Python 3.14+ и [uv](https://docs.astral.sh/uv/).

```powershell
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src tests
uv run pytest tests/unit
```

Полный набор тестов использует отдельную БД `payment_gateway_test` и пересоздаёт её таблицы.

```powershell
docker compose exec database createdb -U postgres payment_gateway_test
$env:TEST_DATABASE_URL = 'postgresql+asyncpg://postgres:postgres@localhost:5432/payment_gateway_test'
uv run pytest
```
