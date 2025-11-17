#app/services/responder/rag/relevance.py
import logging
import re
from typing import List, Tuple, Optional

from app.config import settings
from .knowledge_proc import get_relevant
from .keyword_filter import find_tag_hits

logger = logging.getLogger(__name__)

_CLEAN = re.compile(r"[^\w\s]")
_MIN_CONTENT_CHARS = int(getattr(settings, "RELEVANCE_MIN_CONTENT_CHARS", 5))

async def is_relevant(
    text: str, *, model: str, threshold: float, return_hits: bool
) -> Tuple[bool, Optional[List[Tuple[float, str, str]]]]:

    clean = _CLEAN.sub(" ", text).lower().strip()

    if not clean or len(clean) < _MIN_CONTENT_CHARS:
        logger.info("gate: empty/too-short -> not relevant")
        return False, None

    try:
        topk = int(getattr(settings, "KNOWLEDGE_TOP_K", 3)) or 3
        tag_hits = await find_tag_hits(text, model=model, limit=topk * 10)
    except Exception:
        logger.exception("gate: keyword pre-check failed")
        tag_hits = []

    if tag_hits:
        logger.info("gate: keyword hits -> relevant (skip embeddings); hits=%d", len(tag_hits))
        return True, (tag_hits if return_hits else None)

    try:
        hits = await get_relevant(text, model_name=model)
    except Exception:
        logger.exception("gate: get_relevant failed")
        return False, None

    if not hits:
        logger.info("gate: no-hits -> not relevant")
        return False, None

    try:
        margin = float(getattr(settings, "RELEVANCE_MARGIN", 0.0))
    except Exception:
        margin = 0.0

    top = hits[0][0]
    ok = top >= (threshold + margin)
    logger.info("gate: model=%s ok=%s top=%.3f thr=%.3f hits=%d",
                model, ok, top, threshold, len(hits))
    if not ok:
        return False, (hits if return_hits else None)
    return True, (hits if return_hits else None)