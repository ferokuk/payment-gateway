from dishka import FromDishka
from dishka.integrations.fastapi import inject
from fastapi import APIRouter, status

from src.contexts.core_payment.application.dto.payment import CreatePaymentCommand
from src.contexts.core_payment.application.use_cases.create_payment import CreatePaymentUseCase
from src.contexts.core_payment.presentation.schemas.payment import PaymentResponse, CreatePaymentRequest

router = APIRouter(prefix="/payment", tags=["payments"])


@router.post(
    "",
    response_model=PaymentResponse,
    status_code=status.HTTP_201_CREATED,
)
@inject
async def create_payment(
        request: CreatePaymentRequest,
        use_case: FromDishka[CreatePaymentUseCase]
) -> PaymentResponse:
    command = CreatePaymentCommand(
        amount=request.amount,
        currency=request.currency,
        provider_id=request.provider_id,
        metadata=request.metadata,
    )
    result = await use_case(command)

    return PaymentResponse.model_validate(result)
