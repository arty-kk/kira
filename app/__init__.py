cat >app/__init__.py<< 'EOF'
#app/__init__.py
from .core.db import engine, Base
from .config import _get_env
from .core.memory import close_redis_pools

__all__ = [
    "engine",
    "Base",
    "_get_env",
    "close_redis_pools",
]
EOF