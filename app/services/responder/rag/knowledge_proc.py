# services/responder/rag/knowledge_proc.py

from __future__ import annotations
import json
import logging
import numpy as np
import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings

logger = logging.getLogger(__name__)

_KB_ENTRIES: Dict[str, List[Dict[str, Any]]] = {}

BASE_DIR = Path(__file__).resolve().parents[3]
EMBED_DIR = BASE_DIR / "data" / "embeddings"


def _load_precomputed(model: str) -> List[Dict[str, Any]]:
    filename = f"knowledge_embedded_{model}.json"
    path = EMBED_DIR / filename

    if not path.exists():
        logger.warning("Precomputed embeddings for %s not found at %s", model, path)
        return []

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        valid: List[Dict[str, Any]] = []
        for entry in data:
            emb = entry.get("emb")
            if not isinstance(emb, (list, tuple)):
                logger.warning("Skipping entry without valid emb: %r", entry.get("id"))
                continue
            entry["emb"] = np.asarray(emb, dtype=float)
            valid.append(entry)
        logger.info("Loaded %d embeddings from %s", len(valid), path)
        return valid
    except Exception:
        logger.exception("Failed to load precomputed embeddings from %s", path)
        return []


async def _init_kb(model_name: Optional[str] = None) -> List[Dict[str, Any]]:
    model = model_name or settings.EMBEDDING_MODEL
    if model in _KB_ENTRIES:
        return _KB_ENTRIES[model]
    entries = await asyncio.to_thread(_load_precomputed, model)
    if entries:
        _KB_ENTRIES["_MEAN_"] = np.mean([e["emb"] for e in entries], axis=0)
    _KB_ENTRIES[model] = entries
    return entries


async def get_relevant(
    query: str,
    *,
    model_name: Optional[str] = None
) -> List[Tuple[float, str, str]]:

    model = model_name or settings.EMBEDDING_MODEL
    entries = _KB_ENTRIES.get(model)
    if entries is None:
        entries = await _init_kb(model)
    if not entries:
        return []

    try:
        from app.clients.openai_client import get_openai
        client = get_openai()
        resp = await client.embeddings.create(model=model, input=[query])
        qraw = np.asarray(resp.data[0].embedding, dtype=float)
        mean_vec = _KB_ENTRIES.get("_MEAN_", 0)
        qemb = qraw - mean_vec
        qemb /= np.linalg.norm(qemb) or 1.0
    except Exception:
        logger.exception("Embedding query failed for model %s", model)
        return []

    sims_raw: list[tuple[float, str, str, np.ndarray]] = []
    for entry in entries:
        emb = entry["emb"] - mean_vec
        emb /= np.linalg.norm(emb) or 1.0
        score = float(np.dot(qemb, emb))
        sims_raw.append((score, entry["id"], entry["text"], emb))

    sims_raw.sort(key=lambda x: x[0], reverse=True)

    mmr_k = settings.KNOWLEDGE_TOP_K
    λ = 0.20
    picked: list[tuple[float, str, str]] = []
    chosen_vecs: list[np.ndarray] = []

    while sims_raw and len(picked) < mmr_k:
        if not picked:
            first = sims_raw.pop(0)
            picked.append(first[:3])
            chosen_vecs.append(first[3])
            continue

        def mmr_score(c):
            sim_q = c[0]
            sim_r = max(float(np.dot(c[3], v)) for v in chosen_vecs)
            return λ * sim_q - (1 - λ) * sim_r

        sims_raw.sort(key=mmr_score, reverse=True)
        best = sims_raw.pop(0)
        picked.append(best[:3])
        chosen_vecs.append(best[3])

    return picked
