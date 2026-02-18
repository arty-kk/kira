#app/emo_engine/persona/neurograph.py
from __future__ import annotations

import asyncio
import time
import math
import json
import logging
import random

from dataclasses import dataclass, field
from typing import Dict, List, Any

from app.config import settings
from app.core.memory import get_redis_vector
from .utils.emotion_math import _clamp

logger = logging.getLogger(__name__)


@dataclass
class SelfNeuron:

    id: str
    center: List[float]
    count: int = 0
    salience_sum: float = 0.0
    last_update: float = field(default_factory=time.time)


class SelfNeuronNetwork:

    def __init__(self, chat_id: int):
        self.chat_id = int(chat_id)
        self._redis = None
        self._neurons: List[SelfNeuron] = []
        self._lock = asyncio.Lock()
        self._ready = False
        self._last_ready_attempt_ts = 0.0
        self._last_persist_ts = 0.0
        try:
            self._ready_retry_backoff_sec = float(
                getattr(settings, "SELFNET_READY_RETRY_BACKOFF_SEC", 5.0)
            )
        except Exception:
            self._ready_retry_backoff_sec = 5.0
        self._ready_retry_backoff_sec = max(0.0, self._ready_retry_backoff_sec)

        try:
            self._max_neurons = int(getattr(settings, "SELFNET_MAX_NEURONS", 100))
        except Exception:
            self._max_neurons = 100

        self._feature_keys: List[str] = [
            "valence",
            "arousal",
            "energy",
            "fatigue",
            "stress",
            "anxiety",
            "curiosity",
            "trust",
            "friendliness",
            "empathy",
            "self_reflection",
            "confidence",
            "creativity",
            "precision",
            "humor",
            "charisma",
            "engagement",
        ]

        self._dim: float = float(len(self._feature_keys)) or 1.0
        self._dim_sqrt: float = math.sqrt(self._dim)

    def _key_main(self) -> str:
        return f"selfnet:{self.chat_id}:neurons"

    async def ready(self) -> None:
        if self._ready:
            return

        now = time.time()
        if (
            self._last_ready_attempt_ts > 0.0
            and (now - self._last_ready_attempt_ts) < self._ready_retry_backoff_sec
        ):
            return
        self._last_ready_attempt_ts = now

        try:
            self._redis = get_redis_vector()
        except Exception:
            self._redis = None
            logger.debug("SelfNeuronNetwork.ready: get_redis_vector failed", exc_info=True)
            return

        if not self._redis:
            return

        try:
            loaded = await self._load()
        except Exception:
            logger.debug("SelfNeuronNetwork.ready: _load failed", exc_info=True)
            self._ready = False
            return

        self._ready = bool(loaded)

    async def _load(self) -> bool:
        if not self._redis:
            return False
        try:
            raw = await self._redis.get(self._key_main())
        except Exception:
            logger.debug("SelfNeuronNetwork._load redis error", exc_info=True)
            return False
        if not raw:
            self._neurons = []
            return True
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "ignore")
            payload: Dict[str, Any] = json.loads(raw)
            items = payload.get("neurons") or []
        except Exception:
            logger.debug("SelfNeuronNetwork._load decode error", exc_info=True)
            return False

        neurons: List[SelfNeuron] = []
        now = time.time()
        d = len(self._feature_keys)
        for it in items:
            try:
                nid = str(it.get("id") or "")
                center_raw = it.get("center")
                
                center_vec: List[float]
                if isinstance(center_raw, list):
                    center_vec = [float(v) for v in center_raw]
                elif isinstance(center_raw, dict):
                    center_vec = [
                        float(center_raw.get(k, 0.0))
                        for k in self._feature_keys
                    ]
                else:
                    center_vec = []

                if not nid or not center_vec:
                    continue

                if len(center_vec) < d:
                    center_vec = center_vec + [0.0] * (d - len(center_vec))
                elif len(center_vec) > d:
                    center_vec = center_vec[:d]

                neurons.append(
                    SelfNeuron(
                        id=nid,
                        center=center_vec,
                        count=int(it.get("count", 0)),
                        salience_sum=float(it.get("salience_sum", 0.0)),
                        last_update=float(it.get("last_update", now)),
                    )
                )
            except Exception:
                continue
        self._neurons = neurons
        return True

    async def _persist(self, force: bool = False) -> None:
        if not self._redis:
            return
        now = time.time()
        if not force:
            try:
                min_period = float(
                    getattr(settings, "SELFNET_PERSIST_PERIOD_SEC", 45.0)
                )
            except Exception:
                min_period = 45.0
            if (now - self._last_persist_ts) < min_period:
                return

        self._last_persist_ts = now
        payload = {
            "ts": now,
            "neurons": [
                {
                    "id": n.id,
                    "center": n.center,
                    "count": n.count,
                    "salience_sum": n.salience_sum,
                    "last_update": n.last_update,
                }
                for n in self._neurons
            ],
        }
        try:
            ttl = int(getattr(settings, "SELFNET_TTL_SEC", 30 * 24 * 3600))
        except Exception:
            ttl = 30 * 24 * 3600

        try:
            data = json.dumps(payload, ensure_ascii=False)
            await self._redis.set(self._key_main(), data, ex=max(1, ttl))
        except Exception:
            logger.debug("SelfNeuronNetwork._persist error", exc_info=True)

    def _make_vector(
        self, state: Dict[str, float], readings: Dict[str, float]
    ) -> List[float]:

        vec: List[float] = []
        for k in self._feature_keys:
            if k == "valence":
                v = readings.get("valence", state.get("valence", 0.0))
                v = _clamp(float(v), -1.0, 1.0)
            else:
                v = readings.get(k, state.get(k, 0.5))
                v = _clamp(float(v), 0.0, 1.0)
            vec.append(v)
        return vec

    @staticmethod
    def _dist_sq(a: List[float], b: List[float]) -> float:
        acc = 0.0
        for va, vb in zip(a, b):
            d = float(va) - float(vb)
            acc += d * d
        return acc

    def _effective_hits(self, n: SelfNeuron, now: float) -> float:

        try:
            half_life = float(
                getattr(settings, "SELFNET_HITS_HALFLIFE_SEC", 7 * 24 * 3600)
            )
        except Exception:
            half_life = 7 * 24 * 3600
        if half_life <= 0:
            return float(max(0, n.count))
        age = max(0.0, now - float(getattr(n, "last_update", now) or now))
        return float(max(0, n.count)) * (0.5 ** (age / half_life))

    def _prune_locked(self) -> None:

        if not self._neurons:
            return
        now = time.time()
        try:
            keep = int(getattr(settings, "SELFNET_PRUNE_KEEP", self._max_neurons))
        except Exception:
            keep = self._max_neurons
        if keep <= 0:
            self._neurons.clear()
            return
        scored = [
            (self._effective_hits(n, now), n)
            for n in self._neurons
        ]
        scored.sort(key=lambda kv: kv[0], reverse=True)
        self._neurons = [n for score, n in scored[:keep] if score > 0.0]

    async def observe(
        self,
        uid: int,
        text: str,
        readings: Dict[str, float],
        state: Dict[str, float],
        salience: float,
    ) -> Dict[str, float]:

        try:
            await self.ready()
        except Exception:
            return {}

        vec = self._make_vector(state, readings)
        s = _clamp(float(salience), 0.0, 1.0)
        if s <= 0.0:
            return {}

        async with self._lock:
            try:
                if len(self._neurons) > self._max_neurons:
                    self._prune_locked()
            except Exception:
                logger.debug("SelfNeuronNetwork._prune_locked failed, falling back to tail slice", exc_info=True)
                self._neurons = self._neurons[-self._max_neurons :]
            
            rng = random.Random((self.chat_id ^ uid ^ len(self._neurons)) & 0xFFFFFFFF)

            best = None
            d_min = None
            if self._neurons:
                for n in self._neurons:
                    d2 = self._dist_sq(vec, n.center)
                    if (d_min is None) or (d2 < d_min):
                        d_min = d2
                        best = n

            if best is None or d_min is None:
                nid = f"{int(time.time()*1000):x}-{rng.getrandbits(32):08x}"
                self._neurons.append(
                    SelfNeuron(
                        id=nid,
                        center=list(vec),
                        count=1,
                        salience_sum=s,
                    )
                )
                activ = [1.0]
                dominant_idx = 0
                await self._persist()
                metrics = self._metrics_from_activation(
                    activ,
                    readings,
                    state,
                    meta={"dominant_idx": dominant_idx},
                )
                metrics["_mode_id"] = "mode_0"
                return metrics

            try:
                d = math.sqrt(max(0.0, d_min))
                d_norm = d / self._dim_sqrt
            except Exception:
                d_norm = 0.5

            try:
                join_thr = float(getattr(settings, "SELFNET_JOIN_DISTANCE", 0.22))
            except Exception:
                join_thr = 0.22

            if d_norm > join_thr and len(self._neurons) < self._max_neurons:
                nid = f"{int(time.time()*1000):x}-{rng.getrandbits(32):08x}"
                self._neurons.append(
                    SelfNeuron(
                        id=nid,
                        center=list(vec),
                        count=1,
                        salience_sum=s,
                    )
                )
            else:
                alpha = 0.15 + 0.55 * s
                for i, k in enumerate(self._feature_keys):
                    try:
                        old = float(best.center[i])
                    except (IndexError, TypeError, ValueError):
                        old = 0.0
                    new = old + alpha * (vec[i] - old)
                    if k == "valence":
                        best.center[i] = _clamp(new, -1.0, 1.0)
                    else:
                        best.center[i] = _clamp(new, 0.0, 1.0)
                best.count += 1
                best.salience_sum += s
                best.last_update = time.time()

            dominant_idx = 0
            if self._neurons:
                d2s = [self._dist_sq(vec, n.center) for n in self._neurons]
                try:
                    scale = float(getattr(settings, "SELFNET_DISTANCE_SCALE", 1.0))
                except Exception:
                    scale = 1.0
                logits = []
                for i, d2 in enumerate(d2s):
                    try:
                        d = math.sqrt(max(0.0, d2))
                        d_norm = d / self._dim_sqrt
                    except Exception:
                        d_norm = 0.5
                    logits.append(-((d_norm * scale) ** 2))
                    if i == 0 or d2 < d2s[dominant_idx]:
                        dominant_idx = i
                m = max(logits) if logits else 0.0
                exps = [math.exp(L - m) for L in logits]
                Z = sum(exps) or 1.0
                activ = [e / Z for e in exps]
            else:
                activ = [1.0]

            await self._persist()

        try:
            if activ:
                dominant_idx = max(range(len(activ)), key=lambda i: activ[i])
            else:
                dominant_idx = 0
        except Exception:
            dominant_idx = 0

        metrics = self._metrics_from_activation(
            activ,
            readings,
            state,
            meta={"dominant_idx": dominant_idx},
        )
        try:
            metrics["_mode_id"] = f"mode_{int(dominant_idx)}"
        except Exception:
            metrics["_mode_id"] = "mode_0"
        return metrics

    def _metrics_from_activation(
        self,
        activ: List[float],
        readings: Dict[str, float],
        state: Dict[str, float],
        meta: Dict[str, Any] | None = None,
    ) -> Dict[str, float]:

        if not activ:
            return {}

        Z = sum(activ) or 1.0
        p = [max(0.0, a) / Z for a in activ]

        entropy = 0.0
        k_active = 0
        for pi in p:
            if pi <= 0.0:
                continue
            entropy += -pi * math.log(pi + 1e-9, 2)
            if pi > 0.10:
                k_active += 1

        max_entropy = math.log(len(p), 2) if len(p) > 1 else 1.0
        coherence = 1.0 - entropy / max_entropy if max_entropy > 0 else 1.0
        coherence = _clamp(coherence, 0.0, 1.0)

        try:
            novelty = 1.0 - max(p)
        except Exception:
            novelty = 0.0
        novelty = _clamp(novelty, 0.0, 1.0)

        complexity = _clamp(
            math.sqrt(k_active / max(1.0, float(len(p)))), 0.0, 1.0
        )

        intensity = _clamp(
            float(readings.get("arousal", state.get("arousal", 0.5))), 0.0, 1.0
        )

        base_self = float(state.get("self_reflection", readings.get("self_reflection", 0.5)))
        base_cur = float(state.get("curiosity", readings.get("curiosity", 0.5)))
        base_crea = float(state.get("creativity", readings.get("creativity", 0.5)))
        base_conf = float(state.get("confidence", readings.get("confidence", 0.5)))
        base_eng = float(state.get("engagement", readings.get("engagement", 0.5)))
        base_trust = float(state.get("trust", readings.get("trust", 0.5)))
        base_energy = float(state.get("energy", readings.get("energy", 0.5)))
        base_fatigue = float(state.get("fatigue", readings.get("fatigue", 0.5)))
        base_stress = float(state.get("stress", readings.get("stress", 0.5)))
        base_anxiety = float(state.get("anxiety", readings.get("anxiety", 0.5)))
        base_val = float(state.get("valence", readings.get("valence", 0.0)))
        base_precision = float(state.get("precision", readings.get("precision", 0.5)))
        base_charisma = float(state.get("charisma", readings.get("charisma", 0.5)))
        base_humor = float(state.get("humor", readings.get("humor", 0.5)))

        stress_anx = 0.5 * base_stress + 0.5 * base_anxiety

        self_reflection = base_self
        self_reflection += 0.25 * coherence + 0.15 * complexity - 0.10 * novelty
        self_reflection = _clamp(self_reflection, 0.0, 1.0)

        curiosity = base_cur
        curiosity += 0.30 * novelty + 0.15 * complexity - 0.10 * coherence
        curiosity = _clamp(curiosity, 0.0, 1.0)

        creativity = base_crea
        creativity += 0.20 * novelty + 0.10 * complexity
        creativity -= 0.10 * coherence * (1.0 - intensity)
        creativity = _clamp(creativity, 0.0, 1.0)

        confidence = base_conf
        confidence += 0.10 * coherence - 0.10 * novelty
        confidence -= 0.05 * (stress_anx - 0.5)
        confidence = _clamp(confidence, 0.0, 1.0)

        engagement = base_eng
        engagement += 0.20 * intensity + 0.15 * coherence + 0.10 * complexity
        engagement -= 0.15 * novelty
        engagement -= 0.10 * (base_fatigue - 0.5)
        engagement = _clamp(engagement, 0.0, 1.0)

        trust = base_trust
        trust += 0.08 * coherence - 0.08 * novelty
        trust -= 0.04 * stress_anx
        trust = _clamp(trust, 0.0, 1.0)

        energy = base_energy
        energy += 0.15 * intensity + 0.10 * novelty
        energy -= 0.10 * stress_anx
        energy = _clamp(energy, 0.0, 1.0)

        fatigue = base_fatigue
        fatigue += 0.20 * stress_anx
        fatigue -= 0.10 * intensity
        fatigue -= 0.10 * novelty
        fatigue = _clamp(fatigue, 0.0, 1.0)

        dv = 0.30 * (coherence - novelty)
        dv += 0.15 * (energy - 0.5)
        dv -= 0.25 * (stress_anx - 0.5)
        valence = _clamp(base_val + dv, -1.0, 1.0)

        precision = base_precision
        precision += 0.20 * coherence
        precision -= 0.10 * novelty
        precision -= 0.10 * complexity
        precision = _clamp(precision, 0.0, 1.0)

        charisma = base_charisma
        charisma += 0.22 * engagement
        charisma += 0.12 * energy
        charisma -= 0.10 * stress_anx
        charisma = _clamp(charisma, 0.0, 1.0)

        humor = base_humor
        humor += 0.20 * novelty
        humor += 0.15 * engagement
        humor -= 0.12 * stress_anx
        humor = _clamp(humor, 0.0, 1.0)

        out = {
            "self_reflection": self_reflection,
            "curiosity": curiosity,
            "creativity": creativity,
            "confidence": confidence,
            "engagement": engagement,
            "trust": trust,
            "energy": energy,
            "fatigue": fatigue,
            "valence": valence,
            "precision": precision,
            "charisma": charisma,
            "humor": humor,
        }

        if meta is not None:
            out["_mode_coherence"] = coherence
            out["_mode_novelty"] = novelty
            out["_mode_complexity"] = complexity
            out["_mode_intensity"] = intensity
            out["_mode_dominant_idx"] = float(meta.get("dominant_idx", 0))

        return out

    def describe_state(self) -> str:

        if not self._neurons:
            return "SelfPatterns:neurons=0"
        dominant = max(self._neurons, key=lambda n: n.count or 1)
        age_sec = max(1.0, time.time() - dominant.last_update)
        age_min = age_sec / 60.0
        return (
            f"SelfPatterns:neurons={len(self._neurons)},"
            f"dominant_hits={dominant.count},"
            f"dominant_age_min≈{age_min:.1f}"
        )

    async def close(self) -> None:
        try:
            await self._persist(force=True)
        except Exception:
            logger.debug("SelfNeuronNetwork.close error", exc_info=True)
