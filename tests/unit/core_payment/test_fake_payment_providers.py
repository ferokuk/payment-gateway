import pytest
from src.contexts.core_payment.domain.statuses import PaymentStatuses, RefundStatuses
from src.contexts.core_payment.infrastructure.providers.base import (
    FAKE_PROVIDER_ID,
    PaymentProvider,
    RefundProviderState,
)
from src.contexts.core_payment.infrastructure.providers.fake_manual import (
    ManualFakePaymentProvider,
)
from tests.fixtures.payment import make_payment
from tests.fixtures.refund import make_refund


def test_fake_provider_id_is_one() -> None:
    assert FAKE_PROVIDER_ID == 1


def test_manual_provider_is_a_payment_provider() -> None:
    assert isinstance(ManualFakePaymentProvider(), PaymentProvider)


@pytest.mark.anyio
async def test_manual_provider_accepts_payment_without_side_effects() -> None:
    provider = ManualFakePaymentProvider()
    payment = make_payment(status=PaymentStatuses.CREATED)

    await provider.initiate_payment(payment)

    assert payment.status is PaymentStatuses.CREATED


@pytest.mark.anyio
async def test_di_selects_provider_by_mode() -> None:
    import httpx
    from src.contexts.core_payment.infrastructure.providers.fake_auto import (
        AutoCallbackFakePaymentProvider,
    )
    from src.contexts.core_payment.ioc import PaymentProviderProvider
    from src.shared.config import Settings

    def make_settings(mode: str) -> Settings:
        return Settings(
            database_url="sqlite+aiosqlite:///:memory:",
            api_key="k",
            callback_secret="s",
            fake_provider_mode=mode,
        )

    provider_factory = PaymentProviderProvider()
    # async with guarantees the client is closed even if the asserts below fail.
    async with httpx.AsyncClient() as client:
        manual = provider_factory.get_payment_provider(make_settings("manual"), client)
        auto = provider_factory.get_payment_provider(make_settings("auto"), client)

    assert isinstance(manual, ManualFakePaymentProvider)
    assert isinstance(auto, AutoCallbackFakePaymentProvider)


@pytest.mark.anyio
async def test_manual_provider_accepts_refund_without_side_effects() -> None:
    provider = ManualFakePaymentProvider()
    refund = make_refund(status=RefundStatuses.CREATED)

    await provider.initiate_refund(refund)

    assert refund.status is RefundStatuses.CREATED


@pytest.mark.anyio
async def test_manual_provider_reports_unseen_refund_as_absent() -> None:
    provider = ManualFakePaymentProvider()

    status = await provider.get_refund_status(make_refund(status=RefundStatuses.CREATED))

    # It was never asked to start this refund, so nothing was taken.
    assert status.state is RefundProviderState.ABSENT


@pytest.mark.anyio
async def test_manual_provider_reports_initiated_refund_as_pending() -> None:
    provider = ManualFakePaymentProvider()
    refund = make_refund(status=RefundStatuses.CREATED)
    await provider.initiate_refund(refund)

    status = await provider.get_refund_status(refund)

    # The passive fake never claims more than "I took it": the outcome belongs
    # to whoever posts the callback.
    assert status.state is RefundProviderState.PENDING
