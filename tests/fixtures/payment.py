from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from src.contexts.core_payment.domain.exceptions import (
    PaymentNotFoundError,
    StalePaymentStateError,
)
from src.contexts.core_payment.domain.payment import Payment
from src.contexts.core_payment.domain.statuses import PaymentStatuses
from src.shared.ids import new_uuid


class FakePaymentRepository:
    def __init__(self) -> None:
        self._payments: dict[UUID, Payment] = {}

    async def add(self, payment: Payment) -> None:
        # Copy so the store is an honest boundary: outside mutations of the
        # object must not leak into the "DB" without an update call.
        self._payments[payment.id] = payment.model_copy(deep=True)

    async def get_by_id(self, payment_id: UUID) -> Payment | None:
        stored = self._payments.get(payment_id)
        return stored.model_copy(deep=True) if stored else None

    async def update(self, payment: Payment, *, expected_status: PaymentStatuses) -> None:
        stored = self._payments.get(payment.id)
        if stored is None:
            raise PaymentNotFoundError
        if stored.status is not expected_status:
            raise StalePaymentStateError(payment.id, expected_status)
        self._payments[payment.id] = payment.model_copy(deep=True)


def make_payment(status: PaymentStatuses = PaymentStatuses.PENDING) -> Payment:
    return Payment(
        id=new_uuid(),
        provider_id=1,
        status=status,
        amount=Decimal("100.00"),
        currency="USD",
        created_at=datetime.now(UTC),
    )


@pytest.fixture
def fake_repo() -> FakePaymentRepository:
    return FakePaymentRepository()
