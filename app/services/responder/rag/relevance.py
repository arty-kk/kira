cat >app/services/responder/rag/relevance.py<< 'EOF'
#app/services/responder/rag/relevance.py
import logging
import hashlib
import asyncio
import re

from typing import List, Tuple

from .keyword_filter import get_keyword_processor
from .knowledge_proc import get_relevant
from app.config import settings
from app.clients.openai_client import _call_openai_with_retry
from app.core.memory import get_redis

logger = logging.getLogger(__name__)

_RELEVANCE_CACHE_EX = 3600

async def relevant_enough(
    text: str, 
    model: str, 
    threshold: float, 
    hits: List[Tuple[float, str, str]] | None = None
) -> bool:

    text_clean = re.sub(r"[^\w\s]", " ", text).lower()
    kw_proc = get_keyword_processor()
    kws = [kw for kw in kw_proc.extract_keywords(text_clean) if len(kw) >= 4]
    if len(kws) >= 2:
        logger.debug("relevance: %d keywords %r → force on-topic", len(kws), kws)
        return True
    if len(kws) == 1:
        logger.debug("relevance: single keyword %r → defer to embeddings", kws)

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

    if not hits:
        logger.debug("relevance: no hits → off-topic")
        try:
            await redis.set(cache_key, "0", ex=_RELEVANCE_CACHE_EX)
        except Exception as e:
            logger.debug("Redis set failed: %s", e)
        return False

    top_score = hits[0][0] if hits else 0.0

    margin = settings.RELEVANCE_MARGIN
    logger.debug("relevance: top_score=%.4f, threshold=%.4f, margin=%.4f",
                 top_score, threshold, margin)
                 
    if top_score >= threshold + margin:
        result = True
    elif top_score < threshold - margin:
        result = False
    else:
        safe_text = text.replace("'", "\\'")
        prompt = (
            "You are a strict classifier. Answer only 'Yes' or 'No'.\n"
            f"Your interlocutor's query: '{safe_text}'\n"
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
                timeout=10.0,
            )
            verdict = resp.choices[0].message.content.strip().lower()
            result = verdict.startswith("yes")
        except Exception:
            logger.exception("GPT fallback failed in relevant_enough")
            result = top_score >= threshold
    
    try:
        await redis.set(cache_key, "1" if result else "0", ex=_RELEVANCE_CACHE_EX)
    except Exception as e:
        logger.debug("Redis set failed: %s", e)
    logger.info("relevant_enough: top_score=%.4f threshold=%.4f → %s", 
                top_score, threshold, result)
    return result
EOF