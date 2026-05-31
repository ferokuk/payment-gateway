from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from src.contexts.core_payment.domain.payment import Payment, PaymentStatuses
from src.shared.ids import new_uuid


class FakePaymentRepository:
    def __init__(self) -> None:
        self._payments: dict[UUID, Payment] = {}

    async def add(self, payment: Payment) -> None:
        self._payments[payment.id] = payment

    async def get_by_id(self, payment_id: UUID) -> Payment | None:
        return self._payments.get(payment_id)


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
