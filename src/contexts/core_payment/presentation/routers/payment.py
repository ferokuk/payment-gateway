from dishka import FromDishka
from dishka.integrations.fastapi import inject
from fastapi import APIRouter, status
from src.contexts.core_payment.application.dto.payment import CreatePaymentInputDTO
from src.contexts.core_payment.application.use_cases.create_payment import CreatePaymentUseCase
from src.contexts.core_payment.presentation.schemas.payment import (
    CreatePaymentRequest,
    PaymentResponse,
)
from src.shared.security import Authenticated

router = APIRouter(prefix="/payment", tags=["payments"])


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
