from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.contexts.core_payment.application.dto.refund import ReconciliationReportDTO
from src.contexts.core_payment.domain.exceptions import StaleRefundStateError
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
)

logger = structlog.get_logger(__name__)


class ReconcileStuckRefundsUseCase:
    """Finishes refunds that never left CREATED and frees what they reserved.

    A refund lands there when initiation did not confirm: the reservation on the
    payment is already committed, so the refundable remainder shrinks while the
    payer got nothing back. Re-initiation is safe because the provider
    deduplicates by refund id; a refusal is the only answer that also makes it
    safe to close the refund and release the amount, since silence does not
    prove the provider never took it.

    Transactions mirror CreateRefundUseCase: the read transaction is closed
    before the first network call, and every refund commits on its own, so one
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
        retry_window_start = now - self._give_up_after
        stuck = await self._refund_repository.list_stuck_created(
            created_before=now - self._stuck_after,
            created_after=retry_window_start,
            limit=self._batch_size,
        )
        abandoned = await self._refund_repository.count_stuck_created(
            created_before=retry_window_start
        )
        await self._session.commit()

        report = ReconciliationReportDTO(abandoned=abandoned)
        if abandoned:
            # Not actionable automatically, but silence here means money nobody
            # can refund and nobody knows about.
            logger.warning("stuck_refunds_beyond_retry_window", count=abandoned)
        for refund in stuck:
            await self._reconcile(refund, report)
        logger.info("refund_reconciliation_finished", **report.model_dump())
        return report

    async def _reconcile(self, refund: Refund, report: ReconciliationReportDTO) -> None:
        try:
            await self._payment_provider.initiate_refund(refund)
        except ProviderRejectedError as rejection:
            logger.info(
                "stuck_refund_rejected_by_provider",
                refund_id=str(refund.id),
                error=str(rejection),
            )
            await self._close(refund, report)
        except ProviderInitiationError as failure:
            # Not an answer, just more silence: the refund may exist there after
            # all, so the reservation stays until someone can tell us.
            logger.warning(
                "stuck_refund_still_unresolved", refund_id=str(refund.id), error=str(failure)
            )
            report.unresolved += 1
        else:
            await self._resume(refund, report)

    async def _resume(self, refund: Refund, report: ReconciliationReportDTO) -> None:
        refund.mark_pending()
        if not await self._write(refund, report):
            return
        await self._session.commit()
        report.resumed += 1
        logger.info("stuck_refund_resumed", refund_id=str(refund.id))

    async def _close(self, refund: Refund, report: ReconciliationReportDTO) -> None:
        refund.mark_failed(RefundFailureReasons.NOT_ACCEPTED_BY_PROVIDER)
        if not await self._write(refund, report):
            return
        # Release only after winning the CAS: exactly one writer reaches this
        # line, so the amount goes back exactly once.
        await self._payment_repository.release_refund_amount(refund.payment_id, refund.amount)
        await self._session.commit()
        report.closed += 1
        logger.info("stuck_refund_closed", refund_id=str(refund.id), amount=str(refund.amount))

    async def _write(self, refund: Refund, report: ReconciliationReportDTO) -> bool:
        try:
            await self._refund_repository.update(refund, expected_status=RefundStatuses.CREATED)
        except StaleRefundStateError:
            # A competing reconciler or a late callback finished it first, and
            # whoever won did the bookkeeping — repeating it would release twice.
            await self._session.rollback()
            report.conflicts += 1
            logger.info("stuck_refund_finished_by_competitor", refund_id=str(refund.id))
            return False
        return True
