import pytest
from src.contexts.core_payment.domain.exceptions import PaymentNotFoundError
from src.contexts.core_payment.domain.statuses import FailureReasons, PaymentStatuses
from tests.fixtures.payment import FakePaymentRepository, make_payment


@pytest.mark.anyio
async def test_mutating_loaded_payment_without_update_does_not_persist() -> None:
    repo = FakePaymentRepository()
    payment = make_payment(status=PaymentStatuses.PENDING)
    await repo.add(payment)

    loaded = await repo.get_by_id(payment.id)
    assert loaded is not None
    loaded.mark_processing()  # мутируем загруженный объект, но update не вызываем

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
    await repo.update(loaded)

    again = await repo.get_by_id(payment.id)
    assert again is not None
    assert again.status is PaymentStatuses.FAILED
    assert again.failure_reason is FailureReasons.FRAUD


@pytest.mark.anyio
async def test_update_missing_payment_raises_not_found() -> None:
    repo = FakePaymentRepository()
    payment = make_payment()  # в репозиторий не добавлен

    with pytest.raises(PaymentNotFoundError):
        await repo.update(payment)
