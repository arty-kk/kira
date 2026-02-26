# app/services/responder/rag/keyword_filter.py
import logging
import time
from typing import Dict, List, Optional, Tuple

import numpy as np
from pgvector.sqlalchemy import Vector
from sqlalchemy import bindparam, select

from app.config import settings
from app.core.db import session_scope
from app.core.models import RagTagVector
from .knowledge_proc import _get_query_embedding
from .query_embedding import normalize_query_embedding

logger = logging.getLogger(__name__)

_INDICES = {}
_EMB_CACHE = {}

MMR_CANDIDATES_TOP_N = 30


def _norm_ws(s: str) -> str:
    return " ".join((s or "").strip().casefold().split())


def _l2_normalize(vec: List[float]) -> List[float]:
    arr = np.asarray(vec, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(arr))
    if not np.isfinite(n) or n <= 0.0:
        return [float(x) for x in arr.tolist()]
    return [float(x / n) for x in arr.tolist()]


def _mmr_select_ids(
    cand_ids: List[str],
    vecs_by_id: Dict[str, List[float]],
    scores_by_id: Dict[str, float],
    top_k: int,
    lam: float,
) -> List[str]:
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
            v_r_np = np.asarray(v_r, dtype=np.float32)

            for sid in selected:
                v_s = vecs_by_id.get(sid)
                if v_s:
                    v_s_np = np.asarray(v_s, dtype=np.float32)
                    max_sim = max(max_sim, float(np.dot(v_r_np, v_s_np)))

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
    return _INDICES.get(
        key,
        {
            "ready": True,
            "E": np.zeros((0, 0), dtype=np.float32),
            "row_to_eid": [],
            "row_to_tag": [],
            "row_to_text": [],
            "model": model or settings.EMBEDDING_MODEL,
        },
    )


def invalidate_tags_index(owner_id: Optional[int] = None) -> None:
    _ = owner_id


def _build_vector_distance_expr(*, dim: int):
    """
    IMPORTANT:
    - RagTagVector.embedding is Vector(3072) in your model.
    - Do NOT use HALFVEC here; it triggers pgvector/sqlalchemy halfvec bind processor and breaks.
    """
    query_vec_param = bindparam("query_vec", type_=Vector(dim))
    distance_expr = RagTagVector.embedding.op("<=>")(query_vec_param).label("distance")
    return query_vec_param, distance_expr


async def find_tag_hits(
    text: str,
    *,
    model: Optional[str] = None,
    limit: Optional[int] = None,
    owner_id: Optional[int] = None,
    query_embedding: Optional[List[float]] = None,
    embedding_model: Optional[str] = None,
) -> List[Tuple[float, str, str]]:
    t = _norm_ws(text)
    if not t:
        return []

    emb_model = model or settings.EMBEDDING_MODEL
    expected_dim = int(getattr(RagTagVector.embedding.type, "dim", 3072) or 3072)

    # 1) get query embedding
    if query_embedding is not None:
        qv_src = normalize_query_embedding(query_embedding, expected_dim=expected_dim)
        if qv_src is None:
            logger.info(
                "keyword_filter: invalid query_embedding conversion reason=bad-shape-or-values type=%s",
                type(query_embedding).__name__,
            )
            return []
        qv = _l2_normalize(qv_src)
    else:
        qraw = await _get_query_embedding(embedding_model or emb_model, t)
        if qraw is None:
            return []
        qv_src = normalize_query_embedding(qraw, expected_dim=expected_dim)
        if qv_src is None:
            logger.info(
                "keyword_filter: invalid query embedding from provider reason=bad-shape-or-values shape=%s expected_dim=%s",
                getattr(qraw, "shape", None),
                expected_dim,
            )
            return []
        qv = _l2_normalize(qv_src)

    # 2) hard-shape + finite
    qv_sql = np.asarray(qv, dtype=np.float32).reshape(-1)
    current_len = int(qv_sql.shape[0])
    if current_len != expected_dim:
        logger.warning(
            "keyword_filter: invalid query embedding for SQL reason=bad-len shape=%s dtype=%s expected_dim=%s",
            qv_sql.shape,
            qv_sql.dtype,
            expected_dim,
        )
        return []
    if not np.isfinite(qv_sql).all():
        logger.info(
            "keyword_filter: invalid query embedding reason=non-finite-values shape=%s len=%s expected_dim=%s",
            qv_sql.shape,
            current_len,
            expected_dim,
        )
        return []

    # Vector(dim) bind expects list[float]
    query_vec_sql_param: List[float] = [float(x) for x in qv_sql.tolist()]

    # 3) thresholds
    thr = float(
        getattr(settings, "KEYWORD_RELEVANCE_THRESHOLD", None)
        or (
            float(getattr(settings, "RELEVANCE_THRESHOLD", 0.28) or 0.28)
            + float(getattr(settings, "RELEVANCE_MARGIN", 0.07) or 0.07)
        )
    )
    max_distance = 1.0 - thr

    top_k = (
        int(limit)
        if isinstance(limit, int) and limit > 0
        else int(getattr(settings, "KNOWLEDGE_TOP_K", 3) or 3)
    )
    lam = max(0.0, min(1.0, float(getattr(settings, "MMR_LAMBDA", 0.5) or 0.5)))
    candidate_limit = max(top_k, MMR_CANDIDATES_TOP_N)

    # 4) SQL
    _, distance_expr = _build_vector_distance_expr(dim=expected_dim)

    async with session_scope(read_only=True) as db:
        conditions = [RagTagVector.embedding_model == emb_model]

        if owner_id:
            conditions.append(
                (RagTagVector.scope == "global")
                | ((RagTagVector.scope == "owner") & (RagTagVector.owner_id == int(owner_id)))
            )
        else:
            conditions.append(RagTagVector.scope == "global")

        sql_started = time.perf_counter()
        query_vec_source = "external query_embedding" if query_embedding is not None else "provider"

        logger.debug(
            "keyword_filter: executing SQL with vec_type=%s vec_len=%s expected_dim=%s source=%s",
            type(query_vec_sql_param).__name__,
            len(query_vec_sql_param),
            expected_dim,
            query_vec_source,
        )

        stmt = (
            select(
                RagTagVector.scope,
                RagTagVector.owner_id,
                RagTagVector.external_id,
                RagTagVector.text,
                RagTagVector.embedding,
                distance_expr,
            )
            .where(*conditions)
            .where(distance_expr <= max_distance)
            .order_by(distance_expr.asc())
            .limit(candidate_limit)
        )

        rows = await db.execute(stmt, {"query_vec": query_vec_sql_param})
        payload = rows.all()

        sql_duration_ms = (time.perf_counter() - sql_started) * 1000.0

    logger.info(
        "keyword_filter: sql stage complete duration_ms=%.2f candidate_size=%s model=%s owner_id=%s",
        sql_duration_ms,
        len(payload),
        emb_model,
        owner_id,
    )

    # 5) build candidates + MMR
    scores_by_id: Dict[str, float] = {}
    vec_by_id: Dict[str, List[float]] = {}
    text_by_id: Dict[str, str] = {}

    for scope, oid, ext_id, txt, emb, distance in payload:
        score_f = 1.0 - float(distance)

        vec = np.asarray(emb if emb is not None else [], dtype=np.float32).reshape(-1)
        if int(vec.shape[0]) != expected_dim:
            continue

        rid = f"{oid}:{ext_id}" if scope == "owner" and oid is not None else str(ext_id)
        prev = scores_by_id.get(rid)
        if prev is None or score_f > prev:
            scores_by_id[rid] = score_f
            vec_by_id[rid] = [float(x) for x in vec.tolist()]
            text_by_id[rid] = str(txt or "")

    if not scores_by_id:
        logger.info(
            "keyword_filter: empty-hit no-scored-candidates model=%s owner_id=%s sql_candidate_size=%s",
            emb_model,
            owner_id,
            len(payload),
        )
        return []

    cand_ids = sorted(scores_by_id.keys(), key=lambda i: scores_by_id[i], reverse=True)[
        :MMR_CANDIDATES_TOP_N
    ]
    picked = _mmr_select_ids(cand_ids, vec_by_id, scores_by_id, top_k=top_k, lam=lam)
    return [(scores_by_id[i], i, text_by_id.get(i, "")) for i in picked]
