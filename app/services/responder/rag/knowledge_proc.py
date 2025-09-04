cat >app/services/responder/rag/knowledge_proc.py<< ''
#app/services/responder/rag/knowledge_proc.py
from __future__ import annotations

import json
import logging
import functools
import numpy as np
import base64 as _b64
import asyncio

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.clients.openai_client import _call_openai_with_retry
from asyncio import get_running_loop

logger = logging.getLogger(__name__)


_KB_ENTRIES: Dict[str, List[Dict[str, Any]]] = {}
_kb_init_lock = asyncio.Lock()


BASE_DIR = Path(__file__).resolve().parents[4]
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
            entry["emb"] = np.asarray(emb, dtype=np.float32)
            valid.append(entry)
        logger.info("Loaded %d embeddings from %s", len(valid), path)
        return valid
    except Exception:
        logger.exception("Failed to load precomputed embeddings from %s", path)
        return []


async def _init_kb(model_name: Optional[str] = None) -> List[Dict[str, Any]]:
    model = model_name or settings.EMBEDDING_MODEL
    async with _kb_init_lock:
        if model in _KB_ENTRIES:
            return _KB_ENTRIES[model]
        entries = await asyncio.to_thread(_load_precomputed, model)
        if entries:
            mean_key = f"{model}__MEAN__"
            _KB_ENTRIES[mean_key] = np.mean([e["emb"] for e in entries], axis=0)
        _KB_ENTRIES[model] = entries
        return entries


def _normalize(vec: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(vec) or 1.0
    return vec / norm


async def get_relevant(
    query: str,
    *,
    model_name: Optional[str] = None
) -> List[Tuple[float, str, str]]:

    file_model = model_name or settings.EMBEDDING_MODEL
    entries = _KB_ENTRIES.get(file_model)
    if entries is None:
        entries = await _init_kb(file_model)
    if not entries:
        return []

    mean_key = f"{file_model}__MEAN__"
    mean_vec = _KB_ENTRIES.get(mean_key, 0)

    try:
        api_model = file_model.replace("-offtopic", "")

        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="embeddings.create",
                model=api_model,
                input=[query],
                encoding_format="float",
            ),
            timeout=60.0,
        )
        vec = resp.data[0].embedding

        if isinstance(vec, str):
            try:
                raw = _b64.b64decode(vec)
                qraw = np.frombuffer(raw, dtype=np.float32)
            except Exception:
                logger.error("Embedding returned base64 string and failed to decode for model %s", file_model)
                return []
        else:
            qraw = np.asarray(vec, dtype=np.float32)
        qemb = _normalize(qraw - mean_vec)
    except Exception:
        logger.exception("Embedding query failed for model %s", file_model)
        return []

    def _compute_mmr(
        entries: List[Dict[str, Any]],
        mean_vec: np.ndarray,
        qemb: np.ndarray,
        top_k: int,
        λ: float
    ) -> List[Tuple[float, str, str]]:

        sims: List[Tuple[float, str, str, np.ndarray]] = []
        for entry in entries:
            emb = entry["emb"]
            diff = emb - mean_vec
            emb_norm = diff / (np.linalg.norm(diff) or 1.0)
            score = float(np.dot(qemb, emb_norm))
            sims.append((score, entry["id"], entry["text"], emb_norm))

        sims.sort(key=lambda x: x[0], reverse=True)

        picked: List[Tuple[float, str, str]] = []
        chosen_vecs: List[np.ndarray] = []
        while sims and len(picked) < top_k:
            if not picked:
                first = sims.pop(0)
                picked.append(first[:3])
                chosen_vecs.append(first[3])
                continue

            def mmr_score(c: Tuple[float, str, str, np.ndarray]) -> float:
                sim_q = c[0]
                sim_r = max(float(np.dot(c[3], v)) for v in chosen_vecs)
                return λ * sim_q - (1 - λ) * sim_r

            sims.sort(key=mmr_score, reverse=True)
            best = sims.pop(0)
            picked.append(best[:3])
            chosen_vecs.append(best[3])

        return picked


    loop = get_running_loop()
    picked = await loop.run_in_executor(
        None,
        functools.partial(
            _compute_mmr,
            entries,
            mean_vec,
            qemb,
            settings.KNOWLEDGE_TOP_K,
            0.20,
        )
    )
    return picked
