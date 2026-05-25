from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from src.contexts.core_payment.domain.payment import Payment, PaymentStatuses


def test_payment_is_created_with_given_fields() -> None:
    payment = Payment(
        id=uuid4(),
        provider_id=1,
        status=PaymentStatuses.CREATED,
        amount=Decimal("100.00"),
        currency="USD",
        created_at=datetime.now(UTC),
    )

    assert payment.status is PaymentStatuses.CREATED
    assert payment.amount == Decimal("100.00")
    assert payment.metadata is None
