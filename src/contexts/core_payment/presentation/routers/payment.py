from typing import Annotated
from uuid import UUID

from dishka import FromDishka
from dishka.integrations.fastapi import inject
from fastapi import APIRouter, Header, HTTPException, Response, status
from src.contexts.core_payment.application.dto.payment import CreatePaymentInputDTO
from src.contexts.core_payment.application.use_cases.create_payment import CreatePaymentUseCase
from src.contexts.core_payment.application.use_cases.get_payment_status import (
    GetPaymentStatusUseCase,
)
from src.contexts.core_payment.domain.exceptions import (
    IdempotencyKeyMismatchError,
    PaymentNotFoundError,
    UnknownProviderError,
)
from src.contexts.core_payment.infrastructure.providers.base import ProviderInitiationError
from src.contexts.core_payment.presentation.schemas.payment import (
    CreatePaymentRequest,
    PaymentResponse,
    PaymentStatusResponse,
)
from src.shared.security import Authenticated

router = APIRouter(prefix="/payments", tags=["payments"])


@router.post(
    "",
    response_model=PaymentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a payment",
    responses={
        401: {"description": "Unauthorized"},
        422: {
            "description": "Validation error, unknown provider "
            "or idempotency key reused with different body"
        },
        502: {"description": "Payment provider is unavailable; payment stays in CREATED"},
    },
)
@inject
async def create_payment(
    request: CreatePaymentRequest,
    response: Response,
    use_case: FromDishka[CreatePaymentUseCase],
    _: FromDishka[Authenticated],
    idempotency_key: Annotated[
        str | None,
        Header(min_length=1, max_length=255, description="Idempotency key (Stripe model)"),
    ] = None,
) -> PaymentResponse:
    command = CreatePaymentInputDTO(
        amount=request.amount,
        currency=request.currency,
        provider_id=request.provider_id,
        metadata=request.metadata,
    )
    try:
        result = await use_case(command, idempotency_key=idempotency_key)
    except UnknownProviderError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)) from e
    except IdempotencyKeyMismatchError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)) from e
    except ProviderInitiationError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Payment provider is unavailable"
        ) from e
    if result.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return PaymentResponse.model_validate(result)


@router.get(
    "/{payment_id}",
    response_model=PaymentStatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Get payment status",
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Not found"},
    },
)
@inject
async def get_payment(
    payment_id: UUID,
    use_case: FromDishka[GetPaymentStatusUseCase],
    _: FromDishka[Authenticated],
) -> PaymentStatusResponse:
    try:
        payment = await use_case(payment_id=payment_id)
    except PaymentNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found"
        ) from e
    return PaymentStatusResponse.model_validate(payment)
