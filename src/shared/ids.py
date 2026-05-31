from uuid import UUID

# .compat возвращает штатный uuid.UUID (не нативный uuid_utils.UUID),
# иначе значение не примут Pydantic (UUID7) и SQLAlchemy (PG_UUID(as_uuid=True)).
from uuid_utils.compat import uuid7


def new_uuid() -> UUID:
    """Генерирует идентификатор сущности как UUIDv7 (RFC 9562).

    v7 несёт префикс из Unix-времени (мс) -> значения почти монотонны, что даёт
    лучшую локальность B-tree индекса первичного ключа в PostgreSQL, чем
    случайный v4. Стратегия генерации id вынесена в одну точку, чтобы менять её
    (v4/v7/иное) без правок в домене и инфраструктуре.
    """
    return uuid7()
