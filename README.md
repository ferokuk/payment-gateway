# Payment Gateway

Бэкенд платёжного шлюза: платежи, частичные возвраты, идемпотентность,
callbacks и сверка статусов. **Merchant Service** отвечает за регистрацию,
авторизацию, личный кабинет через REST API, ключи и настройки мерчантов.

Стек: Python 3.14, FastAPI, PostgreSQL, SQLAlchemy, Alembic, Dishka.

## Архитектура

[![Архитектура Payment Gateway](assets/payment-gateway.svg)](assets/payment-gateway.svg)

- Платёжное API — `src/main.py`, порт **8000**.
- Merchant Service — `src/contexts/merchants/main.py`, отдельный процесс, порт **8001**.
- Платёжное API проверяет ключи через внутреннее HTTP API Merchant Service.
- Reconciler продолжает обработку уже принятых возвратов независимо от доступности кабинета.

В текущем Compose сервисы используют существующую PostgreSQL БД: это сохраняет
данные и внешние ключи платежей. Таблицами учётных записей управляет Merchant;
платёжное API не обращается к ним для авторизации. Детали и границы реализации —
[Merchant Service](docs/architecture/merchant-service.md).

## Запуск

Требования: Docker Compose. Для настройки секретов и локальной разработки —
Python 3.14+ и [uv](https://docs.astral.sh/uv/). Команды для PowerShell:

```powershell
if (!(Test-Path .env)) { Copy-Item .env.example .env }
uv sync --frozen
```

Один раз сгенерируйте секреты в `.env`. Команда заполняет только отсутствующие
или пустые значения и не выводит секреты в терминал:

```powershell
@'
import re
import secrets
from pathlib import Path
from cryptography.fernet import Fernet

path = Path('.env')
content = path.read_text(encoding='utf-8')
for name in ('MERCHANT_ENCRYPTION_KEY', 'MERCHANT_SERVICE_SECRET', 'MERCHANT_SUPPORT_SECRET'):
    value = Fernet.generate_key().decode() if name.endswith('ENCRYPTION_KEY') else secrets.token_urlsafe(32)
    pattern = rf'^{name}=[ \t]*$'
    if re.search(pattern, content, re.MULTILINE):
        content = re.sub(pattern, f'{name}={value}', content, flags=re.MULTILINE)
    elif not re.search(rf'^{name}=', content, re.MULTILINE):
        content += f'\n{name}={value}\n'
path.write_text(content, encoding='utf-8')
'@ | uv run python -

docker compose up --build -d
```

Сохраните `MERCHANT_ENCRYPTION_KEY` в хранилище секретов: он нужен для расшифровки
ключей после перезапуска. `MERCHANT_SERVICE_SECRET` используют внутренние сервисы,
`MERCHANT_SUPPORT_SECRET` — только поддержка. Эти значения не передают мерчантам.
Конфигурация: [.env.example](.env.example).

Swagger: [платежи](http://localhost:8000/docs) · [мерчанты](http://localhost:8001/docs).
PostgreSQL: `localhost:5432`. Тестовый платёжный провайдер: `provider_id=1`;
автоматические callbacks: `FAKE_PROVIDER_MODE=auto`.

## Регистрация и личный кабинет

Кабинет представлен REST API. Вход — по почте и паролю; ИНН/ОГРН не требуются.

| Метод | Эндпоинт Merchant Service | Назначение |
| --- | --- | --- |
| POST | `/merchants` | Регистрация и выдача API-ключа |
| POST | `/auth/token` | Вход по почте и паролю |
| GET | `/profile` | Профиль, собственный API-ключ и настройки |
| PATCH | `/profile` | Настройки webhook, повторных уведомлений, имени и провайдера |
| PATCH | `/profile/secrets` | Ротация API-ключа, ключа провайдера или пароля |
| DELETE | `/profile?merchant_id=UUID` | Мягкое удаление через поддержку |

Пример тела `POST /merchants`:

```json
{
  "email": "owner@example.com",
  "password": "Replace-with-your-own-password",
  "name": "My shop",
  "provider_name": "my-provider",
  "provider_secret_key": "provider-issued-secret"
}
```

Ответ содержит UUID мерчанта, API-ключ и профиль. Для входа отправьте
`{"email":"owner@example.com","password":"..."}` в `POST /auth/token`.
Полученный `access_token` передавайте в `Authorization: Bearer ...` при работе
с профилем. Сессия действует один час по умолчанию; `expires_in` возвращается
в ответе. API-ключ для платежей и bearer-токен кабинета имеют разное назначение.

`PATCH /profile` принимает только изменяемые поля, например:

```json
{
  "webhook_url": "https://shop.example.com/payment-events",
  "retry_max_attempts": 5,
  "retry_window_seconds": 300
}
```

`webhook_url: null` удаляет адрес. Для смены провайдера передайте вместе
`provider_name` и `provider_secret_key`; прежний секрет не применяется к новому
провайдеру автоматически. Merchant хранит и отдаёт настройки внутренним сервисам.
Доставка webhook и реальные адаптеры провайдеров — отдельные компоненты на схеме;
текущие fake-провайдеры платежей продолжают работать как прежде.

`PATCH /profile/secrets` поддерживает `rotate_api_key: true`, новый
`provider_secret_key` и/или новый `password`. Старые API-ключи и версии ключей
провайдера сохраняют совместимость **24 часа** после ротации. Повторная ротация
не сокращает срок более ранних ключей. Смена пароля отзывает сессии кабинета.

Профиль показывает текущий API-ключ, но не пароли или секреты провайдера.
Пароли хранятся как Argon2id-хеши, восстанавливаемые ключи — в зашифрованном виде.

Удаление выполняет поддержка с `X-Support-Secret`. Обычный мерчант не может
удалить аккаунт этим запросом. Удаление блокирует новые обращения и отзывает
ключи/сессии, сохраняя финансовую историю. Принятые платежи и возвраты могут
завершаться через callbacks и reconciler.

## Платёжное API

| Метод | Эндпоинт | Назначение |
| --- | --- | --- |
| POST | `/payments` | Создание платежа |
| GET | `/payments/{payment_id}` | Статус платежа |
| POST | `/payments/{payment_id}/refunds` | Создание возврата |
| GET | `/payments/{payment_id}/refunds` | Возвраты платежа |
| GET | `/refunds/{refund_id}` | Статус возврата |
| POST | `/callbacks/payments` | Результат платежа от провайдера |
| POST | `/callbacks/refunds` | Результат возврата от провайдера |
| GET | `/health` | Доступность сервиса и БД |

Клиентские запросы используют `X-API-Key`, callbacks — `X-Callback-Secret`.
Идемпотентность создания платежей/возвратов — `Idempotency-Key`.
Операции изолированы по мерчанту: чужой ID даёт 404, одинаковые ключи
идемпотентности у разных мерчантов независимы, ротация сохраняет историю повторов.
Если Merchant Service недоступен, новые клиентские запросы возвращают 503.

## Внутреннее API

Оба эндпоинта требуют `X-Service-Secret`:

- `POST /internal/authenticate`, тело `{"api_key":"..."}` — UUID мерчанта и ключа.
- `GET /internal/merchants/{merchant_id}/configuration` — настройки уведомлений
  и текущие/ещё действующие предыдущие ключи провайдера для доверенных адаптеров.

## Миграции существующей установки

Перед обновлением остановите API и workers, сделайте резервную копию и
настройте новые секреты. Затем:

```powershell
docker compose stop app reconciler merchants
docker compose build
docker compose run --rm migrate
docker compose up -d merchants app reconciler
```

Существующие финансовые данные и ранее выданные API-ключи сохраняются.
При переходе со старого глобального `API_KEY` **до запуска API** импортируйте его
один раз скрытым вводом:

```powershell
docker compose run --rm --no-deps app python -m src.contexts.merchants import-legacy-key
```

Импорт относится к legacy merchant `00000000-0000-4000-8000-000000000001`.
`API_KEY` из окружения сам по себе больше не авторизует запросы.
Legacy-ключи не превращаются автоматически в аккаунты с почтой/паролем.
Операторский CLI остаётся инструментом сопровождения и импорта.

Откат миграции профилей запрещён, если уже есть аккаунты, сессии, секреты
провайдеров или мягкое удаление. Более ранний откат также запрещает потерю
мерчантов и ограничений ключей. Миграции требуют остановки старых процессов.

## Разработка и проверки

```powershell
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src tests notebooks/gateway_client.py
uv run pytest tests/unit
```

Локальный отдельный запуск Merchant после настройки `.env` и миграций:

```powershell
$env:DATABASE_URL = 'postgresql+asyncpg://postgres:postgres@localhost:5432/payment_gateway'
uv run uvicorn src.contexts.merchants.main:create_app --factory --port 8001
```

Полные тесты используют **отдельную** БД и пересоздают её таблицы:

```powershell
docker compose exec database createdb -U postgres payment_gateway_test
$env:TEST_DATABASE_URL = 'postgresql+asyncpg://postgres:postgres@localhost:5432/payment_gateway_test'
uv run pytest -q -p no:cacheprovider
```
