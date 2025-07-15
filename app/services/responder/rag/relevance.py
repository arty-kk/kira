cat >app/services/responder/rag/relevance.py<< EOF
#app/services/responder/rag/relevance.py
import logging
import hashlib
import asyncio
import re

from app.config import settings
from typing import List, Tuple
from .keyword_filter import get_keyword_processor
from .knowledge_proc import get_relevant
from app.clients.openai_client import _call_openai_with_retry
from app.core.memory import get_redis

logger = logging.getLogger(__name__)

_RELEVANCE_CACHE_EX = 3600

async def relevant_enough(text: str, model: str, threshold: float, hits: List[Tuple[float, str, str]] | None = None) -> bool:

    text_clean = re.sub(r"[^\w\s]", " ", text).lower()
    kw_proc = get_keyword_processor()
    kws = kw_proc.extract_keywords(text_clean)
    if kws:
        logger.debug("relevance: matched keywords → on-topic")
        return True

    cache_key = "relcache:" + hashlib.sha256(f"{model}:{text}".encode()).hexdigest()
    try:
        redis = get_redis()
        cached = await redis.get(cache_key)
    except Exception:
        cached = None
    if cached is not None:
        if isinstance(cached, (bytes, bytearray)):
            cached = cached.decode()
        return cached.strip() == "1"

    if hits is None:
        try:
            hits = await get_relevant(text, model_name=model)
        except Exception:
            logger.exception("get_relevant failed in relevant_enough")
            hits = []
    top_score = hits[0][0] if hits else 0.0

    margin = settings.RELEVANCE_MARGIN
    if top_score >= threshold + margin:
        result = True
    elif top_score < threshold - margin:
        result = False
    else:
        prompt = (
            "You are a strict classifier. Answer only 'Yes' or 'No'.\n"
            f"Your interlocutor's query: '{text}'\n"
            "Does this query relate to any content in the knowledge base?"
        )
        try:
            resp = await asyncio.wait_for(
                _call_openai_with_retry(
                    model=settings.BASE_MODEL,
                    temperature=0.0,
                    messages=[
                        {"role": "system", "content": "Answer ONLY Yes or No."},
                        {"role": "user", "content": prompt},
                    ],
                ),
                timeout=30.0,
            )
            verdict = resp.choices[0].message.content.strip().lower()
            result = verdict.startswith("yes")
        except Exception:
            logger.exception("GPT fallback failed in relevant_enough")
            result = top_score >= threshold
    
    try:
        await redis.set(cache_key, "1" if result else "0", ex=_RELEVANCE_CACHE_EX)
    except:
        pass
    logger.info("relevant_enough: top_score=%.4f threshold=%.4f → %s", top_score, threshold, result)
    return result
EOF