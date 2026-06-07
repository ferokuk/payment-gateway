from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from src.contexts.core_payment.domain.exceptions import PaymentNotFoundError
from src.contexts.core_payment.domain.payment import Payment
from src.contexts.core_payment.domain.statuses import PaymentStatuses
from src.shared.ids import new_uuid


class FakePaymentRepository:
    def __init__(self) -> None:
        self._payments: dict[UUID, Payment] = {}

    async def add(self, payment: Payment) -> None:
        # Копируем, чтобы хранилище было честной границей: мутации объекта
        # снаружи не должны просачиваться в "БД" без вызова update.
        self._payments[payment.id] = payment.model_copy(deep=True)

    async def get_by_id(self, payment_id: UUID) -> Payment | None:
        stored = self._payments.get(payment_id)
        return stored.model_copy(deep=True) if stored else None

    async def update(self, payment: Payment) -> None:
        if payment.id not in self._payments:
            raise PaymentNotFoundError
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
