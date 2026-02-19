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
    strict_autoreply_gate: bool = False,
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

    tag_top = 0.0
    ok_tag = False
    if tag_hits:
        sys_hits = tag_hits
        if strict_autoreply_gate:
            try:
                tag_top = float(tag_hits[0][0])
            except Exception:
                tag_top = 0.0
            try:
                tag_delta = float(getattr(settings, "KEYWORD_RELEVANCE_CONFIRM_DELTA", 0.05) or 0.0)
            except Exception:
                tag_delta = 0.05
            tag_thr_eff = threshold + margin + max(0.0, tag_delta)
            ok_tag = tag_top >= tag_thr_eff
            logger.info(
                "gate: keyword hits=%d top=%.3f thr=%.3f ok=%s (strict autoreply gate)",
                len(tag_hits),
                tag_top,
                tag_thr_eff,
                ok_tag,
            )
        else:
            logger.info(
                "gate: keyword hits -> relevant (non-strict path); hits=%d",
                len(tag_hits),
            )
            ok_tag = True

    emb_hits: List[Tuple[float, str, str]] = []
    if strict_autoreply_gate or not tag_hits:
        try:
            emb_hits = await get_relevant(text, model_name=model)
        except Exception:
            logger.exception("gate: get_relevant (system KB) failed")
            emb_hits = []

        if emb_hits:
            top = emb_hits[0][0]
            thr_eff = threshold + margin
            ok_sys = top >= thr_eff
            logger.info(
                "gate: system KB model=%s ok=%s top=%.3f thr=%.3f hits=%d",
                model,
                ok_sys,
                top,
                thr_eff,
                len(emb_hits),
            )
            if sys_hits:
                merged = list(sys_hits)
                known = {h[1] for h in merged if isinstance(h, (list, tuple)) and len(h) > 1}
                for h in emb_hits:
                    hid = h[1] if isinstance(h, (list, tuple)) and len(h) > 1 else None
                    if hid and hid in known:
                        continue
                    merged.append(h)
                merged.sort(key=lambda h: h[0], reverse=True)
                sys_hits = merged
            else:
                sys_hits = emb_hits

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
    ok_any = bool(ok_tag or ok_sys or ok_custom)

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
