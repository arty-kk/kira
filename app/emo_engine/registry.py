cat >app/emo_engine/registry.py<< 'EOF'
#app/emo_engine/registry.py
from __future__ import annotations

import time
import asyncio
import functools
import logging

from collections import OrderedDict
from .persona.core import Persona
from app.config import settings

_TTL = getattr(settings, "PERSONA_CACHE_TTL", 3600)
_MAX = getattr(settings, "PERSONA_CACHE_MAX", 300)
_cache: "OrderedDict[tuple[int,int], tuple[Persona, float]]" = OrderedDict()
_lock  = asyncio.Lock()


logger = logging.getLogger(__name__)


def _now() -> float:
    return time.monotonic()


def _purge_locked(now: float) -> None:

    stale = [cid for cid, (_, ts) in _cache.items() if now - ts > _TTL]
    for cid in stale:
        _cache.pop(cid, None)
    while len(_cache) > _MAX:
        _cache.popitem(last=False)
    if stale:
        logger.debug("purged %d persona(s)", len(stale))


async def get_persona(chat_id: int, user_id: int | None = None, *, group_mode: bool = False) -> Persona:
    t0 = _now()
    key = (chat_id, 0) if group_mode else (chat_id, user_id or 0)

    async with _lock:
        entry = _cache.get(key)
        if entry is not None:
            persona, ts = entry
            now = _now()
            if now - ts <= _TTL:
                _cache[key] = (persona, now)
                _cache.move_to_end(key, last=True)
                logger.debug("persona.cache hit key=%s", key)
                return persona
            _cache.pop(key, None)

    logger.debug("persona.cache miss key=%s – constructing", key)
    persona = Persona(chat_id)

    async with _lock:
        now = _now()
        current = _cache.get(key)
        if current is not None:
            p2, ts2 = current
            if now - ts2 <= _TTL:
                _cache[key] = (p2, now)
                _cache.move_to_end(key, last=True)
                logger.debug("persona.raced key=%s reused existing", key)
                return p2
            _cache.pop(key, None)
        _cache[key] = (persona, now)
        _cache.move_to_end(key, last=True)
        _purge_locked(now)

    logger.debug("persona.ready key=%s dt=%.3fs", key, _now() - t0)
    return persona
EOF