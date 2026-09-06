from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from src.contexts.core_payment.application.use_cases.reconcile_stuck_refunds import (
    ReconcileStuckRefundsUseCase,
)
from src.contexts.core_payment.domain.exceptions import StaleRefundStateError
from src.contexts.core_payment.domain.payment import Payment
from src.contexts.core_payment.domain.refund import Refund
from src.contexts.core_payment.domain.statuses import (
    PaymentStatuses,
    RefundFailureReasons,
    RefundStatuses,
)
from src.contexts.core_payment.infrastructure.database.repositories import (
    RefundReconciliationCandidate,
)
from src.contexts.core_payment.infrastructure.providers.base import (
    ProviderInitiationError,
    ProviderRejectedError,
    RefundProviderState,
    RefundProviderStatus,
)
from tests.fixtures.payment import FakePaymentRepository, make_payment
from tests.fixtures.providers import RecordingFakeProvider
from tests.fixtures.refund import FakeRefundRepository, make_refund
from tests.fixtures.session import FakeSession

STUCK_AFTER = timedelta(minutes=15)
GIVE_UP_AFTER = timedelta(days=1)
AMOUNT = Decimal("40.00")

ABSENT = RefundProviderStatus(RefundProviderState.ABSENT)
PENDING_AT_PROVIDER = RefundProviderStatus(RefundProviderState.PENDING)
SUCCEEDED = RefundProviderStatus(RefundProviderState.SUCCEEDED)
FAILED = RefundProviderStatus(RefundProviderState.FAILED, RefundFailureReasons.TIMEOUT)
UNKNOWN = RefundProviderStatus(RefundProviderState.UNKNOWN)


async def _payment_with_reservation(payment_repo: FakePaymentRepository) -> Payment:
    payment = make_payment(status=PaymentStatuses.SUCCESS)  # amount 100.00
    await payment_repo.add(payment)
    await payment_repo.reserve_refund_amount(payment.id, AMOUNT)
    return payment


async def _unresolved_refund(
    refund_repo: FakeRefundRepository,
    payment: Payment,
    status: RefundStatuses = RefundStatuses.CREATED,
    age: timedelta = timedelta(hours=1),
) -> Refund:
    refund = make_refund(
        status=status,
        payment_id=payment.id,
        amount=AMOUNT,
        created_at=datetime.now(UTC) - age,
    )
    await refund_repo.add(refund)
    return refund


def _use_case(
    payment_repo: FakePaymentRepository,
    refund_repo: FakeRefundRepository,
    provider: RecordingFakeProvider,
    session: FakeSession,
    *,
    batch_size: int = 100,
) -> ReconcileStuckRefundsUseCase:
    return ReconcileStuckRefundsUseCase(
        refund_repo,  # type: ignore[arg-type]
        payment_repo,  # type: ignore[arg-type]
        provider,
        session,  # type: ignore[arg-type]
        stuck_after=STUCK_AFTER,
        initiation_max_age=GIVE_UP_AFTER,
        batch_size=batch_size,
    )


async def _reserved(payment_repo: FakePaymentRepository, payment: Payment) -> Decimal:
    stored = await payment_repo.get_by_id(payment.id)
    assert stored is not None
    return stored.refunded_amount


async def _status_of(refund_repo: FakeRefundRepository, refund: Refund) -> Refund:
    stored = await refund_repo.get_by_id(refund.id)
    assert stored is not None
    return stored


# --- The refund never left created ---


@pytest.mark.anyio
async def test_absent_refund_inside_the_window_is_re_initiated() -> None:
    payment_repo, refund_repo = FakePaymentRepository(), FakeRefundRepository()
    payment = await _payment_with_reservation(payment_repo)
    refund = await _unresolved_refund(refund_repo, payment)
    provider = RecordingFakeProvider(refund_status=ABSENT)
    session = FakeSession(payment_repo._payments, refund_repo._refunds)

    report = await _use_case(payment_repo, refund_repo, provider, session)()

    # The provider confirmed it never took the refund, and it is young enough
    # for the deduplication key to still be alive — so try again.
    assert report.resumed == 1
    assert [initiated.id for initiated in provider.initiated_refunds] == [refund.id]
    assert (await _status_of(refund_repo, refund)).status is RefundStatuses.PENDING
    assert await _reserved(payment_repo, payment) == AMOUNT


@pytest.mark.anyio
async def test_absent_refund_past_the_window_is_closed_without_retry() -> None:
    payment_repo, refund_repo = FakePaymentRepository(), FakeRefundRepository()
    payment = await _payment_with_reservation(payment_repo)
    refund = await _unresolved_refund(refund_repo, payment, age=timedelta(days=3))
    provider = RecordingFakeProvider(refund_status=ABSENT)
    session = FakeSession(payment_repo._payments, refund_repo._refunds)

    report = await _use_case(payment_repo, refund_repo, provider, session)()

    # Absence is confirmed, so the money goes back; re-initiating this late
    # could slip past the provider's deduplication and pay twice.
    assert report.closed == 1
    assert provider.initiated_refunds == []
    stored = await _status_of(refund_repo, refund)
    assert stored.status is RefundStatuses.FAILED
    assert stored.failure_reason is RefundFailureReasons.NOT_ACCEPTED_BY_PROVIDER
    assert await _reserved(payment_repo, payment) == Decimal("0")


@pytest.mark.parametrize("offset_us", [-1, 0, 1])
@pytest.mark.anyio
async def test_reconciliation_obeys_same_initiation_deadline(
    monkeypatch: pytest.MonkeyPatch,
    offset_us: int,
) -> None:
    payment_repo, refund_repo = FakePaymentRepository(), FakeRefundRepository()
    payment = await _payment_with_reservation(payment_repo)
    refund = await _unresolved_refund(refund_repo, payment)

    class _Clock:
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return refund.created_at + GIVE_UP_AFTER + timedelta(microseconds=offset_us)

    monkeypatch.setattr(
        "src.contexts.core_payment.application.use_cases.reconcile_stuck_refunds.datetime", _Clock
    )
    provider = RecordingFakeProvider(refund_status=ABSENT)
    session = FakeSession(payment_repo._payments, refund_repo._refunds)
    report = await _use_case(payment_repo, refund_repo, provider, session)()
    if offset_us > 0:
        assert report.closed == 1 and report.resumed == 0
        assert provider.initiated_refunds == []
    else:
        assert report.resumed == 1 and report.closed == 0
        assert [item.id for item in provider.initiated_refunds] == [refund.id]


@pytest.mark.anyio
async def test_slow_status_query_cannot_extend_initiation_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payment_repo, refund_repo = FakePaymentRepository(), FakeRefundRepository()
    payment = await _payment_with_reservation(payment_repo)
    refund = await _unresolved_refund(refund_repo, payment)
    deadline = refund.created_at + GIVE_UP_AFTER
    moment = deadline - timedelta(seconds=1)

    class _Clock:
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return moment

    class _SlowProvider(RecordingFakeProvider):
        async def get_refund_status(self, refund: Refund) -> RefundProviderStatus:
            nonlocal moment
            moment = deadline + timedelta(seconds=1)
            return ABSENT

    monkeypatch.setattr(
        "src.contexts.core_payment.application.use_cases.reconcile_stuck_refunds.datetime", _Clock
    )
    provider = _SlowProvider()
    session = FakeSession(payment_repo._payments, refund_repo._refunds)
    report = await _use_case(payment_repo, refund_repo, provider, session)()
    assert report.closed == 1 and report.resumed == 0
    assert provider.initiated_refunds == []
    assert await _reserved(payment_repo, payment) == Decimal("0")


@pytest.mark.anyio
async def test_refusal_on_re_initiation_closes_the_refund() -> None:
    payment_repo, refund_repo = FakePaymentRepository(), FakeRefundRepository()
    payment = await _payment_with_reservation(payment_repo)
    await _unresolved_refund(refund_repo, payment)
    provider = RecordingFakeProvider(
        refund_status=ABSENT, refund_error=ProviderRejectedError("payment too old")
    )
    session = FakeSession(payment_repo._payments, refund_repo._refunds)

    report = await _use_case(payment_repo, refund_repo, provider, session)()

    assert report.closed == 1
    assert await _reserved(payment_repo, payment) == Decimal("0")


@pytest.mark.anyio
async def test_silent_provider_on_re_initiation_holds_the_reservation() -> None:
    payment_repo, refund_repo = FakePaymentRepository(), FakeRefundRepository()
    payment = await _payment_with_reservation(payment_repo)
    refund = await _unresolved_refund(refund_repo, payment)
    provider = RecordingFakeProvider(
        refund_status=ABSENT, refund_error=ProviderInitiationError("connection reset")
    )
    session = FakeSession(payment_repo._payments, refund_repo._refunds)

    report = await _use_case(payment_repo, refund_repo, provider, session)()

    assert report.unresolved == 1
    assert (await _status_of(refund_repo, refund)).status is RefundStatuses.CREATED
    assert await _reserved(payment_repo, payment) == AMOUNT


@pytest.mark.anyio
async def test_refund_the_provider_took_after_all_moves_to_pending() -> None:
    payment_repo, refund_repo = FakePaymentRepository(), FakeRefundRepository()
    payment = await _payment_with_reservation(payment_repo)
    refund = await _unresolved_refund(refund_repo, payment)
    provider = RecordingFakeProvider(refund_status=PENDING_AT_PROVIDER)
    session = FakeSession(payment_repo._payments, refund_repo._refunds)

    report = await _use_case(payment_repo, refund_repo, provider, session)()

    # Our initiation call died on the way back, but the provider did take it:
    # no second initiation, just catch our own state up.
    assert report.resumed == 1
    assert provider.initiated_refunds == []
    assert (await _status_of(refund_repo, refund)).status is RefundStatuses.PENDING


# --- The callback never arrived: eternal pending ---


@pytest.mark.anyio
async def test_eternal_pending_is_settled_as_success() -> None:
    payment_repo, refund_repo = FakePaymentRepository(), FakeRefundRepository()
    payment = await _payment_with_reservation(payment_repo)
    refund = await _unresolved_refund(refund_repo, payment, status=RefundStatuses.PENDING)
    provider = RecordingFakeProvider(refund_status=SUCCEEDED)
    session = FakeSession(payment_repo._payments, refund_repo._refunds)

    report = await _use_case(payment_repo, refund_repo, provider, session)()

    assert report.completed == 1
    assert (await _status_of(refund_repo, refund)).status is RefundStatuses.SUCCESS
    # The money really left: the reservation becomes the actual refund.
    assert await _reserved(payment_repo, payment) == AMOUNT


@pytest.mark.anyio
async def test_eternal_pending_is_settled_as_failure_and_releases() -> None:
    payment_repo, refund_repo = FakePaymentRepository(), FakeRefundRepository()
    payment = await _payment_with_reservation(payment_repo)
    refund = await _unresolved_refund(refund_repo, payment, status=RefundStatuses.PENDING)
    provider = RecordingFakeProvider(refund_status=FAILED)
    session = FakeSession(payment_repo._payments, refund_repo._refunds)

    report = await _use_case(payment_repo, refund_repo, provider, session)()

    assert report.closed == 1
    stored = await _status_of(refund_repo, refund)
    assert stored.status is RefundStatuses.FAILED
    assert stored.failure_reason is RefundFailureReasons.TIMEOUT
    assert await _reserved(payment_repo, payment) == Decimal("0")


# --- The provider itself did not know: error ---


@pytest.mark.anyio
async def test_error_is_settled_as_success() -> None:
    payment_repo, refund_repo = FakePaymentRepository(), FakeRefundRepository()
    payment = await _payment_with_reservation(payment_repo)
    refund = await _unresolved_refund(refund_repo, payment, status=RefundStatuses.ERROR)
    provider = RecordingFakeProvider(refund_status=SUCCEEDED)
    session = FakeSession(payment_repo._payments, refund_repo._refunds)

    report = await _use_case(payment_repo, refund_repo, provider, session)()

    assert report.completed == 1
    assert (await _status_of(refund_repo, refund)).status is RefundStatuses.SUCCESS


@pytest.mark.anyio
async def test_error_is_settled_as_failure_and_releases() -> None:
    payment_repo, refund_repo = FakePaymentRepository(), FakeRefundRepository()
    payment = await _payment_with_reservation(payment_repo)
    refund = await _unresolved_refund(refund_repo, payment, status=RefundStatuses.ERROR)
    provider = RecordingFakeProvider(refund_status=FAILED)
    session = FakeSession(payment_repo._payments, refund_repo._refunds)

    report = await _use_case(payment_repo, refund_repo, provider, session)()

    assert report.closed == 1
    assert (await _status_of(refund_repo, refund)).status is RefundStatuses.FAILED
    assert await _reserved(payment_repo, payment) == Decimal("0")


# --- Answers that must not be acted upon ---


@pytest.mark.anyio
async def test_unknown_answer_changes_nothing() -> None:
    payment_repo, refund_repo = FakePaymentRepository(), FakeRefundRepository()
    payment = await _payment_with_reservation(payment_repo)
    refund = await _unresolved_refund(refund_repo, payment, status=RefundStatuses.PENDING)
    provider = RecordingFakeProvider(refund_status=UNKNOWN)
    session = FakeSession(payment_repo._payments, refund_repo._refunds)

    report = await _use_case(payment_repo, refund_repo, provider, session)()

    assert report.unresolved == 1
    assert (await _status_of(refund_repo, refund)).status is RefundStatuses.PENDING
    assert await _reserved(payment_repo, payment) == AMOUNT


@pytest.mark.anyio
async def test_denial_of_an_accepted_refund_is_only_reported() -> None:
    payment_repo, refund_repo = FakePaymentRepository(), FakeRefundRepository()
    payment = await _payment_with_reservation(payment_repo)
    refund = await _unresolved_refund(refund_repo, payment, status=RefundStatuses.PENDING)
    provider = RecordingFakeProvider(refund_status=ABSENT)
    session = FakeSession(payment_repo._payments, refund_repo._refunds)

    report = await _use_case(payment_repo, refund_repo, provider, session)()

    # The provider denies a refund we know it accepted. Believing the denial
    # and releasing could let the same money go out twice.
    assert report.disputed == 1
    assert (await _status_of(refund_repo, refund)).status is RefundStatuses.PENDING
    assert await _reserved(payment_repo, payment) == AMOUNT


@pytest.mark.anyio
async def test_failure_without_a_reason_is_not_applied() -> None:
    payment_repo, refund_repo = FakePaymentRepository(), FakeRefundRepository()
    payment = await _payment_with_reservation(payment_repo)
    refund = await _unresolved_refund(refund_repo, payment, status=RefundStatuses.PENDING)
    provider = RecordingFakeProvider(refund_status=RefundProviderStatus(RefundProviderState.FAILED))
    session = FakeSession(payment_repo._payments, refund_repo._refunds)

    report = await _use_case(payment_repo, refund_repo, provider, session)()

    # Inventing a reason would put a lie into the merchant's report.
    assert report.unresolved == 1
    assert (await _status_of(refund_repo, refund)).status is RefundStatuses.PENDING


@pytest.mark.anyio
async def test_leaves_fresh_refunds_alone() -> None:
    payment_repo, refund_repo = FakePaymentRepository(), FakeRefundRepository()
    payment = await _payment_with_reservation(payment_repo)
    await _unresolved_refund(refund_repo, payment, age=timedelta(minutes=1))
    provider = RecordingFakeProvider(refund_status=ABSENT)
    session = FakeSession(payment_repo._payments, refund_repo._refunds)

    report = await _use_case(payment_repo, refund_repo, provider, session)()

    # A minute-old refund is still in flight, not stuck.
    assert provider.status_queries == []
    assert report.model_dump() == dict.fromkeys(report.model_dump(), 0)


@pytest.mark.anyio
async def test_skips_refund_finished_by_a_competitor() -> None:
    class _StaleOnUpdateRepository(FakeRefundRepository):
        async def update(self, refund: Refund, *, expected_status: RefundStatuses) -> None:
            raise StaleRefundStateError(refund.id, expected_status)

    payment_repo, refund_repo = FakePaymentRepository(), _StaleOnUpdateRepository()
    payment = await _payment_with_reservation(payment_repo)
    await _unresolved_refund(refund_repo, payment, status=RefundStatuses.PENDING)
    provider = RecordingFakeProvider(refund_status=FAILED)
    session = FakeSession(payment_repo._payments, refund_repo._refunds)

    report = await _use_case(payment_repo, refund_repo, provider, session)()

    # The winner of the CAS did the bookkeeping, including the release.
    assert report.conflicts == 1
    assert await _reserved(payment_repo, payment) == AMOUNT


@pytest.mark.anyio
async def test_commits_each_refund_separately() -> None:
    payment_repo, refund_repo = FakePaymentRepository(), FakeRefundRepository()
    payment = await _payment_with_reservation(payment_repo)
    await _unresolved_refund(
        refund_repo, payment, status=RefundStatuses.PENDING, age=timedelta(hours=1)
    )
    await _unresolved_refund(
        refund_repo, payment, status=RefundStatuses.PENDING, age=timedelta(hours=2)
    )
    provider = RecordingFakeProvider(refund_status=SUCCEEDED)
    session = FakeSession(payment_repo._payments, refund_repo._refunds)

    report = await _use_case(payment_repo, refund_repo, provider, session)()

    # Close the read transaction, then persist the schedule and outcome
    # separately for each refund: a later failure cannot undo earlier work.
    assert report.completed == 2
    assert session.commits == 5


@pytest.mark.parametrize("head_status", [UNKNOWN, ABSENT, PENDING_AT_PROVIDER])
@pytest.mark.anyio
async def test_unresolved_first_batch_does_not_starve_later_refunds(
    head_status: RefundProviderStatus,
) -> None:
    payment_repo, refund_repo = FakePaymentRepository(), FakeRefundRepository()
    head: list[Refund] = []
    tail: list[Refund] = []
    for age, group in [(timedelta(days=3), head), (timedelta(hours=1), tail)]:
        for _ in range(2):
            payment = await _payment_with_reservation(payment_repo)
            group.append(
                await _unresolved_refund(refund_repo, payment, RefundStatuses.PENDING, age)
            )

    class _Provider(RecordingFakeProvider):
        async def get_refund_status(self, refund: Refund) -> RefundProviderStatus:
            self.status_queries.append(refund)
            return head_status if refund.id in {item.id for item in head} else FAILED

    provider = _Provider()
    for _ in range(2):
        # Reconstruct the worker between passes: scheduling lives in the store.
        session = FakeSession(payment_repo._payments, refund_repo._refunds)
        report = await _use_case(payment_repo, refund_repo, provider, session, batch_size=2)()

    assert report.closed == 2
    assert [refund.id for refund in provider.status_queries] == [
        refund.id for refund in head + tail
    ]
    for refund in head:
        assert (await _status_of(refund_repo, refund)).status is RefundStatuses.PENDING
        stored_payment = await payment_repo.get_by_id(refund.payment_id)
        assert stored_payment is not None and stored_payment.refunded_amount == AMOUNT
    for refund in tail:
        assert (await _status_of(refund_repo, refund)).status is RefundStatuses.FAILED
        stored_payment = await payment_repo.get_by_id(refund.payment_id)
        assert stored_payment is not None and stored_payment.refunded_amount == Decimal("0")


@pytest.mark.anyio
async def test_backoff_is_persistent_due_at_boundary_and_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    moment = datetime.now(UTC)

    class _Clock:
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return moment

    monkeypatch.setattr(
        "src.contexts.core_payment.application.use_cases.reconcile_stuck_refunds.datetime", _Clock
    )
    payment_repo, refund_repo = FakePaymentRepository(), FakeRefundRepository()
    payment = await _payment_with_reservation(payment_repo)
    refund = await _unresolved_refund(refund_repo, payment)
    provider = RecordingFakeProvider(refund_status=UNKNOWN)
    session = FakeSession(payment_repo._payments, refund_repo._refunds)
    for attempt, seconds in enumerate([60, 120, 240, 480, 960, 1920, 3600, 3600], start=1):
        use_case = _use_case(payment_repo, refund_repo, provider, session)
        assert (await use_case()).unresolved == 1
        due_at, attempts = refund_repo._reconciliation[refund.id]
        assert attempts == attempt
        assert due_at == moment + timedelta(seconds=seconds)
        moment = due_at - timedelta(microseconds=1)
        assert (await use_case()).unresolved == 0
        moment = due_at
    assert len(provider.status_queries) == 8


@pytest.mark.anyio
async def test_schedule_survives_provider_crash_and_later_refund_is_processed() -> None:
    payment_repo, refund_repo = FakePaymentRepository(), FakeRefundRepository()
    for age in (timedelta(hours=2), timedelta(hours=1)):
        payment = await _payment_with_reservation(payment_repo)
        await _unresolved_refund(refund_repo, payment, age=age)

    class _CrashingProvider(RecordingFakeProvider):
        async def get_refund_status(self, refund: Refund) -> RefundProviderStatus:
            raise RuntimeError("adapter bug")

    session = FakeSession(payment_repo._payments, refund_repo._refunds)
    with pytest.raises(RuntimeError, match="adapter bug"):
        await _use_case(payment_repo, refund_repo, _CrashingProvider(), session, batch_size=1)()
    assert session.commits == 2
    assert (
        await _use_case(
            payment_repo,
            refund_repo,
            RecordingFakeProvider(refund_status=SUCCEEDED),
            session,
            batch_size=1,
        )()
    ).completed == 1


@pytest.mark.anyio
async def test_lost_schedule_claim_does_not_query_provider() -> None:
    class _ClaimedRepository(FakeRefundRepository):
        async def schedule_reconciliation(
            self, candidate: RefundReconciliationCandidate, *, next_check_at: datetime
        ) -> bool:
            return False

    payment_repo, refund_repo = FakePaymentRepository(), _ClaimedRepository()
    payment = await _payment_with_reservation(payment_repo)
    await _unresolved_refund(refund_repo, payment)
    provider = RecordingFakeProvider(refund_status=SUCCEEDED)
    session = FakeSession(payment_repo._payments, refund_repo._refunds)
    report = await _use_case(payment_repo, refund_repo, provider, session)()
    assert report.conflicts == 1
    assert provider.status_queries == []
