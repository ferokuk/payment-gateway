from decimal import Decimal

import pytest
from src.contexts.core_payment.application.use_cases.get_payment_status import (
    GetPaymentStatusUseCase,
)
from src.contexts.core_payment.domain.exceptions import PaymentNotFoundError
from src.contexts.core_payment.domain.statuses import PaymentStatuses
from src.shared.ids import new_uuid
from tests.fixtures.payment import FakePaymentRepository, make_payment


@pytest.mark.anyio
async def test_returns_dto_with_payment_status_when_payment_exists() -> None:
    repo = FakePaymentRepository()
    payment = make_payment(status=PaymentStatuses.PROCESSING)
    await repo.add(payment)
    use_case = GetPaymentStatusUseCase(repo)  # type: ignore[arg-type]

    result = await use_case(payment.id)

    assert result.payment_id == payment.id
    assert result.status is PaymentStatuses.PROCESSING
    assert result.refunded_amount == Decimal("0")


@pytest.mark.anyio
async def test_raises_payment_not_found_when_payment_missing() -> None:
    repo = FakePaymentRepository()
    use_case = GetPaymentStatusUseCase(repo)  # type: ignore[arg-type]

    with pytest.raises(PaymentNotFoundError):
        await use_case(new_uuid())
