"""Background reconciliation of refunds stuck in CREATED.

A separate process, not a task inside the API: its own event loop, its own
connection pool, and a crash here cannot take the API down with it. Everything
below the entrypoint is shared with the API — same DI container, same use case,
same transactional semantics.
"""

import asyncio
import contextlib
import signal

import structlog
from dishka import make_async_container

from src.contexts.core_payment.application.use_cases.reconcile_stuck_refunds import (
    ReconcileStuckRefundsUseCase,
)
from src.contexts.core_payment.ioc import CorePaymentProvider, PaymentProviderProvider
from src.shared.config import settings
from src.shared.ioc import ConfigProvider, DatabaseProvider, RepositoriesProvider
from src.shared.logging import configure_logging

logger = structlog.get_logger(__name__)


async def main() -> None:
    configure_logging(json_logs=not settings.is_debug)
    # No FastapiProvider: it supplies Request and WebSocket, which do not exist
    # here. Everything else is the web application's wiring verbatim, so one
    # pass gets the same session and the same commit-on-success as an HTTP request.
    container = make_async_container(
        ConfigProvider(),
        DatabaseProvider(),
        RepositoriesProvider(),
        CorePaymentProvider(),
        PaymentProviderProvider(),
    )
    stop = asyncio.Event()
    running_loop = asyncio.get_running_loop()
    for received in (signal.SIGINT, signal.SIGTERM):
        # Windows has no add_signal_handler; there the process ends on
        # KeyboardInterrupt instead of finishing the current pass.
        with contextlib.suppress(NotImplementedError):
            running_loop.add_signal_handler(received, stop.set)

    logger.info("reconciler_started", interval_seconds=settings.reconcile_interval_seconds)
    try:
        while not stop.is_set():
            try:
                async with container() as request_scope:
                    use_case = await request_scope.get(ReconcileStuckRefundsUseCase)
                    await use_case()
            except Exception:
                # A pass is idempotent: whatever failed is picked up by the next
                # one, so a single bad pass must not end the loop.
                logger.exception("reconciliation_pass_failed")
            # Sleep that wakes on SIGTERM instead of making shutdown wait out
            # the whole interval.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.reconcile_interval_seconds)
    finally:
        await container.close()
        logger.info("reconciler_stopped")


if __name__ == "__main__":
    asyncio.run(main())
