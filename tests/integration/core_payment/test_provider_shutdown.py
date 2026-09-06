"""Production Dishka provider finalization, including an in-flight HTTP request."""

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from dishka import Provider, Scope, make_async_container, provide
from src.contexts.core_payment.infrastructure.providers.base import PaymentProvider
from src.contexts.core_payment.infrastructure.providers.fake_auto import (
    AutoCallbackFakePaymentProvider,
)
from src.contexts.core_payment.ioc import PaymentProviderProvider
from src.shared.config import Settings
from tests.fixtures.refund import make_refund


@pytest.mark.parametrize("exit_with_error", [False, True])
@pytest.mark.anyio
async def test_dishka_drains_callbacks_before_closing_http_client(exit_with_error: bool) -> None:
    events: list[str] = []
    entered = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            assert not client.is_closed
            await asyncio.sleep(0)
            assert not client.is_closed
            events.append("callback_drained")
        return httpx.Response(200)

    class _SettingsProvider(Provider):
        @provide(scope=Scope.APP)
        def get_settings(self) -> Settings:
            return Settings(
                database_url="postgresql+asyncpg://unused/unused",
                api_key="test",
                callback_secret="test",
                fake_provider_mode="auto",
                fake_callback_delay_seconds=0.001,
            )

    class _HttpProvider(Provider):
        @provide(scope=Scope.APP, override=True)
        async def get_http_client(self) -> AsyncIterator[httpx.AsyncClient]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                yield http_client
                assert provider._tasks == set()
                events.append("http_closing")
            events.append("http_closed")

    container = make_async_container(
        _SettingsProvider(), PaymentProviderProvider(), _HttpProvider()
    )
    provider = await container.get(PaymentProvider)
    assert isinstance(provider, AutoCallbackFakePaymentProvider)
    client = await container.get(httpx.AsyncClient)
    tasks: tuple[asyncio.Task[None], ...] = ()
    try:
        await provider.initiate_refund(make_refund())
        tasks = tuple(provider._tasks)
        await asyncio.wait_for(entered.wait(), timeout=5)
    finally:
        await asyncio.wait_for(
            container.close(
                exception=RuntimeError("application failed") if exit_with_error else None
            ),
            timeout=5,
        )
    assert all(task.cancelled() for task in tasks)
    assert provider._tasks == set()
    assert client.is_closed
    assert events == ["callback_drained", "http_closing", "http_closed"]
