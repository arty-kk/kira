# app/services/responder/rag/keyword_filter.py
import logging
import time
from typing import Dict, List, Optional, Tuple

import numpy as np
from pgvector.psycopg import HalfVector, Vector as PgVector
from sqlalchemy import bindparam, select, func, case
from sqlalchemy.sql import over

from app.config import settings
from app.core.embedding_utils import get_rag_embedding_model, resolve_embedding_dim
from app.core.db import session_scope
from app.core.models import RagTagVector
from app.core.vector_adapter import adapt_vector_for_storage, normalize_vector_for_pg
from .knowledge_proc import _get_query_embedding
from .query_embedding import normalize_query_embedding

logger = logging.getLogger(__name__)

MMR_CANDIDATES_TOP_N = 30


def _embedding_param(vec: List[float], *, expected_dim: int, model: str | None = None) -> object:
    return adapt_vector_for_storage(vec, expected_dim=expected_dim, model=model, l2_normalize=True)


def _normalize_embedding(raw: object) -> List[float]:
    if raw is None:
        return []
    if isinstance(raw, (HalfVector, PgVector)):
        return [float(x) for x in raw.to_list()]
    if hasattr(raw, "tolist"):
        return [float(x) for x in raw.tolist()]
    if isinstance(raw, (list, tuple)):
        return [float(x) for x in raw]
    try:
        return [float(x) for x in list(raw)]
    except TypeError:
        return [float(raw)]


def _norm_ws(s: str) -> str:
    return " ".join((s or "").strip().casefold().split())


def _l2_normalize(vec: List[float]) -> List[float]:
    arr = np.asarray(vec, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(arr))
    if not np.isfinite(n) or n <= 0.0:
        return [float(x) for x in arr.tolist()]
    return [float(x / n) for x in arr.tolist()]


def _l2_normalize_np(arr: np.ndarray) -> np.ndarray:
    arr = np.asarray(arr, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(arr))
    if not np.isfinite(n) or n <= 0.0:
        return arr
    return arr / n


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
    # Keep candidate order deterministic (score-sorted upstream).
    remaining = list(dict.fromkeys(cand_ids))

    first = max(remaining, key=lambda i: scores_by_id.get(i, 0.0))
    selected.append(first)
    remaining.remove(first)

    while len(selected) < top_k and remaining:
        best_id = None
        best_score = -1e9
        for rid in remaining:
            v_r = vecs_by_id.get(rid)
            if not v_r:
                continue
            # Cosine similarity is in [-1, 1]. Start from -1.0 to keep
            # the true maximum even when all pairwise sims are negative.
            max_sim = -1.0
            v_r_np = np.asarray(v_r, dtype=np.float32).reshape(-1)

            for sid in selected:
                v_s = vecs_by_id.get(sid)
                if v_s:
                    v_s_np = np.asarray(v_s, dtype=np.float32).reshape(-1)
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


def _sort_ids_by_similarity(scores_by_id: Dict[str, float]) -> List[str]:
    return sorted(scores_by_id.keys(), key=lambda i: scores_by_id[i], reverse=True)


def invalidate_tags_index(owner_id: Optional[int] = None) -> None:
    _ = owner_id


def _build_vector_distance_expr(*, dim: int):
    q = bindparam("query_vec", type_=RagTagVector.embedding.type)
    dim_ok = func.vector_dims(RagTagVector.embedding) == dim
    distance_raw = case(
        (dim_ok, RagTagVector.embedding.op("<=>")(q)),
        else_=None,
    )
    distance_sel = distance_raw.label("distance")
    return distance_raw, distance_sel


async def find_tag_hits(
    text: str,
    *,
    model: Optional[str] = None,
    limit: Optional[int] = None,
    owner_id: Optional[int] = None,
    kb_id: Optional[int] = None,
    query_embedding: Optional[List[float]] = None,
    embedding_model: Optional[str] = None,
    min_similarity: Optional[float] = None,
    apply_mmr: bool = True,
    kb_scope: str = "global",
) -> List[Tuple[float, str, str]]:
    t = _norm_ws(text)
    if not t:
        return []

    emb_model = get_rag_embedding_model(embedding_model or model)
    expected_dim = resolve_embedding_dim(
        emb_model,
        fallback_dim=int(getattr(settings, "RAG_VECTOR_DIM", 3072) or 3072),
    )
    if expected_dim != 3072:
        logger.warning(
            "keyword_filter: expected_dim=%s differs from halfvec(3072) storage contract model=%s",
            expected_dim,
            emb_model,
        )

    # 1) get query embedding
    if query_embedding is not None:
        qv_src = normalize_query_embedding(query_embedding, expected_dim=expected_dim)
        if qv_src is None:
            logger.info(
                "keyword_filter: invalid query_embedding conversion reason=bad-shape-or-values type=%s",
                type(query_embedding).__name__,
            )
            return []
    else:
        qraw = await _get_query_embedding(emb_model, t)
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

    # 2) hard-shape + finite + centralized normalization for SQL bind
    try:
        query_vec_sql = normalize_vector_for_pg(
            qv_src,
            expected_dim=expected_dim,
            model=emb_model,
            l2_normalize=True,
        )
    except ValueError:
        logger.warning(
            "keyword_filter: invalid query embedding for SQL input_type=%s model=%s owner_id=%s kb_id=%s expected_dim=%s",
            type(qv_src).__name__,
            emb_model,
            owner_id,
            kb_id,
            expected_dim,
        )
        return []

    # 3) thresholds
    if min_similarity is not None:
        thr = float(min_similarity)
    else:
        thr = float(getattr(settings, "RELEVANCE_THRESHOLD", 0.28) or 0.28)
    thr = max(0.0, min(1.0, thr))
    max_distance = 1.0 - thr

    top_k = (
        int(limit)
        if isinstance(limit, int) and limit > 0
        else int(getattr(settings, "KNOWLEDGE_TOP_K", 3) or 3)
    )
    lam = max(0.0, min(1.0, float(getattr(settings, "MMR_LAMBDA", 0.5) or 0.5)))
    try:
        mmr_candidates_top_n = int(getattr(settings, "RAG_MMR_CANDIDATES_TOP_N", MMR_CANDIDATES_TOP_N) or MMR_CANDIDATES_TOP_N)
    except Exception:
        mmr_candidates_top_n = MMR_CANDIDATES_TOP_N
    mmr_candidates_top_n = max(1, mmr_candidates_top_n)

    candidate_limit = max(top_k, mmr_candidates_top_n)

    # 4) SQL
    distance_raw, distance_sel = _build_vector_distance_expr(dim=expected_dim)

    try:
        kb_id_int = int(kb_id) if kb_id is not None else None
        if kb_id_int is not None and kb_id_int <= 0:
            kb_id_int = None
    except Exception:
        kb_id_int = None

    query_vec_sql_param = _embedding_param(query_vec_sql, expected_dim=expected_dim, model=emb_model)

    async with session_scope(read_only=True) as db:
        conditions = [
            RagTagVector.embedding_model == emb_model,
            RagTagVector.embedding_dim == expected_dim,
            func.vector_dims(RagTagVector.embedding) == expected_dim,
        ]

        base_scope = (kb_scope or "global").strip().lower() or "global"
        if owner_id:
            owner_cond = (RagTagVector.scope == "owner") & (RagTagVector.owner_id == int(owner_id))
            if kb_id_int is not None:
                owner_cond = owner_cond & (RagTagVector.kb_id == kb_id_int)
            conditions.append((RagTagVector.scope == base_scope) | owner_cond)
        else:
            conditions.append(RagTagVector.scope == base_scope)

        sql_started = time.perf_counter()

        knn = (
            select(
                RagTagVector.scope.label("scope"),
                RagTagVector.owner_id.label("owner_id"),
                RagTagVector.kb_id.label("kb_id"),
                RagTagVector.external_id.label("external_id"),
                RagTagVector.text.label("text"),
                RagTagVector.tag.label("tag"),
                RagTagVector.embedding.label("embedding"),
                (1.0 - distance_raw).label("similarity"),
                distance_sel,
            )
            .where(*conditions)
            .where(distance_raw <= max_distance)
            .order_by(distance_raw.asc())
            .cte("knn")
        )

        rn = over(
            func.row_number(),
            partition_by=(knn.c.scope, knn.c.owner_id, knn.c.kb_id, knn.c.external_id),
            order_by=knn.c.distance.asc(),
        ).label("rn")

        scored = (
            select(
                knn.c.scope,
                knn.c.owner_id,
                knn.c.kb_id,
                knn.c.external_id,
                knn.c.text,
                knn.c.tag,
                knn.c.embedding,
                knn.c.similarity,
                knn.c.distance,
                rn,
            )
            .cte("scored")
        )

        stmt = (
            select(
                scored.c.scope,
                scored.c.owner_id,
                scored.c.kb_id,
                scored.c.external_id,
                scored.c.text,
                scored.c.tag,
                scored.c.embedding,
                scored.c.similarity,
                scored.c.distance,
            )
            .where(scored.c.rn == 1)
            .order_by(scored.c.distance.asc())
            .limit(candidate_limit)
        )

        try:
            rows = await db.execute(stmt, {"query_vec": query_vec_sql_param})
            payload = rows.all()
        except Exception as exc:
            root_exc = getattr(exc, "orig", None)
            db_err = root_exc or exc
            root_type = type(db_err).__name__ if db_err is not None else "-"
            root_msg = str(db_err or "")
            query_vec_len = len(query_vec_sql_param) if hasattr(query_vec_sql_param, "__len__") else None
            query_vec_sample_type = (
                type(query_vec_sql_param[0]).__name__
                if hasattr(query_vec_sql_param, "__len__") and len(query_vec_sql_param) > 0
                else None
            )
            logger.error(
                "keyword_filter: sql stage failed err_type=%s db_err_type=%s db_err=%r model=%s owner_id=%s kb_id=%s expected_dim=%s query_vec_len=%s query_vec_type=%s query_vec_sample_type=%s",
                type(exc).__name__,
                root_type,
                root_msg[:240],
                emb_model,
                owner_id,
                kb_id_int,
                expected_dim,
                query_vec_len,
                type(query_vec_sql_param).__name__,
                query_vec_sample_type,
            )
            return []

        sql_duration_ms = (time.perf_counter() - sql_started) * 1000.0

    logger.info(
        "keyword_filter: sql stage complete duration_ms=%.2f candidate_size=%s model=%s owner_id=%s",
        sql_duration_ms,
        len(payload),
        emb_model,
        owner_id,
    )

    # 5) build candidates + MMR
    query_vec_n = _l2_normalize_np(np.asarray(query_vec_sql, dtype=np.float32).reshape(-1))
    scores_by_id: Dict[str, float] = {}
    vec_by_id: Dict[str, List[float]] = {}
    text_by_id: Dict[str, str] = {}

    for scope, oid, kb_id, ext_id, txt, tag, emb, _similarity, _distance in payload:
        vec = np.asarray(_normalize_embedding(emb), dtype=np.float32).reshape(-1)
        if int(vec.shape[0]) != expected_dim:
            continue

        rid = f"{scope}:{int(oid or 0)}:{int(kb_id or 0)}:{str(ext_id)}"

        prev = scores_by_id.get(rid)
        vec_n = _l2_normalize_np(vec)
        score_f = float(np.dot(vec_n, query_vec_n))
        if prev is None or score_f > prev:
            scores_by_id[rid] = score_f
            vec_by_id[rid] = [float(x) for x in vec_n.tolist()]
            text_by_id[rid] = str(txt or "")

    if not scores_by_id:
        logger.info(
            "keyword_filter: empty-hit no-scored-candidates model=%s owner_id=%s sql_candidate_size=%s",
            emb_model,
            owner_id,
            len(payload),
        )
        return []

    cand_ids = _sort_ids_by_similarity(scores_by_id)
    if apply_mmr:
        picked = _mmr_select_ids(cand_ids, vec_by_id, scores_by_id, top_k=top_k, lam=lam)
    else:
        picked = cand_ids[:top_k]
    return [(scores_by_id[i], i, text_by_id.get(i, "")) for i in picked]
