from uuid import UUID

import structlog

from src.contexts.core_payment.application.dto.refund import GetRefundStatusOutputDTO
from src.contexts.core_payment.domain.exceptions import PaymentNotFoundError
from src.contexts.core_payment.infrastructure.database.repositories import (
    SQLAlchemyPaymentRepository,
    SQLAlchemyRefundRepository,
)

logger = structlog.get_logger(__name__)


class ListPaymentRefundsUseCase:
    """Lists the refunds of one payment, oldest first.

    The payment is read first so that an unknown id answers 404 instead of an
    empty list: "no such payment" and "nothing refunded yet" are different
    answers, and a typo in the id must not read as the second one.
    """

    def __init__(
        self,
        payment_repository: SQLAlchemyPaymentRepository,
        refund_repository: SQLAlchemyRefundRepository,
    ) -> None:
        self._payment_repository = payment_repository
        self._refund_repository = refund_repository

    async def __call__(self, payment_id: UUID) -> list[GetRefundStatusOutputDTO]:
        payment = await self._payment_repository.get_by_id(payment_id)
        if payment is None:
            logger.info("payment_not_found", payment_id=str(payment_id))
            raise PaymentNotFoundError

        refunds = await self._refund_repository.list_by_payment_id(payment_id)
        logger.info("payment_refunds_listed", payment_id=str(payment_id), count=len(refunds))
        return [
            GetRefundStatusOutputDTO(
                refund_id=refund.id,
                payment_id=refund.payment_id,
                status=refund.status,
                amount=refund.amount,
            )
            for refund in refunds
        ]
