import pytest
from src.contexts.core_payment.domain.statuses import PaymentStatuses
from src.contexts.core_payment.infrastructure.providers.base import (
    FAKE_PROVIDER_ID,
    PaymentProvider,
)
from src.contexts.core_payment.infrastructure.providers.fake_manual import (
    ManualFakePaymentProvider,
)
from tests.fixtures.payment import make_payment


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
