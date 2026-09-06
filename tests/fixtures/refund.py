from collections.abc import Collection
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError
from src.contexts.core_payment.domain.exceptions import (
    RefundNotFoundError,
    StaleRefundStateError,
)
from src.contexts.core_payment.domain.refund import Refund
from src.contexts.core_payment.domain.statuses import RefundStatuses
from src.contexts.core_payment.infrastructure.database.repositories import (
    RefundIdempotencyRecord,
    RefundReconciliationCandidate,
)
from src.shared.ids import new_uuid


class FakeRefundRepository:
    def __init__(self) -> None:
        self._refunds: dict[UUID, Refund] = {}
        self._reconciliation: dict[UUID, tuple[datetime, int]] = {}

    async def add(self, refund: Refund) -> None:
        # Copy so the store is an honest boundary: outside mutations of the
        # object must not leak into the "DB" without an update call.
        self._refunds[refund.id] = refund.model_copy(deep=True)

    async def get_by_id(self, refund_id: UUID) -> Refund | None:
        stored = self._refunds.get(refund_id)
        return stored.model_copy(deep=True) if stored else None

    async def list_by_payment_id(self, payment_id: UUID) -> list[Refund]:
        found = [refund for refund in self._refunds.values() if refund.payment_id == payment_id]
        found.sort(key=lambda refund: (refund.created_at, refund.id))
        return [refund.model_copy(deep=True) for refund in found]

    async def list_unresolved(
        self,
        *,
        statuses: Collection[RefundStatuses],
        created_before: datetime,
        due_before: datetime,
        limit: int,
    ) -> list[RefundReconciliationCandidate]:
        found = [
            refund
            for refund in self._refunds.values()
            if refund.status in statuses
            and refund.status
            in {RefundStatuses.CREATED, RefundStatuses.PENDING, RefundStatuses.ERROR}
            and refund.created_at < created_before
            and self._reconciliation.get(refund.id, (refund.created_at, 0))[0] <= due_before
        ]
        found.sort(
            key=lambda refund: (
                self._reconciliation.get(refund.id, (refund.created_at, 0))[0],
                refund.id,
            )
        )
        return [
            RefundReconciliationCandidate(
                refund.model_copy(deep=True),
                self._reconciliation.get(refund.id, (refund.created_at, 0))[1],
            )
            for refund in found[:limit]
        ]

    async def schedule_reconciliation(
        self, candidate: RefundReconciliationCandidate, *, next_check_at: datetime
    ) -> bool:
        stored = self._refunds.get(candidate.refund.id)
        if stored is None or stored.status is not candidate.refund.status:
            return False
        attempts = self._reconciliation.get(stored.id, (stored.created_at, 0))[1]
        if attempts != candidate.attempts:
            return False
        self._reconciliation[stored.id] = (next_check_at, attempts + 1)
        return True

    async def update(self, refund: Refund, *, expected_status: RefundStatuses) -> None:
        stored = self._refunds.get(refund.id)
        if stored is None:
            raise RefundNotFoundError
        if stored.status is not expected_status:
            raise StaleRefundStateError(refund.id, expected_status)
        self._refunds[refund.id] = refund.model_copy(deep=True)


class FakeRefundIdempotencyKeyRepository:
    def __init__(self) -> None:
        self._records: dict[str, RefundIdempotencyRecord] = {}

    async def get(self, key: str) -> RefundIdempotencyRecord | None:
        return self._records.get(key)

    async def add(self, record: RefundIdempotencyRecord) -> None:
        if record.key in self._records:
            raise IntegrityError(
                "INSERT INTO refund_idempotency_keys",
                params=None,
                orig=Exception("duplicate key value violates unique constraint"),
            )
        self._records[record.key] = record

    async def set_response(self, key: str, response_body: dict[str, Any]) -> dict[str, Any]:
        if key not in self._records:
            raise LookupError(f"Idempotency key {key!r} is not reserved")
        record = self._records[key]
        if record.response_body is not None:
            return record.response_body
        self._records[key] = replace(record, response_body=response_body)
        return response_body


def make_refund(
    status: RefundStatuses = RefundStatuses.PENDING,
    payment_id: UUID | None = None,
    amount: Decimal = Decimal("40.00"),
    created_at: datetime | None = None,
) -> Refund:
    return Refund(
        id=new_uuid(),
        payment_id=payment_id if payment_id is not None else new_uuid(),
        amount=amount,
        status=status,
        created_at=created_at if created_at is not None else datetime.now(UTC),
    )


@pytest.fixture
def fake_refund_repo() -> FakeRefundRepository:
    return FakeRefundRepository()


@pytest.fixture
def fake_refund_key_repo() -> FakeRefundIdempotencyKeyRepository:
    return FakeRefundIdempotencyKeyRepository()
