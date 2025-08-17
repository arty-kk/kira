cat >app/__init__.py<< 'EOF'
#app/__init__.py
from .core.db import engine
from .config import _get_env
from .core.memory import close_redis_pools
from .core.logging_config import setup_logging

__all__ = [
    "engine",
    "_get_env",
    "close_redis_pools",
    "setup_logging",
]
EOF