cat >app/services/responder/gender/gender_detector.py<< EOF
#app/services/responder/gender/gender_detector.py
import asyncio, logging, httpx

from functools import lru_cache

from app.clients.openai_client import _call_openai_with_retry
from app.config import settings

__all__ = ["detect_gender"]

logger = logging.getLogger(__name__)

GENDERIZE_URL = "https://api.genderize.io/"
_GENDERIZE_TIMEOUT = 5.0
_FEW_SHOT_TIMEOUT = 10.0
_COT_TIMEOUT = 15.0
_CONF_THRESHOLD = 0.9


LOCAL_NAMES: dict[str, str] = {
    "анна": "female",
    "александра": "female",
    "artem": "male",
    "артём": "male",
    "александр": "male",
    "ivan": "male",
    "maria": "female",
}


def _accept(gender: str | None, prob: float = 1.0) -> bool:

    return gender in ("male", "female") and prob >= _CONF_THRESHOLD


@lru_cache(maxsize=1024)
async def _genderize_query(first: str) -> tuple[str | None, float]:

    try:
        async with httpx.AsyncClient(timeout=_GENDERIZE_TIMEOUT) as client:
            resp = await client.get(GENDERIZE_URL, params={"name": first})
            data: dict = resp.json()
        gender = data.get("gender")
        prob = float(data.get("probability", 0) or 0)
        return gender, prob
    except (httpx.ReadTimeout, httpx.ConnectTimeout):
        logger.warning("genderize.io timeout (%.1f s)", _GENDERIZE_TIMEOUT)
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
        if resp.choices:
            ans = resp.choices[0].message.content.strip().lower()
            if ans in ("male", "female"):
                return ans
    except asyncio.TimeoutError:
        logger.warning("GPT timeout (%.1f s)", timeout)
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

    gender, prob = await _genderize_query(first_lc)
    if _accept(gender, prob):
        logger.debug("genderize.io: %s (prob=%.2f)", gender, prob)
        return gender

    few_shot_prompt = f"""
You are given a first name and must answer with one of three words: "male", "female", or "unknown".
Answer "unknown" unless you are at least 90 % certain.

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
Respond strictly with "male", "female", or "unknown" (no extra text).
Answer "unknown" unless the probability is ≥90 %.

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