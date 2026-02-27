#app/services/responder/rag/knowledge_proc.py
from __future__ import annotations

import asyncio
import base64 as _b64
import logging
import time
from typing import Optional

import numpy as np

from app.clients.openai_client import _call_openai_with_retry
from app.config import settings

logger = logging.getLogger(__name__)


def _normalize_embedding_1d(raw: object) -> Optional[np.ndarray]:
    try:
        arr = np.asarray(raw, dtype=np.float32)
    except Exception:
        return None
    if arr.ndim == 2 and arr.shape[0] == 1:
        arr = arr[0]
    if arr.ndim != 1 or arr.size == 0:
        return None
    if not np.isfinite(arr).all():
        return None
    return arr


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

    try:
        data = getattr(resp, "data", None)
        vec = data[0].embedding if data else None
    except Exception:
        return None

    if isinstance(vec, str):
        try:
            return _normalize_embedding_1d(np.frombuffer(_b64.b64decode(vec), dtype=np.float32))
        except Exception:
            return None
    return _normalize_embedding_1d(vec)
