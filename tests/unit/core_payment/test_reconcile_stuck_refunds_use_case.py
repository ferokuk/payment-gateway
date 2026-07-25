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
) -> ReconcileStuckRefundsUseCase:
    return ReconcileStuckRefundsUseCase(
        refund_repo,  # type: ignore[arg-type]
        payment_repo,  # type: ignore[arg-type]
        provider,
        session,  # type: ignore[arg-type]
        stuck_after=STUCK_AFTER,
        give_up_after=GIVE_UP_AFTER,
        batch_size=100,
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

    # One commit closes the read transaction before any network call, then one
    # per refund: a failure on the second must not undo the first.
    assert report.completed == 2
    assert session.commits == 3
