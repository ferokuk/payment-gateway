from uuid import UUID

from dishka import FromDishka
from dishka.integrations.fastapi import inject
from fastapi import APIRouter, HTTPException, status
from src.contexts.core_payment.application.dto.payment import CreatePaymentInputDTO
from src.contexts.core_payment.application.use_cases.create_payment import CreatePaymentUseCase
from src.contexts.core_payment.application.use_cases.get_payment_status import (
    GetPaymentStatusUseCase,
)
from src.contexts.core_payment.domain.exceptions import PaymentNotFoundError
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
        422: {"description": "Validation error"},
    },
)
@inject
async def create_payment(
    request: CreatePaymentRequest,
    use_case: FromDishka[CreatePaymentUseCase],
    _: FromDishka[Authenticated],
) -> PaymentResponse:
    command = CreatePaymentInputDTO(
        amount=request.amount,
        currency=request.currency,
        provider_id=request.provider_id,
        metadata=request.metadata,
    )

    return PaymentResponse.model_validate(await use_case(command))


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
