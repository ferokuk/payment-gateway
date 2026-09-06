# Payment Gateway

Учебный платёжный шлюз: FastAPI, PostgreSQL 17, SQLAlchemy async, Alembic и Dishka.
Поддерживает платежи, частичные возвраты, callbacks, ключи идемпотентности и
отдельный процесс сверки зависших возвратов. Сейчас подключён только fake-провайдер
с `provider_id=1`.

## Запуск

Нужны Docker Desktop / Docker Engine с Compose. Для запуска Python на хосте —
Python 3.14+ и `uv`. Команды ниже выполняются из корня репозитория.

```powershell
Copy-Item .env.example .env
docker compose up --build -d
docker compose logs -f app reconciler
```

Не перезаписывайте существующий `.env`. Compose запускает PostgreSQL на хостовом
порту **5433**, применяет миграции сервисом `migrate`, затем запускает API и reconciler.
API: [Swagger](http://localhost:8000/docs), [OpenAPI](http://localhost:8000/openapi.json),
[проверка БД](http://localhost:8000/health) (`{"status":"ok","db":1}`).

По умолчанию `FAKE_PROVIDER_MODE=manual`: результаты передаются вручную через callbacks.
Для автоматической демонстрации задайте в `.env` `FAKE_PROVIDER_MODE=auto` и пересоздайте
сервисы (`docker compose up -d`). Сценарий выбирается через `metadata.fake_scenario`:
`success` (по умолчанию), `timeout`, `error`; для платежей также `insufficient_funds`,
`fraud`, `limit_exceeded`, для возвратов — `card_unavailable`,
`insufficient_merchant_balance`. `initiation_error` и неизвестные сценарии дают 502.

Запуск на хосте, вместо Compose-сервисов API/reconciler:

```powershell
uv sync --frozen
docker compose up -d database
$env:DATABASE_URL = 'postgresql+asyncpg://postgres:postgres@localhost:5433/payment_gateway'
uv run alembic upgrade head
uv run uvicorn src.main:app --host 127.0.0.1 --port 8000
# В другом терминале с тем же DATABASE_URL:
uv run python -m src.reconciler
```

Переменные окружения имеют приоритет над `.env`. Подставьте свои реквизиты БД,
если меняли пример. `SELF_BASE_URL` должен вести к API: на хосте это
`http://localhost:8000`, внутри Compose для reconciler уже задан `http://app:8000`.
`API_KEY` и `CALLBACK_SECRET` обязательны; значения в примере предназначены для локального запуска.

## Миграции

```powershell
uv run alembic current
uv run alembic upgrade head
```

В Compose: `docker compose run --rm migrate`. После изменения исходников сначала
пересоберите образ (`docker compose build`). Для проверки обратимости используйте
отдельную БД, не рабочую:

```powershell
$env:DATABASE_URL = 'postgresql+asyncpg://postgres:postgres@localhost:55433/payment_gateway_migrations'
uv run alembic upgrade head
uv run alembic downgrade base
uv run alembic upgrade head
```

`downgrade base` удаляет таблицы. Для проверки только новой миграции используйте
`downgrade -1` вместо `downgrade base`. Новая ревизия `f4b2a6c8d901` добавляет расписание,
счётчик попыток и частичный индекс возвратов. Её откат удаляет расписание и индекс,
сохраняя статусы и суммы. Откат более ранней ревизии `8c113a4030ae` не пройдёт,
если есть возвраты с причиной `NOT_ACCEPTED_BY_PROVIDER`: старый enum её не поддерживает.

## Проверки

```powershell
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src
```

Полный pytest требует **отдельную PostgreSQL БД**: DB-фикстуры удаляют и создают
таблицы целиком. Запускать их против рабочей БД нельзя. Например:

```powershell
docker run --detach --name payment-gateway-tests --publish 127.0.0.1:55433:5432 --env POSTGRES_USER=postgres --env POSTGRES_PASSWORD=postgres --env POSTGRES_DB=payment_gateway_test postgres:17
# Дождитесь готовности (pg_isready должен завершиться с кодом 0):
docker exec payment-gateway-tests pg_isready -U postgres -d payment_gateway_test
docker exec payment-gateway-tests createdb -U postgres payment_gateway_migrations
$env:TEST_DATABASE_URL = 'postgresql+asyncpg://postgres:postgres@localhost:55433/payment_gateway_test'
uv run pytest
```

Без `TEST_DATABASE_URL` DB-тесты будут skipped; такой запуск не является полной проверкой.
Не запускайте DB-тесты параллельно против одной БД. Быстрая проверка без БД:
`uv run pytest tests/unit`. CI запускает весь pytest с PostgreSQL 17.
После локальных проверок временный контейнер можно удалить:
`docker rm -f -v payment-gateway-tests`.

## API

Денежные значения передаются десятичными строками, например `"40.00"`, с точностью
до двух знаков. Валюта возврата совпадает с валютой исходного платежа.
Статусы в JSON — в нижнем регистре (`created`, `pending`, …).

| Метод и путь | Запрос / результат |
| --- | --- |
| `POST /payments` | `{amount, currency, provider_id, metadata?}` → 201 `{payment_id, status, amount, currency, created_at}` |
| `GET /payments/{payment_id}` | 200 `{payment_id, status, refunded_amount}` |
| `POST /payments/{payment_id}/refunds` | `{amount, metadata?}` → 201 `{refund_id, payment_id, status, amount}` |
| `GET /payments/{payment_id}/refunds` | 200 `{refunds: [...]}`, порядок `(created_at, id)`, без пагинации |
| `GET /refunds/{refund_id}` | 200 `{refund_id, payment_id, status, amount}` |
| `POST /callbacks/payments` | `{payment_id, status, failure_reason?, error_message?}` → 200 статус платежа |
| `POST /callbacks/refunds` | `{refund_id, status, failure_reason?, error_message?}` → 200 возврат |

Merchant-методы требуют `X-API-Key`; callbacks — `X-Callback-Secret`.
Для `POST /payments` и создания возврата доступен `Idempotency-Key` (1–255 символов).
Повтор с тем же телом возвращает сохранённый ответ и `Idempotency-Replayed: true`;
ключ с другим телом или другим `payment_id` возврата даёт 422. Сохранённый ответ —
неизменяемый снимок создания либо восстановления; актуальное состояние читается через GET.
Если воркер уже вывел возврат из `CREATED`, а ответа под ключом ещё нет, повторный POST
сохраняет снимок текущего состояния и возвращает 201 с `Idempotency-Replayed: true`
без новой инициации или брони. Конкурирующие запросы получают один сохранённый снимок.
Ключи глобальны внутри каждого типа операции, merchant-scoping пока отсутствует.

Создать платёж через PowerShell:

```powershell
$headers = @{ 'X-API-Key' = 'local-dev-api-key'; 'Idempotency-Key' = 'payment-demo-1' }
$payment = Invoke-RestMethod http://localhost:8000/payments -Method Post -Headers $headers -ContentType application/json -Body '{"amount":"100.00","currency":"USD","provider_id":1,"metadata":{"fake_scenario":"success"}}'
```

В auto-режиме дождитесь `success` через GET. В manual-режиме отправьте callbacks
сначала `processing`, затем `success`, например тело
`{"payment_id":"<id>","status":"processing"}` с `X-Callback-Secret`.
После успешного платежа можно создать возврат:

```powershell
$headers['Idempotency-Key'] = 'refund-demo-1'
Invoke-RestMethod "http://localhost:8000/payments/$($payment.payment_id)/refunds" -Method Post -Headers $headers -ContentType application/json -Body '{"amount":"40.00"}'
```

Callback возврата принимает `success`, `failed`, `error`; при `failed` нужен
`failure_reason` (`timeout`, `card_unavailable`, `insufficient_merchant_balance`),
при `error` — `error_message`. Для остальных статусов эти поля отсутствуют.
Причина `not_accepted_by_provider` допустима только при отказе из `CREATED`.
Повтор уже применённого статуса — no-op 200. Недопустимый переход — 409.

Ошибки: 401 — отсутствующий/неверный секрет; 404 — объект не найден; 409 —
конфликт состояния, неуспешный исходный платёж или превышение доступной суммы;
также 409 возвращается при истечении общего срока инициации возврата;
422 — валидация, неизвестный провайдер или несовпадающий idempotency-запрос;
502 — ошибка инициации провайдера. При 502 возврат уже сохранён в `CREATED`,
бронь удерживается: новый ключ не является безопасным повтором этой операции.

## Сверка возвратов

Reconciler проверяет старые `CREATED/PENDING/ERROR` через `get_refund_status` провайдера.
`GET /refunds/{id}` читает локальную БД и не вызывает провайдера.
`refunded_amount` означает **забронировано или возвращено**: успех сохраняет сумму,
подтверждённый отказ освобождает её в одной транзакции с CAS статуса. Неизвестный
или спорный результат сохраняет бронь и получает следующую проверку с backoff.

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `RECONCILE_INTERVAL_SECONDS` | 60 | Пауза между batch’ами |
| `RECONCILE_STUCK_AFTER_SECONDS` | 900 | Минимальный возраст для сверки |
| `REFUND_INITIATION_MAX_AGE_SECONDS` | 72000 | Общий максимальный возраст возврата для инициации через API и reconciler |
| `RECONCILE_BATCH_SIZE` | 100 | Максимальный размер batch |
| `RECONCILE_RETRY_AFTER_SECONDS` | 60 | Начальная задержка повторной проверки |
| `RECONCILE_RETRY_MAX_SECONDS` | 3600 | Верхняя граница экспоненциального backoff |

Срок считается от `refund.created_at`, а не от даты исходного платежа. После
истечения срока API recovery возвращает 409 без обращения к провайдеру; запись
и бронь сохраняются для сверки. Воркер по-прежнему запрашивает статус и освобождает
бронь только после подтверждённого отказа/отсутствия. Сохранённый idempotency-ответ
доступен и после срока. Точная граница срока включительна. Старое имя
`RECONCILE_GIVE_UP_AFTER_SECONDS` поддерживается как alias; новое имя имеет приоритет.

Ограничение повторной инициации должно быть короче гарантированного срока
дедупликации конкретного PSP. Fake-провайдеры не являются достоверным внешним реестром.
Спорные случаи требуют разбора по данным провайдера; автоматического освобождения
по одному только возрасту нет. При завершении приложения Dishka отменяет callback-задачи
auto fake и дожидается их завершения перед закрытием HTTP-клиента.
