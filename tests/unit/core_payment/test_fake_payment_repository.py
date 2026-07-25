from decimal import Decimal

import pytest
from src.contexts.core_payment.domain.exceptions import (
    PaymentNotFoundError,
    PaymentNotRefundableError,
    RefundAmountExceededError,
    StalePaymentStateError,
)
from src.contexts.core_payment.domain.statuses import FailureReasons, PaymentStatuses
from src.shared.ids import new_uuid
from tests.fixtures.payment import FakePaymentRepository, make_payment


@pytest.mark.anyio
async def test_mutating_loaded_payment_without_update_does_not_persist() -> None:
    repo = FakePaymentRepository()
    payment = make_payment(status=PaymentStatuses.PENDING)
    await repo.add(payment)

    loaded = await repo.get_by_id(payment.id)
    assert loaded is not None
    loaded.mark_processing()  # mutate the loaded object without calling update

    again = await repo.get_by_id(payment.id)
    assert again is not None
    assert again.status is PaymentStatuses.PENDING


@pytest.mark.anyio
async def test_update_persists_marked_changes() -> None:
    repo = FakePaymentRepository()
    payment = make_payment(status=PaymentStatuses.PROCESSING)
    await repo.add(payment)

    loaded = await repo.get_by_id(payment.id)
    assert loaded is not None
    loaded.mark_failed(FailureReasons.FRAUD)
    await repo.update(loaded, expected_status=PaymentStatuses.PROCESSING)

    again = await repo.get_by_id(payment.id)
    assert again is not None
    assert again.status is PaymentStatuses.FAILED
    assert again.failure_reason is FailureReasons.FRAUD


@pytest.mark.anyio
async def test_update_missing_payment_raises_not_found() -> None:
    repo = FakePaymentRepository()
    payment = make_payment()  # not added to the repository

    with pytest.raises(PaymentNotFoundError):
        await repo.update(payment, expected_status=PaymentStatuses.PENDING)


@pytest.mark.anyio
async def test_update_with_wrong_expected_status_raises_stale_and_keeps_stored() -> None:
    repo = FakePaymentRepository()
    payment = make_payment(status=PaymentStatuses.PENDING)
    await repo.add(payment)
    payment.mark_processing()

    with pytest.raises(StalePaymentStateError) as exc_info:
        await repo.update(payment, expected_status=PaymentStatuses.CREATED)

    assert exc_info.value.payment_id == payment.id
    assert exc_info.value.expected_status is PaymentStatuses.CREATED
    stored = await repo.get_by_id(payment.id)
    assert stored is not None
    assert stored.status is PaymentStatuses.PENDING  # store untouched


@pytest.mark.anyio
async def test_update_missing_payment_raises_not_found_not_stale() -> None:
    repo = FakePaymentRepository()
    payment = make_payment(status=PaymentStatuses.PENDING)
    payment.mark_processing()

    with pytest.raises(PaymentNotFoundError):
        await repo.update(payment, expected_status=PaymentStatuses.PENDING)


# --- reserve_refund_amount / release_refund_amount contract ---


@pytest.mark.anyio
async def test_reserve_refund_amount_accumulates() -> None:
    repo = FakePaymentRepository()
    payment = make_payment(status=PaymentStatuses.SUCCESS)
    await repo.add(payment)

    await repo.reserve_refund_amount(payment.id, Decimal("60.00"))
    await repo.reserve_refund_amount(payment.id, Decimal("40.00"))

    stored = await repo.get_by_id(payment.id)
    assert stored is not None
    assert stored.refunded_amount == Decimal("100.00")


@pytest.mark.anyio
async def test_reserve_refund_amount_missing_payment_raises_not_found() -> None:
    repo = FakePaymentRepository()

    with pytest.raises(PaymentNotFoundError):
        await repo.reserve_refund_amount(new_uuid(), Decimal("10.00"))


@pytest.mark.anyio
async def test_reserve_refund_amount_non_success_raises_not_refundable() -> None:
    repo = FakePaymentRepository()
    payment = make_payment(status=PaymentStatuses.PROCESSING)
    await repo.add(payment)

    with pytest.raises(PaymentNotRefundableError) as exc_info:
        await repo.reserve_refund_amount(payment.id, Decimal("10.00"))

    assert exc_info.value.payment_id == payment.id
    assert exc_info.value.status is PaymentStatuses.PROCESSING


@pytest.mark.anyio
async def test_reserve_refund_amount_over_remainder_raises_and_keeps_counter() -> None:
    repo = FakePaymentRepository()
    payment = make_payment(status=PaymentStatuses.SUCCESS)  # amount 100.00
    await repo.add(payment)
    await repo.reserve_refund_amount(payment.id, Decimal("60.00"))

    with pytest.raises(RefundAmountExceededError):
        await repo.reserve_refund_amount(payment.id, Decimal("60.00"))

    stored = await repo.get_by_id(payment.id)
    assert stored is not None
    assert stored.refunded_amount == Decimal("60.00")


@pytest.mark.anyio
async def test_release_refund_amount_returns_reservation() -> None:
    repo = FakePaymentRepository()
    payment = make_payment(status=PaymentStatuses.SUCCESS)
    await repo.add(payment)
    await repo.reserve_refund_amount(payment.id, Decimal("60.00"))

    await repo.release_refund_amount(payment.id, Decimal("60.00"))

    stored = await repo.get_by_id(payment.id)
    assert stored is not None
    assert stored.refunded_amount == Decimal("0")


@pytest.mark.anyio
async def test_release_refund_amount_below_zero_raises_and_keeps_counter() -> None:
    repo = FakePaymentRepository()
    payment = make_payment(status=PaymentStatuses.SUCCESS)
    await repo.add(payment)

    with pytest.raises(LookupError):
        await repo.release_refund_amount(payment.id, Decimal("10.00"))

    stored = await repo.get_by_id(payment.id)
    assert stored is not None
    assert stored.refunded_amount == Decimal("0")


@pytest.mark.anyio
async def test_release_refund_amount_missing_payment_raises_not_found() -> None:
    repo = FakePaymentRepository()

    with pytest.raises(PaymentNotFoundError):
        await repo.release_refund_amount(new_uuid(), Decimal("10.00"))
