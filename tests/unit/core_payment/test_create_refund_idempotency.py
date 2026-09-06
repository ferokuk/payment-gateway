from datetime import UTC, datetime, timedelta
from decimal import Decimal

import anyio
import pytest
from src.contexts.core_payment.application.use_cases.create_refund import CreateRefundUseCase
from src.contexts.core_payment.domain.exceptions import (
    IdempotencyKeyMismatchError,
    RefundInitiationExpiredError,
)
from src.contexts.core_payment.domain.refund import Refund
from src.contexts.core_payment.domain.statuses import (
    PaymentStatuses,
    RefundFailureReasons,
    RefundStatuses,
)
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
    use_case = CreateRefundUseCase(
        payment_repo,  # type: ignore[arg-type]
        refund_repo,  # type: ignore[arg-type]
        key_repo,  # type: ignore[arg-type]
        provider,
        session,  # type: ignore[arg-type]
        initiation_max_age=timedelta(hours=20),
    )

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


@pytest.mark.parametrize("offset_us", [-1, 0, 1])
@pytest.mark.anyio
async def test_recovery_obeys_refund_initiation_deadline(
    monkeypatch: pytest.MonkeyPatch,
    offset_us: int,
) -> None:
    provider = RecordingFakeProvider(error=ProviderInitiationError("down"))
    payment_repo, refund_repo, key_repo, _, use_case = _setup(provider)
    payment = await _add_success_payment(payment_repo)
    with pytest.raises(ProviderInitiationError):
        await use_case(_make_command(payment.id), idempotency_key=KEY)
    record = await key_repo.get(KEY)
    assert record is not None
    refund = await refund_repo.get_by_id(record.refund_id)
    assert refund is not None
    deadline = refund.created_at + timedelta(hours=20)

    class _Clock:
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return deadline + timedelta(microseconds=offset_us)

    monkeypatch.setattr(
        "src.contexts.core_payment.application.use_cases.create_refund.datetime", _Clock
    )
    provider._error = None
    if offset_us > 0:
        with pytest.raises(RefundInitiationExpiredError) as caught:
            await use_case(_make_command(payment.id), idempotency_key=KEY)
        assert caught.value.refund_id == refund.id
        assert caught.value.expires_at == deadline
        assert provider.initiated_refunds == []
        assert provider.status_queries == []
        current = await refund_repo.get_by_id(refund.id)
        assert current is not None and current.status is RefundStatuses.CREATED
        record = await key_repo.get(KEY)
        assert record is not None and record.response_body is None
    else:
        result = await use_case(_make_command(payment.id), idempotency_key=KEY)
        assert result.refund_id == refund.id and result.status is RefundStatuses.PENDING
        assert [item.id for item in provider.initiated_refunds] == [refund.id]
    stored = await payment_repo.get_by_id(payment.id)
    assert stored is not None and stored.refunded_amount == Decimal("40.00")
    assert len(refund_repo._refunds) == 1


@pytest.mark.anyio
async def test_saved_response_is_replayed_after_initiation_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payment_repo, _, _, provider, use_case = _setup()
    payment = await _add_success_payment(payment_repo)
    first = await use_case(_make_command(payment.id), idempotency_key=KEY)
    future = datetime.now(UTC) + timedelta(days=10)

    class _Clock:
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return future

    monkeypatch.setattr(
        "src.contexts.core_payment.application.use_cases.create_refund.datetime", _Clock
    )
    replay = await use_case(_make_command(payment.id), idempotency_key=KEY)
    assert replay.replayed is True and replay.refund_id == first.refund_id
    assert len(provider.initiated_refunds) == 1


@pytest.mark.anyio
async def test_recovery_checks_deadline_after_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = RecordingFakeProvider(error=ProviderInitiationError("down"))
    payment_repo, refund_repo, key_repo, _, use_case = _setup(provider)
    payment = await _add_success_payment(payment_repo)
    with pytest.raises(ProviderInitiationError):
        await use_case(_make_command(payment.id), idempotency_key=KEY)
    record = await key_repo.get(KEY)
    assert record is not None
    refund = await refund_repo.get_by_id(record.refund_id)
    assert refund is not None
    deadline = refund.created_at + timedelta(hours=20)
    moment = deadline - timedelta(seconds=1)

    class _Clock:
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return moment

    async def slow_commit() -> None:
        nonlocal moment
        moment = deadline + timedelta(seconds=1)

    monkeypatch.setattr(
        "src.contexts.core_payment.application.use_cases.create_refund.datetime", _Clock
    )
    monkeypatch.setattr(use_case._session, "commit", slow_commit)
    provider._error = None
    with pytest.raises(RefundInitiationExpiredError):
        await use_case(_make_command(payment.id), idempotency_key=KEY)
    assert provider.initiated_refunds == []


@pytest.mark.parametrize(
    "status",
    [
        RefundStatuses.PENDING,
        RefundStatuses.ERROR,
        RefundStatuses.SUCCESS,
        RefundStatuses.FAILED,
    ],
)
@pytest.mark.anyio
async def test_recovery_restores_response_for_an_advanced_refund(status: RefundStatuses) -> None:
    provider = RecordingFakeProvider(error=ProviderInitiationError("down"))
    payment_repo, refund_repo, key_repo, _, use_case = _setup(provider)
    payment = await _add_success_payment(payment_repo)
    with pytest.raises(ProviderInitiationError):
        await use_case(_make_command(payment.id), idempotency_key=KEY)
    record = await key_repo.get(KEY)
    assert record is not None and record.response_body is None
    refund = await refund_repo.get_by_id(record.refund_id)
    assert refund is not None
    # Simulate work performed by reconciliation/callbacks after the API failed.
    refund.created_at = datetime.now(UTC) - timedelta(days=3)
    refund.status = status
    if status is RefundStatuses.FAILED:
        refund.failure_reason = RefundFailureReasons.NOT_ACCEPTED_BY_PROVIDER
        await payment_repo.release_refund_amount(payment.id, refund.amount)
    await refund_repo.update(refund, expected_status=RefundStatuses.CREATED)

    # The provider is still down, and the initiation deadline has expired.
    # Restoring a response must need neither initiation nor a new reservation.
    first = await use_case(_make_command(payment.id), idempotency_key=KEY)
    assert first.status is status and first.refund_id == refund.id
    assert first.replayed is True
    record = await key_repo.get(KEY)
    assert record is not None and record.response_body == first.model_dump(mode="json")
    if status in {RefundStatuses.PENDING, RefundStatuses.ERROR}:
        refund.mark_success()
        await refund_repo.update(refund, expected_status=status)
    second = await use_case(_make_command(payment.id), idempotency_key=KEY)
    assert second == first
    assert provider.initiated_refunds == [] and provider.status_queries == []
    assert len(refund_repo._refunds) == 1
    stored = await payment_repo.get_by_id(payment.id)
    expected = Decimal("0") if status is RefundStatuses.FAILED else Decimal("40.00")
    assert stored is not None and stored.refunded_amount == expected
    with pytest.raises(IdempotencyKeyMismatchError):
        await use_case(_make_command(payment.id, amount="50.00"), idempotency_key=KEY)


@pytest.mark.parametrize("key", [None, KEY])
@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.anyio
async def test_worker_winning_during_api_initiation_returns_committed_outcome(
    key: str | None,
    failed: bool,
) -> None:
    class _RacingProvider(RecordingFakeProvider):
        async def initiate_refund(self, refund: Refund) -> None:
            await super().initiate_refund(refund)
            winner = refund.model_copy(deep=True)
            winner.mark_pending()
            if failed:
                winner.mark_failed(RefundFailureReasons.TIMEOUT)
                await payment_repo.release_refund_amount(winner.payment_id, winner.amount)
            else:
                winner.mark_success()
            await refund_repo.update(winner, expected_status=RefundStatuses.CREATED)

    provider = _RacingProvider()
    payment_repo, refund_repo, key_repo, _, use_case = _setup(provider)
    payment = await _add_success_payment(payment_repo)
    result = await use_case(_make_command(payment.id), idempotency_key=key)
    expected_status = RefundStatuses.FAILED if failed else RefundStatuses.SUCCESS
    assert result.status is expected_status
    assert result.replayed is (key is not None)
    assert len(provider.initiated_refunds) == 1 and len(refund_repo._refunds) == 1
    stored = await payment_repo.get_by_id(payment.id)
    assert stored is not None
    assert stored.refunded_amount == (Decimal("0") if failed else Decimal("40.00"))
    if key is not None:
        record = await key_repo.get(key)
        assert record is not None and record.response_body == result.model_dump(mode="json")
        assert await use_case(_make_command(payment.id), idempotency_key=key) == result
