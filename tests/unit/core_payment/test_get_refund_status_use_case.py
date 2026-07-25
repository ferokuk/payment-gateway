from decimal import Decimal

import pytest
from src.contexts.core_payment.application.use_cases.get_refund_status import (
    GetRefundStatusUseCase,
)
from src.contexts.core_payment.domain.exceptions import RefundNotFoundError
from src.contexts.core_payment.domain.statuses import RefundStatuses
from src.shared.ids import new_uuid
from tests.fixtures.refund import FakeRefundRepository, make_refund


@pytest.mark.anyio
async def test_returns_dto_when_refund_exists() -> None:
    repo = FakeRefundRepository()
    refund = make_refund(status=RefundStatuses.PENDING)
    await repo.add(refund)
    use_case = GetRefundStatusUseCase(repo)  # type: ignore[arg-type]

    result = await use_case(refund.id)

    assert result.refund_id == refund.id
    assert result.payment_id == refund.payment_id
    assert result.status is RefundStatuses.PENDING
    assert result.amount == Decimal("40.00")


@pytest.mark.anyio
async def test_raises_not_found_when_refund_missing() -> None:
    use_case = GetRefundStatusUseCase(FakeRefundRepository())  # type: ignore[arg-type]

    with pytest.raises(RefundNotFoundError):
        await use_case(new_uuid())
