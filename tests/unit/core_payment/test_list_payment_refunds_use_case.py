from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from src.contexts.core_payment.application.use_cases.list_payment_refunds import (
    ListPaymentRefundsUseCase,
)
from src.contexts.core_payment.domain.exceptions import PaymentNotFoundError
from src.contexts.core_payment.domain.statuses import PaymentStatuses, RefundStatuses
from src.shared.ids import new_uuid
from tests.fixtures.payment import FakePaymentRepository, make_payment
from tests.fixtures.refund import FakeRefundRepository, make_refund


def _use_case(
    payment_repo: FakePaymentRepository,
    refund_repo: FakeRefundRepository,
) -> ListPaymentRefundsUseCase:
    return ListPaymentRefundsUseCase(payment_repo, refund_repo)  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_returns_refunds_of_the_payment_oldest_first() -> None:
    payment_repo = FakePaymentRepository()
    payment = make_payment(status=PaymentStatuses.SUCCESS)
    await payment_repo.add(payment)
    refund_repo = FakeRefundRepository()
    moment = datetime.now(UTC)
    newer = make_refund(payment_id=payment.id, amount=Decimal("10.00"), created_at=moment)
    older = make_refund(
        payment_id=payment.id,
        amount=Decimal("20.00"),
        created_at=moment - timedelta(minutes=1),
    )
    # Insertion order is deliberately the reverse of the expected order.
    await refund_repo.add(newer)
    await refund_repo.add(older)
    use_case = _use_case(payment_repo, refund_repo)

    result = await use_case(payment.id)

    assert [item.refund_id for item in result] == [older.id, newer.id]
    assert [item.amount for item in result] == [Decimal("20.00"), Decimal("10.00")]


@pytest.mark.anyio
async def test_ignores_refunds_of_another_payment() -> None:
    payment_repo = FakePaymentRepository()
    payment = make_payment(status=PaymentStatuses.SUCCESS)
    await payment_repo.add(payment)
    refund_repo = FakeRefundRepository()
    own = make_refund(payment_id=payment.id, status=RefundStatuses.SUCCESS)
    await refund_repo.add(own)
    await refund_repo.add(make_refund())
    use_case = _use_case(payment_repo, refund_repo)

    result = await use_case(payment.id)

    assert [item.refund_id for item in result] == [own.id]
    assert result[0].status is RefundStatuses.SUCCESS


@pytest.mark.anyio
async def test_returns_empty_list_when_payment_has_no_refunds() -> None:
    payment_repo = FakePaymentRepository()
    payment = make_payment(status=PaymentStatuses.SUCCESS)
    await payment_repo.add(payment)
    use_case = _use_case(payment_repo, FakeRefundRepository())

    assert await use_case(payment.id) == []


@pytest.mark.anyio
async def test_raises_not_found_when_payment_missing() -> None:
    # An unknown payment must not look like "a payment without refunds":
    # otherwise a typo in the id silently reads as "nothing was refunded".
    use_case = _use_case(FakePaymentRepository(), FakeRefundRepository())

    with pytest.raises(PaymentNotFoundError):
        await use_case(new_uuid())
