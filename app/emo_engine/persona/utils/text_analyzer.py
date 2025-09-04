#app/emo_engine/persona/utils/text_analyzer.py
import asyncio
import json
import regex
import logging

from random import Random
from typing import Dict

from app.config import settings
from app.clients.openai_client import _call_openai_with_retry, _get_output_text
from ..constants.emotions import ALL_METRICS, ANALYSIS_METRICS

logger = logging.getLogger(__name__)


def _extract_json_brace(content: str) -> dict | None:

    try:
        candidates = regex.findall(r"\{(?:[^{}]|(?R))*\}", content)
        for candidate in sorted(candidates, key=len, reverse=True):
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    except Exception:
        logger.debug("[TextAnalyzer] regex JSON extraction failed", exc_info=True)
    return None


def _neutral_baseline(metric: str) -> float:
    if metric == "valence":
        return 0.0
    if metric in ("arousal", "dominance"):
        return 0.4
    return 0.0


def _responses_text_schema() -> dict:

    props = {}
    req = []
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

class TextAnalyzer:
    def __init__(self):
        self._rng = Random()
        self.ema = {m: _neutral_baseline(m) for m in ALL_METRICS}

    def _clamp(self, value: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
        return max(min_val, min(max_val, value))

    async def analyze_text(self, text: str, ctx_dialog: str | None = None) -> Dict[str, float]:
        text = text or ''
        if not text.strip():
            return {}

        if ctx_dialog:
            prompt_base = await asyncio.to_thread(
                regex.sub,
                r"\[\d{2}:\d{2}(?::\d{2})?\]",
                "[]",
                ctx_dialog,
            )
        else:
            prompt_base = text

        metric_list = ", ".join(f'"{m}"' for m in ANALYSIS_METRICS)
        system_prompt = (
            "You are a professional analyzer of emotional metrics based on the conversation context.\n"
            "Task: determine the current values of the user's emotion metrics based on their last message and taking into account the conversation context.\n"
            f"Output: exactly JSON object that MUST validate against the provided JSON schema and contain exactly these keys: {metric_list}.\n"
            "Rules:\n"
            "- Use only numbers (no strings), decimal point '.', no NaN/Inf/','.\n"
            "- Ranges: valence in [-1.00, 1.00]; all others in [0.00, 1.00].\n"
            "- Metrics evaluation should be honest, without making up their values.\n"
            "- If you can't determine values ​​for any metrics, use default values: valence 0.00; arousal/dominance 0.40, others 0.00.\n"
            "- Consider emojis and punctuation marks ('...', '!', '!!!', '?', '???') in your analysis to more accurately determine the list of metrics and its values."
        )

        if ctx_dialog:
            user_prompt = (
                "Conversation (oldest→newest):\n"
                f"{prompt_base}\n\n"
                "Determine the current values of the user's emotion metrics based on their last message and taking into account the conversation context.\n"
                "Return ONLY a single minified JSON object."
            )
        else:
            user_prompt = (
                "User last message:\n"
                f"{text}\n\n"
                "Determine current values of the user emotion metrics based on their last message.\n"
                "Return ONLY a single minified JSON object."
            )

        content = ''
        try:
            resp = await asyncio.wait_for(
                _call_openai_with_retry(
                    endpoint="responses.create",
                    model=settings.BASE_MODEL,
                    instructions=system_prompt,
                    input=user_prompt,
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": "user_metrics",
                            "schema": _responses_text_schema(),
                            "strict": True
                        }
                    },
                    temperature=0,
                    max_output_tokens=300,
                ),
                timeout=30.0
            )
            content = (_get_output_text(resp) or "").strip()
        except Exception:
            logger.exception("[TextAnalyzer] analyze_text failed; using jitter baseline")
            def jitter(m):
                base = self.ema.get(m, _neutral_baseline(m))
                delta = self._rng.uniform(-0.02, 0.02)
                if m == "valence":
                    return max(-1.0, min(1.0, base + delta))
                return self._clamp(base + delta)
            return {m: jitter(m) for m in ANALYSIS_METRICS}

        parsed: dict | None = None
        if content:
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                parsed = _extract_json_brace(content)

        if not parsed:
            def jitter(m):
                base = self.ema.get(m, _neutral_baseline(m))
                delta = self._rng.uniform(-0.02, 0.02)
                if m == "valence":
                    return max(-1.0, min(1.0, base + delta))
                return self._clamp(base + delta)
            return {m: jitter(m) for m in ANALYSIS_METRICS}

        full: Dict[str, float] = {}
        for k, v in parsed.items():
            if k in ANALYSIS_METRICS:
                try:
                    val = float(v)
                    if k == "valence":
                        full[k] = max(-1.0, min(1.0, val))
                    else:
                        full[k] = self._clamp(val)
                except (TypeError, ValueError):
                    continue
        return full

