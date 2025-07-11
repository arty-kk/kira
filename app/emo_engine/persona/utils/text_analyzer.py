import asyncio
import time
import json
import hashlib
import logging
import regex
from collections import OrderedDict
from typing import Dict
from app.config import settings
from app.clients.openai_client import _call_openai_with_retry
from ..constants.emotions import ALL_METRICS, ANALYSIS_METRICS

logger = logging.getLogger(__name__)

_EMOJI_THRESHOLD = 1
_WORD_THRESHOLD = 1
_CACHE_TTL = 3600
_MAX_CACHE_SIZE = 1000

_EMOJI_REACTION_RE = regex.compile(r'(?:[\u2639\u263A]|[\U0001F600-\U0001F64F]|\p{Extended_Pictographic})')
_STRONG_WORDS = {
    'WOW','OMG','YAY','UGH','DAMN','HATE','LOVE','ANGRY','SAD','HAPPY','FURIOUS',
    'AWESOME','AMAZING','INCREDIBLE','FANTASTIC','TERRIBLE','AWFUL','HORRIBLE',
    'DISGUSTING','DISGUSTED','DISAPPOINTED'
}

class TextAnalyzer:
    def __init__(self):
        from random import Random
        self._rng = Random()
        self.ema = {m: 0.5 for m in ALL_METRICS}
        self._analysis_cache = OrderedDict()
        self._cache_lock = asyncio.Lock()

    def _contains_enough_emojis(self, text: str) -> bool:
        return len(_EMOJI_REACTION_RE.findall(text)) >= _EMOJI_THRESHOLD

    def _contains_strong_words(self, text: str) -> bool:
        upper = text.upper()
        return sum(1 for w in _STRONG_WORDS if w in upper) >= _WORD_THRESHOLD

    def _clamp(self, value: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
        return max(min_val, min(max_val, value))

    async def analyze_text(self, text: str) -> Dict[str, float]:
        text = text or ''
        if not text.strip():
            return {m: 0.5 for m in ALL_METRICS}

        if len(text) <= 15 and not regex.search(r"[A-Za-zА-Яа-я]", text):
            jitter = lambda: self._rng.uniform(-0.03, 0.03)
            return {
                m: self._clamp(self.ema.get(m, 0.5) + jitter(), -0.90, 0.90) if m == "valence"
                else self._clamp(self.ema.get(m, 0.5) + jitter())
                for m in ALL_METRICS
            }

        digest_key = hashlib.sha1(text.encode("utf-8")).hexdigest()

        async with self._cache_lock:
            cached = self._analysis_cache.get(digest_key)
            if cached:
                ts, payload = cached
                if time.time() - ts < _CACHE_TTL:
                    self._analysis_cache.move_to_end(digest_key)
                    return payload.copy()
                self._analysis_cache.pop(digest_key, None)

        if not (self._contains_enough_emojis(text) or self._contains_strong_words(text)):
            base = {m: 0.5 for m in ALL_METRICS}
            base["valence"] = 0.0
            return base

        safe_text = json.dumps(text)
        metric_list = ", ".join(f'\"{m}\"' for m in ANALYSIS_METRICS)
        prompt = [
            {"role": "system", "content": "You are an expert emotion analyzer. Output ONLY valid JSON."},
            {"role": "user", "content": (
                f"Output ONLY a JSON object with these keys {metric_list}:"
                " valence in [-1,1], others in [0,1].\n\n"
                f"Text: {safe_text}"
            )}
        ]

        content = ''
        try:
            resp = await asyncio.wait_for(
                _call_openai_with_retry(
                    model=settings.REASONING_MODEL,
                    messages=prompt,
                    temperature=0.0,
                    max_tokens=300
                ),
                timeout=10.0
            )
            content = resp.choices[0].message.content.strip()
        except Exception as exc:
            logger.error("[TextAnalyzer] analyze_text failed: %s", exc)

        parsed = None
        try:
            candidates = regex.findall(r"\{(?:[^{}]|(?R))*\}", content)
            for candidate in candidates:
                try:
                    parsed = json.loads(candidate)
                    break
                except json.JSONDecodeError:
                    continue
        except Exception as exc:
            logger.warning("Failed to extract JSON: %s", exc)

        if parsed is None:
            jitter = lambda: self._rng.uniform(-0.03, 0.03)
            return {
                m: self._clamp(self.ema.get(m, 0.5) + jitter(), -0.90, 0.90) if m == "valence"
                else self._clamp(self.ema.get(m, 0.5) + jitter())
                for m in ALL_METRICS
            }

        full = {m: 0.5 for m in ALL_METRICS}
        for k, v in parsed.items():
            if k in full:
                try:
                    val = float(v)
                    if k == "valence":
                        full[k] = self._clamp(val, -1.0, 1.0)
                    else:
                        full[k] = self._clamp(val, 0.0, 1.0)
                except (TypeError, ValueError):
                    continue

        async with self._cache_lock:
            self._analysis_cache[digest_key] = (time.time(), full.copy())
            if len(self._analysis_cache) > _MAX_CACHE_SIZE:
                self._analysis_cache.popitem(last=False)

        return full
