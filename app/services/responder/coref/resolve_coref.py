cat >app/services/responder/coref/resolve_coref.py<< EOF
#app/services/responder/coref/resolve_coref.py
import logging
import asyncio
import hashlib
import json

from typing import List, Dict
from app.clients.openai_client import _call_openai_with_retry
from app.config import settings
from app.core.memory import get_redis

logger = logging.getLogger(__name__)

_COT_PROMPT = """You are a coreference-resolution assistant.

Instructions (CoT Phase):
1. Identify all pronouns in the user's new query that refer to entities other than the assistant.
2. For each such pronoun, locate its antecedent in the provided conversation snippet.
3. Return **only** a numbered list like:
   1. pronoun → antecedent
   2. pronoun → antecedent
4. If no pronouns require resolution, reply exactly: No pronouns to resolve.

Example:
Snippet:
  Alice went home. She was tired.
Query:
  "Can you tell me if she locked the door?"
Reply:
  1. she → Alice
"""

_FINAL_PROMPT = """Now rewrite the user's query using the resolved antecedents.

Rules (Rewrite Phase):
1. Replace each pronoun (excluding those referring to the assistant) with its specific antecedent.
2. Preserve the original meaning and conversational context.
3. **Return exactly** the rewritten query string, with no numbering, quotes, or extra text.

Example:
Original: "Can you tell me if she locked the door?"
Rewritten: "Can you tell me if Alice locked the door?"
"""

CACHE_TTL = getattr(settings, "COREF_CACHE_TTL", 1800)
_CACHE_LOCK = asyncio.Lock()
_COT_TIMEOUT = getattr(settings, "COREF_COT_TIMEOUT", 60.0)
_FINAL_TIMEOUT = getattr(settings, "COREF_FINAL_TIMEOUT", 30.0)

async def resolve_coref(text: str, history: List[Dict[str, str]]) -> str:

    snippet = history[-3:]
    tail = snippet[-1]["content"] if snippet else ""

    digest = hashlib.sha1((text + tail).encode()).hexdigest()[:16]
    cache_key = f"coref:{digest}"
    redis = get_redis()

    if redis:
        async with _CACHE_LOCK:
            try:
                cached = await redis.get(cache_key)
                if cached:
                    val = cached.decode() if isinstance(cached, (bytes, bytearray)) else cached
                    return val
            except Exception:
                logger.debug("Redis GET failed for coref cache", exc_info=True)

    cot_messages = [
        {
            "role": "system",
            "content": _COT_PROMPT
        },
        *snippet,
        {
            "role": "user",
            "content": f"Identify the antecedents for each pronoun in this user query:\n\"{text}\""
        },
    ]
    try:
        start = asyncio.get_event_loop().time()
        cot_resp = await asyncio.wait_for(
            _call_openai_with_retry(
                model=settings.BASE_MODEL,
                messages=cot_messages,
                temperature=0.0,
                top_p=1.0,
                max_tokens=1000,
            ),
            timeout=_COT_TIMEOUT,
        )
        if not cot_resp.choices:
            raise ValueError("empty choices in CoT response")
        reasoning = cot_resp.choices[0].message.content.strip()
        logger.debug("CoT completed in %.2fs", asyncio.get_event_loop().time() - start)
    except asyncio.TimeoutError:
        logger.warning("resolve_coref CoT step timed out after %.1fs", _COT_TIMEOUT)
        reasoning = None
    except Exception as e:
        logger.exception("resolve_coref CoT step failed", exc_info=True)
        reasoning = None

    final_messages = [
        {
            "role": "system",
            "content": _FINAL_PROMPT
        },
        {"role": "assistant", "content": reasoning or "No pronouns to resolve."},
        {
            "role": "user",
            "content": f"Rewrite the user's query with resolved antecedents:\n\"{text}\""
        },
    ]
    try:
        start = asyncio.get_event_loop().time()
        final_resp = await asyncio.wait_for(
            _call_openai_with_retry(
                model=settings.BASE_MODEL,
                messages=final_messages,
                temperature=0.0,
                top_p=1.0,
                max_tokens=1000,
            ),
            timeout=_FINAL_TIMEOUT,
        )
        if not final_resp.choices:
            raise ValueError("empty choices in final response")
        rewritten = final_resp.choices[0].message.content.strip()
        logger.debug("Final rewrite completed in %.2fs", asyncio.get_event_loop().time() - start)

        if rewritten and redis:
            try:
                await redis.set(cache_key, rewritten, ex=CACHE_TTL)
            except Exception:
                logger.debug("Redis SET failed for coref cache %s", cache_key, exc_info=True)
        return rewritten
    except asyncio.TimeoutError:
        logger.warning("resolve_coref final rewrite timed out after %.1fs", _FINAL_TIMEOUT)
    except Exception as e:
        logger.exception("resolve_coref final step failed", exc_info=True)

    return text
EOF