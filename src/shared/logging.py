import logging

import structlog


def configure_logging(*, json_logs: bool = True, level: int = logging.INFO) -> None:
    """Configures structlog once at application startup (composition root).

    json_logs=True  -> JSONRenderer: machine-readable logs for prod/aggregation.
    json_logs=False -> ConsoleRenderer: readable colored output for local development.
    """
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    )
    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        renderer,
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
