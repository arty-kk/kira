from __future__ import annotations

from typing import Any, List

import numpy as np

from app.config import settings


_OPENAI_EMBED_DIMS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
}


def resolve_embedding_dim(model: str | None, *, fallback_dim: int) -> int:
    model_name = str(model or "").strip().lower()
    if model_name in _OPENAI_EMBED_DIMS:
        return _OPENAI_EMBED_DIMS[model_name]
    return int(fallback_dim)




def get_rag_embedding_model(explicit_model: str | None = None) -> str:
    model = str(explicit_model or "").strip()
    if model:
        return model
    return str(getattr(settings, "EMBEDDING_MODEL", "text-embedding-3-large") or "text-embedding-3-large").strip()


def normalize_embedding_row(raw: Any, *, expected_dim: int, error_prefix: str = "") -> List[float]:
    arr = np.asarray(raw, dtype=np.float32)
    if arr.ndim == 2 and arr.shape[0] == 1:
        arr = arr[0]
    if arr.ndim != 1:
        raise RuntimeError(f"{error_prefix}invalid embedding row shape={arr.shape}")
    if int(arr.shape[0]) != expected_dim:
        raise RuntimeError(
            f"{error_prefix}invalid embedding dim got={int(arr.shape[0])} expected={expected_dim}"
        )
    if not np.isfinite(arr).all():
        raise RuntimeError(f"{error_prefix}embedding contains non-finite values")
    return [float(x) for x in arr]
