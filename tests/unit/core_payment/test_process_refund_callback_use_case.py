from decimal import Decimal
from uuid import UUID

import pytest
from src.contexts.core_payment.application.dto.refund import (
    RefundCallbackInputDTO,
    RefundCallbackStatus,
)
from src.contexts.core_payment.application.use_cases.process_refund_callback import (
    ProcessRefundCallbackUseCase,
)
from src.contexts.core_payment.domain.exceptions import (
    InvalidRefundStatusTransitionError,
    RefundNotFoundError,
)
from src.contexts.core_payment.domain.payment import Payment
from src.contexts.core_payment.domain.refund import Refund
from src.contexts.core_payment.domain.statuses import (
    PaymentStatuses,
    RefundFailureReasons,
    RefundStatuses,
)
from src.shared.ids import new_uuid
from tests.fixtures.payment import FakePaymentRepository, make_payment
from tests.fixtures.refund import FakeRefundRepository, make_refund


def _make_use_case(
    refund_repo: FakeRefundRepository, payment_repo: FakePaymentRepository
) -> ProcessRefundCallbackUseCase:
    return ProcessRefundCallbackUseCase(refund_repo, payment_repo)  # type: ignore[arg-type]


def _command(
    refund_id: UUID,
    status: RefundCallbackStatus,
    failure_reason: RefundFailureReasons | None = None,
    error_message: str | None = None,
) -> RefundCallbackInputDTO:
    return RefundCallbackInputDTO(
        refund_id=refund_id,
        status=status,
        failure_reason=failure_reason,
        error_message=error_message,
    )


async def _add_pending_refund(
    payment_repo: FakePaymentRepository,
    refund_repo: FakeRefundRepository,
    amount: str = "40.00",
) -> tuple[Payment, Refund]:
    """A SUCCESS payment with the refund amount already reserved + a PENDING refund."""
    payment = make_payment(status=PaymentStatuses.SUCCESS)
    await payment_repo.add(payment)
    await payment_repo.reserve_refund_amount(payment.id, Decimal(amount))
    refund = make_refund(
        status=RefundStatuses.PENDING, payment_id=payment.id, amount=Decimal(amount)
    )
    await refund_repo.add(refund)
    return payment, refund


@pytest.mark.anyio
async def test_success_applies_and_keeps_reservation() -> None:
    payment_repo, refund_repo = FakePaymentRepository(), FakeRefundRepository()
    payment, refund = await _add_pending_refund(payment_repo, refund_repo)
    use_case = _make_use_case(refund_repo, payment_repo)

    result = await use_case(_command(refund.id, "success"))

    assert result.status is RefundStatuses.SUCCESS
    stored = await refund_repo.get_by_id(refund.id)
    assert stored is not None
    assert stored.status is RefundStatuses.SUCCESS
    stored_payment = await payment_repo.get_by_id(payment.id)
    assert stored_payment is not None
    assert stored_payment.refunded_amount == Decimal("40.00")  # settled, not released


@pytest.mark.parametrize(
    "reason",
    [
        RefundFailureReasons.TIMEOUT,
        RefundFailureReasons.CARD_UNAVAILABLE,
        RefundFailureReasons.INSUFFICIENT_MERCHANT_BALANCE,
    ],
)
@pytest.mark.anyio
async def test_failed_releases_reservation(reason: RefundFailureReasons) -> None:
    payment_repo, refund_repo = FakePaymentRepository(), FakeRefundRepository()
    payment, refund = await _add_pending_refund(payment_repo, refund_repo)
    use_case = _make_use_case(refund_repo, payment_repo)

    result = await use_case(_command(refund.id, "failed", failure_reason=reason))

    assert result.status is RefundStatuses.FAILED
    stored = await refund_repo.get_by_id(refund.id)
    assert stored is not None
    assert stored.failure_reason is reason
    stored_payment = await payment_repo.get_by_id(payment.id)
    assert stored_payment is not None
    assert stored_payment.refunded_amount == Decimal("0")  # released


@pytest.mark.anyio
async def test_error_holds_reservation() -> None:
    payment_repo, refund_repo = FakePaymentRepository(), FakeRefundRepository()
    payment, refund = await _add_pending_refund(payment_repo, refund_repo)
    use_case = _make_use_case(refund_repo, payment_repo)

    result = await use_case(_command(refund.id, "error", error_message="boom"))

    assert result.status is RefundStatuses.ERROR
    stored = await refund_repo.get_by_id(refund.id)
    assert stored is not None
    assert stored.error_message == "boom"
    stored_payment = await payment_repo.get_by_id(payment.id)
    assert stored_payment is not None
    assert stored_payment.refunded_amount == Decimal("40.00")  # unknown outcome — held


@pytest.mark.anyio
async def test_duplicate_failed_callback_is_noop_without_second_release() -> None:
    payment_repo, refund_repo = FakePaymentRepository(), FakeRefundRepository()
    payment, refund = await _add_pending_refund(payment_repo, refund_repo)
    use_case = _make_use_case(refund_repo, payment_repo)
    await use_case(_command(refund.id, "failed", failure_reason=RefundFailureReasons.TIMEOUT))

    result = await use_case(
        _command(refund.id, "failed", failure_reason=RefundFailureReasons.TIMEOUT)
    )

    assert result.status is RefundStatuses.FAILED
    stored_payment = await payment_repo.get_by_id(payment.id)
    assert stored_payment is not None
    assert stored_payment.refunded_amount == Decimal("0")  # released exactly once


@pytest.mark.anyio
async def test_unknown_refund_raises_not_found() -> None:
    use_case = _make_use_case(FakeRefundRepository(), FakePaymentRepository())

    with pytest.raises(RefundNotFoundError):
        await use_case(_command(new_uuid(), "success"))


@pytest.mark.anyio
async def test_invalid_transition_raises() -> None:
    payment_repo, refund_repo = FakePaymentRepository(), FakeRefundRepository()
    refund = make_refund(status=RefundStatuses.CREATED)
    await refund_repo.add(refund)
    use_case = _make_use_case(refund_repo, payment_repo)

    with pytest.raises(InvalidRefundStatusTransitionError):
        await use_case(_command(refund.id, "success"))

    stored = await refund_repo.get_by_id(refund.id)
    assert stored is not None
    assert stored.status is RefundStatuses.CREATED


class _RacedRefundRepo(FakeRefundRepository):
    """Before the first update a competing writer overwrites the refund in the store."""

    def __init__(self, winner: Refund) -> None:
        super().__init__()
        self._winner = winner
        self._raced = False

    async def update(self, refund: Refund, *, expected_status: RefundStatuses) -> None:
        if not self._raced:
            self._raced = True
            self._refunds[self._winner.id] = self._winner.model_copy(deep=True)
        await super().update(refund, expected_status=expected_status)


@pytest.mark.anyio
async def test_concurrent_duplicate_failed_becomes_noop_without_second_release() -> None:
    # The winner applied FAILED and released; the loser must no-op, not release again.
    payment_repo, refund_repo_setup = FakePaymentRepository(), FakeRefundRepository()
    payment, refund = await _add_pending_refund(payment_repo, refund_repo_setup)
    winner = refund.model_copy(deep=True)
    winner.mark_failed(RefundFailureReasons.TIMEOUT)
    raced_repo = _RacedRefundRepo(winner)
    await raced_repo.add(refund)
    # Emulate the winner's release, which happened together with its CAS.
    await payment_repo.release_refund_amount(payment.id, refund.amount)
    use_case = _make_use_case(raced_repo, payment_repo)

    result = await use_case(
        _command(refund.id, "failed", failure_reason=RefundFailureReasons.TIMEOUT)
    )

    assert result.status is RefundStatuses.FAILED
    stored_payment = await payment_repo.get_by_id(payment.id)
    assert stored_payment is not None
    assert stored_payment.refunded_amount == Decimal("0")  # not double-released


@pytest.mark.anyio
async def test_conflicting_callback_with_other_status_raises_and_holds() -> None:
    # A competing writer moved the refund to SUCCESS; our FAILED transition is
    # impossible and must NOT release the reservation.
    payment_repo, refund_repo_setup = FakePaymentRepository(), FakeRefundRepository()
    payment, refund = await _add_pending_refund(payment_repo, refund_repo_setup)
    winner = refund.model_copy(deep=True)
    winner.mark_success()
    raced_repo = _RacedRefundRepo(winner)
    await raced_repo.add(refund)
    use_case = _make_use_case(raced_repo, payment_repo)

    with pytest.raises(InvalidRefundStatusTransitionError):
        await use_case(_command(refund.id, "failed", failure_reason=RefundFailureReasons.TIMEOUT))

    stored = await raced_repo.get_by_id(refund.id)
    assert stored is not None
    assert stored.status is RefundStatuses.SUCCESS  # did not regress
    stored_payment = await payment_repo.get_by_id(payment.id)
    assert stored_payment is not None
    assert stored_payment.refunded_amount == Decimal("40.00")  # reservation intact
