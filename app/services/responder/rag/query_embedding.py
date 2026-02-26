# app/services/responder/rag/query_embedding.py
from __future__ import annotations

from typing import Any

import numpy as np

def normalize_query_embedding(raw: Any, expected_dim: int) -> list[float] | None:
    try:
        target_dim = int(expected_dim)
    except Exception:
        return None

    if isinstance(raw, (str, bytes, bytearray)):
        return None
    
    try:
        arr = np.asarray(raw, dtype=np.float32)
    except Exception:
        arr = None

    if arr is None:
        try:
            if isinstance(raw, (str, bytes, bytearray)):
                return None
            arr = np.asarray(list(raw), dtype=np.float32)
        except Exception:
            return None

    arr = arr.reshape(-1)

    if int(arr.shape[0]) != target_dim:
        return None
    if not np.isfinite(arr).all():
        return None

    return [float(x) for x in arr]
