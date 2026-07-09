from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from src.contexts.core_payment.domain.exceptions import (
    PaymentNotFoundError,
    StalePaymentStateError,
)
from src.contexts.core_payment.domain.payment import Payment
from src.contexts.core_payment.domain.statuses import PaymentStatuses
from src.contexts.core_payment.infrastructure.database.models import (
    IdempotencyKeyModel,
    PaymentModel,
)


@dataclass(frozen=True)
class IdempotencyRecord:
    key: str
    request_hash: str
    payment_id: UUID
    response_body: dict[str, Any] | None = None


class SQLAlchemyPaymentRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def add(self, payment: Payment) -> None:
        model = self._to_model(payment)
        self._session.add(model)

    async def get_by_id(self, payment_id: UUID) -> Payment | None:
        # populate_existing: a re-read after a CAS conflict must issue a fresh
        # SELECT rather than return the object from the identity map.
        model = await self._session.get(PaymentModel, payment_id, populate_existing=True)
        return self._to_domain(model) if model else None

    async def update(self, payment: Payment, *, expected_status: PaymentStatuses) -> None:
        # A payment transition is an atomic CAS: the guard on the from-status
        # protects against overwriting state that has moved ahead.
        stmt = (
            sql_update(PaymentModel)
            .where(PaymentModel.id == payment.id, PaymentModel.status == expected_status)
            .values(
                status=payment.status,
                failure_reason=payment.failure_reason,
                error_message=payment.error_message,
            )
        )
        result = await self._session.execute(stmt)
        # AsyncSession.execute is typed via overload as Result[Any];
        # for an UpdateBase statement the runtime type is CursorResult with
        # .rowcount, but that is not expressed statically, hence the cast.
        cursor_result = cast("CursorResult[Any]", result)
        if cursor_result.rowcount == 0:
            # Distinguish "payment does not exist" from "a competitor got there first".
            model = await self._session.get(PaymentModel, payment.id, populate_existing=True)
            if model is None:
                raise PaymentNotFoundError
            raise StalePaymentStateError(payment.id, expected_status)

    @staticmethod
    def _to_model(payment: Payment) -> PaymentModel:
        return PaymentModel(
            id=payment.id,
            provider_id=payment.provider_id,
            amount=payment.amount,
            currency=payment.currency,
            meta=payment.metadata,
            status=payment.status,
            created_at=payment.created_at,
            failure_reason=payment.failure_reason,
            error_message=payment.error_message,
        )

    @staticmethod
    def _to_domain(payment: PaymentModel) -> Payment:
        return Payment(
            id=payment.id,
            provider_id=payment.provider_id,
            amount=payment.amount,
            currency=payment.currency,
            metadata=payment.meta,
            status=payment.status,
            created_at=payment.created_at,
            failure_reason=payment.failure_reason,
            error_message=payment.error_message,
        )


class SQLAlchemyIdempotencyKeyRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get(self, key: str) -> IdempotencyRecord | None:
        # populate_existing: re-reading the key needs a fresh SELECT rather
        # than an object cached in the identity map (the resume algorithm).
        model = await self._session.get(IdempotencyKeyModel, key, populate_existing=True)
        if model is None:
            return None
        return IdempotencyRecord(
            key=model.key,
            request_hash=model.request_hash,
            payment_id=model.payment_id,
            response_body=model.response_body,
        )

    async def add(self, record: IdempotencyRecord) -> None:
        self._session.add(
            IdempotencyKeyModel(
                key=record.key,
                request_hash=record.request_hash,
                payment_id=record.payment_id,
                response_body=record.response_body,
            )
        )
        # flush inside the SAVEPOINT: a key conflict must surface as an
        # IntegrityError here, where the use case catches it, not at the final commit.
        await self._session.flush()

    async def set_response(self, key: str, response_body: dict[str, Any]) -> None:
        model = await self._session.get(IdempotencyKeyModel, key)
        if model is None:  # the key is reserved before initiation — must not get here
            raise LookupError(f"Idempotency key {key!r} is not reserved")
        model.response_body = response_body
