from decimal import Decimal

import anyio
import pytest
from src.contexts.core_payment.application.use_cases.create_refund import CreateRefundUseCase
from src.contexts.core_payment.domain.exceptions import IdempotencyKeyMismatchError
from src.contexts.core_payment.domain.statuses import PaymentStatuses, RefundStatuses
from src.contexts.core_payment.infrastructure.providers.base import ProviderInitiationError
from tests.fixtures.payment import make_payment
from tests.fixtures.providers import RecordingFakeProvider
from tests.fixtures.session import FakeSession
from tests.unit.core_payment.test_create_refund_use_case import (
    _add_success_payment,
    _make_command,
    _setup,
)

KEY = "refund-key-1"


@pytest.mark.anyio
async def test_first_request_with_key_stores_record_and_response() -> None:
    payment_repo, _refund_repo, key_repo, _provider, use_case = _setup()
    payment = await _add_success_payment(payment_repo)

    result = await use_case(_make_command(payment.id), idempotency_key=KEY)

    assert result.status is RefundStatuses.PENDING
    assert result.replayed is False
    record = await key_repo.get(KEY)
    assert record is not None
    assert record.refund_id == result.refund_id
    assert record.response_body is not None
    assert record.response_body["refund_id"] == str(result.refund_id)
    assert "replayed" not in record.response_body


@pytest.mark.anyio
async def test_repeat_replays_without_new_refund_or_double_reservation() -> None:
    payment_repo, refund_repo, _key_repo, provider, use_case = _setup()
    payment = await _add_success_payment(payment_repo)
    first = await use_case(_make_command(payment.id), idempotency_key=KEY)

    second = await use_case(_make_command(payment.id), idempotency_key=KEY)

    assert second.replayed is True
    assert second.refund_id == first.refund_id
    assert len(refund_repo._refunds) == 1
    assert len(provider.initiated_refunds) == 1
    stored = await payment_repo.get_by_id(payment.id)
    assert stored is not None
    assert stored.refunded_amount == Decimal("40.00")  # reserved exactly once


@pytest.mark.anyio
async def test_same_key_different_amount_raises_mismatch() -> None:
    payment_repo, refund_repo, _key_repo, _provider, use_case = _setup()
    payment = await _add_success_payment(payment_repo)
    await use_case(_make_command(payment.id), idempotency_key=KEY)

    with pytest.raises(IdempotencyKeyMismatchError):
        await use_case(_make_command(payment.id, amount="50.00"), idempotency_key=KEY)

    assert len(refund_repo._refunds) == 1


@pytest.mark.anyio
async def test_same_key_different_payment_raises_mismatch() -> None:
    # payment_id from the path is part of the request hash.
    payment_repo, _refund_repo, _key_repo, _provider, use_case = _setup()
    payment = await _add_success_payment(payment_repo)
    other = make_payment(status=PaymentStatuses.SUCCESS)
    await payment_repo.add(other)
    await use_case(_make_command(payment.id), idempotency_key=KEY)

    with pytest.raises(IdempotencyKeyMismatchError):
        await use_case(_make_command(other.id), idempotency_key=KEY)


@pytest.mark.anyio
async def test_no_key_creates_independent_refunds() -> None:
    payment_repo, refund_repo, key_repo, _provider, use_case = _setup()
    payment = await _add_success_payment(payment_repo)

    first = await use_case(_make_command(payment.id))
    second = await use_case(_make_command(payment.id))

    assert first.refund_id != second.refund_id
    assert len(refund_repo._refunds) == 2
    assert key_repo._records == {}
    stored = await payment_repo.get_by_id(payment.id)
    assert stored is not None
    assert stored.refunded_amount == Decimal("80.00")


@pytest.mark.anyio
async def test_recovery_reinitiates_without_second_reservation() -> None:
    # First attempt fails at the provider: the refund is stuck in CREATED,
    # the reservation is held, no response under the key.
    payment_repo, refund_repo, key_repo, _p, failing_use_case = _setup(
        provider=RecordingFakeProvider(error=ProviderInitiationError("down"))
    )
    payment = await _add_success_payment(payment_repo)
    with pytest.raises(ProviderInitiationError):
        await failing_use_case(_make_command(payment.id), idempotency_key=KEY)
    stuck = await key_repo.get(KEY)
    assert stuck is not None
    assert stuck.response_body is None

    # A retry with a working provider drives the SAME refund to PENDING and
    # must NOT reserve again.
    provider = RecordingFakeProvider()
    session = FakeSession(payment_repo._payments, refund_repo._refunds, key_repo._records)
    use_case = CreateRefundUseCase(payment_repo, refund_repo, key_repo, provider, session)  # type: ignore[arg-type]

    result = await use_case(_make_command(payment.id), idempotency_key=KEY)

    assert result.replayed is False
    assert result.refund_id == stuck.refund_id
    assert result.status is RefundStatuses.PENDING
    assert len(refund_repo._refunds) == 1
    stored = await payment_repo.get_by_id(payment.id)
    assert stored is not None
    assert stored.refunded_amount == Decimal("40.00")  # still the original reservation
    record = await key_repo.get(KEY)
    assert record is not None
    assert record.response_body is not None


@pytest.mark.anyio
async def test_concurrent_requests_with_same_key_create_one_refund() -> None:
    payment_repo, refund_repo, _key_repo, _provider, use_case = _setup()
    payment = await _add_success_payment(payment_repo)
    results = []

    async def _call() -> None:
        results.append(await use_case(_make_command(payment.id), idempotency_key=KEY))

    async with anyio.create_task_group() as tg:
        tg.start_soon(_call)
        tg.start_soon(_call)

    assert len(refund_repo._refunds) == 1
    assert results[0].refund_id == results[1].refund_id
    stored = await payment_repo.get_by_id(payment.id)
    assert stored is not None
    assert stored.refunded_amount == Decimal("40.00")
