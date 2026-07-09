from dishka import FromDishka
from dishka.integrations.fastapi import inject
from fastapi import APIRouter, HTTPException, status
from src.contexts.core_payment.application.dto.callback import ProviderCallbackInputDTO
from src.contexts.core_payment.application.use_cases.process_provider_callback import (
    ProcessProviderCallbackUseCase,
)
from src.contexts.core_payment.domain.exceptions import (
    InvalidPaymentFailureReasonError,
    InvalidPaymentStatusTransitionError,
    PaymentNotFoundError,
)
from src.contexts.core_payment.presentation.schemas.callback import ProviderCallbackRequest
from src.contexts.core_payment.presentation.schemas.payment import PaymentStatusResponse
from src.shared.security import CallbackAuthenticated

router = APIRouter(prefix="/callbacks", tags=["callbacks"])


@router.post(
    "/payments",
    response_model=PaymentStatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Process a provider payment callback",
    responses={
        401: {"description": "Invalid or missing callback secret"},
        404: {"description": "Payment not found"},
        409: {"description": "Transition not allowed from current status"},
        422: {"description": "Validation error or failure reason not allowed"},
    },
)
@inject
async def process_payment_callback(
    request: ProviderCallbackRequest,
    use_case: FromDishka[ProcessProviderCallbackUseCase],
    _: FromDishka[CallbackAuthenticated],
) -> PaymentStatusResponse:
    command = ProviderCallbackInputDTO(
        payment_id=request.payment_id,
        status=request.status,
        failure_reason=request.failure_reason,
        error_message=request.error_message,
    )
    try:
        result = await use_case(command)
    except PaymentNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found"
        ) from e
    except InvalidPaymentStatusTransitionError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    except InvalidPaymentFailureReasonError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)) from e
    return PaymentStatusResponse.model_validate(result)
