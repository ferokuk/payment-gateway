import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.contexts.core_payment.application.dto.refund import (
    CreateRefundInputDTO,
    CreateRefundOutputDTO,
)
from src.contexts.core_payment.domain.exceptions import (
    IdempotencyKeyMismatchError,
    PaymentNotFoundError,
    PaymentNotRefundableError,
    RefundAmountExceededError,
    RefundNotFoundError,
    StaleRefundStateError,
)
from src.contexts.core_payment.domain.refund import Refund
from src.contexts.core_payment.domain.statuses import PaymentStatuses, RefundStatuses
from src.contexts.core_payment.infrastructure.database.repositories import (
    RefundIdempotencyRecord,
    SQLAlchemyPaymentRepository,
    SQLAlchemyRefundIdempotencyKeyRepository,
    SQLAlchemyRefundRepository,
)
from src.contexts.core_payment.infrastructure.providers.base import PaymentProvider
from src.shared.ids import new_uuid


def _request_hash(command: CreateRefundInputDTO) -> str:
    """SHA-256 of the canonicalized body: one key — one body.

    payment_id from the path is part of the DTO and therefore of the hash,
    so the same key with another payment is a mismatch.
    """
    canonical = json.dumps(command.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _replay(record: RefundIdempotencyRecord) -> CreateRefundOutputDTO:
    assert record.response_body is not None  # checked by the caller
    return CreateRefundOutputDTO.model_validate({**record.response_body, "replayed": True})


class CreateRefundUseCase:
    """Creates a refund in two short transactions, mirroring CreatePaymentUseCase.

    Txn1 additionally reserves the refunded amount on the payment with one
    guarded UPDATE — the authority for the over-refund invariant; the domain
    pre-check exists only for honest 4xx errors. The provider call happens
    outside an open transaction; Txn2 (autobegin) moves the refund to PENDING
    and atomically stores the response under the key; the final commit is done
    by the DI session provider when the request exits.
    Invariant: response_body IS NULL <=> the refund is stuck in CREATED
    with the reservation held — a resume must NOT reserve again.
    """

    def __init__(
        self,
        payment_repository: SQLAlchemyPaymentRepository,
        refund_repository: SQLAlchemyRefundRepository,
        idempotency_repository: SQLAlchemyRefundIdempotencyKeyRepository,
        payment_provider: PaymentProvider,
        session: AsyncSession,
    ) -> None:
        self._payment_repository = payment_repository
        self._refund_repository = refund_repository
        self._idempotency_repository = idempotency_repository
        self._payment_provider = payment_provider
        self._session = session

    async def __call__(
        self,
        command: CreateRefundInputDTO,
        idempotency_key: str | None = None,
    ) -> CreateRefundOutputDTO:
        if idempotency_key is None:
            refund = await self._reserve_and_add(command)
            return await self._initiate_and_finalize(refund, idempotency_key=None)

        record = await self._idempotency_repository.get(idempotency_key)
        if record is None:
            try:
                # SAVEPOINT: on a race for the key, the duplicate refund AND its
                # reservation are rolled back together with the key insert.
                async with self._session.begin_nested():
                    refund = await self._reserve_and_add(command)
                    # The refund must be flushed before inserting the key,
                    # otherwise the FK refund_idempotency_keys.refund_id points
                    # at a nonexistent row (same ordering issue as payments).
                    await self._session.flush()
                    await self._idempotency_repository.add(
                        RefundIdempotencyRecord(
                            key=idempotency_key,
                            request_hash=_request_hash(command),
                            refund_id=refund.id,
                        )
                    )
            except IntegrityError:
                record = await self._idempotency_repository.get(idempotency_key)
                if record is None:  # race with key deletion — outside the model
                    raise
            else:
                return await self._initiate_and_finalize(refund, idempotency_key)

        if record.request_hash != _request_hash(command):
            raise IdempotencyKeyMismatchError(idempotency_key)
        if record.response_body is not None:
            return _replay(record)

        # Resuming an interrupted operation: the key exists but has no response.
        # The reservation taken by the original Txn1 is still held — recovery
        # only re-initiates and finishes Txn2.
        found_refund = await self._refund_repository.get_by_id(record.refund_id)
        if found_refund is None:
            raise RefundNotFoundError
        if found_refund.status is RefundStatuses.CREATED:
            return await self._initiate_and_finalize(found_refund, idempotency_key)

        # Someone else finished the operation: PENDING and response are written
        # atomically in Txn2, so a fresh read of the key must see the response.
        record = await self._idempotency_repository.get(idempotency_key)
        if record is None or record.response_body is None:
            raise LookupError(f"Idempotency key {idempotency_key!r} lost its response")
        return _replay(record)

    async def _reserve_and_add(self, command: CreateRefundInputDTO) -> Refund:
        await self._pre_check(command)
        # The guarded UPDATE is the authority under races: the pre-check may
        # have passed and the reservation still hit rowcount == 0.
        await self._payment_repository.reserve_refund_amount(command.payment_id, command.amount)
        refund = Refund(
            id=new_uuid(),
            payment_id=command.payment_id,
            amount=command.amount,
            status=RefundStatuses.CREATED,
            created_at=datetime.now(UTC),
            metadata=command.metadata,
        )
        await self._refund_repository.add(refund)
        return refund

    async def _pre_check(self, command: CreateRefundInputDTO) -> None:
        # Fast honest 4xx before touching the counter; not authoritative.
        payment = await self._payment_repository.get_by_id(command.payment_id)
        if payment is None:
            raise PaymentNotFoundError
        if payment.status is not PaymentStatuses.SUCCESS:
            raise PaymentNotRefundableError(payment.id, payment.status)
        if payment.refunded_amount + command.amount > payment.amount:
            raise RefundAmountExceededError(payment.id, command.amount)

    async def _initiate_and_finalize(
        self, refund: Refund, idempotency_key: str | None
    ) -> CreateRefundOutputDTO:
        # Txn1: close the transaction before the external call — the attempt
        # and the reservation are recorded, the connection returns to the pool.
        await self._session.commit()

        # On ProviderInitiationError the refund stays in CREATED with the
        # reservation held (the router returns 502).
        await self._payment_provider.initiate_refund(refund)

        refund.mark_pending()
        try:
            await self._refund_repository.update(refund, expected_status=RefundStatuses.CREATED)
        except StaleRefundStateError:
            if idempotency_key is None:
                # Without a key there can be no competitors: the refund id is
                # new and callbacks never move CREATED — this violates the invariant.
                raise
            # A concurrent resume finished first: its Txn2 atomically wrote
            # PENDING + response — return its response, don't write our own.
            record = await self._idempotency_repository.get(idempotency_key)
            if record is None or record.response_body is None:
                raise LookupError(
                    f"Idempotency key {idempotency_key!r} lost its response"
                ) from None
            return _replay(record)
        dto = CreateRefundOutputDTO(
            refund_id=refund.id,
            payment_id=refund.payment_id,
            status=refund.status,
            amount=refund.amount,
        )
        if idempotency_key is not None:
            # replayed has exclude=True: the field is left out of the response snapshot.
            await self._idempotency_repository.set_response(
                idempotency_key, dto.model_dump(mode="json")
            )
        return dto
