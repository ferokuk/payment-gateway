from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError
from src.contexts.core_payment.infrastructure.database.repositories import (
    IdempotencyRecord,
)


class FakeIdempotencyKeyRepository:
    def __init__(self) -> None:
        self._records: dict[str, IdempotencyRecord] = {}

    async def get(self, key: str) -> IdempotencyRecord | None:
        return self._records.get(key)

    async def add(self, record: IdempotencyRecord) -> None:
        if record.key in self._records:
            raise IntegrityError(
                "INSERT INTO idempotency_keys",
                params=None,
                orig=Exception("duplicate key value violates unique constraint"),
            )
        self._records[record.key] = record

    async def set_response(self, key: str, response_body: dict[str, Any]) -> None:
        if key not in self._records:
            raise LookupError(f"Idempotency key {key!r} is not reserved")
        self._records[key] = replace(self._records[key], response_body=response_body)


@pytest.fixture
def fake_key_repo() -> FakeIdempotencyKeyRepository:
    return FakeIdempotencyKeyRepository()
