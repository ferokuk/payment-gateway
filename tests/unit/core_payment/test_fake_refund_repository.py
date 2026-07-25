from datetime import UTC, datetime, timedelta

import pytest
from src.contexts.core_payment.domain.exceptions import (
    RefundNotFoundError,
    StaleRefundStateError,
)
from src.contexts.core_payment.domain.statuses import RefundFailureReasons, RefundStatuses
from tests.fixtures.refund import FakeRefundRepository, make_refund


@pytest.mark.anyio
async def test_list_stuck_created_filters_by_status_age_and_limit() -> None:
    repo = FakeRefundRepository()
    moment = datetime.now(UTC)
    old_created = make_refund(status=RefundStatuses.CREATED, created_at=moment - timedelta(hours=2))
    older_created = make_refund(
        status=RefundStatuses.CREATED, created_at=moment - timedelta(hours=3)
    )
    fresh_created = make_refund(status=RefundStatuses.CREATED, created_at=moment)
    ancient_created = make_refund(
        status=RefundStatuses.CREATED, created_at=moment - timedelta(days=2)
    )
    old_pending = make_refund(status=RefundStatuses.PENDING, created_at=moment - timedelta(hours=2))
    for refund in (old_created, older_created, fresh_created, ancient_created, old_pending):
        await repo.add(refund)

    window = {
        "created_before": moment - timedelta(hours=1),
        "created_after": moment - timedelta(days=1),
    }
    stuck = await repo.list_stuck_created(**window, limit=10)

    # Oldest first, and only what is inside the window: fresh refunds are still
    # in flight, the two-day-old one is past safe re-initiation, and a pending
    # refund is not stuck at all.
    assert [refund.id for refund in stuck] == [older_created.id, old_created.id]
    assert [refund.id for refund in await repo.list_stuck_created(**window, limit=1)] == [
        older_created.id
    ]


@pytest.mark.anyio
async def test_count_stuck_created_counts_everything_past_the_bound() -> None:
    repo = FakeRefundRepository()
    moment = datetime.now(UTC)
    for refund in (
        make_refund(status=RefundStatuses.CREATED, created_at=moment - timedelta(days=2)),
        make_refund(status=RefundStatuses.CREATED, created_at=moment - timedelta(days=3)),
        make_refund(status=RefundStatuses.CREATED, created_at=moment),
        make_refund(status=RefundStatuses.PENDING, created_at=moment - timedelta(days=2)),
    ):
        await repo.add(refund)

    assert await repo.count_stuck_created(created_before=moment - timedelta(days=1)) == 2


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
