from dataclasses import replace
from decimal import Decimal

import anyio
import pytest
from src.contexts.core_payment.application.use_cases.create_payment import (
    CreatePaymentUseCase,
    _request_hash,
)
from src.contexts.core_payment.domain.exceptions import (
    IdempotencyKeyMismatchError,
    StalePaymentStateError,
)
from src.contexts.core_payment.domain.payment import Payment
from src.contexts.core_payment.domain.statuses import PaymentStatuses
from src.contexts.core_payment.infrastructure.database.repositories import IdempotencyRecord
from tests.fixtures.idempotency import FakeIdempotencyKeyRepository
from tests.fixtures.payment import FakePaymentRepository, make_payment
from tests.fixtures.providers import RecordingFakeProvider
from tests.fixtures.session import FakeSession
from tests.unit.core_payment.test_create_payment_use_case import _make_command

KEY = "idem-key-1"


def _setup(
    provider: RecordingFakeProvider | None = None,
) -> tuple[
    FakePaymentRepository, FakeIdempotencyKeyRepository, RecordingFakeProvider, CreatePaymentUseCase
]:
    repo = FakePaymentRepository()
    key_repo = FakeIdempotencyKeyRepository()
    provider = provider or RecordingFakeProvider()
    session = FakeSession(repo._payments, key_repo._records)
    use_case = CreatePaymentUseCase(repo, key_repo, provider, session)  # type: ignore[arg-type]
    return repo, key_repo, provider, use_case


@pytest.mark.anyio
async def test_first_request_with_key_stores_record_and_response() -> None:
    _repo, key_repo, _provider, use_case = _setup()

    result = await use_case(_make_command(), idempotency_key=KEY)

    assert result.status is PaymentStatuses.PENDING
    assert result.replayed is False
    record = await key_repo.get(KEY)
    assert record is not None
    assert record.payment_id == result.payment_id
    assert record.response_body is not None
    assert record.response_body["payment_id"] == str(result.payment_id)
    assert "replayed" not in record.response_body


@pytest.mark.anyio
async def test_repeat_request_replays_without_new_payment_or_initiation() -> None:
    repo, _key_repo, provider, use_case = _setup()
    first = await use_case(_make_command(), idempotency_key=KEY)

    second = await use_case(_make_command(), idempotency_key=KEY)

    assert second.replayed is True
    assert second.payment_id == first.payment_id
    assert len(repo._payments) == 1
    assert len(provider.initiated) == 1


@pytest.mark.anyio
async def test_same_key_different_body_raises_mismatch() -> None:
    repo, _key_repo, _provider, use_case = _setup()
    await use_case(_make_command(), idempotency_key=KEY)

    with pytest.raises(IdempotencyKeyMismatchError):
        await use_case(_make_command(metadata={"other": "body"}), idempotency_key=KEY)

    assert len(repo._payments) == 1


@pytest.mark.anyio
async def test_no_key_creates_independent_payments() -> None:
    repo, key_repo, _provider, use_case = _setup()

    first = await use_case(_make_command())
    second = await use_case(_make_command())

    assert first.payment_id != second.payment_id
    assert len(repo._payments) == 2
    assert key_repo._records == {}


@pytest.mark.anyio
async def test_recovery_finishes_created_payment() -> None:
    # Recovery: the first request failed at the provider, the payment is stuck
    # in CREATED, no response recorded under the key.
    from src.contexts.core_payment.infrastructure.providers.base import (
        ProviderInitiationError,
    )

    repo, key_repo, _, failing_use_case = _setup(
        provider=RecordingFakeProvider(error=ProviderInitiationError("down"))
    )
    with pytest.raises(ProviderInitiationError):
        await failing_use_case(_make_command(), idempotency_key=KEY)
    stuck = await key_repo.get(KEY)
    assert stuck is not None
    assert stuck.response_body is None

    # A retry with a working provider drives the SAME payment to PENDING.
    provider = RecordingFakeProvider()
    session = FakeSession(repo._payments, key_repo._records)
    use_case = CreatePaymentUseCase(repo, key_repo, provider, session)  # type: ignore[arg-type]

    result = await use_case(_make_command(), idempotency_key=KEY)

    assert result.replayed is False
    assert result.payment_id == stuck.payment_id
    assert result.status is PaymentStatuses.PENDING
    assert len(repo._payments) == 1
    record = await key_repo.get(KEY)
    assert record is not None
    assert record.response_body is not None


@pytest.mark.anyio
async def test_recovery_when_payment_already_pending_rereads_key_and_replays() -> None:
    # Recovery when a competitor already finished the operation: our first
    # SELECT of the key saw the record still WITHOUT a response, but the payment
    # is already PENDING -> the algorithm must re-read the key and return a
    # replay, without initiating the payment again.
    repo, key_repo, provider, use_case = _setup()
    first = await use_case(_make_command(), idempotency_key=KEY)

    class _StaleFirstReadKeyRepo(FakeIdempotencyKeyRepository):
        """First get returns the record without response — a snapshot before the rival commit."""

        def __init__(self, records: dict[str, IdempotencyRecord]) -> None:
            super().__init__()
            self._records = records
            self._stale = True

        async def get(self, key: str) -> IdempotencyRecord | None:
            current = await super().get(key)
            if self._stale and current is not None:
                self._stale = False
                return replace(current, response_body=None)
            return current

    stale_repo = _StaleFirstReadKeyRepo(key_repo._records)
    session = FakeSession(repo._payments, stale_repo._records)
    retry_use_case = CreatePaymentUseCase(repo, stale_repo, provider, session)  # type: ignore[arg-type]

    result = await retry_use_case(_make_command(), idempotency_key=KEY)

    assert result.replayed is True
    assert result.payment_id == first.payment_id
    assert len(provider.initiated) == 1  # no repeated initiation


@pytest.mark.anyio
async def test_lost_insert_race_falls_back_to_replay() -> None:
    # Race: our SELECT missed the rival row, INSERT failed on the unique
    # constraint, SAVEPOINT rolled back the duplicate payment, then — replay
    # of the rival response.
    repo, key_repo, provider, rival = _setup()
    other = await rival(_make_command(), idempotency_key=KEY)

    class _BlindKeyRepo(FakeIdempotencyKeyRepository):
        """First get does not "see" the rival row — like a SELECT before the rival commit."""

        def __init__(self, records: dict[str, IdempotencyRecord]) -> None:
            super().__init__()
            self._records = records
            self._blind = True

        async def get(self, key: str) -> IdempotencyRecord | None:
            if self._blind:
                self._blind = False
                return None
            return await super().get(key)

    blind_repo = _BlindKeyRepo(key_repo._records)
    session = FakeSession(repo._payments, blind_repo._records)
    use_case = CreatePaymentUseCase(repo, blind_repo, provider, session)  # type: ignore[arg-type]

    result = await use_case(_make_command(), idempotency_key=KEY)

    assert result.replayed is True
    assert result.payment_id == other.payment_id
    assert len(repo._payments) == 1  # duplicate payment rolled back with the SAVEPOINT


@pytest.mark.anyio
async def test_concurrent_requests_with_same_key_create_one_payment() -> None:
    repo, _key_repo, _provider, use_case = _setup()
    results = []

    async def _call() -> None:
        results.append(await use_case(_make_command(), idempotency_key=KEY))

    async with anyio.create_task_group() as tg:
        tg.start_soon(_call)
        tg.start_soon(_call)

    assert len(repo._payments) == 1
    assert results[0].payment_id == results[1].payment_id


class _RacedPaymentRepo(FakePaymentRepository):
    """Before the first update a competitor completes the recovery: PENDING + response."""

    def __init__(self, key_repo: FakeIdempotencyKeyRepository, key: str) -> None:
        super().__init__()
        self._key_repo = key_repo
        self._key = key
        self._raced = False

    async def update(self, payment: Payment, *, expected_status: PaymentStatuses) -> None:
        if not self._raced:
            self._raced = True
            winner = self._payments[payment.id].model_copy(deep=True)
            winner.mark_pending()
            self._payments[payment.id] = winner
            # A valid snapshot of the winner's 201 response; amount 999.99 is
            # the marker the test uses to tell the rival response from its own.
            await self._key_repo.set_response(
                self._key,
                {
                    "payment_id": str(payment.id),
                    "status": "pending",
                    "amount": "999.99",
                    "currency": "EUR",
                    "created_at": "2026-07-09T00:00:00Z",
                },
            )
        await super().update(payment, expected_status=expected_status)


@pytest.mark.anyio
async def test_stale_recovery_returns_winner_replay_without_stomp() -> None:
    key_repo = FakeIdempotencyKeyRepository()
    repo = _RacedPaymentRepo(key_repo, KEY)
    provider = RecordingFakeProvider()
    session = FakeSession(repo._payments, key_repo._records)
    use_case = CreatePaymentUseCase(repo, key_repo, provider, session)  # type: ignore[arg-type]
    # A stuck CREATED payment with a reserved key and no response.
    stuck = make_payment(status=PaymentStatuses.CREATED)
    await repo.add(stuck)
    await key_repo.add(
        IdempotencyRecord(
            key=KEY,
            request_hash=_request_hash(_make_command()),
            payment_id=stuck.id,
        )
    )

    result = await use_case(_make_command(), idempotency_key=KEY)

    assert result.replayed is True
    assert result.payment_id == stuck.id
    assert result.amount == Decimal("999.99")  # the winner's response, not ours (250.00)
    stored = await repo.get_by_id(stuck.id)
    assert stored is not None
    assert stored.status is PaymentStatuses.PENDING  # the winner's state, not stomped
    record = await key_repo.get(KEY)
    assert record is not None
    assert record.response_body is not None
    assert record.response_body["amount"] == "999.99"
    # our own set_response was never called — the winner's response is intact


@pytest.mark.anyio
async def test_stale_conflict_without_key_is_reraised() -> None:
    key_repo = FakeIdempotencyKeyRepository()
    repo = _RacedPaymentRepo(key_repo, KEY)
    provider = RecordingFakeProvider()
    session = FakeSession(repo._payments, key_repo._records)
    use_case = CreatePaymentUseCase(repo, key_repo, provider, session)  # type: ignore[arg-type]
    # The winner in _RacedPaymentRepo will try set_response on a nonexistent
    # key — for the no-key scenario reserve it manually up front.
    await key_repo.add(
        IdempotencyRecord(key=KEY, request_hash="0" * 64, payment_id=make_payment().id)
    )

    with pytest.raises(StalePaymentStateError):
        await use_case(_make_command())
