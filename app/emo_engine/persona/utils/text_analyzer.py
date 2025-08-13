cat >app/emo_engine/persona/utils/text_analyzer.py<< 'EOF'
#app/emo_engine/persona/utils/text_analyzer.py
import asyncio
import time
import json
import regex
import hashlib
import logging
import functools

from collections import OrderedDict
from typing import Dict

from app.config import settings
from app.clients.openai_client import _call_openai_with_retry
from ..constants.emotions import ALL_METRICS, ANALYSIS_METRICS


logger = logging.getLogger(__name__)


def _extract_json_brace(content: str) -> dict | None:
    try:
        candidates = regex.findall(r"\{(?:[^{}]|(?R))*\}", content)
        for candidate in candidates:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    except Exception as exc:
        raise
    return None


class TextAnalyzer:
    def __init__(self):
        from random import Random
        self._rng = Random()
        self.ema = {m: 0.33 for m in ALL_METRICS}


    def _clamp(self, value: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
        return max(min_val, min(max_val, value))


    async def analyze_text(self, text: str) -> Dict[str, float]:
        text = text or ''
        if not text.strip():
            return {m: 0.33 for m in ALL_METRICS}

        ctx_dialog: str | None = getattr(self, "_ctx_dialog", None)
        if ctx_dialog:
            prompt_base = await asyncio.to_thread(
                regex.sub,
                r"\[\d{2}:\d{2}(?::\d{2})?\]",
                "[]",
                ctx_dialog,
            )
            delattr(self, "_ctx_dialog")
        else:
            prompt_base = text

        metric_list = ", ".join(f'"{m}"' for m in ANALYSIS_METRICS)

        if ctx_dialog:
            prompt = [
                {
                    "role": "system",
                    "content": (
                        "You are a professional psychologist specialised in affective-computing. "
                        f"Reply **only** with a single JSON object that contains exactly these keys: {metric_list}. "
                        "Valence: -1.00 (very negative)…+1.00 (very positive)\n"
                        "Others: 0.00 (none)…1.00 (maximum)\n"
                        "Format every value with **exactly two decimals** (e.g. 0.23, -0.75).\n"
                        "Return **only** a single-line minified JSON (no spaces, no newlines, no markdown, no extra keys, no comments).\n"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Conversation history (oldest → newest):\n"
                        f"{prompt_base}\n\n"
                        "What emotional state does the user have at the time of the last message?"
                        "Return only the JSON now."
                    ),
                },
            ]
        else:
            safe_text = json.dumps(text)
            prompt = [
                {
                    "role": "system",
                    "content": (
                        "You are a professional psychologist specialised in affective-computing. "
                        f"Reply **only** with a single JSON object that contains exactly these keys: {metric_list}. "
                        "Valence: -1.00 (very negative)…+1.00 (very positive)\n"
                        "Others: 0.00 (none)…1.00 (maximum)\n"
                        "Format every value with **exactly two decimals** (e.g. 0.23, -0.75).\n"
                        "Return **only** a single-line minified JSON (no spaces, no newlines, no markdown, no extra keys, no comments).\n"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"User's message to analyse:\n{safe_text}\n\n"
                        "What emotional state does the user have at the time of the last message?"
                        "Return only the JSON now."
                    ),
                },
            ]
        content = ''
        try:
            resp = await asyncio.wait_for(
                _call_openai_with_retry(
                    model=settings.REASONING_MODEL,
                    messages=prompt,
                    temperature=0.0,
                    max_completion_tokens=300
                ),
                timeout=30.0
            )
            content = resp.choices[0].message.content.strip()
        except Exception:
            logger.exception("[TextAnalyzer] analyze_text failed returning full defaults")
            return {m: 0.33 for m in ALL_METRICS}

        try:
            parsed = await asyncio.to_thread(_extract_json_brace, content)
        except Exception as exc:
            logger.warning("[TextAnalyzer] JSON extraction in thread failed: %s", exc)
            parsed = None

        if not parsed:
            def jitter(m):
                base = self.ema.get(m, 0.33)
                delta = self._rng.uniform(-0.03, 0.03)
                if m == "valence":
                    return self._clamp(base + delta, -0.90, 0.90)
                return self._clamp(base + delta)
            return {m: jitter(m) for m in ALL_METRICS}

        full = {m: 0.33 for m in ALL_METRICS}
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

        return full
EOF