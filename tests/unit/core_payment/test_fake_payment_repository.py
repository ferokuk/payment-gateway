import pytest
from src.contexts.core_payment.domain.exceptions import (
    PaymentNotFoundError,
    StalePaymentStateError,
)
from src.contexts.core_payment.domain.statuses import FailureReasons, PaymentStatuses
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
