from datetime import UTC
from decimal import Decimal

import pytest
from src.contexts.core_payment.application.dto.payment import CreatePaymentInputDTO
from src.contexts.core_payment.application.use_cases.create_payment import CreatePaymentUseCase
from src.contexts.core_payment.domain.statuses import PaymentStatuses
from tests.fixtures.payment import FakePaymentRepository


def _make_command(
    metadata: dict[str, object] | None = None,
) -> CreatePaymentInputDTO:
    return CreatePaymentInputDTO(
        amount=Decimal("250.00"),
        currency="EUR",
        provider_id=7,
        metadata=metadata,
    )


@pytest.mark.anyio
async def test_creates_payment_with_status_created_and_returns_dto() -> None:
    repo = FakePaymentRepository()
    use_case = CreatePaymentUseCase(repo)  # type: ignore[arg-type]

    result = await use_case(_make_command(metadata={"order_id": "abc"}))

    assert result.status is PaymentStatuses.CREATED
    assert result.amount == Decimal("250.00")
    assert result.currency == "EUR"
    assert result.created_at.tzinfo == UTC


@pytest.mark.anyio
async def test_persists_payment_via_repository() -> None:
    repo = FakePaymentRepository()
    use_case = CreatePaymentUseCase(repo)  # type: ignore[arg-type]

    result = await use_case(_make_command(metadata={"order_id": "abc"}))

    stored = await repo.get_by_id(result.payment_id)
    assert stored is not None
    assert stored.id == result.payment_id
    assert stored.provider_id == 7
    assert stored.metadata == {"order_id": "abc"}
    assert stored.status is PaymentStatuses.CREATED


@pytest.mark.anyio
async def test_accepts_command_without_metadata() -> None:
    repo = FakePaymentRepository()
    use_case = CreatePaymentUseCase(repo)  # type: ignore[arg-type]

    result = await use_case(_make_command(metadata=None))

    stored = await repo.get_by_id(result.payment_id)
    assert stored is not None
    assert stored.metadata is None
