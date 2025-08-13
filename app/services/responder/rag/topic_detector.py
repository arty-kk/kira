cat >app/services/responder/rag/topic_detector.py<< 'EOF'
#app/services/responder/rag/topic_detector.py
import logging
import hashlib
import asyncio
import re

from typing import List, Optional, Tuple
from app.config import settings
from app.core.memory import get_redis
from app.clients.openai_client import _call_openai_with_retry
from .knowledge_proc import get_relevant
from .keyword_filter import get_keyword_processor

logger = logging.getLogger(__name__)

_CLEAN_RE = re.compile(r"[^\w\s]")
_KW_PROC = get_keyword_processor()
_ON_TOPIC_CACHE_EX = 1800

async def is_on_topic(text: str) -> Tuple[bool, Optional[List[Tuple[float, str, str]]]]:

    text_clean = _CLEAN_RE.sub(" ", text).lower()
    kws = _KW_PROC.extract_keywords(text_clean)
    if len(kws) >= 2:
        logger.debug("is_on_topic: matched >=2 keywords %r → on-topic", kws)
        return True, None
    elif len(kws) == 1:
        logger.debug("is_on_topic: only 1 keyword %r, falling back to embedding", kws)
    else:
        logger.debug("is_on_topic: no keywords, using embedding logic")

    key = "on_topic_cache:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    try:
        redis = get_redis()
        cached = await redis.get(key)
    except Exception:
        cached = None
    if cached is not None:
        if isinstance(cached, (bytes, bytearray)):
            cached = cached.decode()
        return (cached.strip() == "1"), None

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
        try:
            await redis.set(key, "0", ex=_ON_TOPIC_CACHE_EX)
        except:
            pass
        return False, None

    margin = settings.RELEVANCE_MARGIN
    thr = settings.RELEVANCE_THRESHOLD
    if top_score >= thr + margin:
        result = True
    elif top_score < thr - margin:
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
            logger.exception("GPT fallback failed in is_on_topic")
            result = top_score >= settings.RELEVANCE_THRESHOLD

    try:
        await redis.set(key, "1" if result else "0", ex=_ON_TOPIC_CACHE_EX)
    except:
        pass
    logger.info("is_on_topic: text=%r → on_topic=%s (score=%.4f)", text, result, top_score)
    return result, hits
EOF