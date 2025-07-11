# app/core/__init__.py

from .db import (
    engine, 
    AsyncSessionLocal, 
    Base, 
    get_db,
)
from .models import User
from .memory import (
    get_redis,
    push_message,
    load_context,
    is_spam,
    inc_msg_count,
    record_activity,
    SHORT_LIMIT,
    MEMORY_TTL,
)
from .logging_config import setup_logging

__all__ = [
    # db
    "engine",
    "AsyncSessionLocal",
    "Base",
    "get_db",
    # models
    "User",
    # memory
    "get_redis",
    "push_message",
    "close_redis_pools",
    "load_context",
    "is_spam",
    "inc_msg_count",
    "record_activity",
    "SHORT_LIMIT",
    "MEMORY_TTL",
    "get_cached_gender",
    "cache_gender",
    # logging
    "setup_logging",
]
