#app/__init__.py
from __future__ import annotations

__all__ = [
    "settings",
    "get_settings",
    "engine",
    "_get_env",
    "close_redis_pools",
    "setup_logging",
]


def __getattr__(name: str):
    if name in {"settings", "get_settings", "_get_env"}:
        from . import config as _config

        return getattr(_config, name)
    if name == "engine":
        from .core.db import engine

        return engine
    if name == "close_redis_pools":
        from .core.memory import close_redis_pools

        return close_redis_pools
    if name == "setup_logging":
        from .core.logging_config import setup_logging

        return setup_logging
    raise AttributeError(name)
