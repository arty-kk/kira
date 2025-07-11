#app/core/logging_config.py

import logging

from logging.config import dictConfig

def setup_logging() -> None:
    dictConfig({
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
                "level": "INFO",
                "stream": "ext://sys.stderr"
            }
        },
        "loggers": {
            "": {
                "handlers": ["console"],
                "level": "DEBUG",
                "propagate": True
            },
            "app": {
                "handlers": ["console"],
                "level": "DEBUG",
                "propagate": True
            },
            "sqlalchemy.engine": {
                "handlers": ["console"],
                "level": "ERROR"
            },
            "aiohttp.server": {
                "handlers": ["console"],
                "level": "ERROR"
            }
        }
    }
)