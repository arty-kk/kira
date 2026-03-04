# app/emo_engine/persona/utils/text_analyzer.py
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from random import Random
from typing import Dict, Optional

from app.config import settings
from app.prompts_base import (
    TEXT_ANALYZER_SYSTEM_PROMPT_TEMPLATE,
    TEXT_ANALYZER_USER_PROMPT_NO_CTX_TEMPLATE,
    TEXT_ANALYZER_USER_PROMPT_WITH_CTX_TEMPLATE,
)
from app.clients.openai_client import _call_openai_with_retry, _get_output_text
from ..constants.emotions import ALL_METRICS, ANALYSIS_METRICS

logger = logging.getLogger(__name__)


def _neutral_baseline(metric: str) -> float:
    if metric == "valence":
        return 0.0
    if metric in ("arousal", "dominance"):
        return 0.4
    return 0.0


def _responses_text_schema() -> dict:
    props: dict = {}
    req: list[str] = []
    for m in ANALYSIS_METRICS:
        req.append(m)
        if m == "valence":
            props[m] = {"type": "number", "minimum": -1.00, "maximum": 1.00}
        else:
            props[m] = {"type": "number", "minimum": 0.00, "maximum": 1.00}
    return {
        "type": "object",
        "properties": props,
        "required": req,
        "additionalProperties": False,
    }


_SCHEMA_ANALYSIS = _responses_text_schema()


def _extract_largest_json_object(content: str) -> Optional[dict]:
    """
    Best-effort extractor: returns the largest {...} JSON object from a string,
    ignoring braces inside JSON strings.
    """
    best: Optional[str] = None
    depth = 0
    start = -1
    in_str = False
    esc = False

    for i, ch in enumerate(content):
        if ch == '"' and not esc:
            in_str = not in_str
        esc = (ch == "\\") and not esc

        if in_str:
            continue

        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start != -1:
                cand = content[start : i + 1]
                if best is None or len(cand) > len(best):
                    best = cand

    if not best:
        return None

    try:
        return json.loads(best)
    except json.JSONDecodeError:
        return None


def _strip_code_fences_and_bom(s: str) -> str:
    s = s.strip().lstrip("\ufeff")
    if s.startswith("```"):
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1 :].strip()
        s = s.rstrip("`").strip()
    return s


class TextAnalyzer:
    def __init__(self) -> None:
        self._rng = Random()
        self.ema: Dict[str, float] = {m: _neutral_baseline(m) for m in ALL_METRICS}

    @staticmethod
    def _clamp(value: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
        return max(min_val, min(max_val, value))

    def _jitter(self, metric: str, amp: float = 0.02) -> float:
        base = self.ema.get(metric, _neutral_baseline(metric))
        delta = self._rng.uniform(-amp, amp)
        val = base + delta
        if metric == "valence":
            return max(-1.0, min(1.0, val))
        return self._clamp(val)

    def _fallback(self, missing: Optional[list[str]] = None) -> Dict[str, float]:
        out = {m: self._jitter(m) for m in ANALYSIS_METRICS}
        out["_analysis_missing"] = missing if missing is not None else list(ANALYSIS_METRICS)
        return out

    def _parse_json(self, content: str) -> Optional[dict]:
        if not content:
            return None
        cleaned = _strip_code_fences_and_bom(content)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return _extract_largest_json_object(content)

    def _normalize_metrics(self, parsed: dict) -> Dict[str, float]:
        """
        Takes parsed dict, extracts ANALYSIS_METRICS only, converts to finite floats,
        clamps into ranges. Does NOT fill missing.
        """
        out: Dict[str, float] = {}
        for k, v in parsed.items():
            if k not in ANALYSIS_METRICS:
                continue
            try:
                val = float(v)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(val):
                continue

            if k == "valence":
                out[k] = max(-1.0, min(1.0, val))
            else:
                out[k] = self._clamp(val)
        return out

    def _update_ema(self, metrics: Dict[str, float], alpha: float = 0.25) -> None:
        for m in ANALYSIS_METRICS:
            cur = metrics.get(m, self.ema.get(m, _neutral_baseline(m)))
            prev = self.ema.get(m, _neutral_baseline(m))
            self.ema[m] = (1 - alpha) * prev + alpha * cur

    async def analyze_text(self, text: str, ctx_dialog: str | None = None) -> Dict[str, float]:
        text = (text or "").strip()
        if not text:
            return {}

        cleaned_ctx_dialog = ""
        if ctx_dialog:
            # remove timestamps like [12:34] / [12:34:56]
            cleaned_ctx_dialog = re.sub(
                r"\[\d{2}:\d{2}(?::\d{2})?\]",
                "[]",
                ctx_dialog,
            )

        metric_list = ", ".join(f'"{m}"' for m in ANALYSIS_METRICS)
        system_prompt = TEXT_ANALYZER_SYSTEM_PROMPT_TEMPLATE.format(metric_list=metric_list)

        if ctx_dialog:
            user_prompt = TEXT_ANALYZER_USER_PROMPT_WITH_CTX_TEMPLATE.format(
                text=text,
                ctx_dialog=cleaned_ctx_dialog,
            )
        else:
            user_prompt = TEXT_ANALYZER_USER_PROMPT_NO_CTX_TEMPLATE.format(text=text)

        try:
            resp = await asyncio.wait_for(
                _call_openai_with_retry(
                    endpoint="responses.create",
                    model=settings.BASE_MODEL,
                    model_role="base",
                    instructions=system_prompt,
                    input=user_prompt,
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": "user_metrics",
                            "schema": _SCHEMA_ANALYSIS,
                            "strict": True,
                        }
                    },
                    temperature=0,
                    max_output_tokens=300,
                ),
                settings.BASE_MODEL_TIMEOUT,
            )
            content = (_get_output_text(resp) or "").strip()
        except Exception:
            logger.exception("[TextAnalyzer] analyze_text failed; using jitter baseline")
            return self._fallback()

        parsed = self._parse_json(content)
        if not isinstance(parsed, dict):
            logger.debug("[TextAnalyzer] JSON parse failed; using jitter baseline")
            return self._fallback()

        full = self._normalize_metrics(parsed)
        missing = [m for m in ANALYSIS_METRICS if m not in full]
        full["_analysis_missing"] = missing

        if missing:
            for m in missing:
                full[m] = self._jitter(m)

        try:
            self._update_ema(full)
        except Exception:
            # EMA must never break main flow
            logger.debug("[TextAnalyzer] EMA update failed", exc_info=True)

        return full
