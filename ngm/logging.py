import logging
import os

import structlog

SERVICE_NAME = "ngm"


def configure_logging():
    timestamper = structlog.processors.TimeStamper(fmt="iso")

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        timestamper,
    ]

    debug = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")
    if debug:
        renderer = structlog.dev.ConsoleRenderer()
        log_level = logging.DEBUG
    else:
        renderer = structlog.processors.JSONRenderer()
        log_level = logging.INFO

    env_log_level = os.getenv("LOG_LEVEL")
    if env_log_level:
        log_level = getattr(logging, env_log_level.upper(), log_level)

    structlog.configure(
        processors=shared_processors
        + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    structlog.contextvars.bind_contextvars(service=SERVICE_NAME)

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(log_level)

    for _log_name in [
        "scrapy",
        "scrapy.core.engine",
        "scrapy.downloadermiddlewares",
        "scrapy.extensions",
        "scrapy.spidermiddlewares",
        "scrapy.utils.signal",
        "protego._protego",
        "sqlalchemy",
        "sqlalchemy.engine",
        "boto3",
        "botocore",
        "urllib3",
    ]:
        logger = logging.getLogger(_log_name)
        logger.handlers = [handler]
        logger.propagate = False


def init_sentry():
    sentry_dsn = os.getenv("SENTRY_DSN")
    if not sentry_dsn:
        return

    import sentry_sdk
    from sentry_sdk.integrations.structlog import StructlogIntegration

    sentry_sdk.init(
        dsn=sentry_dsn,
        environment=os.getenv("SENTRY_ENVIRONMENT", "development"),
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
        profiles_sample_rate=float(os.getenv("SENTRY_PROFILES_SAMPLE_RATE", "0.1")),
        send_default_pii=True,
        integrations=[
            StructlogIntegration(),
        ],
    )


def setup():
    configure_logging()
    init_sentry()
