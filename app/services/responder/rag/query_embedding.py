# app/services/responder/rag/query_embedding.py
from __future__ import annotations

from typing import Any

from app.core.vector_adapter import normalize_vector_for_pg


def normalize_query_embedding(raw: Any, expected_dim: int) -> list[float] | None:
    try:
        return normalize_vector_for_pg(raw, expected_dim=int(expected_dim))
    except Exception:
        return None
