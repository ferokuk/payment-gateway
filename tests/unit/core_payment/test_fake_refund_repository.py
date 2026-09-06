from datetime import UTC, datetime, timedelta

import pytest
from src.contexts.core_payment.domain.exceptions import (
    RefundNotFoundError,
    StaleRefundStateError,
)
from src.contexts.core_payment.domain.statuses import RefundFailureReasons, RefundStatuses
from tests.fixtures.refund import FakeRefundRepository, make_refund

UNRESOLVED = (RefundStatuses.CREATED, RefundStatuses.PENDING, RefundStatuses.ERROR)


@pytest.mark.anyio
async def test_list_unresolved_filters_by_status_age_and_limit() -> None:
    repo = FakeRefundRepository()
    moment = datetime.now(UTC)
    created = make_refund(status=RefundStatuses.CREATED, created_at=moment - timedelta(hours=2))
    pending = make_refund(status=RefundStatuses.PENDING, created_at=moment - timedelta(hours=3))
    errored = make_refund(status=RefundStatuses.ERROR, created_at=moment - timedelta(hours=1))
    fresh = make_refund(status=RefundStatuses.CREATED, created_at=moment)
    settled = make_refund(status=RefundStatuses.SUCCESS, created_at=moment - timedelta(hours=4))
    for refund in (created, pending, errored, fresh, settled):
        await repo.add(refund)

    stale = moment - timedelta(minutes=30)
    unresolved = await repo.list_unresolved(
        statuses=UNRESOLVED, created_before=stale, due_before=moment, limit=10
    )
    capped = await repo.list_unresolved(
        statuses=UNRESOLVED, created_before=stale, due_before=moment, limit=1
    )

    # Oldest first. A refund created a moment ago is still in flight, and one
    # that already reached success has nothing left to reconcile.
    assert [item.refund.id for item in unresolved] == [pending.id, created.id, errored.id]
    assert [item.refund.id for item in capped] == [pending.id]


@pytest.mark.anyio
async def test_mutating_loaded_refund_without_update_does_not_persist() -> None:
    repo = FakeRefundRepository()
    refund = make_refund(status=RefundStatuses.CREATED)
    await repo.add(refund)

    loaded = await repo.get_by_id(refund.id)
    assert loaded is not None
    loaded.mark_pending()  # mutate the loaded object without calling update

    again = await repo.get_by_id(refund.id)
    assert again is not None
    assert again.status is RefundStatuses.CREATED


@pytest.mark.anyio
async def test_update_persists_marked_changes() -> None:
    repo = FakeRefundRepository()
    refund = make_refund(status=RefundStatuses.PENDING)
    await repo.add(refund)

    loaded = await repo.get_by_id(refund.id)
    assert loaded is not None
    loaded.mark_failed(RefundFailureReasons.CARD_UNAVAILABLE)
    await repo.update(loaded, expected_status=RefundStatuses.PENDING)

    again = await repo.get_by_id(refund.id)
    assert again is not None
    assert again.status is RefundStatuses.FAILED
    assert again.failure_reason is RefundFailureReasons.CARD_UNAVAILABLE


@pytest.mark.anyio
async def test_update_missing_refund_raises_not_found() -> None:
    repo = FakeRefundRepository()
    refund = make_refund()  # not added to the repository

    with pytest.raises(RefundNotFoundError):
        await repo.update(refund, expected_status=RefundStatuses.PENDING)


@pytest.mark.anyio
async def test_update_with_wrong_expected_status_raises_stale_and_keeps_stored() -> None:
    repo = FakeRefundRepository()
    refund = make_refund(status=RefundStatuses.PENDING)
    await repo.add(refund)
    refund.mark_success()

    with pytest.raises(StaleRefundStateError) as exc_info:
        await repo.update(refund, expected_status=RefundStatuses.CREATED)

    assert exc_info.value.refund_id == refund.id
    assert exc_info.value.expected_status is RefundStatuses.CREATED
    stored = await repo.get_by_id(refund.id)
    assert stored is not None
    assert stored.status is RefundStatuses.PENDING  # store untouched
