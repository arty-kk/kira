import logging
import os

from logging.config import dictConfig


def _resolve_log_level() -> str:
    process_level = os.getenv("PROCESS_SPECIFIC_LOG_LEVEL", "").strip().upper()
    if process_level:
        return process_level

    global_level = os.getenv("LOG_LEVEL", "").strip().upper()
    if global_level:
        return global_level

    return "INFO"


def _resolve_sqlalchemy_log_level() -> str:
    sqlalchemy_level = os.getenv("SQLALCHEMY_LOG_LEVEL", "").strip().upper()
    if sqlalchemy_level:
        return sqlalchemy_level

    return "CRITICAL"


def setup_logging() -> None:
    log_level = _resolve_log_level()
    sqlalchemy_log_level = _resolve_sqlalchemy_log_level()

    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)

    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {
                    "format": "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "standard",
                    "level": log_level,
                    "stream": "ext://sys.stderr",
                }
            },
            "loggers": {
                "": {
                    "handlers": ["console"],
                    "level": log_level,
                    "propagate": False,
                },
                "app": {
                    "handlers": ["console"],
                    "level": log_level,
                    "propagate": False,
                },
                "aiohttp.server": {
                    "handlers": ["console"],
                    "level": log_level,
                    "propagate": False,
                },
                "sqlalchemy": {
                    "handlers": ["console"],
                    "level": sqlalchemy_log_level,
                    "propagate": False,
                },
                "sqlalchemy.engine": {
                    "handlers": ["console"],
                    "level": sqlalchemy_log_level,
                    "propagate": False,
                },
            },
        }
    )
