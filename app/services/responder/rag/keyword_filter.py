import logging
from typing import Dict, List, Optional, Tuple

import numpy as np
from sqlalchemy import select

from app.config import settings
from app.core.db import session_scope
from app.core.models import RagTagVector
from .knowledge_proc import _get_query_embedding

logger = logging.getLogger(__name__)

_INDICES = {}
_EMB_CACHE = {}
MMR_CANDIDATES_TOP_N = 30


def _norm_ws(s: str) -> str:
    return " ".join((s or "").strip().casefold().split())


def _l2_normalize(vec: List[float]) -> List[float]:
    arr = np.asarray(vec, dtype=np.float32)
    n = float(np.linalg.norm(arr))
    if not np.isfinite(n) or n <= 0.0:
        return [float(x) for x in arr]
    return [float(x / n) for x in arr]


def _mmr_select_ids(cand_ids: List[str], vecs_by_id: Dict[str, List[float]], scores_by_id: Dict[str, float], top_k: int, lam: float) -> List[str]:
    if top_k <= 0 or not cand_ids:
        return []
    selected: List[str] = []
    remaining = set(cand_ids)
    first = max(remaining, key=lambda i: scores_by_id.get(i, 0.0))
    selected.append(first)
    remaining.remove(first)
    while len(selected) < top_k and remaining:
        best_id = None
        best_score = -1e9
        for rid in list(remaining):
            v_r = vecs_by_id.get(rid)
            if not v_r:
                continue
            max_sim = 0.0
            for sid in selected:
                v_s = vecs_by_id.get(sid)
                if v_s:
                    max_sim = max(max_sim, float(np.dot(np.asarray(v_r, dtype=np.float32), np.asarray(v_s, dtype=np.float32))))
            mmr = lam * scores_by_id.get(rid, 0.0) - (1.0 - lam) * max_sim
            if mmr > best_score:
                best_score = mmr
                best_id = rid
        if best_id is None:
            break
        selected.append(best_id)
        remaining.remove(best_id)
    return selected


async def _ensure_index(model=None):
    key = f"sys::{model or settings.EMBEDDING_MODEL}"
    return _INDICES.get(key, {"ready": True, "E": np.zeros((0, 0), dtype=np.float32), "row_to_eid": [], "row_to_tag": [], "row_to_text": [], "model": model or settings.EMBEDDING_MODEL})


def invalidate_tags_index(owner_id: Optional[int] = None) -> None:
    _ = owner_id


async def find_tag_hits(text: str, *, model: Optional[str] = None, limit: Optional[int] = None, owner_id: Optional[int] = None, query_embedding: Optional[List[float]] = None, embedding_model: Optional[str] = None) -> List[Tuple[float, str, str]]:
    t = _norm_ws(text)
    if not t:
        return []
    emb_model = model or settings.EMBEDDING_MODEL

    qv: List[float]
    if query_embedding is not None:
        qv = _l2_normalize([float(x) for x in query_embedding])
    else:
        qraw = await _get_query_embedding(embedding_model or emb_model, t)
        if qraw is None:
            return []
        qv = _l2_normalize([float(x) for x in qraw])

    async with session_scope(read_only=True) as db:
        conditions = [RagTagVector.embedding_model == emb_model]
        if owner_id:
            conditions.append((RagTagVector.scope == "global") | ((RagTagVector.scope == "owner") & (RagTagVector.owner_id == int(owner_id))))
        else:
            conditions.append(RagTagVector.scope == "global")
        rows = await db.execute(select(RagTagVector.scope, RagTagVector.owner_id, RagTagVector.external_id, RagTagVector.text, RagTagVector.embedding).where(*conditions))
        payload = rows.all()

    scores_by_id: Dict[str, float] = {}
    vec_by_id: Dict[str, List[float]] = {}
    text_by_id: Dict[str, str] = {}
    thr = float(getattr(settings, "KEYWORD_RELEVANCE_THRESHOLD", None) or (float(getattr(settings, "RELEVANCE_THRESHOLD", 0.28) or 0.28) + float(getattr(settings, "RELEVANCE_MARGIN", 0.07) or 0.07)))

    qv_arr = np.asarray(qv, dtype=np.float32)
    expected_dim = int(getattr(settings, "RAG_VECTOR_DIM", 3072) or 3072)
    if int(qv_arr.shape[0]) != expected_dim:
        return []
    for scope, oid, ext_id, txt, emb in payload:
        vec = np.asarray(emb or [], dtype=np.float32)
        if vec.ndim != 1 or int(vec.shape[0]) != expected_dim:
            continue
        rid = f"{oid}:{ext_id}" if scope == "owner" and oid is not None else str(ext_id)
        score = float(np.dot(vec, qv_arr) / (np.linalg.norm(vec) or 1.0))
        if score < thr:
            continue
        prev = scores_by_id.get(rid)
        if prev is None or score > prev:
            scores_by_id[rid] = score
            vec_by_id[rid] = [float(x) for x in vec]
            text_by_id[rid] = str(txt or "")

    if not scores_by_id:
        logger.info("keyword_filter: no DB tag rows found for model=%s owner_id=%s", emb_model, owner_id)
        return []

    top_k = int(limit) if isinstance(limit, int) and limit > 0 else int(getattr(settings, "KNOWLEDGE_TOP_K", 3) or 3)
    lam = max(0.0, min(1.0, float(getattr(settings, "MMR_LAMBDA", 0.5) or 0.5)))
    cand_ids = sorted(scores_by_id.keys(), key=lambda i: scores_by_id[i], reverse=True)[:MMR_CANDIDATES_TOP_N]
    picked = _mmr_select_ids(cand_ids, vec_by_id, scores_by_id, top_k=top_k, lam=lam)
    return [(scores_by_id[i], i, text_by_id.get(i, "")) for i in picked]
