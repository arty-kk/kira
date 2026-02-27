from __future__ import annotations

from typing import Any, List


from app.config import settings
from app.core.vector_adapter import normalize_vector_for_pg


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
    try:
        return normalize_vector_for_pg(raw, expected_dim=expected_dim)
    except ValueError as exc:
        raise RuntimeError(f"{error_prefix}{exc}") from exc
