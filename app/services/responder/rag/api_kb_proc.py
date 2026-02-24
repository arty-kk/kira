# app/services/responder/rag/api_kb_proc.py
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from app.config import settings
from sqlalchemy import select

from app.core.db import session_scope
from app.core.models import ApiKeyKnowledge
from .knowledge_proc import _get_query_embedding, _mmr_select, EMBED_DIR

logger = logging.getLogger(__name__)


def _owner_dir(owner_id: int) -> Path:

    return EMBED_DIR / "api_keys" / str(owner_id)


def _npz_path(owner_id: int, model: str) -> Path:

    return _owner_dir(owner_id) / f"knowledge_embedded_{model}.npz"


def _load_state_from_npz(owner_id: int, model: str) -> Optional[Dict[str, Any]]:

    p = _npz_path(owner_id, model)
    if not p.exists():
        logger.info("API-KB: NPZ not found for owner_id=%s model=%s at %s", owner_id, model, p)
        return None

    try:
        with np.load(p, allow_pickle=True) as z:
            E = z["E"].astype(np.float32, copy=False)
            mean = z["mean"].astype(np.float32, copy=False)
            ids = list(z["ids"].tolist())
            texts = list(z["texts"].tolist())
            meta = None
            if "meta" in z.files:
                try:
                    meta = z["meta"].tolist()
                except Exception:
                    meta = None

        E = np.ascontiguousarray(np.nan_to_num(E, nan=0.0, posinf=0.0, neginf=0.0), dtype=np.float32)
        mean = np.ascontiguousarray(np.nan_to_num(mean, nan=0.0, posinf=0.0, neginf=0.0), dtype=np.float32)

        if E.ndim != 2 or mean.ndim != 1 or E.shape[1] != mean.shape[0]:
            logger.error(
                "API-KB NPZ shape mismatch for owner_id=%s model=%s: E=%s mean=%s",
                owner_id,
                model,
                E.shape,
                mean.shape,
            )
            return None
        if len(ids) != E.shape[0] or len(texts) != E.shape[0]:
            logger.error(
                "API-KB NPZ meta size mismatch for owner_id=%s model=%s: ids/texts=%d/%d vs E=%d",
                owner_id,
                model,
                len(ids),
                len(texts),
                E.shape[0],
            )
            return None

        if isinstance(meta, dict):
            dim_meta = meta.get("dim")
            if dim_meta is not None and int(dim_meta) != int(E.shape[1]):
                logger.error(
                    "API-KB NPZ dim mismatch for owner_id=%s model=%s: meta_dim=%s vs E_dim=%s",
                    owner_id,
                    model,
                    dim_meta,
                    E.shape[1],
                )

        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = None

        logger.info(
            "API-KB: loaded NPZ for owner_id=%s model=%s (N=%d, D=%d)",
            owner_id,
            model,
            E.shape[0],
            E.shape[1],
        )
        return {
            "mean": mean,
            "E": E,
            "ids": ids,
            "texts": texts,
            "_mtime": mtime,
        }
    except Exception:
        logger.exception("API-KB: failed to load NPZ for owner_id=%s model=%s", owner_id, model)
        return None


async def _has_ready_kb(owner_id: int, model: str) -> bool:

    owner_id = int(owner_id)
    model = (model or "").strip()
    if not owner_id or not model:
        return False

    try:
        async with session_scope(read_only=True) as db:
            res = await db.execute(
                select(ApiKeyKnowledge.id)
                .where(
                    ApiKeyKnowledge.api_key_id == owner_id,
                    ApiKeyKnowledge.status == "ready",
                    ApiKeyKnowledge.embedding_model == model,
                )
                .limit(1)
            )
            exists = res.scalar_one_or_none() is not None
            return bool(exists)
    except Exception:
        logger.exception(
            "API-KB: DB check for ready KB failed; fail-closed for owner_id=%s model=%s",
            owner_id,
            model,
            extra={"owner_id": owner_id, "model": model, "mode": "fail_closed"},
        )
        return False


async def _ensure_state(owner_id: int, model: str) -> Optional[Dict[str, Any]]:

    owner_id = int(owner_id)
    model = (model or "").strip()

    has_ready = await _has_ready_kb(owner_id, model)
    if not has_ready:
        logger.info(
            "API-KB load skipped: no ready KB in DB for owner_id=%s model=%s",
            owner_id,
            model,
        )
        return None

    st = await asyncio.to_thread(_load_state_from_npz, owner_id, model)
    return st or None


def invalidate_api_kb_cache(owner_id: Optional[int] = None) -> None:

    _ = owner_id
    logger.info("API-KB cache invalidation requested, but runtime cache is disabled")


async def get_relevant_for_owner(
    query: str,
    *,
    owner_id: int,
    model_name: Optional[str] = None,
) -> List[Tuple[float, str, str]]:

    if not owner_id:
        return []
    if not (query or "").strip():
        return []

    file_model = model_name or settings.EMBEDDING_MODEL

    state = await _ensure_state(int(owner_id), file_model)
    if not state:
        return []

    mean: np.ndarray = state["mean"]
    E: np.ndarray = state["E"]
    ids: List[str] = state["ids"]
    texts: List[str] = state["texts"]

    api_model = file_model
    qraw = await _get_query_embedding(api_model, query)
    if qraw is None:
        return []

    diff = (qraw - mean).astype(np.float32, copy=False)
    n = float(np.linalg.norm(diff))
    if not np.isfinite(n) or n < 1e-12:
        diff = qraw.astype(np.float32, copy=False)
        n = float(np.linalg.norm(diff))
        if not np.isfinite(n) or n < 1e-12:
            return []
    qemb = diff / n
    qemb = np.nan_to_num(qemb, nan=0.0, posinf=0.0, neginf=0.0)

    if E.ndim != 2 or qemb.ndim != 1 or E.shape[1] != qemb.shape[0]:
        logger.error(
            "API-KB shape mismatch for owner_id=%s model=%s: E=%s q=%s",
            owner_id,
            file_model,
            E.shape,
            qemb.shape,
        )
        return []

    try:
        scores = E @ qemb  # (N,)
    except Exception:
        logger.exception("API-KB dot-product failed for owner_id=%s model=%s", owner_id, file_model)
        return []

    N = int(E.shape[0])
    if N == 0:
        return []

    raw_top_k = getattr(settings, "KNOWLEDGE_TOP_K", 3)
    try:
        parsed_top_k = int(raw_top_k)
    except Exception:
        parsed_top_k = 3
        logger.warning(
            "api_kb_proc.get_relevant_for_owner: invalid KNOWLEDGE_TOP_K raw_top_k=%r, applied_top_k=%d",
            raw_top_k,
            parsed_top_k,
        )
    if parsed_top_k <= 0:
        logger.warning(
            "api_kb_proc.get_relevant_for_owner: non-positive KNOWLEDGE_TOP_K raw_top_k=%r, applied_top_k=%d",
            raw_top_k,
            1,
        )
    top_k_cfg = max(1, parsed_top_k)
    top_k_eff = min(top_k_cfg, N)

    L = min(max(10 * top_k_eff, 200), N)
    if L < top_k_eff:
        L = top_k_eff

    idx = np.argpartition(scores, -L)[-L:]
    idx = idx[np.argsort(scores[idx])[::-1]]

    E_cand = E[idx]
    scores_cand = scores[idx]

    try:
        lam = settings.MMR_LAMBDA
    except Exception:
        lam = 0.55
    if not np.isfinite(lam):
        lam = 0.55
    lam = max(0.0, min(1.0, lam))

    picked_local = _mmr_select(E_cand, scores_cand, top_k=top_k_eff, lam=lam)
    picked_idx = idx[picked_local].tolist()

    result: List[Tuple[float, str, str]] = [
        (float(scores[i]), ids[i], texts[i]) for i in picked_idx
    ]

    try:
        top_score = float(scores[picked_idx[0]]) if picked_idx else float("nan")
        logger.debug(
            "API-KB[owner_id=%s, model=%s]: N=%d, top_k=%d, L=%d, top_score=%.4f",
            owner_id,
            file_model,
            N,
            top_k_eff,
            len(idx),
            top_score,
        )
    except Exception:
        pass

    return result
