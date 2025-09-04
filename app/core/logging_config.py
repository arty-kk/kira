#app/core/logging_config.py
import logging

from logging.config import dictConfig

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
                "level": "INFO",
                "stream": "ext://sys.stderr"
            }
        },
        "loggers": {
            "": {
                "handlers": ["console"],
                "level": "INFO",
                "propagate": False
            },
            "app": {
                "handlers": ["console"],
                "level": "INFO",
                "propagate": False
            },
            "sqlalchemy.engine": {
                "handlers": ["console"],
                "level": "INFO",
                "propagate": False
            },
            "aiohttp.server": {
                "handlers": ["console"],
                "level": "INFO",
                "propagate": False
            }
        }
    }
)
