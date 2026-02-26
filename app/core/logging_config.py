import logging
import os

from logging.config import dictConfig

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
SQLALCHEMY_LOG_LEVEL = os.getenv("SQLALCHEMY_LOG_LEVEL", "ERROR").upper()

def setup_logging() -> None:
    
    for h in logging.root.handlers[:]:
        logging.root.removeHandler(h)

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
                "level": LOG_LEVEL,
                "stream": "ext://sys.stderr"
            }
        },
        "loggers": {
            "": {
                "handlers": ["console"],
                "level": LOG_LEVEL,
                "propagate": False
            },
            "app": {
                "handlers": ["console"],
                "level": LOG_LEVEL,
                "propagate": False
            },
            "sqlalchemy.engine": {
                "handlers": ["console"],
                "level": SQLALCHEMY_LOG_LEVEL,
                "propagate": False
            },
            "aiohttp.server": {
                "handlers": ["console"],
                "level": LOG_LEVEL,
                "propagate": False
            }
        }
    }
)
