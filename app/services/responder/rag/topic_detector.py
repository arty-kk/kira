cat >app/services/responder/rag/topic_detector.py<< EOF
#app/services/responder/rag/topic_detector.py

import logging
import hashlib
import asyncio
import re

from app.config import settings
from app.core.memory import get_redis
from app.clients.openai_client import _call_openai_with_retry
from .knowledge_proc import get_relevant
from .keyword_filter import get_keyword_processor

logger = logging.getLogger(__name__)

_ON_TOPIC_CACHE_EX = 3600

async def is_on_topic(text: str) -> bool:

    text_clean = re.sub(r"[^\w\s]", " ", text).lower()
    kw_proc = get_keyword_processor()
    kws = kw_proc.extract_keywords(text_clean)
    if kws:
        logger.debug("is_on_topic: matched keywords %r → on-topic", kws)
        return True

    key = "on_topic_cache:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    redis = get_redis()
    cached = await redis.get(key)
    if cached is not None:
        if isinstance(cached, (bytes, bytearray)):
            cached = cached.decode()
        return cached.strip() == "1"

    try:
        hits = await get_relevant(text, model_name=settings.EMBEDDING_MODEL)
        top_score = hits[0][0] if hits else 0.0
        logger.debug(
            "is_on_topic: embedding scores top=%0.4f (threshold=%0.4f)",
            top_score,
            settings.RELEVANCE_THRESHOLD,
        )
    except Exception:
        logger.exception("get_relevant failed in is_on_topic")
        await redis.set(key, "0", ex=_ON_TOPIC_CACHE_EX)
        return False

    thr = settings.RELEVANCE_THRESHOLD
    if top_score >= thr + 0.05:
        result = True
    elif top_score < thr - 0.05:
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
            logger.exception("GPT fallback failed in is_on_topic")
            result = top_score >= thr

    await redis.set(key, "1" if result else "0", ex=_ON_TOPIC_CACHE_EX)
    logger.info(
        "is_on_topic: text=%r → on_topic=%s (score=%.4f)", text, result, top_score
    )
    return result
EOF