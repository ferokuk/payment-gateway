from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from src.contexts.core_payment.application.dto.refund import CreateRefundInputDTO
from src.contexts.core_payment.application.use_cases.create_refund import CreateRefundUseCase
from src.contexts.core_payment.domain.exceptions import (
    PaymentNotFoundError,
    PaymentNotRefundableError,
    RefundAmountExceededError,
)
from src.contexts.core_payment.domain.payment import Payment
from src.contexts.core_payment.domain.statuses import PaymentStatuses, RefundStatuses
from src.contexts.core_payment.infrastructure.providers.base import ProviderInitiationError
from src.shared.ids import new_uuid
from tests.fixtures.payment import FakePaymentRepository, make_payment
from tests.fixtures.providers import RecordingFakeProvider
from tests.fixtures.refund import FakeRefundIdempotencyKeyRepository, FakeRefundRepository
from tests.fixtures.session import FakeSession


def _make_command(payment_id: UUID, amount: str = "40.00") -> CreateRefundInputDTO:
    return CreateRefundInputDTO(payment_id=payment_id, amount=Decimal(amount))


def _setup(
    provider: RecordingFakeProvider | None = None,
) -> tuple[
    FakePaymentRepository,
    FakeRefundRepository,
    FakeRefundIdempotencyKeyRepository,
    RecordingFakeProvider,
    CreateRefundUseCase,
]:
    payment_repo = FakePaymentRepository()
    refund_repo = FakeRefundRepository()
    key_repo = FakeRefundIdempotencyKeyRepository()
    provider = provider or RecordingFakeProvider()
    session = FakeSession(payment_repo._payments, refund_repo._refunds, key_repo._records)
    use_case = CreateRefundUseCase(
        payment_repo,  # type: ignore[arg-type]
        refund_repo,  # type: ignore[arg-type]
        key_repo,  # type: ignore[arg-type]
        provider,
        session,  # type: ignore[arg-type]
        initiation_max_age=timedelta(hours=20),
    )
    return payment_repo, refund_repo, key_repo, provider, use_case


async def _add_success_payment(repo: FakePaymentRepository) -> Payment:
    payment = make_payment(status=PaymentStatuses.SUCCESS)  # amount 100.00
    await repo.add(payment)
    return payment


@pytest.mark.anyio
async def test_happy_path_reserves_and_returns_pending() -> None:
    payment_repo, refund_repo, _key_repo, provider, use_case = _setup()
    payment = await _add_success_payment(payment_repo)

    result = await use_case(_make_command(payment.id))

    assert result.status is RefundStatuses.PENDING
    assert result.payment_id == payment.id
    assert result.amount == Decimal("40.00")

    stored = await refund_repo.get_by_id(result.refund_id)
    assert stored is not None
    assert stored.status is RefundStatuses.PENDING
    stored_payment = await payment_repo.get_by_id(payment.id)
    assert stored_payment is not None
    assert stored_payment.refunded_amount == Decimal("40.00")
    assert len(provider.initiated_refunds) == 1


@pytest.mark.anyio
async def test_missing_payment_raises_not_found() -> None:
    _payment_repo, refund_repo, _key_repo, _provider, use_case = _setup()

    with pytest.raises(PaymentNotFoundError):
        await use_case(_make_command(new_uuid()))

    assert refund_repo._refunds == {}


@pytest.mark.anyio
async def test_non_success_payment_raises_not_refundable() -> None:
    payment_repo, refund_repo, _key_repo, _provider, use_case = _setup()
    payment = make_payment(status=PaymentStatuses.PROCESSING)
    await payment_repo.add(payment)

    with pytest.raises(PaymentNotRefundableError):
        await use_case(_make_command(payment.id))

    assert refund_repo._refunds == {}
    stored = await payment_repo.get_by_id(payment.id)
    assert stored is not None
    assert stored.refunded_amount == Decimal("0")


@pytest.mark.anyio
async def test_amount_over_remainder_raises_exceeded() -> None:
    payment_repo, refund_repo, _key_repo, _provider, use_case = _setup()
    payment = await _add_success_payment(payment_repo)

    with pytest.raises(RefundAmountExceededError):
        await use_case(_make_command(payment.id, amount="100.01"))

    assert refund_repo._refunds == {}


@pytest.mark.anyio
async def test_partial_refunds_until_exhaustion() -> None:
    payment_repo, refund_repo, _key_repo, _provider, use_case = _setup()
    payment = await _add_success_payment(payment_repo)

    first = await use_case(_make_command(payment.id, amount="60.00"))
    second = await use_case(_make_command(payment.id, amount="40.00"))

    assert first.refund_id != second.refund_id
    stored = await payment_repo.get_by_id(payment.id)
    assert stored is not None
    assert stored.refunded_amount == Decimal("100.00")

    with pytest.raises(RefundAmountExceededError):
        await use_case(_make_command(payment.id, amount="0.01"))

    assert len(refund_repo._refunds) == 2


@pytest.mark.anyio
async def test_initiation_error_keeps_created_and_reservation() -> None:
    payment_repo, refund_repo, _key_repo, _provider, use_case = _setup(
        provider=RecordingFakeProvider(error=ProviderInitiationError("down"))
    )
    payment = await _add_success_payment(payment_repo)

    with pytest.raises(ProviderInitiationError):
        await use_case(_make_command(payment.id))

    refunds = list(refund_repo._refunds.values())
    assert len(refunds) == 1
    assert refunds[0].status is RefundStatuses.CREATED
    stored = await payment_repo.get_by_id(payment.id)
    assert stored is not None
    assert stored.refunded_amount == Decimal("40.00")  # reservation held
