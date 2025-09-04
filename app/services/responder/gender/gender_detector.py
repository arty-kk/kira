cat >app/services/responder/gender/gender_detector.py<< 'EOF'
#app/services/responder/gender/gender_detector.py
import asyncio
import logging
import httpx
import json

from typing import Optional, Tuple, Dict

from app.clients.openai_client import _call_openai_with_retry, _get_output_text
from app.config import settings

__all__ = ["detect_gender"]

logger = logging.getLogger(__name__)

GENDERIZE_URL = "https://api.genderize.io/"
_GENDERIZE_TIMEOUT = getattr(settings, "GENDERIZE_TIMEOUT", 10.0)
_FEW_SHOT_TIMEOUT = getattr(settings, "FEW_SHOT_TIMEOUT", 10.0)
_COT_TIMEOUT = getattr(settings, "COT_TIMEOUT", 15.0)
_CONF_THRESHOLD = getattr(settings, "CONF_THRESHOLD", 0.9)


LOCAL_NAMES: dict[str, str] = {
    "анна": "female",
    "александра": "female",
    "artem": "male",
    "артём": "male",
    "александр": "male",
    "ivan": "male",
    "maria": "female",
}


def _accept(gender: Optional[str], prob: float = 1.0) -> bool:
    return gender in ("male", "female") and prob >= _CONF_THRESHOLD

_genderize_cache: Dict[str, Tuple[Optional[str], float]] = {}
_genderize_lock = asyncio.Lock()

async def _genderize_query(first: str) -> Tuple[Optional[str], float]:

    try:
        async with httpx.AsyncClient(timeout=_GENDERIZE_TIMEOUT) as client:
            resp = await client.get(GENDERIZE_URL, params={"name": first})
            resp.raise_for_status()
            data: dict = resp.json()
        gender = data.get("gender")
        prob = float(data.get("probability", 0) or 0)
        async with _genderize_lock:
            _genderize_cache[first] = (gender, prob)
        logger.debug("genderize.io: %s -> %s (prob=%.2f)", first, gender, prob)
        return gender, prob
    except (httpx.ReadTimeout, httpx.ConnectTimeout):
        logger.warning("genderize.io timeout (%.1f s)", _GENDERIZE_TIMEOUT)
    except httpx.HTTPStatusError as exc:
        logger.warning("genderize.io HTTP error %s: %s", exc.response.status_code, exc)
    except Exception as exc:
        logger.warning("genderize.io error: %s", exc)
    return None, 0.0


def _gender_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "gender": {
                "type": "string",
                "enum": ["male", "female", "unknown"]
            }
        },
        "required": ["gender"],
        "additionalProperties": False,
    }

def _build_system_prompt() -> str:
    return (
        "You are a multilingual onomastics classifier.\n"
        "Return ONLY minified JSON that conforms to the provided json_schema.\n"
        "Policy:\n"
        "- Choose 'unknown' unless you are at least 90% certain the name/message indicates 'male' or 'female'.\n"
        "- No explanations, no markdown, no code fences.\n"
        "Return only the JSON."
    )

def _build_user_prompt(name: str, message: Optional[str] = None) -> str:
    if message:
        return f'Name: "{name}"\nMessage: "{message}"\nTask: Output the JSON.'
    return f'Name: "{name}"\nTask: Output the JSON.'

def _norm_gender(v: Optional[str]) -> Optional[str]:
    if not isinstance(v, str):
        return None
    v = v.strip().lower()
    return v if v in ("male", "female", "unknown") else None

async def _ask_gender(name: str, *, message: Optional[str], timeout: float) -> str | None:

    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=settings.BASE_MODEL,
                instructions=_build_system_prompt(),
                input=_build_user_prompt(name, message),
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "gender_label",
                        "schema": _gender_schema(),
                        "strict": True
                    }
                },
                temperature=0,
                max_output_tokens=32,
            ),
            timeout=timeout,
        )
        if not resp:
            return None
        raw = (_get_output_text(resp) or "").strip()
        try:
            obj = json.loads(raw)
            ans = _norm_gender(obj.get("gender"))
            if ans:
                logger.debug("LLM gender JSON: %s", ans)
                return ans
        except Exception:
            ans = _norm_gender(raw)
            if ans:
                logger.debug("LLM gender (plain): %s", ans)
                return ans
    except asyncio.TimeoutError:
        logger.warning("LLM timeout (%.1f s)", timeout)
    except Exception as exc:
        logger.warning("LLM error: %s", exc)
    return None


async def detect_gender(name: str, text: str) -> str:

    parts = name.strip().split()
    if not parts:
        return "unknown"

    first = parts[0]
    first_lc = first.lower()

    local = LOCAL_NAMES.get(first_lc)
    if local:
        logger.debug("local dict: %s -> %s", first, local)
        return local

    if first_lc in _genderize_cache:
        gender, prob = _genderize_cache[first_lc]
    else:
        gender, prob = await _genderize_query(first_lc)
    if _accept(gender, prob):
        logger.debug("genderize.io accepted: %s (prob=%.2f)", gender, prob)
        return gender

    ans = await _ask_gender(first, message=None, timeout=_FEW_SHOT_TIMEOUT)
    if ans:
        logger.debug("LLM (name-only): %s", ans)
        return ans

    ans = await _ask_gender(first, message=text, timeout=_COT_TIMEOUT)
    if ans:
        logger.debug("LLM (name+context): %s", ans)
        return ans

    return "unknown"
EOF