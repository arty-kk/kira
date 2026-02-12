#app/services/responder/rag/relevance.py
import logging
import re

from typing import List, Tuple, Optional

from app.config import settings
from .knowledge_proc import get_relevant
from .keyword_filter import find_tag_hits
from .api_kb_proc import get_relevant_for_owner

logger = logging.getLogger(__name__)

_CLEAN = re.compile(r"[^\w\s]")
_MIN_CONTENT_CHARS = int(getattr(settings, "RELEVANCE_MIN_CONTENT_CHARS", 5))


async def is_relevant(
    text: str,
    *,
    model: str,
    threshold: float,
    return_hits: bool,
    persona_owner_id: Optional[int] = None,
    knowledge_owner_id: Optional[int] = None,
) -> Tuple[bool, Optional[List[Tuple[float, str, str]]]]:

    clean = _CLEAN.sub(" ", text).lower().strip()

    if not clean or len(clean) < _MIN_CONTENT_CHARS:
        logger.info("gate: empty/too-short -> not relevant")
        return False, None

    try:
        topk = int(getattr(settings, "KNOWLEDGE_TOP_K", 3)) or 3
    except Exception:
        topk = 3

    try:
        owner_id_int = int(knowledge_owner_id) if knowledge_owner_id is not None else 0
    except (TypeError, ValueError):
        owner_id_int = 0

    owner_id_for_scoped_paths = owner_id_int if owner_id_int > 0 else None

    try:
        tag_hits = await find_tag_hits(
            text,
            model=model,
            limit=topk * 10,
            owner_id=owner_id_for_scoped_paths,
        )
    except Exception:
        logger.exception("gate: keyword pre-check failed")
        tag_hits = []

    sys_hits: List[Tuple[float, str, str]] = []
    ok_sys = False

    try:
        margin = float(getattr(settings, "RELEVANCE_MARGIN", 0.0))
    except Exception:
        margin = 0.0

    if tag_hits:
        logger.info(
            "gate: keyword hits -> relevant (skip sys embeddings); hits=%d",
            len(tag_hits),
        )
        sys_hits = tag_hits
        ok_sys = True
    else:
        try:
            sys_hits = await get_relevant(text, model_name=model)
        except Exception:
            logger.exception("gate: get_relevant (system KB) failed")
            sys_hits = []

        if sys_hits:
            top = sys_hits[0][0]
            thr_eff = threshold + margin
            ok_sys = top >= thr_eff
            logger.info(
                "gate: system KB model=%s ok=%s top=%.3f thr=%.3f hits=%d",
                model,
                ok_sys,
                top,
                thr_eff,
                len(sys_hits),
            )

    custom_hits: List[Tuple[float, str, str]] = []
    ok_custom = False

    if owner_id_for_scoped_paths is not None:
        try:
            custom_hits = await get_relevant_for_owner(
                text,
                owner_id=owner_id_for_scoped_paths,
                model_name=model,
            )
        except Exception:
            logger.exception(
                "gate: get_relevant_for_owner failed for owner=%s",
                owner_id_for_scoped_paths,
            )
            custom_hits = []

        if custom_hits:
            top_c = custom_hits[0][0]
            thr_eff = threshold + margin
            ok_custom = top_c >= thr_eff
            logger.info(
                "gate: custom KB owner=%s model=%s ok=%s top=%.3f thr=%.3f hits=%d",
                owner_id_for_scoped_paths,
                model,
                ok_custom,
                top_c,
                thr_eff,
                len(custom_hits),
            )
    else:
        logger.info(
            "gate: skip owner KB path due to missing/invalid knowledge_owner_id=%r",
            knowledge_owner_id,
        )

    has_any_hits = bool(sys_hits or custom_hits)
    ok_any = bool(ok_sys or ok_custom)

    if not has_any_hits:
        logger.info("gate: no-hits (system + custom) -> not relevant")
        return False, None

    if not ok_any and not return_hits:
        return False, None

    if not return_hits:
        return ok_any, None

    combined: List[Tuple[float, str, str]] = list(sys_hits) + list(custom_hits)
    combined.sort(key=lambda h: h[0], reverse=True)

    return ok_any, combined
