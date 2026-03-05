#app/services/responder/rag/relevance.py
import logging
import re

from typing import List, Tuple, Optional

from app.config import settings
from .keyword_filter import find_tag_hits

logger = logging.getLogger(__name__)

_CLEAN = re.compile(r"[^\w\s]")
_MIN_CONTENT_CHARS = int(getattr(settings, "RELEVANCE_MIN_CONTENT_CHARS", 3))


async def is_relevant(
    text: str,
    *,
    model: str,
    threshold: float,
    return_hits: bool,
    persona_owner_id: Optional[int] = None,
    knowledge_owner_id: Optional[int] = None,
    knowledge_kb_id: Optional[int] = None,
    strict_autoreply_gate: bool = False,
    query_embedding: Optional[List[float]] = None,
    embedding_model: Optional[str] = None,
    query_embedding_reuse_counter: Optional[List[int]] = None,
    apply_mmr: bool = True,
    kb_scope: str = "global",
) -> Tuple[bool, Optional[List[Tuple[float, str, str]]]]:

    _ = persona_owner_id
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

    kb_id_int: Optional[int] = None
    try:
        if knowledge_kb_id is not None:
            kb_id_int = int(knowledge_kb_id)
            if kb_id_int <= 0:
                kb_id_int = None
    except Exception:
        kb_id_int = None

    try:
        keyword_thr_direct = float(getattr(settings, "RELEVANCE_THRESHOLD", 0.28) or 0.28)
    except Exception:
        keyword_thr_direct = 0.28

    keyword_thr = float(threshold) if strict_autoreply_gate else keyword_thr_direct
    keyword_thr = max(0.0, min(1.0, keyword_thr))

    try:
        if query_embedding is not None and query_embedding_reuse_counter is not None:
            query_embedding_reuse_counter[0] += 1
        tag_hits = await find_tag_hits(
            text,
            model=model,
            limit=topk,
            owner_id=owner_id_for_scoped_paths,
            kb_id=kb_id_int,
            query_embedding=query_embedding,
            embedding_model=(embedding_model or model),
            min_similarity=keyword_thr,
            apply_mmr=apply_mmr,
            kb_scope=kb_scope,
        )
    except Exception as exc:
        query_embedding_len = len(query_embedding) if query_embedding is not None and hasattr(query_embedding, "__len__") else None
        logger.warning(
            "gate: keyword pre-check input query_embedding_type=%s query_embedding_len=%s",
            type(query_embedding).__name__,
            query_embedding_len,
        )
        logger.warning(
            "gate: keyword pre-check failed err_type=%s",
            type(exc).__name__,
        )
        tag_hits = []

    if not tag_hits:
        logger.info("gate: no tag-hits -> not relevant")
        return False, None

    ok_tag = True
    if strict_autoreply_gate:
        try:
            tag_top = float(tag_hits[0][0])
        except Exception:
            tag_top = 0.0
        tag_thr_eff = max(0.0, min(1.0, float(threshold)))
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

    if not ok_tag and not return_hits:
        return False, None

    if not return_hits:
        return ok_tag, None

    return ok_tag, tag_hits
