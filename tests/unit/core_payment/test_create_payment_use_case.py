from decimal import Decimal

import pytest
from src.contexts.core_payment.application.dto.payment import CreatePaymentInputDTO
from src.contexts.core_payment.application.use_cases.create_payment import CreatePaymentUseCase
from src.contexts.core_payment.domain.exceptions import UnknownProviderError
from src.contexts.core_payment.domain.payment import Payment
from src.contexts.core_payment.domain.refund import Refund
from src.contexts.core_payment.domain.statuses import PaymentStatuses
from src.contexts.core_payment.infrastructure.providers.base import (
    FAKE_PROVIDER_ID,
    PaymentProvider,
    ProviderInitiationError,
)
from tests.fixtures.idempotency import FakeIdempotencyKeyRepository
from tests.fixtures.payment import FakePaymentRepository
from tests.fixtures.providers import RecordingFakeProvider
from tests.fixtures.session import FakeSession


def _make_command(
    provider_id: int = FAKE_PROVIDER_ID,
    metadata: dict[str, object] | None = None,
) -> CreatePaymentInputDTO:
    return CreatePaymentInputDTO(
        amount=Decimal("250.00"),
        currency="EUR",
        provider_id=provider_id,
        metadata=metadata,
    )


def _make_use_case(
    repo: FakePaymentRepository,
    provider: PaymentProvider,
    session: FakeSession,
) -> CreatePaymentUseCase:
    key_repo = FakeIdempotencyKeyRepository()
    return CreatePaymentUseCase(repo, key_repo, provider, session)  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_success_returns_pending_and_persists_payment() -> None:
    repo = FakePaymentRepository()
    provider = RecordingFakeProvider()
    session = FakeSession()
    use_case = _make_use_case(repo, provider, session)

    result = await use_case(_make_command(metadata={"order_id": "abc"}))

    assert result.status is PaymentStatuses.PENDING
    assert result.amount == Decimal("250.00")
    assert result.currency == "EUR"

    stored = await repo.get_by_id(result.payment_id)
    assert stored is not None
    assert stored.status is PaymentStatuses.PENDING
    assert stored.metadata == {"order_id": "abc"}


@pytest.mark.anyio
async def test_commits_txn1_before_calling_provider() -> None:
    repo = FakePaymentRepository()
    session = FakeSession()

    class _CommitTrackingProvider(PaymentProvider):
        """Records how many commits happened by the time the provider is called."""

        commits_seen: int | None = None

        async def initiate_payment(self, payment: Payment) -> None:
            self.commits_seen = session.commits

        async def initiate_refund(self, refund: Refund) -> None:
            raise NotImplementedError  # payments only in this test

    provider = _CommitTrackingProvider()
    use_case = _make_use_case(repo, provider, session)

    await use_case(_make_command())

    # Txn1 is committed BEFORE the external call; the final Txn2 commit is
    # done by the session DI provider on request exit, so inside the use case
    # there is exactly one commit.
    assert provider.commits_seen == 1
    assert session.commits == 1


@pytest.mark.anyio
async def test_provider_failure_keeps_payment_created_and_propagates() -> None:
    repo = FakePaymentRepository()
    provider = RecordingFakeProvider(error=ProviderInitiationError("provider is down"))
    session = FakeSession()
    use_case = _make_use_case(repo, provider, session)

    with pytest.raises(ProviderInitiationError):
        await use_case(_make_command())

    payments = list(repo._payments.values())
    assert len(payments) == 1
    assert payments[0].status is PaymentStatuses.CREATED
    assert session.commits == 1


@pytest.mark.anyio
async def test_unknown_provider_id_rejected_before_any_work() -> None:
    repo = FakePaymentRepository()
    provider = RecordingFakeProvider()
    session = FakeSession()
    use_case = _make_use_case(repo, provider, session)

    with pytest.raises(UnknownProviderError) as exc_info:
        await use_case(_make_command(provider_id=99))

    assert exc_info.value.provider_id == 99
    assert provider.initiated == []
    assert repo._payments == {}
    assert session.commits == 0
