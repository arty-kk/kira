#app/emo_engine/registry.py

from __future__ import annotations

import time
import threading

from collections import OrderedDict
from .persona.core import Persona
from app.config import settings

_TTL   = 3600
_MAX   = getattr(settings, "PERSONA_CACHE_MAX", 300)
_cache: "OrderedDict[int, tuple[Persona, float]]" = OrderedDict()
_lock  = threading.Lock()

def _purge() -> None:
    now = time.time()
    stale = [cid for cid, (_, ts) in _cache.items() if now - ts > _TTL]
    for cid in stale:
        _cache.pop(cid, None)
    while len(_cache) > _MAX:
        _cache.popitem(last=False)

def get_persona(chat_id: int) -> Persona:
    with _lock:
        _purge()

        entry = _cache.get(chat_id)
        if entry:
            persona, _ = entry
            _cache.move_to_end(chat_id)
        else:
            persona = Persona(chat_id)

        _cache[chat_id] = (persona, time.time())
        _purge()
        return persona