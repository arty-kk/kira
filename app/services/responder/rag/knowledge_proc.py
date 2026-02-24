from __future__ import annotations

import asyncio
import base64 as _b64
import logging
import time
from typing import List, Optional

import numpy as np

from app.clients.openai_client import _call_openai_with_retry
from app.config import settings

logger = logging.getLogger(__name__)


async def _get_query_embedding(api_model: str, query: str) -> Optional[np.ndarray]:
    _t0 = time.perf_counter()
    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(endpoint="embeddings.create", model=api_model, input=[query], encoding_format="float"),
            timeout=settings.EMBEDDING_TIMEOUT,
        )
        logger.info("OpenAI call ok: endpoint=embeddings.create model=%s elapsed_ms=%.1f", api_model, (time.perf_counter() - _t0) * 1000.0)
    except Exception:
        logger.exception("Embedding query failed for model %s", api_model)
        return None

    vec = resp.data[0].embedding
    if isinstance(vec, str):
        try:
            return np.frombuffer(_b64.b64decode(vec), dtype=np.float32)
        except Exception:
            return None
    return np.asarray(vec, dtype=np.float32)


def _mmr_select(E_cand: np.ndarray, scores_cand: np.ndarray, top_k: int, lam: float) -> List[int]:
    L = int(E_cand.shape[0])
    if L == 0 or top_k <= 0:
        return []
    if top_k >= L:
        return list(range(L))
    chosen: List[int] = []
    remaining = np.arange(L)
    i0 = int(np.argmax(scores_cand))
    chosen.append(i0)
    remaining = remaining[remaining != i0]
    while len(chosen) < top_k and remaining.size > 0:
        C = E_cand[chosen]
        R = E_cand[remaining]
        sim_mat = R @ C.T
        max_sim = sim_mat.max(axis=1)
        mmr = lam * scores_cand[remaining] - (1.0 - lam) * max_sim
        pick_rel_idx = int(np.argmax(mmr))
        pick = int(remaining[pick_rel_idx])
        chosen.append(pick)
        remaining = np.delete(remaining, pick_rel_idx)
    return chosen
