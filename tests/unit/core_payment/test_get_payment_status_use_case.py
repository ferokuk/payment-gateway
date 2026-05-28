from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from src.contexts.core_payment.application.use_cases.get_payment_status import (
    GetPaymentStatusUseCase,
)
from src.contexts.core_payment.domain.exceptions import PaymentNotFoundError
from src.contexts.core_payment.domain.payment import Payment, PaymentStatuses


class FakePaymentRepository:
    def __init__(self) -> None:
        self._payments: dict[UUID, Payment] = {}

    async def add(self, payment: Payment) -> None:
        self._payments[payment.id] = payment

    async def get_by_id(self, payment_id: UUID) -> Payment | None:
        return self._payments.get(payment_id)


def _make_payment(status: PaymentStatuses = PaymentStatuses.PENDING) -> Payment:
    return Payment(
        id=uuid4(),
        provider_id=1,
        status=status,
        amount=Decimal("100.00"),
        currency="USD",
        created_at=datetime.now(UTC),
    )


@pytest.mark.anyio
async def test_returns_dto_with_payment_status_when_payment_exists() -> None:
    repo = FakePaymentRepository()
    payment = _make_payment(status=PaymentStatuses.PROCESSING)
    await repo.add(payment)
    use_case = GetPaymentStatusUseCase(repo)  # type: ignore[arg-type]

    result = await use_case(payment.id)

    assert result.payment_id == payment.id
    assert result.status is PaymentStatuses.PROCESSING


@pytest.mark.anyio
async def test_raises_payment_not_found_when_payment_missing() -> None:
    repo = FakePaymentRepository()
    use_case = GetPaymentStatusUseCase(repo)  # type: ignore[arg-type]

    with pytest.raises(PaymentNotFoundError):
        await use_case(uuid4())
