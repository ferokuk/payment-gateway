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
)
from tests.fixtures.payment import FakePaymentRepository, make_payment
from tests.fixtures.providers import RecordingFakeProvider
from tests.fixtures.refund import FakeRefundRepository, make_refund
from tests.fixtures.session import FakeSession

STUCK_AFTER = timedelta(minutes=15)
GIVE_UP_AFTER = timedelta(days=1)
AMOUNT = Decimal("40.00")


async def _payment_with_reservation(payment_repo: FakePaymentRepository) -> Payment:
    payment = make_payment(status=PaymentStatuses.SUCCESS)  # amount 100.00
    await payment_repo.add(payment)
    await payment_repo.reserve_refund_amount(payment.id, AMOUNT)
    return payment


async def _stuck_refund(
    refund_repo: FakeRefundRepository, payment: Payment, age: timedelta = timedelta(hours=1)
) -> Refund:
    refund = make_refund(
        status=RefundStatuses.CREATED,
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


@pytest.mark.anyio
async def test_resumes_refund_the_provider_accepts() -> None:
    payment_repo, refund_repo = FakePaymentRepository(), FakeRefundRepository()
    payment = await _payment_with_reservation(payment_repo)
    refund = await _stuck_refund(refund_repo, payment)
    provider = RecordingFakeProvider()
    session = FakeSession(payment_repo._payments, refund_repo._refunds)

    report = await _use_case(payment_repo, refund_repo, provider, session)()

    assert report.resumed == 1
    stored = await refund_repo.get_by_id(refund.id)
    assert stored is not None
    assert stored.status is RefundStatuses.PENDING
    assert [initiated.id for initiated in provider.initiated_refunds] == [refund.id]
    # The reservation stays: the refund is alive at the provider now.
    stored_payment = await payment_repo.get_by_id(payment.id)
    assert stored_payment is not None
    assert stored_payment.refunded_amount == AMOUNT


@pytest.mark.anyio
async def test_closes_refund_the_provider_refuses_and_releases_reservation() -> None:
    payment_repo, refund_repo = FakePaymentRepository(), FakeRefundRepository()
    payment = await _payment_with_reservation(payment_repo)
    refund = await _stuck_refund(refund_repo, payment)
    provider = RecordingFakeProvider(refund_error=ProviderRejectedError("no such refund"))
    session = FakeSession(payment_repo._payments, refund_repo._refunds)

    report = await _use_case(payment_repo, refund_repo, provider, session)()

    assert report.closed == 1
    stored = await refund_repo.get_by_id(refund.id)
    assert stored is not None
    assert stored.status is RefundStatuses.FAILED
    assert stored.failure_reason is RefundFailureReasons.NOT_ACCEPTED_BY_PROVIDER
    stored_payment = await payment_repo.get_by_id(payment.id)
    assert stored_payment is not None
    assert stored_payment.refunded_amount == Decimal("0")


@pytest.mark.anyio
async def test_holds_reservation_while_the_outcome_is_unknown() -> None:
    payment_repo, refund_repo = FakePaymentRepository(), FakeRefundRepository()
    payment = await _payment_with_reservation(payment_repo)
    refund = await _stuck_refund(refund_repo, payment)
    provider = RecordingFakeProvider(refund_error=ProviderInitiationError("connection reset"))
    session = FakeSession(payment_repo._payments, refund_repo._refunds)

    report = await _use_case(payment_repo, refund_repo, provider, session)()

    # Releasing here could let the same money go out twice.
    assert report.unresolved == 1
    stored = await refund_repo.get_by_id(refund.id)
    assert stored is not None
    assert stored.status is RefundStatuses.CREATED
    stored_payment = await payment_repo.get_by_id(payment.id)
    assert stored_payment is not None
    assert stored_payment.refunded_amount == AMOUNT


@pytest.mark.anyio
async def test_leaves_fresh_refunds_alone() -> None:
    payment_repo, refund_repo = FakePaymentRepository(), FakeRefundRepository()
    payment = await _payment_with_reservation(payment_repo)
    refund = await _stuck_refund(refund_repo, payment, age=timedelta(minutes=1))
    provider = RecordingFakeProvider()
    session = FakeSession(payment_repo._payments, refund_repo._refunds)

    report = await _use_case(payment_repo, refund_repo, provider, session)()

    # A minute-old refund is still in flight, not stuck.
    assert (report.resumed, report.closed, report.unresolved, report.abandoned) == (0, 0, 0, 0)
    assert provider.initiated_refunds == []
    stored = await refund_repo.get_by_id(refund.id)
    assert stored is not None
    assert stored.status is RefundStatuses.CREATED


@pytest.mark.anyio
async def test_reports_refunds_too_old_to_retry_without_touching_them() -> None:
    payment_repo, refund_repo = FakePaymentRepository(), FakeRefundRepository()
    payment = await _payment_with_reservation(payment_repo)
    refund = await _stuck_refund(refund_repo, payment, age=timedelta(days=3))
    provider = RecordingFakeProvider()
    session = FakeSession(payment_repo._payments, refund_repo._refunds)

    report = await _use_case(payment_repo, refund_repo, provider, session)()

    # Past the provider's deduplication window a repeat could refund twice,
    # so the refund is only reported — a human decides.
    assert report.abandoned == 1
    assert report.resumed == 0
    assert provider.initiated_refunds == []
    stored = await refund_repo.get_by_id(refund.id)
    assert stored is not None
    assert stored.status is RefundStatuses.CREATED


@pytest.mark.anyio
async def test_skips_refund_finished_by_a_competitor() -> None:
    class _StaleOnUpdateRepository(FakeRefundRepository):
        async def update(self, refund: Refund, *, expected_status: RefundStatuses) -> None:
            raise StaleRefundStateError(refund.id, expected_status)

    payment_repo, refund_repo = FakePaymentRepository(), _StaleOnUpdateRepository()
    payment = await _payment_with_reservation(payment_repo)
    await _stuck_refund(refund_repo, payment)
    provider = RecordingFakeProvider()
    session = FakeSession(payment_repo._payments, refund_repo._refunds)

    report = await _use_case(payment_repo, refund_repo, provider, session)()

    # Another reconciler or a late callback got there first; the winner did the
    # bookkeeping, so we must not repeat it.
    assert report.conflicts == 1
    stored_payment = await payment_repo.get_by_id(payment.id)
    assert stored_payment is not None
    assert stored_payment.refunded_amount == AMOUNT


@pytest.mark.anyio
async def test_commits_each_refund_separately() -> None:
    payment_repo, refund_repo = FakePaymentRepository(), FakeRefundRepository()
    payment = await _payment_with_reservation(payment_repo)
    await _stuck_refund(refund_repo, payment, age=timedelta(hours=1))
    await _stuck_refund(refund_repo, payment, age=timedelta(hours=2))
    provider = RecordingFakeProvider()
    session = FakeSession(payment_repo._payments, refund_repo._refunds)

    report = await _use_case(payment_repo, refund_repo, provider, session)()

    # One commit closes the read transaction before any network call, then one
    # per refund: a failure on the second must not undo the first.
    assert report.resumed == 2
    assert session.commits == 3
