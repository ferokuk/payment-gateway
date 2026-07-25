from uuid import UUID

import structlog

from src.contexts.core_payment.application.dto.refund import GetRefundStatusOutputDTO
from src.contexts.core_payment.domain.exceptions import RefundNotFoundError
from src.contexts.core_payment.infrastructure.database.repositories import (
    SQLAlchemyRefundRepository,
)

logger = structlog.get_logger(__name__)


class GetRefundStatusUseCase:
    def __init__(self, refund_repository: SQLAlchemyRefundRepository):
        self._refund_repository = refund_repository

    async def __call__(self, refund_id: UUID) -> GetRefundStatusOutputDTO:
        refund = await self._refund_repository.get_by_id(refund_id)
        if not refund:
            logger.info("refund_not_found", refund_id=str(refund_id))
            raise RefundNotFoundError

        logger.info("refund_found", refund_id=str(refund_id), status=refund.status.value)
        return GetRefundStatusOutputDTO(
            refund_id=refund.id,
            payment_id=refund.payment_id,
            status=refund.status,
            amount=refund.amount,
        )
