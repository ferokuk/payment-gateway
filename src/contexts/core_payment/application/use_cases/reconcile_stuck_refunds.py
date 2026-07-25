from datetime import UTC, datetime, timedelta
from typing import assert_never

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.contexts.core_payment.application.dto.refund import ReconciliationReportDTO
from src.contexts.core_payment.domain.exceptions import (
    InvalidRefundFailureReasonError,
    StaleRefundStateError,
)
from src.contexts.core_payment.domain.refund import Refund
from src.contexts.core_payment.domain.statuses import RefundFailureReasons, RefundStatuses
from src.contexts.core_payment.infrastructure.database.repositories import (
    SQLAlchemyPaymentRepository,
    SQLAlchemyRefundRepository,
)
from src.contexts.core_payment.infrastructure.providers.base import (
    PaymentProvider,
    ProviderInitiationError,
    ProviderRejectedError,
    RefundProviderState,
    RefundProviderStatus,
)

logger = structlog.get_logger(__name__)

# The three ways a refund can hold a reservation without ever reaching an
# outcome: initiation never confirmed, the callback was lost, or the callback
# itself said the provider did not know.
UNRESOLVED_STATUSES = frozenset(
    {RefundStatuses.CREATED, RefundStatuses.PENDING, RefundStatuses.ERROR}
)


class ReconcileStuckRefundsUseCase:
    """Closes refunds whose fate stayed open, freeing what they reserved.

    Every such refund holds part of a payment's refundable remainder while the
    payer got nothing, so the remainder shrinks without a reason anyone can see.

    The pass asks the provider first and acts on the answer, rather than
    retrying blindly: only the provider can distinguish "never took it" from
    "took it and the answer was lost", and those two demand opposite actions.
    Re-initiation happens in exactly one case — the provider confirms absence
    and the refund is young enough for its deduplication key to still be alive.

    Transactions mirror CreateRefundUseCase: the read transaction closes before
    the first network call, and every refund commits on its own, so one
    unreachable provider cannot undo the work already done in this pass.
    """

    def __init__(
        self,
        refund_repository: SQLAlchemyRefundRepository,
        payment_repository: SQLAlchemyPaymentRepository,
        payment_provider: PaymentProvider,
        session: AsyncSession,
        *,
        stuck_after: timedelta,
        give_up_after: timedelta,
        batch_size: int,
    ) -> None:
        self._refund_repository = refund_repository
        self._payment_repository = payment_repository
        self._payment_provider = payment_provider
        self._session = session
        self._stuck_after = stuck_after
        self._give_up_after = give_up_after
        self._batch_size = batch_size

    async def __call__(self) -> ReconciliationReportDTO:
        now = datetime.now(UTC)
        unresolved = await self._refund_repository.list_unresolved(
            statuses=UNRESOLVED_STATUSES,
            created_before=now - self._stuck_after,
            limit=self._batch_size,
        )
        await self._session.commit()

        report = ReconciliationReportDTO()
        for refund in unresolved:
            await self._reconcile(refund, report, now)
        logger.info("refund_reconciliation_finished", **report.model_dump())
        return report

    async def _reconcile(
        self, refund: Refund, report: ReconciliationReportDTO, now: datetime
    ) -> None:
        status = await self._payment_provider.get_refund_status(refund)
        match status.state:
            case RefundProviderState.UNKNOWN:
                logger.info("refund_status_unknown", refund_id=str(refund.id))
                report.unresolved += 1
            case RefundProviderState.ABSENT:
                await self._handle_absence(refund, report, now)
            case RefundProviderState.PENDING:
                await self._resume(refund, report)
            case RefundProviderState.SUCCEEDED:
                await self._settle_success(refund, report)
            case RefundProviderState.FAILED:
                await self._settle_failure(refund, status, report)
            case _:
                assert_never(status.state)

    async def _handle_absence(
        self, refund: Refund, report: ReconciliationReportDTO, now: datetime
    ) -> None:
        if refund.status is not RefundStatuses.CREATED:
            # The provider denies a refund it once accepted. Believing that and
            # releasing the reservation could let the same money go out twice,
            # so this is reported rather than acted upon.
            logger.warning(
                "provider_denies_accepted_refund",
                refund_id=str(refund.id),
                status=refund.status.value,
            )
            report.disputed += 1
            return

        if now - refund.created_at > self._give_up_after:
            # Absence is confirmed, so the money can go back — but a repeat this
            # late could slip past the provider's deduplication key and pay
            # twice. The merchant starts a fresh refund instead.
            await self._close_as_not_accepted(refund, report)
            return

        try:
            await self._payment_provider.initiate_refund(refund)
        except ProviderRejectedError as rejection:
            logger.info(
                "stuck_refund_rejected_by_provider", refund_id=str(refund.id), error=str(rejection)
            )
            await self._close_as_not_accepted(refund, report)
        except ProviderInitiationError as failure:
            logger.warning(
                "stuck_refund_still_unresolved", refund_id=str(refund.id), error=str(failure)
            )
            report.unresolved += 1
        else:
            await self._resume(refund, report)

    async def _resume(self, refund: Refund, report: ReconciliationReportDTO) -> None:
        if refund.status is not RefundStatuses.CREATED:
            # Already pending or error: the provider is still working, and there
            # is nothing to catch up.
            report.unresolved += 1
            return
        refund.mark_pending()
        if not await self._write(refund, RefundStatuses.CREATED, report):
            return
        await self._session.commit()
        report.resumed += 1
        logger.info("stuck_refund_resumed", refund_id=str(refund.id))

    async def _settle_success(self, refund: Refund, report: ReconciliationReportDTO) -> None:
        from_status = refund.status
        if from_status is RefundStatuses.CREATED:
            # The refund walks its legal path in memory and reaches the database
            # as a single CAS, so no intermediate state is ever stored.
            refund.mark_pending()
        refund.mark_success()
        if not await self._write(refund, from_status, report):
            return
        # No release: the money did leave, the reservation became the refund.
        await self._session.commit()
        report.completed += 1
        logger.info("stuck_refund_settled_as_success", refund_id=str(refund.id))

    async def _settle_failure(
        self, refund: Refund, status: RefundProviderStatus, report: ReconciliationReportDTO
    ) -> None:
        if status.failure_reason is None:
            # Inventing a reason would put a fabricated line into the merchant's
            # report; better to ask again next pass.
            logger.warning("provider_failure_without_reason", refund_id=str(refund.id))
            report.unresolved += 1
            return

        from_status = refund.status
        if from_status is RefundStatuses.CREATED:
            refund.mark_pending()
        try:
            refund.mark_failed(status.failure_reason)
        except InvalidRefundFailureReasonError as rejected:
            logger.warning(
                "provider_verdict_rejected_by_domain",
                refund_id=str(refund.id),
                error=str(rejected),
            )
            report.unresolved += 1
            return
        await self._close(refund, from_status, report)

    async def _close_as_not_accepted(self, refund: Refund, report: ReconciliationReportDTO) -> None:
        refund.mark_failed(RefundFailureReasons.NOT_ACCEPTED_BY_PROVIDER)
        await self._close(refund, RefundStatuses.CREATED, report)

    async def _close(
        self, refund: Refund, from_status: RefundStatuses, report: ReconciliationReportDTO
    ) -> None:
        if not await self._write(refund, from_status, report):
            return
        # Release only after winning the CAS: exactly one writer reaches this
        # line, so the amount goes back exactly once.
        await self._payment_repository.release_refund_amount(refund.payment_id, refund.amount)
        await self._session.commit()
        report.closed += 1
        logger.info(
            "stuck_refund_closed",
            refund_id=str(refund.id),
            amount=str(refund.amount),
            reason=refund.failure_reason.value if refund.failure_reason else None,
        )

    async def _write(
        self, refund: Refund, from_status: RefundStatuses, report: ReconciliationReportDTO
    ) -> bool:
        try:
            await self._refund_repository.update(refund, expected_status=from_status)
        except StaleRefundStateError:
            # A competing reconciler or a late callback finished it first, and
            # whoever won did the bookkeeping — repeating it would release twice.
            await self._session.rollback()
            report.conflicts += 1
            logger.info("stuck_refund_finished_by_competitor", refund_id=str(refund.id))
            return False
        return True
