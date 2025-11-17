#app/services/responder/rag/knowledge_proc.py
from __future__ import annotations

import json
import logging
import numpy as np
import base64 as _b64
import asyncio
import time

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from collections import OrderedDict

from app.config import settings
from app.clients.openai_client import _call_openai_with_retry

logger = logging.getLogger(__name__)

_KB_ENTRIES: Dict[str, List[Dict[str, Any]]] = {}
_KB_STATE:   Dict[str, Dict[str, Any]] = {}

_kb_init_lock = asyncio.Lock()

BASE_DIR = Path(__file__).resolve().parents[4]
EMBED_DIR = BASE_DIR / "data" / "embeddings"

def _npz_path(model: str) -> Path:
    return EMBED_DIR / f"knowledge_embedded_{model}.npz"

_EMB_CACHE: "OrderedDict[Tuple[str, str], np.ndarray]" = OrderedDict()
_EMB_CACHE_LOCK = asyncio.Lock()
_EMB_CACHE_MAX = int(getattr(settings, "EMBED_CACHE_SIZE", 2048))

def _cache_key(model: str, text: str) -> Tuple[str, str]:
    norm = " ".join((text or "").strip().lower().split())
    return (model, norm)

async def _get_query_embedding(api_model: str, query: str) -> Optional[np.ndarray]:
    key = _cache_key(api_model, query)
    async with _EMB_CACHE_LOCK:
        if key in _EMB_CACHE:
            vec = _EMB_CACHE.pop(key)
            _EMB_CACHE[key] = vec
            return vec

    _t0 = time.perf_counter()
    endpoint_name = "embeddings.create"
    
    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint=endpoint_name,
                model=api_model,
                input=[query],
                encoding_format="float",
            ),
            timeout=settings.EMBEDDING_TIMEOUT,
        )
        elapsed_ms = (time.perf_counter() - _t0) * 1000.0
        logger.info("OpenAI call ok: endpoint=%s model=%s elapsed_ms=%.1f", endpoint_name, api_model, elapsed_ms)
    except Exception:
        elapsed_ms = (time.perf_counter() - _t0) * 1000.0
        logger.warning("OpenAI call failed: endpoint=%s model=%s elapsed_ms=%.1f", endpoint_name, api_model, elapsed_ms)
        logger.exception("Embedding query failed for model %s", api_model)
        return None

    vec = resp.data[0].embedding
    if isinstance(vec, str):
        try:
            raw = _b64.b64decode(vec)
            qraw = np.frombuffer(raw, dtype=np.float32)
        except Exception:
            logger.error("Embedding returned base64 string and failed to decode for model %s", api_model)
            return None
    else:
        qraw = np.asarray(vec, dtype=np.float32)

    async with _EMB_CACHE_LOCK:
        _EMB_CACHE[key] = qraw
        if len(_EMB_CACHE) > _EMB_CACHE_MAX:
            _EMB_CACHE.popitem(last=False)
    return qraw

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
        dim0 = None
        for entry in data:
            emb = entry.get("emb")
            if not isinstance(emb, (list, tuple)):
                logger.warning("Skipping entry without valid emb: %r", entry.get("id"))
                continue
            arr = np.asarray(emb, dtype=np.float32)
            if dim0 is None:
                dim0 = arr.shape[-1]
            elif arr.shape[-1] != dim0:
                logger.warning("Skip entry %r: dim mismatch %s != %s", entry.get("id"), arr.shape[-1], dim0)
                continue
            entry["emb"] = arr
            valid.append(entry)
        logger.info("Loaded %d embeddings from %s", len(valid), path)
        return valid
    except Exception:
        logger.exception("Failed to load precomputed embeddings from %s", path)
        return []

def _load_state_from_npz(model: str) -> Optional[Dict[str, Any]]:
    p = _npz_path(model)
    if not p.exists():
        return None
    try:
        with np.load(p, allow_pickle=True) as z:
            E    = z["E"].astype(np.float32, copy=False)
            mean = z["mean"].astype(np.float32, copy=False)
            ids  = list(z["ids"].tolist())
            texts = list(z["texts"].tolist())

            meta = None
            if "meta" in z.files:
                try:
                    meta = z["meta"].tolist()
                except Exception:
                    meta = None

        E = np.ascontiguousarray(np.nan_to_num(E, nan=0.0, posinf=0.0, neginf=0.0), dtype=np.float32)
        mean = np.ascontiguousarray(np.nan_to_num(mean, nan=0.0, posinf=0.0, neginf=0.0), dtype=np.float32)

        if E.ndim != 2 or mean.ndim != 1 or E.shape[1] != mean.shape[0]:
            logger.error("NPZ shape mismatch: E=%s mean=%s", E.shape, mean.shape)
            return None
        if len(ids) != E.shape[0] or len(texts) != E.shape[0]:
            logger.error("NPZ meta mismatch: ids/texts vs E: %d/%d vs %d", len(ids), len(texts), E.shape[0])
            return None

        if isinstance(meta, dict):
            dim_meta = meta.get("dim")
            if dim_meta is not None and int(dim_meta) != int(E.shape[1]):
                logger.error("NPZ dim mismatch: file dim=%s vs E.shape[1]=%s", dim_meta, E.shape[1])
                return None
            model_meta = meta.get("model")
            if model_meta is not None and str(model_meta) != str(model):
                logger.error("NPZ model mismatch: file model=%r vs requested %r", model_meta, model)
                return None

        logger.info("Loaded NPZ state for %s: E=%s", model, E.shape)
        return {"mean": mean, "E": E, "ids": ids, "texts": texts}
    except Exception:
        logger.exception("Failed to load NPZ state for %s", model)
        return None

def _build_state(entries: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not entries:
        return None
    try:
        embs_list = [e["emb"] for e in entries]
        mean = np.mean(embs_list, axis=0).astype(np.float32)

        embs = np.stack(embs_list, axis=0).astype(np.float32)  # (N,D)
        embs -= mean
        norms = np.linalg.norm(embs, axis=1, keepdims=True)
        norms[norms == 0.0] = 1.0
        E = embs / norms

        E = np.ascontiguousarray(np.nan_to_num(E, nan=0.0, posinf=0.0, neginf=0.0), dtype=np.float32)
        mean = np.ascontiguousarray(np.nan_to_num(mean, nan=0.0, posinf=0.0, neginf=0.0), dtype=np.float32)

        ids   = [str(e.get("id", "")) for e in entries]
        texts = [str(e.get("text", "")) for e in entries]

        return {"mean": mean, "E": E, "ids": ids, "texts": texts}
    except Exception:
        logger.exception("Failed to build KB state")
        return None

def _strip_emb_memory(entries: List[Dict[str, Any]]) -> None:
    for e in entries:
        e.pop("emb", None)

async def _init_kb(model_name: Optional[str] = None) -> List[Dict[str, Any]]:
    model = model_name or settings.EMBEDDING_MODEL
    async with _kb_init_lock:
        if model in _KB_ENTRIES:
            if model not in _KB_STATE and _KB_ENTRIES[model]:
                state = await asyncio.to_thread(_build_state, _KB_ENTRIES[model])
                if state:
                    _KB_STATE[model] = state
                    _strip_emb_memory(_KB_ENTRIES[model])
            return _KB_ENTRIES[model]

        state = await asyncio.to_thread(_load_state_from_npz, model)
        if state:
            _KB_STATE[model] = state
            entries = [{"id": i, "text": t} for i, t in zip(state["ids"], state["texts"])]
            _KB_ENTRIES[model] = entries
            return entries

        entries = await asyncio.to_thread(_load_precomputed, model)
        _KB_ENTRIES[model] = entries

        if entries:
            state = await asyncio.to_thread(_build_state, entries)
            if state:
                _KB_STATE[model] = state
            _strip_emb_memory(entries)
        return entries

def _mmr_select(
    E_cand: np.ndarray,
    scores_cand: np.ndarray,
    top_k: int,
    lam: float,
) -> List[int]:
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

    state = _KB_STATE.get(file_model)
    if not state:
        state = await asyncio.to_thread(_build_state, entries)
        if not state:
            return []
        _KB_STATE[file_model] = state
        _strip_emb_memory(entries)

    mean_vec: np.ndarray = state["mean"]
    E: np.ndarray = state["E"]
    ids: List[str] = state["ids"]
    texts: List[str] = state["texts"]

    api_model = file_model
    qraw = await _get_query_embedding(api_model, query)
    if qraw is None:
        return []

    diff = (qraw - mean_vec).astype(np.float32, copy=False)
    n = float(np.linalg.norm(diff))
    if not np.isfinite(n) or n < 1e-12:
        diff = qraw.astype(np.float32, copy=False)
        n = float(np.linalg.norm(diff))
        if not np.isfinite(n) or n < 1e-12:
            return []
    qemb = diff / n
    qemb = np.nan_to_num(qemb, nan=0.0, posinf=0.0, neginf=0.0)

    if E.ndim != 2 or qemb.ndim != 1 or E.shape[1] != qemb.shape[0]:
        logger.error("Shape mismatch: E=%s, q=%s", E.shape, qemb.shape)
        return []

    try:
        scores = E @ qemb
    except Exception:
        logger.exception("Dot-product failed for model %s", file_model)
        return []

    N = int(E.shape[0])
    top_k_cfg = int(getattr(settings, "KNOWLEDGE_TOP_K", 3)) or 3
    top_k_eff = min(top_k_cfg, N)

    L = min(max(10 * top_k_eff, 200), N)
    if L < top_k_eff:
        L = top_k_eff
    if L == 0:
        return []

    idx = np.argpartition(scores, -L)[-L:]
    idx = idx[np.argsort(scores[idx])[::-1]]

    E_cand = E[idx]
    scores_cand = scores[idx]

    lam = float(getattr(settings, "MMR_LAMBDA", 0.55))
    if not np.isfinite(lam):
        lam = 0.55
    lam = max(0.0, min(1.0, lam))

    picked_local = _mmr_select(E_cand, scores_cand, top_k=top_k_eff, lam=lam)
    picked_idx = idx[picked_local].tolist()

    result: List[Tuple[float, str, str]] = [
        (float(scores[i]), ids[i], texts[i]) for i in picked_idx
    ]
    try:
        logger.debug("RAG[%s]: N=%d, top_k=%d, L=%d, top_score=%.4f",
                     file_model, N, top_k_eff, len(idx),
                     float(scores[picked_idx[0]]) if picked_idx else float("nan"))
    except Exception:
        pass
    return result