cat >app/__init__.py<< EOF
#app/__init__.py
from .core.db import engine, Base
from .core.logging_config import setup_logging
from .tasks.scheduler import start_scheduler, sched
from .bot import start_bot
from .config import _get_env
from .core.memory import close_redis_pools

__all__ = [
    #db
    "engine",
    "Base",
    #logging
    "setup_logging",
    #scheduler
    "start_scheduler",
    "sched",
    #webhook
    "start_bot",
    #config
    "_get_env",
    #redis
    "close_redis_pools",
]
EOF