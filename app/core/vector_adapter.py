from __future__ import annotations

from typing import Iterable, List

import numpy as np
from pgvector.psycopg import HalfVector


def normalize_vector_for_pg(
    vec: Iterable[float],
    *,
    expected_dim: int,
    model: str | None = None,
    l2_normalize: bool = False,
) -> List[float]:
    """Normalize/validate a vector for pgvector SQL binds and storage."""
    _ = model
    try:
        arr = np.asarray(vec, dtype=np.float32).reshape(-1)
    except Exception as exc:
        raise ValueError("invalid vector values") from exc
    if arr.ndim != 1:
        raise ValueError(f"invalid vector ndim={arr.ndim}")
    if int(arr.shape[0]) != int(expected_dim):
        raise ValueError(f"invalid vector dim={int(arr.shape[0])} expected={int(expected_dim)}")
    if not np.isfinite(arr).all():
        raise ValueError("vector contains non-finite values")
    if l2_normalize:
        norm = float(np.linalg.norm(arr))
        if np.isfinite(norm) and norm > 0.0:
            arr = arr / norm
    return arr.astype(np.float32, copy=False).tolist()


def adapt_vector_for_storage(
    vec: Iterable[float],
    *,
    expected_dim: int,
    model: str | None = None,
    l2_normalize: bool = False,
) -> object:
    """Centralized adapter for pgvector SQL binds/storage.

    Rule: use ``HalfVector`` for all RAG pgvector SQL binds/storage.
    """
    return HalfVector(
        normalize_vector_for_pg(
            vec,
            expected_dim=expected_dim,
            model=model,
            l2_normalize=l2_normalize,
        )
    )
