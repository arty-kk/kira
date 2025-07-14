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

CACHE_TTL = getattr(settings, "COREF_CACHE_TTL", 1800)

async def resolve_coref(text: str, history: List[Dict[str, str]]) -> str:

    snippet = history[-3:]
    tail = snippet[-1]["content"] if snippet else ""

    digest = hashlib.sha1((text + tail).encode()).hexdigest()[:16]
    cache_key = f"coref:{digest}"
    redis = get_redis()

    try:
        cached = await redis.get(cache_key) if redis else None
        if isinstance(cached, (bytes, bytearray)):
            return cached.decode()
        if cached:
            return cached
    except Exception:
        logger.debug("Redis GET failed for coref cache", exc_info=True)

    cot_messages = [
        {
            "role": "system",
            "content": (
                "You are a coreference-resolution assistant.\n\n"
                "Instructions (CoT Phase):\n"
                "1. Identify all pronouns in the user's new query that refer to entities other than the assistant.\n"
                "2. For each pronoun, locate its antecedent in the provided conversation snippet.\n"
                "3. Return a numbered list in the format: Pronoun → Antecedent.\n"
                "4. If no pronouns require resolution, reply only: 'No pronouns to resolve.'"
            )
        },
        *snippet,
        {
            "role": "user",
            "content": f"Identify the antecedents for each pronoun in this user query:\n\"{text}\""
        },
    ]
    try:
        cot_resp = await asyncio.wait_for(
            _call_openai_with_retry(
                model=settings.REASONING_MODEL,
                messages=cot_messages,
                temperature=0.0,
                top_p=1.0,
                max_tokens=1000,
            ),
            timeout=60.0,
        )
        reasoning = cot_resp.choices[0].message.content.strip()
    except asyncio.TimeoutError:
        logger.warning("resolve_coref CoT step timed out")
        reasoning = None
    except Exception as e:
        logger.exception("resolve_coref CoT step failed", exc_info=True)
        reasoning = None

    final_messages = [
        {
            "role": "system",
            "content": (
                "Now rewrite the user's query using the resolved antecedents.\n\n"
                "Rules (Rewrite Phase):\n"
                "1) Replace each pronoun (other than those addressing the assistant) with its specific antecedent.\n"
                "2) Preserve the original meaning and conversational context.\n"
                "3) Output only the fully rewritten user query, without any additional commentary."
            )
        },
        {"role": "assistant", "content": reasoning or "No pronouns to resolve."},
        {
            "role": "user",
            "content": f"Rewrite the user's query with resolved antecedents:\n\"{text}\""
        },
    ]
    try:
        final_resp = await asyncio.wait_for(
            _call_openai_with_retry(
                model=settings.REASONING_MODEL,
                messages=final_messages,
                temperature=0.0,
                top_p=1.0,
                max_tokens=1000,
            ),
            timeout=30.0,
        )
        rewritten = final_resp.choices[0].message.content.strip()
        if rewritten:
            try:
                await redis.set(cache_key, rewritten, ex=1800)
            except Exception:
                logger.debug("Redis SET failed for coref cache %s", cache_key, exc_info=True)
            return rewritten
    except asyncio.TimeoutError:
        logger.warning("resolve_coref final rewrite timed out")
    except Exception as e:
        logger.exception("resolve_coref final step failed", exc_info=True)

    return text
EOF