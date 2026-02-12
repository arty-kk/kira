#app/emo_engine/registry.py
from __future__ import annotations

import time
import asyncio
import logging

from collections import OrderedDict
from contextlib import suppress
from typing import Dict, Tuple, Hashable

from .persona.core import Persona
from app.core.db import session_scope
from app.core.models import User, ApiKey
from app.config import settings

_TTL = getattr(settings, "PERSONA_CACHE_TTL", 3600)
_MAX = getattr(settings, "PERSONA_CACHE_MAX", 300)
_Key = Tuple[int, int, int, Hashable]
_cache: "OrderedDict[_Key, tuple[Persona, float]]" = OrderedDict()
_inflight: Dict[_Key, asyncio.Future[Persona]] = {}
_lock  = asyncio.Lock()
_bg_closers: set[asyncio.Task] = set()


logger = logging.getLogger(__name__)


def _now() -> float:
    return time.monotonic()

def _purge_locked(now: float) -> list[Persona]:
    to_close: list[Persona] = []
    stale = [cid for cid, (_, ts) in _cache.items() if now - ts > _TTL]
    for cid in stale:
        entry = _cache.pop(cid, None)
        if entry:
            to_close.append(entry[0])
    while len(_cache) > _MAX:
        _k, entry = _cache.popitem(last=False)
        to_close.append(entry[0])
    if stale:
        logger.debug("purged %d persona(s)", len(stale))
    return to_close

def _schedule_closes(personas: list[Persona]) -> None:
    if not personas:
        return
    loop = asyncio.get_running_loop()
    for p in personas:
        try:
            t = loop.create_task(p.close(), name="persona-purge-close")
            _bg_closers.add(t)
            def _done(fut: asyncio.Task) -> None:
                _bg_closers.discard(fut)
                if fut.cancelled():
                    return
                try:
                    exc = fut.exception()
                except asyncio.CancelledError:
                    return
                if exc:
                    logger.debug("background close failed", exc_info=True)
            t.add_done_callback(_done)
        except Exception:
            logger.exception("schedule_closes failed", exc_info=True)

async def get_persona(
    chat_id: int,
    user_id: int | None = None,
    *,
    group_mode: bool = False,
    profile_id: str | int | None = None,
) -> Persona:
    t0 = _now()
    profile_key: Hashable = str(profile_id) if profile_id is not None else ""
    key: _Key = (chat_id, user_id or 0, 1 if group_mode else 0, profile_key)
    closers: list[Persona] = []
    persona_hit: Persona | None = None
    fut: asyncio.Future[Persona] | None = None

    async with _lock:
        entry = _cache.get(key)
        now = _now()
        if entry is not None:
            persona, ts = entry
            if now - ts <= _TTL:
                _cache[key] = (persona, now)
                _cache.move_to_end(key, last=True)
                logger.debug("persona.cache hit key=%s", key)
                persona_hit = persona
            else:
                _cache.pop(key, None)
                closers.append(persona)
                persona_hit = None
        else:
            persona_hit = None

        fut = _inflight.get(key)
        if persona_hit is None and fut is None:
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            _inflight[key] = fut
            creator = True
        else:
            creator = False

        closers += _purge_locked(now)

    _schedule_closes(closers)

    if persona_hit is not None:
        try:
            persona_hit._spawn(persona_hit._ensure_background_started, name="persona-ensure-bg")
        except Exception:
            logger.debug("persona.ensure_background start failed (cache hit)", exc_info=True)
        return persona_hit

    if not creator:
        logger.debug("persona.cache wait key=%s – awaiting in-flight build", key)
        assert fut is not None
        return await fut

    logger.debug("persona.cache miss key=%s – constructing", key)

    dispose: Persona | None = None
    closers2: list[Persona] = []
    build_error: BaseException | None = None
    try:
        persona = Persona(chat_id)

        async with session_scope(read_only=True) as db:
            prefs = None
            ak = None
            try:
                if user_id:
                    with suppress(Exception):
                        ak = await db.get(ApiKey, int(user_id))
                if ak and getattr(ak, "persona_prefs", None):
                    prefs = ak.persona_prefs
                owner_uid = getattr(ak, "user_id", None) if ak else None
                target_id = owner_uid or (user_id if not ak else None) or chat_id
                if prefs is None and target_id:
                    with suppress(Exception):
                        u = await db.get(User, int(target_id))
                    if u and getattr(u, "persona_prefs", None):
                        prefs = u.persona_prefs
            except Exception:
                logger.debug("load persona_prefs failed", exc_info=True)

            if prefs:
                try:
                    persona.apply_overrides(prefs)
                except Exception:
                    logger.debug("apply_overrides failed", exc_info=True)

        async with _lock:
            now = _now()
            current = _cache.get(key)
            if current is not None:
                p2, ts2 = current
                if now - ts2 <= _TTL:
                    _cache[key] = (p2, now)
                    _cache.move_to_end(key, last=True)
                    logger.debug("persona.raced key=%s reused existing", key)
                    dispose = persona
                    ret = p2
                else:
                    _cache.pop(key, None)
                    _cache[key] = (persona, now)
                    _cache.move_to_end(key, last=True)
                    ret = persona
            else:
                _cache[key] = (persona, now)
                _cache.move_to_end(key, last=True)
                ret = persona
            closers2 = _purge_locked(now)
            try:
                loop = asyncio.get_running_loop()
                ret._spawn(ret._ensure_background_started, name="persona-ensure-bg")
            except Exception:
                logger.debug("persona.ensure_background start failed (miss)", exc_info=True)
    except BaseException as e:
        build_error = e
    finally:
        async with _lock:
            fut_done = _inflight.pop(key, None)
            if fut_done and not fut_done.done():
                if build_error is None:
                    fut_done.set_result(ret)
                else:
                    exc: BaseException = build_error
                    try:
                        fut_done.set_exception(exc)
                        fut_done.add_done_callback(lambda done: done.exception())
                    except Exception:
                        logger.debug("persona.cache: set_exception failed", exc_info=True)

    if build_error is not None:
        logger.debug("persona.cache build failed key=%s", key, exc_info=True)
        raise build_error

    _schedule_closes(closers2)

    if dispose is not None:
        try:
            await dispose.close()
        except Exception:
            logger.debug("persona.dispose close failed", exc_info=True)
    logger.debug("persona.ready key=%s dt=%.3fs", key, _now() - t0)
    return ret

async def update_cached_personas_for_owner(owner_id: int, prefs: dict) -> None:
    if not prefs:
        return
    async with _lock:
        now = _now()
        for key, (persona, ts) in list(_cache.items()):
            chat_id, uid, group_flag, profile_key = key
            if uid == owner_id:
                try:
                    persona.apply_overrides(prefs)
                    _cache[key] = (persona, now)
                except Exception:
                    logger.debug(
                        "update_cached_personas_for_owner failed for key=%s",
                        key,
                        exc_info=True,
                    )


async def shutdown_personas() -> None:
    async with _lock:
        items = list(_cache.values())
        _cache.clear()
        if _inflight:
            for fut in list(_inflight.values()):
                if fut and not fut.done():
                    fut.set_exception(RuntimeError("persona registry shutdown"))
            _inflight.clear()
    personas = [p for (p, _ts) in items]
    if not personas:
        return
    try:
        tasks = [asyncio.create_task(p.close()) for p in personas]
        tasks += list(_bg_closers)
        _bg_closers.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        logger.info("persona.cache: closed %d persona(s)", len(personas))

__all__ = [
    "get_persona",
    "shutdown_personas",
    "update_cached_personas_for_owner",
]
