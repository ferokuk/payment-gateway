from uuid import UUID

import pytest
from src.contexts.core_payment.application.dto.callback import (
    CallbackStatus,
    ProviderCallbackInputDTO,
)
from src.contexts.core_payment.application.use_cases.process_provider_callback import (
    ProcessProviderCallbackUseCase,
)
from src.contexts.core_payment.domain.exceptions import (
    InvalidPaymentFailureReasonError,
    InvalidPaymentStatusTransitionError,
    PaymentNotFoundError,
)
from src.contexts.core_payment.domain.payment import Payment
from src.contexts.core_payment.domain.statuses import FailureReasons, PaymentStatuses
from src.shared.ids import new_uuid
from tests.fixtures.payment import FakePaymentRepository, make_payment


def _make_use_case(repo: FakePaymentRepository) -> ProcessProviderCallbackUseCase:
    return ProcessProviderCallbackUseCase(repo)  # type: ignore[arg-type]


def _command(
    payment_id: UUID,
    status: CallbackStatus,
    failure_reason: FailureReasons | None = None,
    error_message: str | None = None,
) -> ProviderCallbackInputDTO:
    return ProviderCallbackInputDTO(
        payment_id=payment_id,
        status=status,
        failure_reason=failure_reason,
        error_message=error_message,
    )


@pytest.mark.parametrize(
    ("initial", "status", "reason", "message", "expected"),
    [
        (PaymentStatuses.PENDING, "processing", None, None, PaymentStatuses.PROCESSING),
        (PaymentStatuses.PROCESSING, "success", None, None, PaymentStatuses.SUCCESS),
        (
            PaymentStatuses.PROCESSING,
            "failed",
            FailureReasons.INSUFFICIENT_FUNDS,
            None,
            PaymentStatuses.FAILED,
        ),
        (PaymentStatuses.PROCESSING, "failed", FailureReasons.FRAUD, None, PaymentStatuses.FAILED),
        (
            PaymentStatuses.PROCESSING,
            "failed",
            FailureReasons.LIMIT_EXCEEDED,
            None,
            PaymentStatuses.FAILED,
        ),
        (PaymentStatuses.PENDING, "failed", FailureReasons.TIMEOUT, None, PaymentStatuses.FAILED),
        (PaymentStatuses.PROCESSING, "error", None, "provider exploded", PaymentStatuses.ERROR),
    ],
)
@pytest.mark.anyio
async def test_applies_transition_and_persists(
    initial: PaymentStatuses,
    status: CallbackStatus,
    reason: FailureReasons | None,
    message: str | None,
    expected: PaymentStatuses,
) -> None:
    repo = FakePaymentRepository()
    payment = make_payment(status=initial)
    await repo.add(payment)
    use_case = _make_use_case(repo)

    result = await use_case(_command(payment.id, status, reason, message))

    assert result.payment_id == payment.id
    assert result.status is expected
    stored = await repo.get_by_id(payment.id)
    assert stored is not None
    assert stored.status is expected
    assert stored.failure_reason == reason
    assert stored.error_message == message


@pytest.mark.anyio
async def test_duplicate_callback_is_noop() -> None:
    repo = FakePaymentRepository()
    payment = make_payment(status=PaymentStatuses.PROCESSING)
    await repo.add(payment)
    use_case = _make_use_case(repo)

    result = await use_case(_command(payment.id, "processing"))

    assert result.status is PaymentStatuses.PROCESSING
    stored = await repo.get_by_id(payment.id)
    assert stored is not None
    assert stored.status is PaymentStatuses.PROCESSING


@pytest.mark.anyio
async def test_unknown_payment_raises_not_found() -> None:
    use_case = _make_use_case(FakePaymentRepository())

    with pytest.raises(PaymentNotFoundError):
        await use_case(_command(new_uuid(), "processing"))


@pytest.mark.anyio
async def test_invalid_transition_raises() -> None:
    repo = FakePaymentRepository()
    payment = make_payment(status=PaymentStatuses.PENDING)
    await repo.add(payment)
    use_case = _make_use_case(repo)

    with pytest.raises(InvalidPaymentStatusTransitionError):
        await use_case(_command(payment.id, "success"))

    stored = await repo.get_by_id(payment.id)
    assert stored is not None
    assert stored.status is PaymentStatuses.PENDING


@pytest.mark.anyio
async def test_invalid_failure_reason_for_state_raises() -> None:
    repo = FakePaymentRepository()
    payment = make_payment(status=PaymentStatuses.PENDING)
    await repo.add(payment)
    use_case = _make_use_case(repo)

    # From PENDING only timeout is allowed (state machine).
    with pytest.raises(InvalidPaymentFailureReasonError):
        await use_case(_command(payment.id, "failed", FailureReasons.FRAUD))


class _RacedRepo(FakePaymentRepository):
    """Before the first update a competing writer overwrites the payment in the store."""

    def __init__(self, winner: Payment) -> None:
        super().__init__()
        self._winner = winner
        self._raced = False

    async def update(self, payment: Payment, *, expected_status: PaymentStatuses) -> None:
        if not self._raced:
            self._raced = True
            self._payments[self._winner.id] = self._winner.model_copy(deep=True)
        await super().update(payment, expected_status=expected_status)


@pytest.mark.anyio
async def test_concurrent_duplicate_callback_becomes_noop() -> None:
    # A competing writer applied the same transition first: CAS conflict -> no-op 200.
    payment = make_payment(status=PaymentStatuses.PENDING)
    winner = payment.model_copy(deep=True)
    winner.mark_processing()
    repo = _RacedRepo(winner)
    await repo.add(payment)
    use_case = _make_use_case(repo)

    result = await use_case(_command(payment.id, "processing"))

    assert result.status is PaymentStatuses.PROCESSING
    stored = await repo.get_by_id(payment.id)
    assert stored is not None
    assert stored.status is PaymentStatuses.PROCESSING


@pytest.mark.anyio
async def test_conflicting_callback_with_other_status_raises_transition_error() -> None:
    # A competing writer moved the payment on (SUCCESS); our PROCESSING transition is impossible.
    payment = make_payment(status=PaymentStatuses.PENDING)
    winner = payment.model_copy(deep=True)
    winner.mark_processing()
    winner.mark_success()
    repo = _RacedRepo(winner)
    await repo.add(payment)
    use_case = _make_use_case(repo)

    with pytest.raises(InvalidPaymentStatusTransitionError):
        await use_case(_command(payment.id, "processing"))

    stored = await repo.get_by_id(payment.id)
    assert stored is not None
    assert stored.status is PaymentStatuses.SUCCESS  # did not regress
