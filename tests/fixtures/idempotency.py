from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError
from src.contexts.core_payment.infrastructure.database.repositories import (
    IdempotencyRecord,
)
from tests.fixtures.merchants import MERCHANT_ID


class FakeIdempotencyKeyRepository:
    def __init__(self, merchant_id: UUID = MERCHANT_ID) -> None:
        self.merchant_id = merchant_id
        self._records: dict[tuple[UUID, str], IdempotencyRecord] = {}

    async def get(self, key: str) -> IdempotencyRecord | None:
        return self._records.get((self.merchant_id, key))

    async def add(self, record: IdempotencyRecord) -> None:
        if record.merchant_id != self.merchant_id:
            raise ValueError("Object does not belong to this merchant scope")
        if (self.merchant_id, record.key) in self._records:
            raise IntegrityError(
                "INSERT INTO idempotency_keys",
                params=None,
                orig=Exception("duplicate key value violates unique constraint"),
            )
        self._records[(self.merchant_id, record.key)] = record

    async def set_response(self, key: str, response_body: dict[str, Any]) -> None:
        if (self.merchant_id, key) not in self._records:
            raise LookupError(f"Idempotency key {key!r} is not reserved")
        self._records[(self.merchant_id, key)] = replace(
            self._records[(self.merchant_id, key)], response_body=response_body
        )


@pytest.fixture
def fake_key_repo() -> FakeIdempotencyKeyRepository:
    return FakeIdempotencyKeyRepository()
