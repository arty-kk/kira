from __future__ import annotations

from typing import Any, List

import numpy as np


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
