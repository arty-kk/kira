#app/services/responder/rag/keyword_filter.py
import json
import logging
import math
import re
import numpy as np
import asyncio
import time

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.clients.openai_client import _call_openai_with_retry
from .knowledge_proc import EMBED_DIR

logger = logging.getLogger(__name__)


_INDICES: Dict[str, Dict[str, Any]] = {}
MMR_CANDIDATES_TOP_N = 30

# Backward-compat test hook: runtime cache is disabled, keep a clearable container.
_EMB_CACHE: Dict[Tuple[str, str], List[float]] = {}
_JSON_TRAILING_COMMAS = re.compile(r',\s*([}\]])')


def _owner_dir(owner_id: int) -> Path:
    return EMBED_DIR / "api_keys" / str(int(owner_id))


def _tags_npz_path(model: str) -> Path:
    return EMBED_DIR / f"tags_embedded_{model}.npz"


def _owner_tags_npz_path(owner_id: int, model: str) -> Path:
    return _owner_dir(owner_id) / f"tags_embedded_{model}.npz"


def _api_model_name(model: Optional[str]) -> str:
    m = model or settings.EMBEDDING_MODEL
    return str(m).replace("-offtopic", "")



def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().casefold())


def _read_json_list(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        logger.warning("keyword_filter: file not found %s", path)
        return []
    raw = path.read_text(encoding="utf-8")
    cleaned = _JSON_TRAILING_COMMAS.sub(r"\1", raw)
    try:
        data = json.loads(cleaned)
    except Exception:
        logger.exception("keyword_filter: invalid json %s", path)
        return []
    if not isinstance(data, list):
        logger.error("keyword_filter: json root must be list, got %s", type(data))
        return []
    out: List[Dict[str, Any]] = []
    for x in data:
        if isinstance(x, dict):
            out.append(x)
    return out


def _collect_keywords(item: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    tags = item.get("tags") or []
    if isinstance(tags, list):
        for t in tags:
            if isinstance(t, str):
                t2 = _norm_ws(t)
                if t2:
                    out.append(t2)
    return out


def _l2_normalize(vec: List[float]) -> List[float]:
    s = 0.0
    for v in vec:
        s += v * v
    n = math.sqrt(s) if s > 0 else 1.0
    return [v / n for v in vec]


def _cosine(a: List[float], b: List[float]) -> float:
    s = 0.0
    la = len(a)
    lb = len(b)
    L = la if la < lb else lb
    for i in range(L):
        s += a[i] * b[i]
    return float(s)


def _mmr_select_ids(
    cand_ids: List[str],
    vecs_by_id: Dict[str, List[float]],
    scores_by_id: Dict[str, float],
    top_k: int,
    lam: float,
) -> List[str]:
    if top_k <= 0 or not cand_ids:
        return []
    if top_k >= len(cand_ids) or lam >= 0.999:
        return sorted(cand_ids, key=lambda i: scores_by_id.get(i, 0.0), reverse=True)[
            :top_k
        ]

    selected: List[str] = []
    remaining: set[str] = set(cand_ids)

    first = max(remaining, key=lambda i: scores_by_id.get(i, 0.0))
    selected.append(first)
    remaining.remove(first)

    def _cos_sim(a: List[float], b: List[float]) -> float:
        s = 0.0
        L = min(len(a), len(b))
        for k in range(L):
            s += a[k] * b[k]
        return float(s)

    while len(selected) < top_k and remaining:
        best_id: Optional[str] = None
        best_score = -1e9
        for rid in list(remaining):
            v_r = vecs_by_id.get(rid)
            if not v_r:
                continue
            max_sim = 0.0
            for sid in selected:
                v_s = vecs_by_id.get(sid)
                if v_s:
                    cs = _cos_sim(v_r, v_s)
                    if cs > max_sim:
                        max_sim = cs
            mmr = lam * scores_by_id.get(rid, 0.0) - (1.0 - lam) * max_sim
            if mmr > best_score:
                best_score = mmr
                best_id = rid
        if best_id is None:
            break
        selected.append(best_id)
        remaining.remove(best_id)

    return selected


async def _embed_texts(texts: List[str], model: Optional[str] = None) -> List[List[float]]:
    api_model = _api_model_name(model)
    if not texts:
        return []

    try:
        bs = settings.EMBED_BATCH_SIZE
    except Exception:
        bs = 128
    if bs <= 0:
        bs = 128

    out: List[List[float]] = []
    overall_start = time.perf_counter()
    for i in range(0, len(texts), bs):
        chunk = texts[i : i + bs]
        t0 = time.perf_counter()
        try:
            resp = await asyncio.wait_for(
                _call_openai_with_retry(
                    endpoint="embeddings.create", model=api_model, input=chunk
                ),
                timeout=settings.EMBEDDING_TIMEOUT,
            )
            elapsed = time.perf_counter() - t0
            logger.info(
                "keyword_filter: openai embeddings.create ok model=%s batch_index=%d batch_size=%d elapsed=%.3fs",
                api_model,
                (i // bs),
                len(chunk),
                elapsed,
            )
        except Exception:
            elapsed = time.perf_counter() - t0
            logger.exception(
                "keyword_filter: openai embeddings.create FAILED model=%s batch_index=%d batch_size=%d elapsed=%.3fs",
                api_model,
                (i // bs),
                len(chunk),
                elapsed,
            )
            raise

        data = getattr(resp, "data", None) or (
            resp.get("data") if isinstance(resp, dict) else None
        )
        if not isinstance(data, list) or len(data) != len(chunk):
            raise RuntimeError("keyword_filter: embeddings response size mismatch")

        for row in data:
            emb = getattr(row, "embedding", None)
            if emb is None and isinstance(row, dict):
                emb = row.get("embedding")
            if not isinstance(emb, list):
                raise RuntimeError("keyword_filter: invalid embedding row")
            out.append(_l2_normalize([float(x) for x in emb]))

    total_elapsed = time.perf_counter() - overall_start
    logger.info(
        "keyword_filter: openai embeddings.create total model=%s texts=%d batches=%d elapsed=%.3fs",
        api_model,
        len(texts),
        ((len(texts) + bs - 1) // bs),
        total_elapsed,
    )
    return out


def _load_tags_index_from_npz(p: Path) -> Optional[Dict[str, Any]]:
    if not p.exists():
        return None
    try:
        with np.load(p, allow_pickle=True) as z:
            files = set(z.files)
            meta = None
            if "meta" in z.files:
                try:
                    meta = z["meta"].tolist()
                except Exception:
                    meta = None
            # format v2 (per-tag): TE[M,D], tag_item_ids[M], tag_item_texts[M], optional tag_texts[M]
            if {"TE", "tag_item_ids", "tag_item_texts"}.issubset(files):
                E = z["TE"].astype(np.float32, copy=False)
                ids = list(z["tag_item_ids"].tolist())
                texts = list(z["tag_item_texts"].tolist())
                tag_texts = (
                    list(z["tag_texts"].tolist())
                    if "tag_texts" in files
                    else ["" for _ in range(len(ids))]
                )
                if E.ndim != 2:
                    logger.error("tags NPZ TE ndim mismatch: %s", E.shape)
                    return None
                if len(ids) != E.shape[0] or len(texts) != E.shape[0]:
                    logger.error(
                        "tags NPZ v2 size mismatch: ids/texts vs TE: %d/%d vs %d",
                        len(ids),
                        len(texts),
                        E.shape[0],
                    )
                    return None
                E = np.nan_to_num(E, nan=0.0, posinf=0.0, neginf=0.0).astype(
                    np.float32, copy=False
                )
                norms = np.linalg.norm(E, axis=1, keepdims=True)
                norms[norms == 0.0] = 1.0
                E = E / norms

                tag_vecs_by_id: Dict[str, List[List[float]]] = {}
                tags_by_id: Dict[str, List[str]] = {}
                texts_by_id: Dict[str, str] = {}
                for k, raw_id in enumerate(ids):
                    eid = str(raw_id)
                    tag_vecs_by_id.setdefault(eid, []).append(E[k].tolist())
                    tags_by_id.setdefault(eid, []).append(str(tag_texts[k] or ""))
                    if eid not in texts_by_id:
                        texts_by_id[eid] = str(texts[k] or "")

                if isinstance(meta, dict):
                    dim_file = int(meta.get("dim", E.shape[1]))
                    if dim_file != int(E.shape[1]):
                        logger.error(
                            "tags NPZ v2 dim mismatch meta=%s vs TE=%s",
                            dim_file,
                            E.shape[1],
                        )
                        return None
                logger.info(
                    "keyword_filter: precomputed TAGS v2 loaded from %s (rows=%d, items=%d, D=%d)",
                    p,
                    E.shape[0],
                    len(tag_vecs_by_id),
                    E.shape[1],
                )
                return {
                    "ready": True,
                    "E": E,
                    "row_to_eid": [str(i) for i in ids],
                    "row_to_tag": [str(t or "") for t in tag_texts],
                    "row_to_text": [str(t or "") for t in texts],
                    "tag_vecs": tag_vecs_by_id,
                    "tags": tags_by_id,
                    "texts": texts_by_id,
                }

            # backward-compat v1: E[N,D], ids[N], texts[N]
            if "E" in files and "ids" in files and "texts" in files:
                E = z["E"].astype(np.float32, copy=False)
                ids = list(z["ids"].tolist())
                texts = list(z["texts"].tolist())
                row_tags = ["" for _ in range(len(ids))]
            elif "vecs" in files:
                vecs = z["vecs"].tolist()
                texts_map = z["texts"].tolist() if "texts" in files else {}
                tags_map = z["tags"].tolist() if "tags" in files else {}
                ids, texts, row_tags, rows = [], [], [], []
                if isinstance(vecs, dict):
                    for raw_id, raw_vecs in vecs.items():
                        eid = str(raw_id)
                        item_text = str((texts_map or {}).get(raw_id, "") if isinstance(texts_map, dict) else "")
                        item_tags = (tags_map or {}).get(raw_id, []) if isinstance(tags_map, dict) else []
                        arr = np.asarray(raw_vecs, dtype=np.float32)
                        if arr.ndim == 1 and arr.size > 0:
                            rows.append(arr.tolist())
                            ids.append(eid)
                            texts.append(item_text)
                            row_tags.append(str(item_tags[0]) if item_tags else "")
                            continue
                        raw_iter = raw_vecs if isinstance(raw_vecs, (list, tuple, np.ndarray)) else []
                        for i, raw_vec in enumerate(raw_iter):
                            rows.append([float(x) for x in raw_vec])
                            ids.append(eid)
                            texts.append(item_text)
                            row_tags.append(str(item_tags[i]) if i < len(item_tags) else "")
                E = np.asarray(rows, dtype=np.float32)
            elif "tag_vecs" in files:
                vecs = z["tag_vecs"].tolist()
                texts_map = z["texts"].tolist() if "texts" in files else {}
                tags_map = z["tags"].tolist() if "tags" in files else {}
                ids, texts, row_tags, rows = [], [], [], []
                if isinstance(vecs, dict):
                    for raw_id, raw_vecs in vecs.items():
                        eid = str(raw_id)
                        item_text = str((texts_map or {}).get(raw_id, "") if isinstance(texts_map, dict) else "")
                        item_tags = (tags_map or {}).get(raw_id, []) if isinstance(tags_map, dict) else []
                        arr = np.asarray(raw_vecs, dtype=np.float32)
                        if arr.ndim == 1 and arr.size > 0:
                            rows.append(arr.tolist())
                            ids.append(eid)
                            texts.append(item_text)
                            row_tags.append(str(item_tags[0]) if item_tags else "")
                            continue
                        raw_iter = raw_vecs if isinstance(raw_vecs, (list, tuple, np.ndarray)) else []
                        for i, raw_vec in enumerate(raw_iter):
                            rows.append([float(x) for x in raw_vec])
                            ids.append(eid)
                            texts.append(item_text)
                            row_tags.append(str(item_tags[i]) if i < len(item_tags) else "")
                E = np.asarray(rows, dtype=np.float32)
            else:
                logger.error("tags NPZ unsupported keys: %s", sorted(files))
                return None

        if E.ndim != 2:
            logger.error("tags NPZ E ndim mismatch: %s", E.shape)
            return None
        if len(ids) != E.shape[0] or len(texts) != E.shape[0]:
            logger.error(
                "tags NPZ meta size mismatch: ids/texts vs E: %d/%d vs %d",
                len(ids),
                len(texts),
                E.shape[0],
            )
            return None
        E = np.nan_to_num(E, nan=0.0, posinf=0.0, neginf=0.0).astype(
            np.float32, copy=False
        )
        norms = np.linalg.norm(E, axis=1, keepdims=True)
        norms[norms == 0.0] = 1.0
        E = E / norms
        tag_vecs_by_id: Dict[str, List[List[float]]] = {}
        texts_by_id: Dict[str, str] = {}
        for k, raw_id in enumerate(ids):
            eid = str(raw_id)
            tag_vecs_by_id.setdefault(eid, []).append(E[k].tolist())
            if eid not in texts_by_id:
                texts_by_id[eid] = str(texts[k] or "")
        if isinstance(meta, dict):
            dim_file = int(meta.get("dim", E.shape[1]))
            if dim_file != int(E.shape[1]):
                logger.error(
                    "tags NPZ dim mismatch meta=%s vs E=%s",
                    dim_file,
                    E.shape[1],
                )
                return None
        logger.info(
            "keyword_filter: precomputed TAGS loaded from %s (N=%d, D=%d)",
            p,
            E.shape[0],
            E.shape[1],
        )
        return {
            "ready": True,
            "E": E,
            "row_to_eid": [str(i) for i in ids],
            "row_to_tag": row_tags if 'row_tags' in locals() else ["" for _ in range(len(ids))],
            "row_to_text": [str(t or "") for t in texts],
            "tag_vecs": tag_vecs_by_id,
            "texts": texts_by_id,
        }
    except Exception:
        logger.exception(
            "keyword_filter: failed to load precomputed TAGS from %s", p
        )
        return None


def _load_precomputed_tags_index(model_file_name: str) -> Optional[Dict[str, Any]]:
    return _load_tags_index_from_npz(_tags_npz_path(model_file_name))


def _load_precomputed_tags_index_for_owner(
    owner_id: int, model_file_name: str
) -> Optional[Dict[str, Any]]:
    return _load_tags_index_from_npz(_owner_tags_npz_path(owner_id, model_file_name))


async def _ensure_index(model: Optional[str]) -> Dict[str, Any]:
    """System-wide TAGS index (from KNOWLEDGE_ON_FILE or global tags_embedded_*.npz)."""

    model_file_name = model or settings.EMBEDDING_MODEL
    key = f"sys::{model_file_name}"
    if key in _INDICES and _INDICES[key].get("ready"):
        return _INDICES[key]

    loop = asyncio.get_running_loop()
    pre_idx = await loop.run_in_executor(
        None, _load_precomputed_tags_index, model_file_name
    )
    if pre_idx:
        pre_idx["model"] = model_file_name
        _INDICES[key] = pre_idx
        return _INDICES[key]

    emb_model = model_file_name
    base_dir = Path(__file__).resolve().parent
    filename = settings.KNOWLEDGE_ON_FILE
    if not filename:
        _INDICES[key] = {
            "ready": True,
            "E": np.zeros((0, 0), dtype=np.float32),
            "row_to_eid": [],
            "row_to_tag": [],
            "row_to_text": [],
            "tag_vecs": {},
            "texts": {},
            "model": emb_model,
        }
        return _INDICES[key]

    items = _read_json_list(base_dir / filename)
    texts_by_id: Dict[str, str] = {}
    kw_by_id: Dict[str, List[str]] = {}
    for it in items:
        eid = str(it.get("id", "") or "")
        etext = str(it.get("text", "") or "")
        if not eid or not etext:
            continue
        texts_by_id[eid] = etext
        kws = _collect_keywords(it)
        if kws:
            kws = list(dict.fromkeys(kws))
        kw_by_id[eid] = kws

    all_kws: List[str] = []
    for lst in kw_by_id.values():
        all_kws.extend(lst)

    embedded_kws = await _embed_texts(all_kws, model=emb_model) if all_kws else []
    kw_vec_pairs = list(zip(all_kws, embedded_kws))

    tag_vecs_by_id: Dict[str, List[List[float]]] = {}
    tags_by_id: Dict[str, List[str]] = {}
    cursor = 0
    for eid, kws in kw_by_id.items():
        if not kws:
            continue
        item_vecs: List[List[float]] = []
        item_tags: List[str] = []
        for _ in kws:
            if cursor >= len(kw_vec_pairs):
                break
            k, v = kw_vec_pairs[cursor]
            cursor += 1
            item_vecs.append([float(x) for x in v])
            item_tags.append(k)
        if not item_vecs:
            continue
        tag_vecs_by_id[eid] = item_vecs
        tags_by_id[eid] = item_tags

    rows: List[List[float]] = []
    row_to_eid: List[str] = []
    row_to_tag: List[str] = []
    row_to_text: List[str] = []
    for eid, vecs in tag_vecs_by_id.items():
        tags = tags_by_id.get(eid) or []
        txt = texts_by_id.get(eid, "")
        for i, vec in enumerate(vecs):
            rows.append(vec)
            row_to_eid.append(eid)
            row_to_tag.append(str(tags[i]) if i < len(tags) else "")
            row_to_text.append(txt)

    _INDICES[key] = {
        "ready": True,
        "E": np.asarray(rows, dtype=np.float32),
        "row_to_eid": row_to_eid,
        "row_to_tag": row_to_tag,
        "row_to_text": row_to_text,
        "tag_vecs": tag_vecs_by_id,
        "tags": tags_by_id,
        "texts": texts_by_id,
        "model": emb_model,
    }
    logger.info(
        "keyword_filter: built index key=%s items=%d (fallback path)",
        key,
        len(tag_vecs_by_id),
    )
    return _INDICES[key]


async def _ensure_owner_index(owner_id: int, model: Optional[str]) -> Dict[str, Any]:
    """Per-API-key TAGS index from api_keys/<owner_id>/tags_embedded_*.npz."""
    owner_id_int = int(owner_id)
    model_file_name = model or settings.EMBEDDING_MODEL
    key = f"owner::{owner_id_int}::{model_file_name}"
    if key in _INDICES and _INDICES[key].get("ready"):
        return _INDICES[key]
    loop = asyncio.get_running_loop()
    pre_idx = await loop.run_in_executor(
        None, _load_precomputed_tags_index_for_owner, owner_id_int, model_file_name
    )
    if pre_idx:
        pre_idx["model"] = model_file_name
        _INDICES[key] = pre_idx
        return _INDICES[key]

    idx: Dict[str, Any] = {
        "ready": True,
        "E": np.zeros((0, 0), dtype=np.float32),
        "row_to_eid": [],
        "row_to_tag": [],
        "row_to_text": [],
        "tag_vecs": {},
        "texts": {},
        "model": model_file_name,
    }
    _INDICES[key] = idx
    logger.info(
        "keyword_filter: no per-owner TAGS index for owner_id=%s model=%s",
        owner_id_int,
        model_file_name,
    )
    return idx


def invalidate_tags_index(owner_id: Optional[int] = None) -> None:
    """Инвалидация кеша TAGS-индексов.

    Если owner_id is None – чистим всё.
    Если задан owner_id – чистим только per-key индексы этого owner.
    """
    if owner_id is None:
        _INDICES.clear()
        logger.info("keyword_filter: TAGS index cache fully invalidated")
        return

    owner_id_int = int(owner_id)
    prefix = f"owner::{owner_id_int}::"
    removed = 0
    for k in list(_INDICES.keys()):
        if k.startswith(prefix):
            _INDICES.pop(k, None)
            removed += 1
    logger.info(
        "keyword_filter: TAGS index cache invalidated for owner_id=%s entries=%d",
        owner_id_int,
        removed,
    )


async def find_tag_hits(
    text: str,
    *,
    model: Optional[str] = None,
    limit: Optional[int] = None,
    owner_id: Optional[int] = None,
    query_embedding: Optional[List[float]] = None,
    embedding_model: Optional[str] = None,
) -> List[Tuple[float, str, str]]:
    t_total0 = time.perf_counter()

    t = _norm_ws(text)
    if not t:
        return []

    t_load0 = time.perf_counter()
    sys_idx = await _ensure_index(model)
    indices: List[Tuple[str, Optional[int], Dict[str, Any]]] = [("sys", None, sys_idx)]

    owner_id_int: Optional[int] = None
    if owner_id is not None:
        try:
            owner_id_int = int(owner_id)
        except (TypeError, ValueError):
            owner_id_int = None

    if owner_id_int and owner_id_int > 0:
        owner_idx = await _ensure_owner_index(owner_id_int, model)
        indices.append(("owner", owner_id_int, owner_idx))

    union_texts_by_id: Dict[str, str] = {}
    has_any_rows = False

    for scope, oid, idx in indices:
        E = idx.get("E")
        row_to_eid = idx.get("row_to_eid") or []
        row_to_text = idx.get("row_to_text") or []
        if isinstance(E, np.ndarray) and E.size > 0 and row_to_eid:
            has_any_rows = True
            for row_idx, eid in enumerate(row_to_eid):
                rid = f"{oid}:{eid}" if (scope == "owner" and oid is not None) else str(eid)
                if rid not in union_texts_by_id:
                    union_texts_by_id[rid] = (
                        str(row_to_text[row_idx]) if row_idx < len(row_to_text) else ""
                    )
            continue

        tag_vecs_by_id = idx.get("tag_vecs") or {}
        if not tag_vecs_by_id:
            legacy_vecs = idx.get("vecs") or {}
            tag_vecs_by_id = {k: [v] for k, v in legacy_vecs.items()}
        if not tag_vecs_by_id:
            continue
        has_any_rows = True
        texts_by_id = idx.get("texts") or {}
        for eid in tag_vecs_by_id.keys():
            rid = f"{oid}:{eid}" if (scope == "owner" and oid is not None) else str(eid)
            if rid not in union_texts_by_id:
                union_texts_by_id[rid] = str(texts_by_id.get(eid, "") or "")

    if not has_any_rows:
        return []

    try:
        base_thr = settings.RELEVANCE_THRESHOLD
    except Exception:
        base_thr = 0.28

    kw_thr: Optional[float]
    try:
        kw_thr = settings.KEYWORD_RELEVANCE_THRESHOLD
        kw_thr = float(kw_thr) if kw_thr is not None else None
    except Exception:
        kw_thr = None
    if kw_thr is None:
        try:
            margin = settings.RELEVANCE_MARGIN
        except Exception:
            margin = 0.07
        kw_thr = base_thr + margin

    try:
        lam = settings.MMR_LAMBDA
    except Exception:
        lam = 0.50
    lam = max(0.0, min(1.0, lam))

    qv_by_model: Dict[str, List[float]] = {}
    models_needed: set[str] = set()
    for _, _, idx in indices:
        E = idx.get("E")
        has_rows = isinstance(E, np.ndarray) and E.size > 0
        if not has_rows:
            has_rows = bool(idx.get("tag_vecs") or idx.get("vecs"))
        if not has_rows:
            continue
        emb_model = idx.get("model") or (model or settings.EMBEDDING_MODEL)
        models_needed.add(emb_model)

    precomputed_qv: Optional[List[float]] = None
    if query_embedding is not None:
        try:
            precomputed_qv = _l2_normalize([float(x) for x in query_embedding])
        except Exception:
            precomputed_qv = None

    for emb_model in models_needed:
        if precomputed_qv is not None:
            if embedding_model and emb_model != embedding_model:
                qv_by_model[emb_model] = (await _embed_texts([t], model=emb_model))[0]
            else:
                qv_by_model[emb_model] = precomputed_qv
            continue
        qv_by_model[emb_model] = (await _embed_texts([t], model=emb_model))[0]
    load_elapsed = time.perf_counter() - t_load0

    scores_by_id: Dict[str, float] = {}
    rid_model: Dict[str, str] = {}
    best_tag_vec_by_id: Dict[str, List[float]] = {}
    best_tag_name_by_id: Dict[str, str] = {}

    t_score0 = time.perf_counter()
    total_rows = 0
    for scope, oid, idx in indices:
        E = idx.get("E")
        row_to_eid = idx.get("row_to_eid") or []
        row_to_tag = idx.get("row_to_tag") or []
        row_to_text = idx.get("row_to_text") or []
        if not isinstance(E, np.ndarray):
            tag_vecs_by_id = idx.get("tag_vecs") or {}
            if not tag_vecs_by_id:
                tag_vecs_by_id = {k: [v] for k, v in (idx.get("vecs") or {}).items()}
            rows: List[List[float]] = []
            row_to_eid = []
            row_to_tag = []
            row_to_text = []
            texts_map = idx.get("texts") or {}
            tags_map = idx.get("tags") or {}
            for eid, vecs in tag_vecs_by_id.items():
                tags = tags_map.get(eid) or []
                txt = str(texts_map.get(eid, "") or "")
                for i, vec in enumerate(vecs):
                    rows.append([float(x) for x in vec])
                    row_to_eid.append(str(eid))
                    row_to_tag.append(str(tags[i]) if i < len(tags) else "")
                    row_to_text.append(txt)
            E = np.asarray(rows, dtype=np.float32)
        if E.size == 0:
            continue
        emb_model = idx.get("model") or (model or settings.EMBEDDING_MODEL)
        qv = qv_by_model[emb_model]
        total_rows += int(E.shape[0])
        qv_arr = np.asarray(qv, dtype=np.float32)
        dim = min(int(E.shape[1]), int(qv_arr.shape[0])) if qv_arr.ndim == 1 else 0
        if dim <= 0:
            continue
        scores = E[:, :dim] @ qv_arr[:dim]
        for row_idx, score in enumerate(scores.tolist()):
            if score < kw_thr:
                continue
            eid = str(row_to_eid[row_idx]) if row_idx < len(row_to_eid) else ""
            if not eid:
                continue
            rid = f"{oid}:{eid}" if (scope == "owner" and oid is not None) else eid
            if rid in rid_model and rid_model[rid] != emb_model:
                continue
            rid_model[rid] = emb_model
            prev = scores_by_id.get(rid)
            if prev is None or score > prev:
                scores_by_id[rid] = float(score)
                best_tag_vec_by_id[rid] = E[row_idx].tolist()
                best_tag_name_by_id[rid] = (
                    str(row_to_tag[row_idx]) if row_idx < len(row_to_tag) else ""
                )
                if rid not in union_texts_by_id:
                    union_texts_by_id[rid] = (
                        str(row_to_text[row_idx]) if row_idx < len(row_to_text) else ""
                    )
    score_elapsed = time.perf_counter() - t_score0

    if not scores_by_id:
        logger.info(
            "keyword_filter: hits=0 (kw_thr=%.3f, base_thr=%.3f, owner_id=%s)",
            kw_thr,
            base_thr,
            owner_id_int,
        )
        return []

    try:
        top_k = (
            int(limit)
            if (isinstance(limit, int) and limit > 0)
            else settings.KNOWLEDGE_TOP_K
            or 3
        )
    except Exception:
        top_k = 3

    cand_ids = sorted(scores_by_id.keys(), key=lambda i: scores_by_id[i], reverse=True)
    top_n = MMR_CANDIDATES_TOP_N
    cand_ids = cand_ids[:top_n]
    t_mmr0 = time.perf_counter()
    picked_ids = _mmr_select_ids(
        cand_ids, best_tag_vec_by_id, scores_by_id, top_k=top_k, lam=lam
    )
    mmr_elapsed = time.perf_counter() - t_mmr0

    results: List[Tuple[float, str, str]] = [
        (scores_by_id[i], i, union_texts_by_id.get(i, "")) for i in picked_ids
    ]
    total_elapsed = time.perf_counter() - t_total0
    logger.info(
        "keyword_filter: embedding rows=%d candidates=%d topN=%d returned=%d timings(load=%.3fs score=%.3fs mmr=%.3fs total=%.3fs) (kw_thr=%.3f, λ=%.2f, owner_id=%s)",
        total_rows,
        len(scores_by_id),
        min(top_n, len(scores_by_id)),
        len(results),
        load_elapsed,
        score_elapsed,
        mmr_elapsed,
        total_elapsed,
        kw_thr,
        lam,
        owner_id_int,
    )
    if results:
        logger.debug(
            "keyword_filter: best tags selected=%s",
            {rid: best_tag_name_by_id.get(rid, "") for _, rid, _ in results},
        )
    return results
