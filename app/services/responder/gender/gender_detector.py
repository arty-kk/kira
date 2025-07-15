cat >app/services/responder/gender/gender_detector.py<< EOF
#app/services/responder/gender/gender_detector.py
import asyncio
import logging
import httpx
from typing import Optional, Tuple, Dict

from app.clients.openai_client import _call_openai_with_retry
from app.config import settings

__all__ = ["detect_gender"]

logger = logging.getLogger(__name__)

GENDERIZE_URL = "https://api.genderize.io/"
_GENDERIZE_TIMEOUT = getattr(settings, "GENDERIZE_TIMEOUT", 5.0)
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
            data: dict = await resp.json()
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


async def _ask_gpt(prompt: str, *, timeout: float) -> str | None:

    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                model=settings.BASE_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=3,
            ),
            timeout=timeout,
        )
        if resp and getattr(resp, 'choices', None):
            ans = resp.choices[0].message.content.strip().lower()
            if ans in ("male", "female", "unknown"):
                logger.debug("GPT answered: %s", ans)
                return ans
    except asyncio.TimeoutError:
        logger.warning("GPT timeout (%.1f s)", timeout)
    except Exception as exc:
        logger.warning("GPT error: %s", exc)
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

    few_shot_prompt = f"""
You are given a first name and must answer with exactly one lowercase word: male, female, or unknown.
Answer ‘unknown’ unless you are at least 90% certain that the name is male or female.

Name: "Maria"
Answer: female

Name: "John"
Answer: male

Name: "{first}"
Answer:
""".strip()

    ans = await _ask_gpt(few_shot_prompt, timeout=_FEW_SHOT_TIMEOUT)
    if ans:
        logger.debug("few‑shot GPT: %s", ans)
        return ans

    cot_prompt = f"""
You are an onomastics expert and discourse analyst.
Given a first name and one of the user's messages, decide their likely gender.
Reply with exactly one lowercase word: male, female, or unknown.
Answer ‘unknown’ unless you are at least 90% certain.

Example:
Name: "Alice"
Message: "I love my cat."
Answer: female

Now:
Name: "{first}"
Message: "{text}"
Answer:
""".strip()

    ans = await _ask_gpt(cot_prompt, timeout=_COT_TIMEOUT)
    if ans:
        logger.debug("CoT GPT: %s", ans)
        return ans

    return "unknown"
EOF