from typing import Annotated
from uuid import UUID

from dishka import FromDishka
from dishka.integrations.fastapi import inject
from fastapi import APIRouter, Header, HTTPException, Response, status
from src.contexts.core_payment.application.dto.refund import CreateRefundInputDTO
from src.contexts.core_payment.application.use_cases.create_refund import CreateRefundUseCase
from src.contexts.core_payment.application.use_cases.get_refund_status import (
    GetRefundStatusUseCase,
)
from src.contexts.core_payment.application.use_cases.list_payment_refunds import (
    ListPaymentRefundsUseCase,
)
from src.contexts.core_payment.domain.exceptions import (
    IdempotencyKeyMismatchError,
    PaymentNotFoundError,
    PaymentNotRefundableError,
    RefundAmountExceededError,
    RefundNotFoundError,
)
from src.contexts.core_payment.infrastructure.providers.base import ProviderInitiationError
from src.contexts.core_payment.presentation.schemas.refund import (
    CreateRefundRequest,
    RefundListResponse,
    RefundResponse,
)
from src.shared.security import Authenticated

# No prefix: creation is nested under /payments (ownership in the path),
# reads are flat under /refunds (polling must not require the payment id).
router = APIRouter(tags=["refunds"])


@router.post(
    "/payments/{payment_id}/refunds",
    response_model=RefundResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a refund",
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Payment not found"},
        409: {"description": "Payment is not refundable or amount exceeds the remainder"},
        422: {"description": "Validation error or idempotency key reused with different body"},
        502: {
            "description": "Payment provider is unavailable; "
            "refund stays in CREATED with the reservation held"
        },
    },
)
@inject
async def create_refund(
    payment_id: UUID,
    request: CreateRefundRequest,
    response: Response,
    use_case: FromDishka[CreateRefundUseCase],
    _: FromDishka[Authenticated],
    idempotency_key: Annotated[
        str | None,
        Header(min_length=1, max_length=255, description="Idempotency key (Stripe model)"),
    ] = None,
) -> RefundResponse:
    command = CreateRefundInputDTO(
        payment_id=payment_id,
        amount=request.amount,
        metadata=request.metadata,
    )
    try:
        result = await use_case(command, idempotency_key=idempotency_key)
    except PaymentNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found"
        ) from e
    except PaymentNotRefundableError as e:
        # 409, not 422: a conflict with the current resource state, not a
        # malformed request — the same body may become valid later.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    except RefundAmountExceededError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    except IdempotencyKeyMismatchError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)) from e
    except ProviderInitiationError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Payment provider is unavailable"
        ) from e
    if result.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return RefundResponse.model_validate(result)


@router.get(
    "/payments/{payment_id}/refunds",
    response_model=RefundListResponse,
    status_code=status.HTTP_200_OK,
    summary="List refunds of a payment",
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Payment not found"},
    },
)
@inject
async def list_payment_refunds(
    payment_id: UUID,
    use_case: FromDishka[ListPaymentRefundsUseCase],
    _: FromDishka[Authenticated],
) -> RefundListResponse:
    try:
        refunds = await use_case(payment_id)
    except PaymentNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found"
        ) from e
    return RefundListResponse(refunds=[RefundResponse.model_validate(refund) for refund in refunds])


@router.get(
    "/refunds/{refund_id}",
    response_model=RefundResponse,
    status_code=status.HTTP_200_OK,
    summary="Get refund status",
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Not found"},
    },
)
@inject
async def get_refund(
    refund_id: UUID,
    use_case: FromDishka[GetRefundStatusUseCase],
    _: FromDishka[Authenticated],
) -> RefundResponse:
    try:
        refund = await use_case(refund_id=refund_id)
    except RefundNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Refund not found") from e
    return RefundResponse.model_validate(refund)
