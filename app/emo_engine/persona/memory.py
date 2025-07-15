cat >app/emo_engine/persona/memory.py<< EOF
# app/emo_engine/persona/memory.py
from __future__ import annotations

import asyncio, json, time, logging

from collections import deque
from dataclasses import dataclass, field
from math import exp
from typing import Dict, List

from app.config import settings
from app.core.memory import get_redis


logger = logging.getLogger(__name__)


@dataclass
class MemoryEntry:
    id: str
    snippet: str
    timestamp: float
    salience: float
    readings: Dict[str, float] = field(default_factory=dict)


async def _restore(self) -> None:
    """Restore persona state, weights, and memory from Redis, guarded by lock."""
    async with self._lock:
        redis = get_redis()
        if redis is None:
            logger.warning("Redis unavailable during persona restore")
            self._restored_evt.set()
            return

        try:
            # Fetch state and weights
            async with redis.pipeline() as pipe:
                pipe.get(self._k_state())
                pipe.get(self._k_weights())
                raw, raw_w = await asyncio.wait_for(pipe.execute(), timeout=1.0)

            if raw is not None:
                if isinstance(raw, bytes):
                    raw = raw.decode()
                loaded = json.loads(raw)
                if isinstance(loaded, dict) and "data" in loaded:
                    self.state.update(loaded["data"])
                else:
                    self.state.update(loaded)
                    await redis.set(
                        self._k_state(),
                        json.dumps({"v": 0, "data": self.state}),
                        ex=settings.PERSONA_REDIS_TTL,
                    )

            if raw_w:
                if isinstance(raw_w, bytes):
                    raw_w = raw_w.decode()
                self.user_weights = {
                    int(k): v for k, v in json.loads(raw_w).items()
                }

            # Fetch memory entries
            raw_mem = await asyncio.wait_for(
                redis.zrevrange(self._k_memory(), 0, -1), timeout=1.0
            )
            entries: List[MemoryEntry] = []
            for item in raw_mem:
                try:
                    if isinstance(item, bytes):
                        item = item.decode()
                    entries.append(MemoryEntry(**json.loads(item)))
                except Exception as exc:
                    logger.warning("Skip bad memory entry: %s", exc)
            self.memory_entries = deque(entries, maxlen=settings.MEMORY_MAX_ENTRIES)

        except Exception:
            logger.exception("Persona restore failed")
        finally:
            self._restored_evt.set()


async def _persist(self) -> None:
    """Persist persona state, weights, and memory to Redis, guarded by lock."""
    async with self._lock:
        redis = get_redis()
        if redis is None:
            logger.warning("Redis unavailable during persona persist")
            return

        # Persist state & weights
        try:
            state_blob = json.dumps({"v": self.state_version, "data": self.state})
            async with redis.pipeline() as pipe:
                pipe.set(self._k_state(), state_blob, ex=settings.PERSONA_REDIS_TTL)
                pipe.set(
                    self._k_weights(),
                    json.dumps(self.user_weights),
                    ex=settings.PERSONA_REDIS_TTL,
                )
                await pipe.execute()
            self.state_version += 1
        except Exception:
            logger.exception("Persona persist(state/weights) failed")

        # Persist memory entries
        try:
            key = self._k_memory()
            tmp = f"{key}:tmp"
            now = time.time()
            async with redis.pipeline() as pipe:
                pipe.delete(tmp)
                for entry in self.memory_entries:
                    age = now - entry.timestamp
                    score = entry.salience * exp(
                        -settings.MEMORY_SALIENCE_DECAY_RATE * age
                    )
                    pipe.zadd(tmp, {json.dumps(entry.__dict__): score})
                pipe.expire(tmp, settings.PERSONA_REDIS_TTL)
                pipe.renamenx(tmp, key)
                await pipe.execute()
        except Exception:
            logger.exception("Persona persist(memory) failed")
EOF