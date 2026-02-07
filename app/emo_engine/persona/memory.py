#app/emo_engine/persona/memory.py
from __future__ import annotations

import json
import time
import math
import base64
import random
import hashlib
import unicodedata
import re
import os
import asyncio
import logging
import numpy as np
import redis.exceptions
import atexit
import contextlib

from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from typing import List, Tuple, Dict, Optional
from dateparser import parse as dp_parse
from dateutil.parser import isoparse
from dateutil.tz import UTC

from redis.commands.search.field import (
    TextField, NumericField, TagField, VectorField
)
try:
    from redis.commands.search.indexDefinition import IndexDefinition, IndexType
except Exception:
    from redis.commands.search.index_definition import IndexDefinition, IndexType
from redis.commands.search.query import Query
from redis.exceptions import ResponseError

try:
    from jsonschema import Draft7Validator, ValidationError as JSONSchemaError
except Exception:
    Draft7Validator = None
    class JSONSchemaError(Exception): ...

from app.core.memory import (
    USER_KEYS_REGISTRY_TTL,
    _register_user_key,
    get_redis,
    get_redis_vector,
)
from app.clients.openai_client import _call_openai_with_retry, _get_output_text
from app.config import settings


logger = logging.getLogger(__name__)

_TOPIC_LOGGED = False
_EVENT_FRAME_SEM = asyncio.Semaphore(int(getattr(settings, "EVENT_FRAME_MAX_CONCURRENCY", 2)))
_EMOTION_WEIGHT = float(getattr(settings, "EMOTION_WEIGHT", 0.55))
_RECENCY_WEIGHT = float(getattr(settings, "RECENCY_WEIGHT", 0.35))
_FORGET_SALIENCE_WEIGHT = float(getattr(settings, "FORGET_SALIENCE_WEIGHT", 0.12))
_DUP_DIST_MAX = float(getattr(settings, "DUPLICATE_DISTANCE_MAX", 0.18))
_MIN_SIMILARITY = float(getattr(settings, "MIN_MEMORY_SIMILARITY", 0.7))
_DP_EXECUTOR = ThreadPoolExecutor(max_workers=8)
try:
    atexit.register(lambda: _DP_EXECUTOR.shutdown(wait=False))
except Exception:
    pass
_DIM = int(getattr(settings,"EMBED_DIM",3072))
_BG_BATCH_MAX = int(getattr(settings, "BG_BATCH_MAX", 32))
_BG_BATCH_WAIT_MS = int(getattr(settings, "BG_BATCH_WAIT_MS", 25))
_MEMTXT_SEEN_TTL = int(getattr(settings, "MEMTXT_SEEN_TTL", 86400))
_MEMTXT_SEEN_SCOPE = str(getattr(settings, "MEMTXT_SEEN_SCOPE", "chat")).lower()
_SALIENCE_REINFORCE_STEP = float(getattr(settings, "MEMORY_SALIENCE_REINFORCE_STEP", 0.02))
_MEMTXT_MAX_PER_UID = int(getattr(settings, "MEMTXT_MAX_PER_UID", 150))
_MEMTXT_MAX_PER_CHAT = int(getattr(settings, "MEMTXT_MAX_PER_CHAT", 500))
_FORGET_ATTACHMENT_WEIGHT = float(getattr(settings, "FORGET_ATTACHMENT_WEIGHT", 0.06))
_DEDUP_EVENTTIME_MIN_SHIFT = float(getattr(settings, "DEDUP_EVENTTIME_MIN_SHIFT", 300.0))
_RERANK_ENABLE = bool(getattr(settings, "MEMORY_RERANK_ENABLE", True))
_RERANK_W_SALIENCE = float(getattr(settings, "RERANK_W_SALIENCE", 0.20))
_RERANK_W_RECENCY = float(getattr(settings, "RERANK_W_RECENCY", 0.15))
_RERANK_W_ATTACHMENT = float(getattr(settings, "RERANK_W_ATTACHMENT", 0.10))
_RERANK_W_USE = float(getattr(settings, "RERANK_W_USECOUNT", 0.08))
_RERANK_W_LASTUSED = float(getattr(settings, "RERANK_W_LASTUSED", 0.08))
_RERANK_LAST_USED_TAU = float(getattr(settings, "RERANK_LAST_USED_TAU", 7*86400))
_MEMTXT_PER_UID_CLEAN_MAXSETS = int(getattr(settings, "MEMTXT_PER_UID_CLEAN_MAXSETS", 200))
_FT_TIMEOUT = float(getattr(settings, "REDISSEARCH_TIMEOUT", 3))
_SIM_FROM_DIST = str(getattr(settings, "SIM_FROM_DIST", "one_minus_half")).lower()

async def _register_persona_user_key(redis, uid: int, key: str) -> None:
    """Register per-uid memory/memtxt keys for delete_user_redis_data cleanup."""
    if uid is None:
        return
    await _register_user_key(redis, int(uid), key, USER_KEYS_REGISTRY_TTL)

_REL_UNIT_SEC = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "с": 1, "сек": 1, "секунда": 1, "секунды": 1, "секунд": 1, "секунду": 1,
    "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600,
    "day": 86400, "days": 86400,
    "week": 604800, "weeks": 604800,
    "month": 2592000, "months": 2592000,
    "year": 31536000, "years": 31536000,
    "м": 60, "мин": 60, "минут": 60, "минуты": 60, "минуту": 60,
    "ч": 3600, "час": 3600, "часа": 3600, "часов": 3600,
    "день": 86400, "дня": 86400, "дней": 86400, "сутки": 86400,
    "неделя": 604800, "недели": 604800, "недель": 604800,
    "месяц": 2592000, "месяца": 2592000, "месяцев": 2592000,
    "год": 31536000, "года": 31536000, "лет": 31536000,
}

def _b2s(v, default: str = "") -> str:
    if isinstance(v, (bytes, bytearray)):
        try:
            return v.decode()
        except Exception:
            return default
    return v if isinstance(v, str) else default

def _to_float(v, default: float = 0.0) -> float:
    try:
        if isinstance(v, (bytes, bytearray)):
            v = v.decode()
        return float(v)
    except Exception:
        return default

def _to_int(v, default: int = 0) -> int:
    try:
        if isinstance(v, (bytes, bytearray)):
            v = v.decode()
        return int(v)
    except Exception:
        return default

_ZERO_VEC: bytes = np.zeros(_DIM, dtype=np.float32).tobytes()

def _rerank_score(
    sim: float,
    *,
    now_ts: float,
    event_time: float | None,
    ts: float | None,
    salience: float,
    attachment: float,
    use_count: int,
    last_used_ts: float,
    consolidation_age: float = float(getattr(settings, "CONSOLIDATION_AGE", 7*86400)),
    w_sali: float = _RERANK_W_SALIENCE,
    w_rec: float = _RERANK_W_RECENCY,
    w_att: float = _RERANK_W_ATTACHMENT,
    w_use: float = _RERANK_W_USE,
    w_lu: float = _RERANK_W_LASTUSED,
    last_used_tau: float = _RERANK_LAST_USED_TAU,
) -> float:
    ev = event_time if (isinstance(event_time, (int, float)) and math.isfinite(event_time)) else (
        ts if (isinstance(ts, (int, float)) and math.isfinite(ts)) else now_ts
    )
    rec_boost = math.exp(-max(0.0, now_ts - ev) / max(1.0, consolidation_age))
    lu_boost = math.exp(-max(0.0, now_ts - last_used_ts) / max(1.0, last_used_tau)) if last_used_ts > 0 else 0.0
    use_boost = math.log1p(max(0, int(use_count))) / math.log1p(10)
    return sim * (1.0 + w_sali * salience + w_rec * rec_boost + w_att * attachment + w_use * use_boost + w_lu * lu_boost)

def _is_zero_embedding(raw: bytes | bytearray | None, expected_dim: int = _DIM) -> bool:
    if not raw or not isinstance(raw, (bytes, bytearray)) or len(raw) != expected_dim * 4:
        return True
    try:
        return not np.frombuffer(raw, dtype=np.float32, count=expected_dim).any()
    except Exception:
        return True

def _pool_occupancy(rds) -> float:
    try:
        client = getattr(rds, "_client", rds)
        pool = getattr(client, "connection_pool", None)
        if not pool:
            return 0.0
        in_use = getattr(pool, "_in_use_connections", None)
        max_conn = getattr(pool, "max_connections", None)
        if isinstance(in_use, (list, set, tuple)):
            in_use_count = float(len(in_use))
        elif in_use is None:
            in_use_count = 0.0
        else:
            in_use_count = float(in_use)
        if not max_conn:
            return 0.0
        return in_use_count / max(1, int(max_conn))
    except Exception as e:
        logger.debug("pool occupancy unavailable: %s", e)
        return 0.0

def _norm_text_for_embed(text: str) -> str:
    t = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text or "")).strip()
    try:
        max_chars = int(getattr(settings, "EMBED_MAX_CHARS", 0) or 0)
    except Exception:
        max_chars = 0
    return t[:max_chars] if max_chars > 0 and len(t) > max_chars else t

def _topic_tagify(w: str) -> str:
    s = unicodedata.normalize("NFKC", (w or "").strip().lower())
    s = re.sub(r"[,\{\}\|\\]+", " ", s)
    s = re.sub(r"[^0-9a-z\u0400-\u04FF]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    try:
        max_len = int(getattr(settings, "TOPIC_TAG_MAXLEN", 40))
    except Exception:
        max_len = 40
    return (s[:max_len].strip("_")) if len(s) > max_len else s

def _is_missing_index_error(exc: Exception) -> bool:

    msg = str(exc).lower()
    return any(
        s in msg
        for s in (
            "no such index",
            "unknown index",
            "unknown index name",
            "index does not exist",
            "index not found"
        )
    )

def _tag_literal(s: str) -> str:
    if not s:
        return '""'
    s = (s or "")
    s = (s.replace("\\", "\\\\")
           .replace('"', r'\"')
           .replace("|", r"\|")
           .replace(",", r"\,")
           .replace("{", r"\{")
           .replace("}", r"\}"))
    needs_quotes = (
        any(ch.isspace() for ch in s)
        or bool(re.fullmatch(r"[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", s))
    )
    return f'"{s}"' if needs_quotes else s

def _norm_text_key(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"\s+", " ", s).strip().lower()

def _fts_escape(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r'([+\-\=<>\~"@{}\[\]\^\*\(\)\!:\|\\\/\?\.])', r'\\\1', s)

def _doc_str(v) -> str:
    if isinstance(v, (bytes, bytearray)):
        try:
            return v.decode()
        except Exception:
            return str(v)
    return v if isinstance(v, str) else str(v)

def _doc_id(d) -> str:
    did = getattr(d, "id", "")
    return _doc_str(did)

def _filter_knn_docs(docs, *, want_chat: str, want_uid: Optional[str], topic_hint: Optional[str] = None, want_event_type: Optional[str] = None):
    th = _topic_tagify(topic_hint) if topic_hint else None
    out = []
    for d in docs or []:
        # chat
        ch = _doc_str(getattr(d, "chat", None))
        if ch in ("None", "", "null", "NoneType", None):
            if not _doc_id(d).startswith(f"memory:{want_chat}:"):
                continue
        elif ch != want_chat:
            continue
        if want_uid is not None:
            u = _doc_str(getattr(d, "uid", ""))
            if u != want_uid:
                continue
        if th:
            tp = _doc_str(getattr(d, "topic", ""))
            if th not in (tp.split(",") if tp else []):
                continue
        if want_event_type is not None:
            ev = _doc_str(getattr(d, "event_type", ""))
            if ev != want_event_type:
                continue
        out.append(d)
    return out

def _fallback_rel(text: str, now_ts: float) -> Optional[float]:
    t = (text or "").lower()
    base = datetime.fromtimestamp(now_ts, tz=UTC)

    if re.search(r"\b(today|сегодня)\b", t):
        return now_ts
    if re.search(r"\b(tomorrow|завтра)\b", t):
        return (base + timedelta(days=1)).timestamp()
    if re.search(r"\b(yesterday|вчера)\b", t):
        return (base - timedelta(days=1)).timestamp()
    if re.search(r"(day\s+after\s+tomorrow|послезавтра)", t):
        return (base + timedelta(days=2)).timestamp()
    if re.search(r"(day\s+before\s+yesterday|позавчера)", t):
        return (base - timedelta(days=2)).timestamp()

    m = re.search(r"\b(?:in|через)\s+(\d+)\s+([a-zA-Zа-яА-Я]+)", t)
    if m:
        try:
            n = int(m.group(1))
        except Exception:
            n = None
        unit = m.group(2)
        sec = _REL_UNIT_SEC.get(unit) or _REL_UNIT_SEC.get(unit.rstrip("s"))
        if n is not None and sec:
            return (base + timedelta(seconds=n * sec)).timestamp()

    if re.search(r"(half an hour|полчаса|пол часа)", t):
        return (base + timedelta(minutes=30)).timestamp()

    if re.search(r"(next week|следующ\w+\s+недел\w+)", t):
        return (base + timedelta(weeks=1)).timestamp()
    if re.search(r"(next month|следующ\w+\s+месяц\w*)", t):
        return (base + timedelta(days=30)).timestamp()
    if re.search(r"(next year|следующ\w+\s+год\w*)", t):
        return (base + timedelta(days=365)).timestamp()
    return None

def _dist_to_sim(d: float) -> float:
    
    try:
        v = float(d)
    except Exception:
        return 0.0
    if not math.isfinite(v) or v < 0.0:
        return 0.0
    if _SIM_FROM_DIST == "one_minus":
        sim = 1.0 - v
    else:
        sim = 1.0 - (v / 2.0)
    if sim < 0.0:
        sim = 0.0
    if sim > 1.0:
        sim = 1.0
    return sim

def _build_index_fields(initial_cap: int, dim: Optional[int] = None, use_initial_cap: bool = True):

    dim = int(dim or _DIM)

    try:
        m_val = int(getattr(settings, "HNSW_M", 24))
    except Exception:
        m_val = 24
    try:
        efc_val = int(getattr(settings, "HNSW_EF_CONSTRUCTION", 400))
    except Exception:
        efc_val = 400
    vec_opts = {
        "TYPE":            "FLOAT32",
        "DIM":             dim,
        "DISTANCE_METRIC": "COSINE",
        "M":               m_val,
        "EF_CONSTRUCTION": efc_val,
    }
    if use_initial_cap and int(initial_cap) > 0:
        vec_opts["INITIAL_CAP"] = int(initial_cap)

    _text_field_vec = TextField("text", no_stem=True, no_index=True, sortable=True)

    return [
        _text_field_vec,
        NumericField("ts", sortable=True),
        NumericField("event_time", sortable=True),
        TagField("event_type"),
        TagField("topic"),
        TagField("chat"),
        TagField("uid"),
        NumericField("salience", sortable=True),
        NumericField("arousal", sortable=True),
        NumericField("valence", sortable=True),
        NumericField("stress", sortable=True),
        NumericField("attachment", sortable=True),
        NumericField("use_count", sortable=True),
        NumericField("last_used_ts", sortable=True),
        VectorField("embedding", "HNSW", vec_opts),
    ]

def _build_text_index_fields():
    index_text = bool(getattr(settings, "MEMTXT_TEXT_INDEXED", True))

    if index_text:
        _text_field_txt = TextField("text", no_stem=True)
    else:
        _text_field_txt = TextField("text", no_stem=True, no_index=True, sortable=True)

    fields = [_text_field_txt]
    fields += [
        NumericField("ts", sortable=True),
        NumericField("event_time", sortable=True),
        TagField("event_type"),
        TagField("topic"),
        TagField("chat"),
        TagField("uid"),
    ]
    return fields

def _event_frame_schema() -> dict:

    lower_token_pattern = r"^[a-z0-9][a-z0-9\- ]{0,29}$"
    tag_pattern        = r"^[a-z0-9][a-z0-9\- ]{0,29}$"
    iso_z_pattern      = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"

    short_str = {"type": "string", "minLength": 1, "maxLength": 40}

    return {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "minLength": 1,
                "maxLength": 30,
                "pattern": lower_token_pattern
            },
            "when_iso": {
                "type": "string",
                "anyOf": [
                    {"const": ""},
                    {
                        "pattern": iso_z_pattern,
                        "minLength": 20,
                        "maxLength": 20
                    }
                ]
            },
            "tense": {"type": "string", "enum": ["past", "present", "future"]},
            "participants": {
                "type": "array",
                "uniqueItems": True,
                "minItems": 0,
                "maxItems": 6,
                "items": short_str
            },
            "intent": {
                "type": "array",
                "uniqueItems": True,
                "minItems": 0,
                "maxItems": 2,
                "items": {"type": "string", "minLength": 1, "maxLength": 40}
            },
            "commitments": {
                "type": "array",
                "uniqueItems": True,
                "minItems": 0,
                "maxItems": 5,
                "items": {"type": "string", "minLength": 1, "maxLength": 40}
            },
            "places": {
                "type": "array",
                "uniqueItems": True,
                "minItems": 0,
                "maxItems": 5,
                "items": short_str
            },
            "tags": {
                "type": "array",
                "uniqueItems": True,
                "minItems": 0,
                "maxItems": 5,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 30,
                    "pattern": tag_pattern
                }
            }
        },
        "additionalProperties": False,
        "required": ["type", "when_iso", "tense", "participants", "intent", "commitments", "places", "tags"]
    }

def _is_valid_event_frame(obj: dict) -> bool:

    if not isinstance(obj, dict):
        return False
    if Draft7Validator is None:
        return True
    try:
        Draft7Validator(_event_frame_schema()).validate(obj)
        return True
    except JSONSchemaError:
        return False

async def get_embedding(text: str) -> bytes:
    
    rds = get_redis()
    text_norm = _norm_text_for_embed(text)

    min_len = int(getattr(settings, "EMBED_MIN_LEN", 1))
    if len(text_norm) < max(1, min_len):
        return _ZERO_VEC

    model_for_key = str(getattr(settings, "EMBEDDING_MODEL", "")).strip()
    md5_key = (f"emb:{model_for_key}:{_DIM}:" + hashlib.md5(text_norm.encode("utf-8")).hexdigest())
    if cached := await rds.get(md5_key):
        try:
            raw = base64.b64decode(cached)
            if len(raw) == _DIM * 4:
                return raw
            else:
                logger.warning(
                    "get_embedding: cached vector size mismatch (%d != %d*4). Drop & recompute.",
                    len(raw), _DIM
                )
                try:
                    await rds.delete(md5_key)
                except Exception:
                    pass
        except Exception:
            logger.warning("get_embedding: cached embedding decode failed, drop & recompute")
            try:
                await rds.delete(md5_key)
            except Exception:
                pass

    if _BG_BATCH_MAX > 1:
        return await _EMBED_BATCHER.add(text)
    else:
        try:
            model = settings.EMBEDDING_MODEL
            if not model:
                logger.error("EMBEDDING_MODEL is not configured; returning zero vector")
                return _ZERO_VEC
            timeout_s = settings.EMBEDDING_TIMEOUT
            _t0 = time.perf_counter()
            try:
                resp = await asyncio.wait_for(
                    _call_openai_with_retry(
                        endpoint="embeddings.create",
                        input=text_norm,
                        model=model,
                        encoding_format="float"
                    ),
                    timeout=timeout_s,
                )
                _dt_ms = (time.perf_counter() - _t0) * 1000.0
                logger.info("openai_timing: embeddings.create ok t=%.1fms model=%s chars=%d",
                            _dt_ms, model, len(text_norm))
            except asyncio.TimeoutError:
                _dt_ms = (time.perf_counter() - _t0) * 1000.0
                logger.warning("openai_timing: embeddings.create TIMEOUT t=%.1fms model=%s chars=%d",
                               _dt_ms, model, len(text_norm))
                return _ZERO_VEC
        except Exception as e:
            _dt_ms = (time.perf_counter() - _t0) * 1000.0 if "_t0" in locals() else -1.0
            logger.warning("openai_timing: embeddings.create FAIL t=%.1fms model=%s chars=%d err=%s",
                           _dt_ms, (model if "model" in locals() else None), len(text_norm), e)
            logger.warning("get_embedding: OpenAI embed failed, returning zeros: %s", e)
            return _ZERO_VEC

    try:
        vec = resp.data[0].embedding
    except Exception as e:
        _dt_ms = (time.perf_counter() - _t0) * 1000.0 if '_t0' in locals() else -1.0
        logger.warning(
            "openai_timing: embeddings.create FAIL t=%.1fms model=%s chars=%d err=%s",
            _dt_ms, (model if 'model' in locals() else None), len(text_norm), e
        )
        logger.warning("get_embedding: OpenAI embed failed, returning zeros: %s", e)
        return _ZERO_VEC

    if isinstance(vec, str):
        try:
            raw = base64.b64decode(vec)
            arr = np.frombuffer(raw, dtype=np.float32)
        except Exception as de:
            logger.warning("get_embedding: got base64 embedding, decode failed: %s", de)
            arr = np.zeros(_DIM, dtype=np.float32)
    else:
        try:
            arr = np.asarray(vec, dtype=np.float32)
            strict_dim = bool(getattr(settings, "EMBED_STRICT_DIM", True))
            if arr.shape[0] != _DIM:
                logger.error("get_embedding: embedding dim=%d != EMBED_DIM=%d (model=%s, strict=%s). Check model/index settings.",
                             arr.shape[0], _DIM, getattr(settings, "EMBEDDING_MODEL", None), strict_dim)
                if strict_dim:
                    raise RuntimeError(f"EMBED_DIM mismatch: got {arr.shape[0]} from model, expected {_DIM}")
        except Exception as ce:
            logger.warning("get_embedding: cast to float32 failed: %s", ce)
            arr = np.zeros(_DIM, dtype=np.float32)

    if arr.shape[0] != _DIM:
        logger.warning("get_embedding: _DIM mismatch %d vs %d", arr.shape[0], _DIM)
        if not bool(getattr(settings, "EMBED_STRICT_DIM", True)):
            if arr.shape[0] > _DIM:
                arr = arr[:_DIM]
            else:
                arr = np.pad(arr, (0, _DIM - arr.shape[0])).astype(np.float32)
        else:
            arr = np.zeros(_DIM, dtype=np.float32)
    try:
        norm = float(np.linalg.norm(arr))
        if norm > 0.0:
            arr = arr / norm
    except Exception:
        pass
    arr = arr.tobytes()
    try:
        if not _is_zero_embedding(arr):
            ttl = int(86400 * (0.9 + 0.2 * random.random()))
            await rds.set(md5_key, base64.b64encode(arr).decode("ascii"), ex=ttl)
    except redis.exceptions.RedisError as e:
        logger.warning("get_embedding: Redis cache store failed: %s", e)
    return arr


class _EmbedBatcher:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._queue = []
        self._flush_task = None

    async def add(self, text: str) -> bytes:
        norm = _norm_text_for_embed(text)
        min_len = int(getattr(settings, "EMBED_MIN_LEN", 1))
        if len(norm) < max(1, min_len):
            return _ZERO_VEC
        model = getattr(settings, "EMBEDDING_MODEL", None)
        if not model:
            logger.error("EMBEDDING_MODEL is not configured; returning zero vector")
            return _ZERO_VEC
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        async with self._lock:
            self._queue.append((norm, fut))
            if len(self._queue) >= _BG_BATCH_MAX:
                await self._flush_locked()
            elif not self._flush_task:
                self._flush_task = loop.create_task(self._delayed_flush())
        return await fut

    async def _delayed_flush(self):
        try:
            await asyncio.sleep(_BG_BATCH_WAIT_MS / 1000)
            async with self._lock:
                await self._flush_locked()
        finally:
            self._flush_task = None

    async def _flush_locked(self):
        if not self._queue:
            return
        batch = self._queue[:_BG_BATCH_MAX]
        self._queue = self._queue[_BG_BATCH_MAX:]
        uniq_map: dict[str, dict] = {}
        uniq_list: list[str] = []
        key_order: list[str] = []
        for text_norm, fut in batch:
            model_for_key = str(getattr(settings, "EMBEDDING_MODEL", "")).strip()
            cache_key = (
                f"emb:{model_for_key}:{_DIM}:"
                + hashlib.md5(text_norm.encode("utf-8")).hexdigest()
            )
            if cache_key in uniq_map:
                uniq_map[cache_key]["futs"].append(fut)
            else:
                uniq_map[cache_key] = {"text": text_norm, "futs": [fut]}
                uniq_list.append(text_norm)
                key_order.append(cache_key)
        try:
            rds = get_redis()
            cached = await rds.mget(key_order) if key_order else []
            miss_keys = []
            miss_texts = []
            if cached:
                for i, b64 in enumerate(cached):
                    if not b64:
                        miss_keys.append(key_order[i])
                        miss_texts.append(uniq_list[i])
                        continue
                    try:
                        raw = base64.b64decode(b64)
                        if len(raw) != _DIM * 4:
                            try:
                                await rds.delete(key_order[i])
                            except Exception:
                                pass
                            miss_keys.append(key_order[i])
                            miss_texts.append(uniq_list[i])
                            continue
                    except Exception:
                        try:
                            await rds.delete(key_order[i])
                        except Exception:
                            pass
                        miss_keys.append(key_order[i])
                        miss_texts.append(uniq_list[i])
                        continue
                    for fut in uniq_map[key_order[i]]["futs"]:
                        if not fut.done():
                            fut.set_result(raw)
            else:
                miss_keys = key_order[:]
                miss_texts = uniq_list[:]

            items = []
            if miss_texts:
                model = settings.EMBEDDING_MODEL
                timeout_s = settings.EMBEDDING_TIMEOUT
                if not model:
                    logger.error("EMBEDDING_MODEL is not configured; returning zero embeddings for %d items", len(miss_texts))
                    items = []
                else:
                    _t0 = time.perf_counter()
                    try:
                        resp = await asyncio.wait_for(
                            _call_openai_with_retry(
                                endpoint="embeddings.create",
                                input=miss_texts,
                                model=model,
                                encoding_format="float"
                            ),
                            timeout=timeout_s,
                        )
                        items = getattr(resp, "data", []) or []
                        _dt_ms = (time.perf_counter() - _t0) * 1000.0
                        logger.info("openai_timing: embeddings.create batch ok t=%.1fms model=%s batch=%d",
                                    _dt_ms, model, len(miss_texts))
                    except asyncio.TimeoutError:
                        _dt_ms = (time.perf_counter() - _t0) * 1000.0
                        logger.warning("openai_timing: embeddings.create batch TIMEOUT t=%.1fms model=%s batch=%d",
                                       _dt_ms, model, len(miss_texts))
                        items = []
                    except Exception as e:
                        _dt_ms = (time.perf_counter() - _t0) * 1000.0
                        logger.warning("openai_timing: embeddings.create batch FAIL t=%.1fms model=%s batch=%d err=%s",
                                       _dt_ms, model, len(miss_texts), e)
                        items = []

            n_items = len(items)
            to_cache = []
            for i, (cache_key, text) in enumerate(zip(miss_keys, miss_texts)):
                futs = uniq_map.get(cache_key, {}).get("futs", [])
                try:
                    if i < n_items:
                        vec = items[i].embedding
                        if isinstance(vec, str):
                            try:
                                raw = base64.b64decode(vec)
                                arr = np.frombuffer(raw, dtype=np.float32)
                            except Exception:
                                arr = np.zeros(_DIM, dtype=np.float32)
                        else:
                            arr = np.asarray(vec, dtype=np.float32)
                        strict_dim = bool(getattr(settings, "EMBED_STRICT_DIM", True))
                        if arr.shape[0] != _DIM:
                            if strict_dim:
                                logger.error("embed-batch: embedding dim=%d != EMBED_DIM=%d", arr.shape[0], _DIM)
                                arr = np.zeros(_DIM, dtype=np.float32)
                            else:
                                arr = arr[:_DIM] if arr.shape[0] > _DIM else np.pad(arr, (0, _DIM - arr.shape[0])).astype(np.float32)
                        try:
                            norm = float(np.linalg.norm(arr))
                            if norm > 0.0:
                                arr = arr / norm
                        except Exception:
                            pass
                        raw = arr.tobytes()
                        to_cache.append((cache_key, raw))
                        for fut in futs:
                            if not fut.done():
                                fut.set_result(raw)
                    else:
                        zero_raw = _ZERO_VEC
                        for fut in futs:
                            if not fut.done():
                                fut.set_result(zero_raw)
                except Exception:
                    zero_raw = _ZERO_VEC
                    for fut in futs:
                        if not fut.done():
                            fut.set_result(zero_raw)
            if to_cache:
                try:
                    pipe = rds.pipeline(transaction=False)
                    for md5_key, raw in to_cache:
                        try:
                            if not _is_zero_embedding(raw):
                                ttl = int(86400 * (0.9 + 0.2 * random.random()))
                                pipe.set(md5_key, base64.b64encode(raw).decode("ascii"), ex=ttl)
                        except Exception:
                            logger.debug("embed-batch: cache store failed for one item", exc_info=True)
                            continue
                    await pipe.execute()
                except Exception:
                    pass
        except Exception:
            zero_raw = _ZERO_VEC
            for _, fut in batch:
                if not fut.done():
                    fut.set_result(zero_raw)
        finally:
            if self._queue and not self._flush_task:
                loop = asyncio.get_running_loop()
                self._flush_task = loop.create_task(self._delayed_flush())

_EMBED_BATCHER = _EmbedBatcher()

class PersonaMemory:
    INDEX_NAME = "idx:memory"
    ZSET_IDS: str
    MAX_ENTRIES = int(getattr(settings, "MEMORY_MAX_ENTRIES", 25000))
    FORGET_THRESHOLD = float(getattr(settings, "FORGET_THRESHOLD", 0.40))
    CONSOLIDATION_AGE = float(getattr(settings, "CONSOLIDATION_AGE", 7*86400))
    MAINT_INTERVAL = float(getattr(settings, "MEMORY_MAINTENANCE_INTERVAL", 3600))
    EMBED_DIM = int(getattr(settings, "EMBED_DIM", 3072))
    SCHEMA_VER = 2
    INIT_LOCK_KEY = "lock:idx:memory:init"
    INIT_LOCK_TTL = int(getattr(settings, "INDEX_INIT_LOCK_TTL", 30))
    MAINT_LEADER_KEY_TPL = "lock:memory:maint:leader:{chat}"
    MAINT_LEADER_TTL = int(getattr(settings, "MEMORY_MAINT_LEADER_TTL", 0)) or (int(getattr(settings, "MEMORY_MAINTENANCE_INTERVAL", 3600)) + 30)

    def __init__(self, *, chat_id: int, start_maintenance: bool = True):
        if self.EMBED_DIM <= 0:
            raise RuntimeError("EMBED_DIM must be positive; check settings")
        if _DIM != self.EMBED_DIM:
            strict_init = bool(getattr(settings, "EMBED_DIM_STRICT_INIT", True))
            msg = f"EMBED_DIM mismatch: module={_DIM}, class={self.EMBED_DIM}. "\
                  "Ensure settings.EMBED_DIM, embedding model dim and index dim match."
            if strict_init:
                raise RuntimeError(msg)
            else:
                logger.warning(msg)
        self._redis = get_redis_vector()
        self.chat = str(chat_id)
        self.ZSET_IDS = f"memory:ids:{self.chat}"
        self._ready = asyncio.Event()
        self._index_lock = asyncio.Lock()
        self._alias = self.INDEX_NAME
        self._index_real = f"{self._alias}:{self.EMBED_DIM}:v{self.SCHEMA_VER}"
        self._alias_txt = "idx:memtxt"
        self._index_txt_real = f"{self._alias_txt}:v{self.SCHEMA_VER}"
        try:
            self._memtxt_available = bool(getattr(settings, "MEMTXT_TEXT_INDEXED", True))
        except Exception:
            self._memtxt_available = True
        self._start_maintenance = start_maintenance
        self._init_scheduled = False
        self._no_ef_runtime = False
        self._hybrid_supported = True
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._initialize())
            self._init_scheduled = True
        except RuntimeError:
            pass

    async def _initialize(self):

        ts = time.time()
        ok = False
        try:
            await self.init_index()
            logger.info("PersonaMemory.init_index END (t=%.3fs)", time.time() - ts)
            await self._ensure_index_dim()
            logger.info("PersonaMemory._ensure_index_dim END (t=%.3fs)", time.time() - ts)
            await self._redis.ft(self._alias).info()
            ok = True
        except asyncio.TimeoutError:
            logger.error("init_index/_ensure_index_dim timeout")
        except redis.exceptions.RedisError as e:
            logger.error("init/ensure failed due to Redis error: %s", e)
        finally:
            if not ok:
                logger.warning("PersonaMemory: index not confirmed at init; will auto-heal on first query.")
            self._ready.set()

        if self._start_maintenance:
            asyncio.create_task(self._periodic_maintenance())

    async def _with_init_lock(self, coro):
        token = f"{os.getpid()}:{time.time():.6f}"
        try:
            got = await self._redis.set(self.INIT_LOCK_KEY, token, ex=self.INIT_LOCK_TTL, nx=True)
        except Exception as e:
            logger.warning("init lock SET failed: %s", e)
            got = False
        if not got:
            deadline = time.time() + max(2, int(self.INIT_LOCK_TTL))
            while time.time() < deadline:
                try:
                    await self._redis.ft(self._alias).info()
                    return
                except ResponseError as e:
                    if not _is_missing_index_error(e):
                        raise
                except Exception:
                    pass
                await asyncio.sleep(0.25)
            try:
                await self._redis.ft(self._index_real).info()
                await self._ensure_alias()
            except ResponseError:
                pass
            return
        try:
            async def _keeper():
                try:
                    while True:
                        await asyncio.sleep(max(2, int(self.INIT_LOCK_TTL)) * 0.5)
                        await self._redis.expire(self.INIT_LOCK_KEY,
                                                  int(self.INIT_LOCK_TTL))
                except asyncio.CancelledError:
                    return
                except Exception:
                    logger.debug("_with_init_lock keeper exited unexpectedly",
                                 exc_info=True)
                    return
            keeper = asyncio.create_task(_keeper())
            try:
                return await coro()
            finally:
                keeper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await keeper
                lua = (
                    "if redis.call('GET', KEYS[1]) == ARGV[1] "
                    "then return redis.call('DEL', KEYS[1]) else return 0 end"
                )
                with contextlib.suppress(Exception):
                    await self._redis.eval(lua, 1, self.INIT_LOCK_KEY, token)
        except Exception:
            logger.debug("_with_init_lock: unexpected error; lock cleanup attempted", exc_info=True)

    async def _ensure_index_available(self) -> None:
        try:
            await self._redis.ft(self._alias).info()
            return
        except ResponseError as e:
            if not _is_missing_index_error(e):
                raise
        async with self._index_lock:
            async def inner():
                try:
                    await self._redis.ft(self._alias).info()
                    return
                except ResponseError as e:
                    if not _is_missing_index_error(e):
                        raise
                logger.warning("PersonaMemory: rebuilding missing index %s", self.INDEX_NAME)
                await self.init_index()
                await self._ensure_index_dim()
                await self._redis.ft(self._alias).info()
                logger.info("PersonaMemory: rebuilt and verified index alias %s (DIM=%s)", self._alias, self.EMBED_DIM)
            await self._with_init_lock(lambda: inner())
        for _ in range(6):
            try:
                await self._redis.ft(self._alias).info()
                return
            except ResponseError as e:
                if not _is_missing_index_error(e):
                    raise
            await asyncio.sleep(0.5)

    async def _ensure_index_dim(self) -> None:

        try:
            await self._redis.ft(self._index_real).info()
        except ResponseError as e:
            if _is_missing_index_error(e):
                await self.init_index()
            else:
                logger.warning("_ensure_index_dim: Redis error: %s", e)
                return
        await self._ensure_alias()

    async def init_index(self):

        desired_cap = max(
            int(getattr(settings, "MEMORY_MAX_ENTRIES", self.MAX_ENTRIES)) * 2,
            int(getattr(settings, "EMBED_INITIAL_CAP", 4096))
        )

        async def _create_or_alias():
            fields = _build_index_fields(desired_cap, self.EMBED_DIM, use_initial_cap=True)
            definition = IndexDefinition(prefix=["memory:"], index_type=IndexType.HASH)
            created_real = False
            try:
                await self._redis.ft(self._index_real).info()
            except ResponseError:
                try:
                    await self._redis.ft(self._index_real).create_index(fields, definition=definition)
                    created_real = True
                except ResponseError as e:
                    msg = str(e).lower()
                    if "already exists" in msg:
                        logger.warning("PersonaMemory: real index %s already exists, continuing", self._index_real)
                    elif "initial capacity" in msg and "server limit" in msg:
                        m = re.search(r"\((\d+)\s+with the given parameters\)", str(e))
                        server_limit = int(m.group(1)) if m else None
                        new_cap = max(int(getattr(settings, "MEMORY_MAX_ENTRIES", self.MAX_ENTRIES)) * 2, 2048)
                        if server_limit:
                            new_cap = min(new_cap, server_limit)
                        logger.warning("init_index: reducing INITIAL_CAP from %s to %s due to server limit (%s).",
                                       desired_cap, new_cap, server_limit if server_limit is not None else "unknown")
                        fields_retry = _build_index_fields(new_cap, self.EMBED_DIM)
                        await self._redis.ft(self._index_real).create_index(fields_retry, definition=definition)
                        created_real = True
                    elif any(s in msg for s in ("unknown argument", "invalid argument", "unsupported", "unexpected attribute")):
                        logger.warning("init_index: retrying FT.CREATE without INITIAL_CAP due to server compatibility issue: %s", e)
                        fields_nc = _build_index_fields(desired_cap, self.EMBED_DIM, use_initial_cap=False)
                        await self._redis.ft(self._index_real).create_index(fields_nc, definition=definition)
                        created_real = True
                    else:
                        raise
            await self._ensure_alias()
            logger.info("PersonaMemory: alias %s -> %s %s", self._alias, self._index_real, "(created)" if created_real else "")

            index_text = bool(getattr(settings, "MEMTXT_TEXT_INDEXED", True))
            if index_text:
                try:
                    await self._redis.ft(self._index_txt_real).info()
                except ResponseError:
                    try:
                        await self._redis.ft(self._index_txt_real).create_index(
                            _build_text_index_fields(),
                            definition=IndexDefinition(prefix=["memtxt:"], index_type=IndexType.HASH),
                        )
                        logger.info("PersonaMemory: created text index %s", self._index_txt_real)
                    except ResponseError as e:
                        low = str(e).lower()
                        if "already exists" in low:
                            logger.warning("PersonaMemory: text index %s already exists", self._index_txt_real)
                        else:
                            raise
                try:
                    await self._redis.execute_command("FT.ALIASUPDATE", self._alias_txt, self._index_txt_real)
                except ResponseError as e:
                    low = str(e).lower()
                    if "unknown alias" in low:
                        await self._redis.execute_command("FT.ALIASADD", self._alias_txt, self._index_txt_real)
                    elif "unknown command" in low or "syntax" in low:
                        with contextlib.suppress(Exception):
                            await self._redis.execute_command("FT.ALIASDEL", self._alias_txt)
                        await self._redis.execute_command("FT.ALIASADD", self._alias_txt, self._index_txt_real)
                    else:
                        raise
                self._memtxt_available = True
            else:
                self._memtxt_available = False

        async def _guarded():
            async with self._index_lock:
                return await _create_or_alias()
        await self._with_init_lock(_guarded)

    async def _ensure_alias(self):
        try:
            await self._redis.execute_command("FT.ALIASUPDATE", self._alias, self._index_real)
            return
        except ResponseError as e:
            low = str(e).lower()
            if "unknown alias" in low:
                await self._redis.execute_command("FT.ALIASADD", self._alias, self._index_real)
                return
            if "unknown command" in low or "syntax" in low:
                with contextlib.suppress(Exception):
                    await self._redis.execute_command("FT.ALIASDEL", self._alias)
                await self._redis.execute_command("FT.ALIASADD", self._alias, self._index_real)
                return
            raise
        

    async def ready(self) -> None:
        if not self._init_scheduled and not self._ready.is_set():
            await self._initialize()
            self._init_scheduled = True
        await self._ready.wait()


    async def _fts_search(self, index_alias: str, query, *, query_params=None, timeout: float | None = None):

        cli = self._redis.ft(index_alias)
        try:
            query = query.dialect(2)
        except Exception:
            pass
        try:
            qtimeout_ms = int(getattr(settings, "REDISSEARCH_SERVER_TIMEOUT_MS", 0) or 0)
        except Exception:
            qtimeout_ms = 0
        if qtimeout_ms > 0:
            try:
                query = query.timeout(qtimeout_ms)
            except Exception:
                pass
        if timeout is None:
            return await cli.search(query, query_params=query_params)
        else:
            return await asyncio.wait_for(
                cli.search(query, query_params=query_params),
                timeout=timeout
            )


    async def _clamped_hincr(self, key: str, field: str, delta: float) -> None:
        try:
            script = (
                "local v=redis.call('HINCRBYFLOAT', KEYS[1], ARGV[1], ARGV[2]);"
                "if v>1 then redis.call('HSET', KEYS[1], ARGV[1], 1) "
                "elseif v<0 then redis.call('HSET', KEYS[1], ARGV[1], 0) end;"
                "return v"
            )
            await self._redis.eval(script, 1, key, field, str(float(delta)))
        except Exception:
            try:
                await self._redis.hincrbyfloat(key, field, float(delta))
                try:
                    v = await self._redis.hget(key, field)
                    v = float(v.decode() if isinstance(v, (bytes, bytearray)) else v)
                    if v > 1.0:
                        await self._redis.hset(key, field, 1.0)
                    elif v < 0.0:
                        await self._redis.hset(key, field, 0.0)
                except Exception:
                    pass
            except Exception:
                pass

    async def _reinforce_ids(self, doc_ids: List[str], ts: float) -> None:
        if not doc_ids:
            return
        try:
            script = (
                "local ts=ARGV[1]; "
                "for i=1,#KEYS do "
                "  local k=KEYS[i]; "
                "  if redis.call('EXISTS', k)==1 then "
                "    redis.call('HINCRBY', k, 'use_count', 1); "
                "    redis.call('HSET', k, 'last_used_ts', ts); "
                "  end "
                "end; "
                "return 1"
            )
            await self._redis.eval(script, len(doc_ids), *doc_ids, str(float(ts)))
        except Exception:
            try:
                pipe = self._redis.pipeline(transaction=True)
                for did in doc_ids:
                    pipe.exists(did)
                flags = await pipe.execute()
                pipe = self._redis.pipeline(transaction=True)
                for did, ok in zip(doc_ids, flags):
                    if ok:
                        pipe.hincrby(did, "use_count", 1)
                        pipe.hset(did, mapping={"last_used_ts": ts})
                await pipe.execute()
            except Exception:
                logger.debug("reinforcement fallback failed", exc_info=True)

    @staticmethod
    def _extract_topics(txt: str, top_k: int = 3) -> List[str]:

        try:
            import yake
        except Exception:
            yake = None
        if not yake:
            global _TOPIC_LOGGED
            try:
                if not _TOPIC_LOGGED:
                    logger.debug("_extract_topics: YAKE unavailable; topic extraction disabled")
                    _TOPIC_LOGGED = True
            except Exception:
                pass
        if not yake:
            return []
        try:
            from langdetect import detect
            lang = detect(txt) or "en"
        except Exception:
            lang = "en"
        try:
            kw = yake.KeywordExtractor(lan=lang, top=top_k)
            return [w for w, _ in kw.extract_keywords(txt)]
        except Exception:
            return []


    async def record(
        self,
        text: str,
        embedding: bytes,
        emotions: Dict[str, float],
        state_metrics: Dict[str, float],
        *,
        uid: int | None = None,
        salience: float | None = None,
        event_frame: bool = True
        ) -> Tuple[Optional[str], bool]:

        ts = time.time()
        now_ts = ts
        
        await self.ready()
        await self._ensure_index_available()
        topics_raw = self._extract_topics(text) if float(salience or 0.0) >= float(getattr(settings, "TOPIC_MIN_SALIENCE", 0.4)) and len(text) >= int(getattr(settings, "TOPIC_MIN_LEN", 60)) else []
        topics = [_topic_tagify(w) for w in topics_raw if w]

        event_time = _fallback_rel(text, now_ts)
        if event_time is None:
            def _prefer(text: str) -> str:
                t = (text or "").lower()
                if re.search(r"\b(ago|yesterday|last|был[аио]?|вчера|прошл(ой|ая|ую|ые))\b", t):
                    return "past"
                if re.search(r"\b(tomorrow|next|завтра|следующ(?:ая|ий|ее|ую))\b", t):
                    return "future"
                return "past"

            prefer = _prefer(text)
            def _parse_with(pref: str):
                return dp_parse(
                    text,
                    settings={
                        'PREFER_DATES_FROM': pref,
                        'RELATIVE_BASE': datetime.now(tz=UTC),
                        'TIMEZONE': 'UTC',
                        'RETURN_AS_TIMEZONE_AWARE': True,
                    }
                )
            try:
                dt = await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(_DP_EXECUTOR, _parse_with, prefer),
                    timeout=1.0
                )
                if not dt:
                    alt = "future" if prefer == "past" else "past"
                    dt = await asyncio.wait_for(
                        asyncio.get_running_loop().run_in_executor(_DP_EXECUTOR, _parse_with, alt),
                        timeout=0.5
                    )
            except asyncio.TimeoutError:
                dt = None
            except Exception:
                logger.exception("date parsing failed")
                dt = None
            if dt:
                try:
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=UTC)
                    event_time = dt.astimezone(UTC).timestamp()
                except Exception:
                    event_time = None
        event_time = event_time or now_ts
        if not math.isfinite(event_time):
            event_time = now_ts

        if abs(event_time - now_ts) < 3600:
            etype = "present"
        elif event_time < now_ts:
            etype = "past"
        else:
            etype = "future"

        uid_s = str(uid or "")
        store_vec = float(salience or 0.0) >= float(getattr(settings, "VEC_SALIENCE_MIN", 0.0))

        async def _write_memtxt_and_return():
            try:
                seen_key = f"memtxt:seen:{self.chat}:{uid_s}" if (_MEMTXT_SEEN_SCOPE == "uid" and uid is not None) else f"memtxt:seen:{self.chat}"
                fp = hashlib.sha1(_norm_text_for_embed(text).encode("utf-8")).hexdigest()
                added = await self._redis.sadd(seen_key, fp)
                if _MEMTXT_SEEN_TTL > 0:
                    await self._redis.expire(seen_key, _MEMTXT_SEEN_TTL)
                if _MEMTXT_SEEN_SCOPE == "uid" and uid is not None:
                    await _register_persona_user_key(self._redis, uid, seen_key)
                if not added:
                    return
            except Exception:
                logger.debug("memtxt: mark seen failed", exc_info=True)
            try:
                eid_txt = await self._redis.incr(f"memtxt:{self.chat}:next_id")
                key_txt = f"memtxt:{self.chat}:{eid_txt}"
                data_txt = {
                    "text": text, "ts": time.time(), "event_time": event_time, "event_type": etype,
                    "chat": self.chat, "uid": uid_s, "topic": ",".join(topics) if topics else "",
                }
                pipe_txt = self._redis.pipeline(transaction=True)
                pipe_txt.hset(key_txt, mapping=data_txt)
                pipe_txt.zadd(f"memtxt:ids:{self.chat}", {key_txt: event_time})
                if uid is not None:
                    pipe_txt.zadd(f"memtxt:ids:{self.chat}:{uid_s}", {key_txt: event_time})
                await pipe_txt.execute()
                if uid is not None:
                    await _register_persona_user_key(self._redis, uid, f"memtxt:ids:{self.chat}:{uid_s}")
                try:
                    memtxt_ttl_days = getattr(settings, "MEMTXT_TTL_DAYS", None)
                    if memtxt_ttl_days is None:
                        memtxt_ttl_days = int(getattr(settings, "MEMORY_TTL_DAYS", 7))
                    else:
                        memtxt_ttl_days = int(memtxt_ttl_days)
                    if memtxt_ttl_days > 0:
                        await self._redis.expire(key_txt, memtxt_ttl_days * 86400)
                except Exception:
                    pass
                try:
                    if uid is not None:
                        total_uid = await self._redis.zcard(f"memtxt:ids:{self.chat}:{uid_s}")
                        if total_uid > _MEMTXT_MAX_PER_UID:
                            drop = await self._redis.zrange(f"memtxt:ids:{self.chat}:{uid_s}", 0, total_uid - _MEMTXT_MAX_PER_UID - 1)
                            p2 = self._redis.pipeline(transaction=True)
                            for doc_key in drop:
                                k = _b2s(doc_key, str(doc_key))
                                p2.delete(k)
                                p2.zrem(f"memtxt:ids:{self.chat}:{uid_s}", k)
                                p2.zrem(f"memtxt:ids:{self.chat}", k)
                            await p2.execute()
                    total_chat = await self._redis.zcard(f"memtxt:ids:{self.chat}")
                    if total_chat > _MEMTXT_MAX_PER_CHAT:
                        drop = await self._redis.zrange(f"memtxt:ids:{self.chat}", 0, total_chat - _MEMTXT_MAX_PER_CHAT - 1)
                        keys_dec = []
                        rpipe = self._redis.pipeline()
                        for doc_key in drop:
                            k = _b2s(doc_key, str(doc_key))
                            keys_dec.append(k)
                            rpipe.hget(k, "uid")
                        try:
                            uids = await rpipe.execute()
                        except Exception:
                            uids = [None] * len(keys_dec)
                        p3 = self._redis.pipeline(transaction=True)
                        for k, uid_val in zip(keys_dec, uids):
                            uid_str = (
                                uid_val.decode() if isinstance(uid_val, (bytes, bytearray))
                                else (uid_val or "")
                            )
                            p3.delete(k)
                            p3.zrem(f"memtxt:ids:{self.chat}", k)
                            if uid_str:
                                p3.zrem(f"memtxt:ids:{self.chat}:{uid_str}", k)
                        await p3.execute()
                except Exception:
                    logger.debug("memtxt trim failed", exc_info=True)
            except Exception:
                logger.debug("memtxt write failed", exc_info=True)

        if not store_vec:
            await _write_memtxt_and_return()
            return (None, False)

        if not isinstance(embedding, (bytes, bytearray)) or len(embedding) != self.EMBED_DIM * 4:
            logger.error(
                "record: invalid embedding size %s, expected %d bytes — writing zero vector",
                (len(embedding) if isinstance(embedding, (bytes, bytearray)) else "NA"),
                self.EMBED_DIM * 4,
            )
            embedding = np.zeros(self.EMBED_DIM, dtype=np.float32).tobytes()

        if _is_zero_embedding(embedding, self.EMBED_DIM):
            await _write_memtxt_and_return()
            return (None, False)

        try:
            base_k = int(getattr(settings, "REDISSEARCH_KNN_K", 5))
        except Exception:
            base_k = 5
        occ = _pool_occupancy(self._redis)
        k = max(5, int(base_k * (1.0 - 0.5*min(1.0, occ))))

        ef_rt = 0
        try:
            ef_rt = int(getattr(settings, "HNSW_EF_RUNTIME", 100) or 0)
        except Exception:
            ef_rt = 0
        if self._no_ef_runtime:
            ef_rt = 0
        if ef_rt > 0 and occ > 0.0:
            new_ef = max(20, int(ef_rt * (1.0 - 0.6*min(1.0, occ))))
            if new_ef != ef_rt:
                logger.debug("record: EF_RUNTIME reduced from %s to %s due to pool occupancy=%.2f", ef_rt, new_ef, occ)
            ef_rt = new_ef
        if ef_rt > 0:
            knn_clause = f"[KNN {k} @embedding $vec AS vector_score EF_RUNTIME $ef]"
            params = {"vec": embedding, "ef": ef_rt}
        else:
            knn_clause = f"[KNN {k} @embedding $vec AS vector_score]"
            params = {"vec": embedding}
        if not self._hybrid_supported:
            try:
                fetch = max(k * 5, k + 2)
                knn_only = (
                    Query(f"*=>[KNN {fetch} @embedding $vec AS vector_score]")
                    .sort_by("vector_score", asc=True)
                    .return_fields("vector_score", "chat", "uid")
                    .dialect(2)
                    .paging(0, fetch)
                )
                res_all = await self._fts_search(
                    self._alias, knn_only, query_params={"vec": embedding},
                    timeout=_FT_TIMEOUT
                )
                want_chat = self.chat
                want_uid  = str(uid) if uid is not None else None
                docs = _filter_knn_docs(getattr(res_all, "docs", []),
                                        want_chat=want_chat,
                                        want_uid=want_uid)
                class _Res:
                    pass

                res = _Res()
                res.docs = docs
            except Exception as e3:
                logger.warning("record: forced KNN-only failed (%s), skipping dedup", e3)
                res = None
        else:
            filters = [f'@chat:{{{_tag_literal(self.chat)}}}']
            if uid is not None:
                filters.append(f'@uid:{{{_tag_literal(str(uid))}}}')
            _lhs_filters = " ".join(filters)
            base_filter = f"({_lhs_filters})" if _lhs_filters else "*"
            q = (
                Query(f"{base_filter}=>{knn_clause}")
                .sort_by("vector_score", asc=True)
                .return_fields("vector_score")
                .dialect(2)
                .paging(0, k)
            )
            zero_vec = _is_zero_embedding(embedding, self.EMBED_DIM)
            try:
                res = None
                if not zero_vec:
                    res = await self._fts_search(
                        self._alias, q, query_params=params,
                        timeout=_FT_TIMEOUT
                    )
                    logger.debug("record: dedupe search END (t=%.3fs)", time.time() - ts)
            except (ResponseError, asyncio.TimeoutError, redis.exceptions.RedisError) as e:
                if ef_rt > 0:
                    try:
                        q_fallback = (
                            Query(f"{base_filter}=>[KNN {k} @embedding $vec AS vector_score]")
                            .sort_by("vector_score", asc=True)
                            .return_fields("vector_score")
                            .dialect(2)
                            .paging(0, k)
                        )
                        if not zero_vec:
                            res = await self._fts_search(
                                self._alias, q_fallback, query_params={"vec": embedding},
                                timeout=_FT_TIMEOUT
                            )
                    except ResponseError as e_fb:
                        low = str(e_fb).lower()
                        if "ef_runtime" in low or "unknown argument" in low or "unexpected attribute" in low:
                            logger.debug("record: disabling EF_RUNTIME due to server response: %s", e_fb)
                            self._no_ef_runtime = True
                        if _is_missing_index_error(e_fb):
                            await self._ensure_index_available()
                            try:
                                if not zero_vec:
                                    res = await self._fts_search(
                                        self._alias, q_fallback, query_params={"vec": embedding},
                                        timeout=_FT_TIMEOUT
                                    )
                            except Exception as e2:
                                logger.warning("record: dedupe retry after ensure failed (%s), skipping", e2)
                                res = None
                        elif "syntax" in low and any(s in low for s in ("near chat", "near uid", "near topic")):
                            self._hybrid_supported = False
                            try:
                                fetch = max(k * 5, k + 2)
                                knn_only = (
                                    Query(f"*=>[KNN {fetch} @embedding $vec AS vector_score]")
                                    .sort_by("vector_score", asc=True)
                                    .return_fields("vector_score", "chat", "uid")
                                    .dialect(2)
                                    .paging(0, fetch)
                                )
                                res_all = await self._fts_search(
                                    self._alias, knn_only,
                                    query_params={"vec": embedding},
                                    timeout=_FT_TIMEOUT
                                )
                                want_chat = self.chat
                                want_uid  = str(uid) if uid is not None else None
                                docs = _filter_knn_docs(getattr(res_all, "docs", []),
                                                        want_chat=want_chat,
                                                        want_uid=want_uid)
                                class _Res:
                                    pass

                                res = _Res()
                                res.docs = docs
                                logger.debug("record: used KNN-only fallback, kept %d/%d docs", len(docs), len(getattr(res_all, "docs", [])))
                            except Exception as e3:
                                logger.warning("record: KNN-only fallback failed (%s), skipping", e3)
                                res = None
                        else:
                            logger.warning("record: dedupe fallback failed (%s), skipping", e_fb)
                            res = None
                elif _is_missing_index_error(e):
                    await self._ensure_index_available()
                    try:
                        if not zero_vec:
                            res = await self._fts_search(
                                self._alias, q, query_params=params,
                                timeout=_FT_TIMEOUT
                            )
                    except Exception as e2:
                        logger.warning("record: dedupe retry failed (%s), skipping deduplication", e2)
                        res = None
                else:
                    low = str(e).lower()
                    if "ef_runtime" in low or "unknown argument" in low or "unexpected attribute" in low:
                        logger.debug("record: disabling EF_RUNTIME due to server response: %s", e)
                        self._no_ef_runtime = True
                    if "syntax" in low and any(s in low for s in ("near chat", "near uid", "near topic")):
                        self._hybrid_supported = False
                        try:
                            fetch = max(k * 5, k + 2)
                            knn_only = (
                                Query(f"*=>[KNN {fetch} @embedding $vec AS vector_score]")
                                .sort_by("vector_score", asc=True)
                                .return_fields("vector_score", "chat", "uid")
                                .dialect(2)
                                .paging(0, fetch)
                            )
                            res_all = await self._fts_search(
                                self._alias, knn_only,
                                query_params={"vec": embedding},
                                timeout=_FT_TIMEOUT
                            )
                            want_chat = self.chat
                            want_uid  = str(uid) if uid is not None else None
                            docs = _filter_knn_docs(getattr(res_all, "docs", []),
                                                    want_chat=want_chat,
                                                    want_uid=want_uid)
                            class _Res:
                                pass

                            res = _Res()
                            res.docs = docs
                            logger.debug("record: used KNN-only fallback (outer), kept %d/%d docs", len(docs), len(getattr(res_all, "docs", [])))
                        except Exception as e3:
                            logger.warning("record: KNN-only outer fallback failed (%s), skipping deduplication", e3)
                            res = None
                    else:
                        logger.warning("record: dedupe search failed (%s), skipping deduplication", e)
                        res = None

        pipe = self._redis.pipeline(transaction=True)
        matched_doc_id = None
        matched_eid = None
        if res:
            best = None
            for doc in res.docs:
                try:
                    dist = float(doc.vector_score)
                    if not math.isfinite(dist):
                        continue
                except Exception:
                    continue
                if dist <= _DUP_DIST_MAX:
                    if best is None or dist < best[0]:
                        did = doc.id.decode() if isinstance(doc.id, (bytes, bytearray)) else str(doc.id)
                        best = (dist, did)
            if best:
                matched_doc_id = best[1]
                matched_eid = matched_doc_id.rsplit(":", 1)[-1]
                try:
                    logger.debug("dedup: matched dist=%.3f (thr=%.3f) chat=%s",
                                 best[0], _DUP_DIST_MAX, self.chat)
                except Exception:
                    pass

        if matched_doc_id:
            try:
                prev_sal_raw, prev_event_time_raw, prev_topics_raw, prev_att_raw = await self._redis.hmget(
                    matched_doc_id, "salience", "event_time", "topic", "attachment"
                )

                prev_sal = _to_float(prev_sal_raw, 0.0)
                prev_event_time = _to_float(prev_event_time_raw, 0.0)
                set_map = {"last_used_ts": now_ts}
                prev_topics = set(_b2s(prev_topics_raw, "").split(",")) if prev_topics_raw else set()
                new_topics = set(t for t in topics if t)
                merged_topics = [t for t in (prev_topics | new_topics) if t][:8]
                merged_topic_str = ",".join(merged_topics)
                set_map["topic"] = merged_topic_str
                try:
                    cur_att = float(state_metrics.get("attachment", 0.0))
                    prev_att = _to_float(prev_att_raw, 0.0)
                    if cur_att > 0.0:
                        set_map["attachment"] = max(0.0, min(1.0, max(prev_att, cur_att)))
                except Exception:
                    pass
                if salience is not None:
                    try:
                        new_sal = max(prev_sal, float(salience or 0.0))
                    except Exception:
                        new_sal = prev_sal
                    set_map["salience"] = max(0.0, min(1.0, new_sal))
                try:
                    if event_time and math.isfinite(event_time):
                        prev_i = int(prev_event_time) if math.isfinite(prev_event_time) else 0
                        new_i  = int(event_time)
                        prev_day_level = (prev_i > 0) and (prev_i % 86400 == 0)
                        new_has_time   = (new_i % 86400 != 0)
                        became_more_precise = prev_day_level and new_has_time

                        need_shift = (
                            not math.isfinite(prev_event_time)
                            or (abs(event_time - prev_event_time) >= _DEDUP_EVENTTIME_MIN_SHIFT and event_time > prev_event_time)
                            or became_more_precise
                        )
                        if need_shift:
                            set_map["event_time"] = event_time
                            if abs(event_time - now_ts) < 3600:
                                new_type = "present"
                            elif event_time < now_ts:
                                new_type = "past"
                            else:
                                new_type = "future"
                            set_map["event_type"] = new_type
                            eid_s = matched_doc_id.rsplit(":", 1)[-1]
                            pz = self._redis.pipeline(transaction=True)
                            pz.zadd(self.ZSET_IDS, {eid_s: event_time})
                            if uid is not None:
                                pz.zadd(f"memory:ids:{self.chat}:{uid_s}", {eid_s: event_time})
                                pz.sadd(f"memory:uidsets:{self.chat}", f"memory:ids:{self.chat}:{uid_s}")
                            await pz.execute()
                            if uid is not None:
                                await _register_persona_user_key(
                                    self._redis,
                                    uid,
                                    f"memory:ids:{self.chat}:{uid_s}",
                                )
                except Exception:
                    logger.debug("merge-duplicate: event_time update skipped", exc_info=True)
                pipe.hincrby(matched_doc_id, "use_count", 1)
                pipe.hset(matched_doc_id, mapping=set_map)
                await pipe.execute()
                if salience is None:
                    await self._clamped_hincr(matched_doc_id, "salience", _SALIENCE_REINFORCE_STEP)
            except Exception:
                logger.debug("merge-duplicate failed", exc_info=True)
            return (matched_eid, False)

        eid = await self._redis.incr(f"memory:{self.chat}:next_id")
        key = f"memory:{self.chat}:{eid}"

        async def _event_frame_call(t: str) -> dict | None:
            has_digits = any(ch.isdigit() for ch in t)
            informative = (len(t) >= 60) or has_digits
            if not informative:
                return None
            system_prompt = (
                "You are an information-extraction model that outputs ONE JSON object "
                "that STRICTLY matches the provided JSON schema.\n"
                "Rules:\n"
                "- Output ONLY minified JSON on a single line. No prose, no markdown.\n"
                "- Keys must be exactly: type, when_iso, tense, participants, intent, commitments, places, tags.\n"
                "- Use lowercase ENGLISH for 'type', 'tense', and 'tags'.\n"
                "- 'tense' must be one of: past, present, future (if unclear, choose the closest).\n"
                "- 'when_iso' must be ISO8601 UTC in the form YYYY-MM-DDTHH:MM:SSZ. "
                "  If the text does NOT give a concrete absolute time/date, set an empty string \"\".\n"
                "- 'participants' is a list of short names/roles. Use 'user' for the speaker and "
                "  keep any explicit names from the text (shortened if needed). Deduplicate.\n"
                "- 'intent' is a LIST (0–2 items) of short verb phrases in English (≤ 40 chars each) "
                "  describing the user's main intent; use [] if none.\n"
                "- 'commitments' is a list of short action items (≤ 40 chars each) if any promises/obligations exist, else [].\n"
                "- 'places' is a list of short place names if any, else [].\n"
                "- 'tags' is a list (0-5) of helpful lowercase keywords in English (e.g., 'meeting', 'deadline'); if unclear, include 'other'. "
                "  include 'relative-time' if only relative timing is mentioned.\n"
                "- Do NOT invent facts. If unknown: use empty string for scalars (when a string is required), [] for arrays.\n"
                "- Do NOT add extra fields."
            )
            user_prompt = (
                "Extract the event frame from the text below.\n"
                "Text:\n"
                f"{t}\n\n"
                "Return ONLY a single minified JSON object."
            )
            try:
                resp = await asyncio.wait_for(
                    _call_openai_with_retry(
                        endpoint="responses.create",
                        model=settings.REASONING_MODEL,
                        instructions=system_prompt,
                        input=user_prompt,
                        text={
                            "format": {
                                "type": "json_schema",
                                "name": "EventFrame",
                                "schema": _event_frame_schema(),
                                "strict": True
                            }
                        },
                        temperature=0,
                        max_output_tokens=500,
                    ),
                    timeout=settings.REASONING_MODEL_TIMEOUT,
                )
                out = (_get_output_text(resp) or "").strip()
                if out.lstrip().startswith("```"):
                    out = re.sub(r"^```[a-z0-9]*\s*|\s*```$", "", out, flags=re.IGNORECASE | re.DOTALL)
                out = out.lstrip("\ufeff")
                if "{" in out and "}" in out:
                    out = out[out.find("{"): out.rfind("}") + 1]
                j = json.loads(out or "{}")
                return j if isinstance(j, dict) else None
            except Exception:
                return None

        async def _extract_and_update_frame(key_id: str, zset_key: str, base_event_time: float) -> None:
            try:
                async with _EVENT_FRAME_SEM:
                    frame = await _event_frame_call(text)
            except Exception:
                frame = None
            if not frame or not _is_valid_event_frame(frame):
                return
            new_time = base_event_time
            new_type = etype
            try:
                w = frame.get("when_iso")
                if isinstance(w, str) and w:
                    dt_iso = isoparse(w)
                    if dt_iso.tzinfo is None:
                        dt_iso = dt_iso.replace(tzinfo=UTC)
                    new_time = dt_iso.astimezone(UTC).timestamp()
                    if new_time < time.time() - 60:
                        new_type = "past"
                    elif new_time > time.time() + 60:
                        new_type = "future"
                    else:
                        new_type = "present"
            except Exception:
                pass
            try:
                payload = {"event_frame": json.dumps(frame, ensure_ascii=False),
                           "event_time": new_time,
                           "event_type": new_type}
                eid_s = key_id.rsplit(":", 1)[-1]
                
                try:
                    uid_raw = await self._redis.hget(key_id, "uid")
                    uid_str = _b2s(uid_raw, "").strip()
                except Exception:
                    uid_str = ""

                pipe2 = self._redis.pipeline(transaction=True)
                pipe2.hset(key_id, mapping=payload)
                pipe2.zadd(zset_key, {eid_s: new_time})
                if uid_str:
                    per_uid_zset = f"memory:ids:{self.chat}:{uid_str}"
                    pipe2.zadd(per_uid_zset, {eid_s: new_time})
                await pipe2.execute()
                if uid_str:
                    with contextlib.suppress(Exception):
                        await self._redis.sadd(f"memory:uidsets:{self.chat}", f"memory:ids:{self.chat}:{uid_str}")
                    try:
                        await _register_persona_user_key(self._redis, int(uid_str), per_uid_zset)
                    except Exception:
                        pass
            except Exception:
                logger.debug("event-frame update failed", exc_info=True)

        data = {
            "text":       text,
            "ts":         ts,
            "event_time": event_time,
            "event_type": etype,
            "emotions":   json.dumps(emotions),
            "topic":      ",".join(topics) if topics else "",
            "chat":       self.chat,
            "uid":        str(uid or ""),
            "salience":   max(0.0, min(1.0, float(salience or 0.0))) if salience is not None else 0.0,
            "embedding":  embedding,
        }
        data.update({k: v for k, v in state_metrics.items()})
        pipe.hset(key, mapping=data)
        pipe.zadd(self.ZSET_IDS, {str(eid): event_time})
        await pipe.execute()
        logger.debug("record: hset/zadd END (t=%.3fs)", time.time() - ts)

        try:
            vec_ttl_days = int(getattr(settings, "MEMORY_VEC_TTL_DAYS", 0))
            if vec_ttl_days > 0:
                await self._redis.expire(key, vec_ttl_days * 86400)
        except Exception:
            logger.debug("record: set TTL for vector memory failed", exc_info=True)

        if getattr(settings, "EVENT_FRAME_ENABLED", True) and bool(event_frame):
            min_sal = float(getattr(settings, "EVENT_FRAME_MIN_SALIENCE", 0.6))
            if float(salience or 0.0) >= min_sal:
                try:
                    asyncio.create_task(_extract_and_update_frame(key, self.ZSET_IDS, event_time))
                except Exception:
                    logger.debug("schedule event-frame failed", exc_info=True)

        if uid is not None:
            try:
                per_uid_zset = f"memory:ids:{self.chat}:{uid_s}"
                await self._redis.zadd(per_uid_zset, {str(eid): event_time})
                try:
                    await self._redis.sadd(f"memory:uidsets:{self.chat}", per_uid_zset)
                except Exception:
                    pass
                await _register_persona_user_key(self._redis, uid, per_uid_zset)
                total_uid = await self._redis.zcard(f"memory:ids:{self.chat}:{uid_s}")
                max_uid = int(getattr(settings, "MEMORY_MAX_PER_UID", 120))
                if total_uid > max_uid:
                    drop = await self._redis.zrange(f"memory:ids:{self.chat}:{uid_s}", 0, total_uid - max_uid - 1)
                    p3 = self._redis.pipeline(transaction=True)
                    for eid_s in drop:
                        s = eid_s.decode() if isinstance(eid_s, (bytes, bytearray)) else str(eid_s)
                        p3.delete(f"memory:{self.chat}:{s}")
                        p3.zrem(f"memory:ids:{self.chat}:{uid_s}", s)
                        p3.zrem(self.ZSET_IDS, s)
                    await p3.execute()
            except Exception:
                logger.debug("per-uid vec trim failed", exc_info=True)

        count = await self._redis.zcard(self.ZSET_IDS)
        if count > self.MAX_ENTRIES:
            await self._forget_if_needed()
        return (str(eid), True)

    async def query(self, embedding: bytes, top_k: int = 5, topic_hint: str | None = None, uid: int | None = None) -> List[Tuple[str, float]]:
        ts = time.time()
        await self.ready()
        await self._ensure_index_available()
        logger.debug("query: ready.wait END (t=%.3fs)", time.time() - ts)

        if not isinstance(embedding, (bytes, bytearray)) or len(embedding) != self.EMBED_DIM * 4:
            logger.debug("query: bad embedding size → []")
            return []
        if _is_zero_embedding(embedding, self.EMBED_DIM):
            logger.debug("query: zero embedding fallback → []")
            return []

        try:
            logger.debug("query: min_sim=%.3f chat=%s uid=%s topic=%s",
                         _MIN_SIMILARITY, self.chat, str(uid) if uid is not None else "", topic_hint or "")
        except Exception:
            pass

        filters = [f'@chat:{{{_tag_literal(self.chat)}}}']
        if uid is not None:
            filters.append(f'@uid:{{{_tag_literal(str(uid))}}}')
        if topic_hint:
            th = _topic_tagify(topic_hint)
            if th:
                filters.append(f'@topic:{{{_tag_literal(th)}}}')
        _lhs_filters = " ".join(filters)
        qbase = f"({_lhs_filters})" if _lhs_filters else "*"

        try:
            top_k = int(top_k)
        except Exception:
            top_k = 5
        if top_k < 1:
            top_k = 1

        ef_rt = 0
        try:
            ef_rt = int(getattr(settings, "HNSW_EF_RUNTIME", 100) or 0)
        except Exception:
            ef_rt = 0
        if self._no_ef_runtime:
            ef_rt = 0
        occ = _pool_occupancy(self._redis)
        if ef_rt > 0 and occ > 0.0:
            new_ef = max(20, int(ef_rt * (1.0 - 0.6*min(1.0, occ))))
            if new_ef != ef_rt:
                logger.debug("query: EF_RUNTIME reduced from %s to %s due to pool occupancy=%.2f", ef_rt, new_ef, occ)
            ef_rt = new_ef
        if ef_rt > 0:
            knn_clause = f"[KNN {top_k} @embedding $vec AS vector_score EF_RUNTIME $ef]"
            params = {"vec": embedding, "ef": ef_rt}
        else:
            knn_clause = f"[KNN {top_k} @embedding $vec AS vector_score]"
            params = {"vec": embedding}

        if not self._hybrid_supported:
            try:
                fetch = max(top_k * 5, top_k + 2)
                knn_only = (
                    Query(f"*=>[KNN {fetch} @embedding $vec AS vector_score]")
                    .sort_by("vector_score", asc=True)
                    .return_fields(
                        "vector_score", "text", "ts", "event_time",
                        "salience", "attachment", "use_count", "last_used_ts",
                        "chat", "uid", "topic", "event_type"
                    )
                    .dialect(2)
                    .paging(0, fetch)
                )
                res_all = await self._fts_search(
                    self._alias, knn_only,
                    query_params={"vec": embedding},
                    timeout=_FT_TIMEOUT
                )
                want_chat = self.chat
                want_uid  = str(uid) if uid is not None else None
                docs = _filter_knn_docs(getattr(res_all, "docs", []),
                                        want_chat=want_chat,
                                        want_uid=want_uid,
                                        topic_hint=topic_hint)
                class _Res:
                    pass

                res = _Res()
                res.docs = docs
            except Exception as e3:
                logger.warning("query: forced KNN-only failed (%s)", e3)
                return []
        else:
            q = (
                Query(f"{qbase}=>{knn_clause}")
                .sort_by("vector_score", asc=True)
                .return_fields("vector_score", "text", "ts", "event_time", "salience", "attachment", "use_count", "last_used_ts")
                .dialect(2)
                .paging(0, top_k)
            )
        
            try:
                res = await self._fts_search(
                    self._alias, q, query_params=params,
                    timeout=_FT_TIMEOUT
                )
                logger.debug("query: search END (t=%.3fs)", time.time() - ts)
            except (ResponseError, asyncio.TimeoutError, redis.exceptions.RedisError) as e:
                if ef_rt > 0:
                    try:
                        q_fallback = (
                            Query(f"{qbase}=>[KNN {top_k} @embedding $vec AS vector_score]")
                            .sort_by("vector_score", asc=True)
                            .return_fields(
                                "vector_score", "text", "ts", "event_time",
                                "salience", "attachment", "use_count", "last_used_ts"
                            )
                            .dialect(2)
                            .paging(0, top_k)
                        )
                        res = await self._fts_search(
                            self._alias, q_fallback, query_params={"vec": embedding},
                            timeout=_FT_TIMEOUT
                        )
                    except ResponseError as e_fb:
                        low = str(e_fb).lower()
                        if "ef_runtime" in low or "unknown argument" in low or "unexpected attribute" in low:
                            logger.debug("query: disabling EF_RUNTIME due to server response: %s", e_fb)
                            self._no_ef_runtime = True
                        if _is_missing_index_error(e_fb):
                            await self._ensure_index_available()
                            try:
                                res = await self._fts_search(
                                    self._alias, q_fallback, query_params={"vec": embedding},
                                    timeout=_FT_TIMEOUT
                                )
                            except Exception as e2:
                                logger.warning("PersonaMemory.query retry after ensure failed (%s)", e2)
                                return []
                        elif "syntax" in low and any(s in low for s in ("near chat", "near uid", "near topic")):
                            self._hybrid_supported = False
                            try:
                                fetch = max(top_k * 5, top_k + 2)
                                knn_only = (
                                    Query(f"*=>[KNN {fetch} @embedding $vec AS vector_score]")
                                    .sort_by("vector_score", asc=True)
                                    .return_fields(
                                        "vector_score", "text", "ts", "event_time",
                                        "salience", "attachment", "use_count", "last_used_ts",
                                        "chat", "uid", "topic", "event_type"
                                    )
                                    .dialect(2)
                                    .paging(0, fetch)
                                )
                                res_all = await self._fts_search(
                                    self._alias, knn_only,
                                    query_params={"vec": embedding},
                                    timeout=_FT_TIMEOUT
                                )
                                want_chat = self.chat
                                want_uid  = str(uid) if uid is not None else None
                                docs = _filter_knn_docs(getattr(res_all, "docs", []),
                                                        want_chat=want_chat,
                                                        want_uid=want_uid,
                                                        topic_hint=topic_hint)
                                class _Res:
                                    pass

                                res = _Res()
                                res.docs = docs
                                logger.debug("query: used KNN-only fallback, kept %d/%d docs", len(docs), len(getattr(res_all, "docs", [])))
                            except Exception as e3:
                                logger.warning("PersonaMemory.query KNN-only fallback failed (%s)", e3)
                                return []
                elif _is_missing_index_error(e):
                    await self._ensure_index_available()
                    try:
                        res = await self._fts_search(
                            self._alias, q, query_params=params,
                            timeout=_FT_TIMEOUT
                        )
                    except Exception as e2:
                        logger.warning("PersonaMemory.query retry failed (%s)", e2)
                        return []
                else:
                    low = str(e).lower()
                    if "ef_runtime" in low or "unknown argument" in low or "unexpected attribute" in low:
                        logger.debug("query: disabling EF_RUNTIME due to server response: %s", e)
                        self._no_ef_runtime = True
                    if "syntax" in low and any(s in low for s in ("near chat", "near uid", "near topic")):
                        self._hybrid_supported = False
                        try:
                            fetch = max(top_k * 5, top_k + 2)
                            knn_only = (
                                Query(f"*=>[KNN {fetch} @embedding $vec AS vector_score]")
                                .sort_by("vector_score", asc=True)
                                .return_fields(
                                    "vector_score", "text", "ts", "event_time",
                                    "salience", "attachment", "use_count", "last_used_ts",
                                    "chat", "uid", "topic", "event_type"
                                )
                                .dialect(2)
                                .paging(0, fetch)
                            )
                            res_all = await self._fts_search(
                                self._alias, knn_only,
                                query_params={"vec": embedding},
                                timeout=_FT_TIMEOUT
                            )
                            want_chat = self.chat
                            want_uid  = str(uid) if uid is not None else None
                            docs = _filter_knn_docs(getattr(res_all, "docs", []),
                                                    want_chat=want_chat,
                                                    want_uid=want_uid,
                                                    topic_hint=topic_hint)
                            class _Res:
                                pass

                            res = _Res()
                            res.docs = docs
                            logger.debug("query: used KNN-only fallback (outer), kept %d/%d docs", len(docs), len(getattr(res_all, "docs", [])))
                        except Exception as e3:
                            logger.warning("PersonaMemory.query KNN-only outer fallback failed (%s)", e3)
                            return []
                    else:
                        logger.warning("PersonaMemory.query failed (%s)", e)
                        return []

        out: List[Tuple[str, float]] = []
        ranked: list[tuple[float, str, float]] = []
        reinforce_ids: List[str] = []
        now_ts = time.time()
        for doc in res.docs:
            sim = _dist_to_sim(float(doc.vector_score))
            if sim < _MIN_SIMILARITY:
                continue
            text = doc.text if isinstance(doc.text, str) else (doc.text.decode('utf-8','ignore') if doc.text else "")
            if _RERANK_ENABLE:
                ev_raw = getattr(doc, "event_time", None)
                ts_raw = getattr(doc, "ts", None)
                comp = _rerank_score(
                    sim,
                    now_ts=now_ts,
                    event_time=_to_float(ev_raw, float("nan")) if ev_raw is not None else float("nan"),
                    ts=_to_float(ts_raw, float("nan")) if ts_raw is not None else float("nan"),
                    salience=_to_float(getattr(doc, "salience", 0.0), 0.0),
                    attachment=_to_float(getattr(doc, "attachment", 0.0), 0.0),
                    use_count=_to_int(getattr(doc, "use_count", 0), 0),
                    last_used_ts=_to_float(getattr(doc, "last_used_ts", 0.0), 0.0),
                    consolidation_age=self.CONSOLIDATION_AGE,
                )
                ranked.append((comp, text, sim))
            else:
                out.append((text, sim))
            doc_id = doc.id.decode() if isinstance(doc.id, (bytes, bytearray)) else str(doc.id)
            reinforce_ids.append(doc_id)
        if _RERANK_ENABLE and ranked:
            ranked.sort(key=lambda x: x[0], reverse=True)
            out = [(t, s) for _, t, s in ranked[:top_k]]
        if reinforce_ids:
            await self._reinforce_ids(reinforce_ids, now_ts)
            try:
                await asyncio.gather(
                    *[self._clamped_hincr(did, "salience", _SALIENCE_REINFORCE_STEP) for did in reinforce_ids]
                )
            except Exception:
                logger.debug("reinforcement (query) salience failed", exc_info=True)

        if not out and topic_hint:
            try:
                if ef_rt > 0:
                    knn_clause = f"[KNN {top_k} @embedding $vec AS vector_score EF_RUNTIME $ef]"
                    params = {"vec": embedding, "ef": ef_rt}
                else:
                    knn_clause = f"[KNN {top_k} @embedding $vec AS vector_score]"
                    params = {"vec": embedding}
                fb_filters = [f'@chat:{{{_tag_literal(self.chat)}}}']
                if uid is not None:
                    fb_filters.append(f'@uid:{{{_tag_literal(str(uid))}}}')
                _lhs_fb = " ".join(fb_filters)
                base_fb = f"({_lhs_fb})" if _lhs_fb else "*"
                q_fb = (
                    Query(f"{base_fb}=>{knn_clause}")
                    .sort_by("vector_score", asc=True)
                    .return_fields("vector_score", "text", "ts", "event_time", "salience", "attachment", "use_count", "last_used_ts")
                    .dialect(2)
                    .paging(0, top_k)
                )
                res_fb = await self._fts_search(
                    self._alias, q_fb, query_params=params,
                    timeout=_FT_TIMEOUT
                )
                for doc in res_fb.docs:
                    sim = _dist_to_sim(float(doc.vector_score))
                    if sim >= _MIN_SIMILARITY:
                        text = doc.text if isinstance(doc.text, str) else doc.text.decode('utf-8', 'ignore')
                        out.append((text, sim))
            except Exception:
                logger.debug("query: fallback without topic failed", exc_info=True)

        try:
            need_more = max(0, int(top_k) - len(out))
        except Exception:
            need_more = 0
        cap_txt = int(getattr(settings, "HYBRID_TOPK_TXT", 2))
        fetch_txt = min(cap_txt, need_more)

        if fetch_txt > 0 and getattr(self, "_memtxt_available", False):
            _timeout_cap = float(getattr(settings, "REDISSEARCH_TIMEOUT", 3.0))
            timeout_s = min(0.5 + 0.002 * max(1, fetch_txt), _timeout_cap)
            try:
                fparts = [f'@chat:{{{_tag_literal(self.chat)}}}']
                if uid is not None:
                    fparts.append(f'@uid:{{{_tag_literal(str(uid))}}}')
                if topic_hint:
                    th = _topic_tagify(topic_hint)
                    if th:
                        fparts.append(f'@topic:{{{_tag_literal(th)}}}')
                base_filters = " ".join(fparts)
                base = f"({base_filters})" if base_filters else "*"

                bm25_enable = bool(getattr(settings, "MEMTXT_BM25_ENABLE", True))
                tokens = []
                if topic_hint:
                    tokens = re.findall(r"[\w\u0400-\u04FF]+", topic_hint.lower())
                    tokens = [t for t in tokens if len(t) >= 2][:8]
                qtxt = None
                fts_expr = None
                if tokens:
                    terms = []
                    for tkn in tokens:
                        esc = _fts_escape(tkn)
                        terms.append(f"{esc}*" if len(esc) >= 3 else esc)
                    fts_expr = f"@text:({' | '.join(terms)})"
                if bm25_enable and bool(getattr(settings, "MEMTXT_TEXT_INDEXED", True)) and tokens and fts_expr:
                    qtxt = (
                        Query(f"({base}) ({fts_expr})")
                        .return_fields("text", "ts", "chat", "uid", "topic")
                        .dialect(2)
                        .paging(0, int(getattr(settings, "MEMTXT_BM25_CANDIDATES", 40)))
                    )
                else:
                    qtxt = (
                        Query(base)
                        .sort_by("ts", asc=False)
                        .return_fields("text", "ts", "chat", "uid", "topic")
                        .dialect(2)
                        .paging(0, int(getattr(settings, "MEMTXT_BM25_CANDIDATES", 40)))
                    )
                res_txt = await self._fts_search(
                    self._alias_txt, qtxt,
                    timeout=timeout_s
                )
                added = 0
                seen_norms = set(_norm_text_key(t) for t, _ in out)
                for d in getattr(res_txt, "docs", [])[:fetch_txt]:
                    txt = d.text if isinstance(d.text, str) else (d.text.decode("utf-8", "ignore") if d.text else "")
                    key = _norm_text_key(txt)
                    if not txt or key in seen_norms:
                        continue
                    out.append((txt, 0.51))
                    seen_norms.add(key)
                    added += 1
                    if added >= fetch_txt:
                        break
            except ResponseError as e:
                low = str(e).lower()
                if "ef_runtime" in low or "unknown argument" in low or "unexpected attribute" in low:
                    logger.debug("query: disabling EF_RUNTIME due to server response: %s", e)
                    self._no_ef_runtime = True
                if _is_missing_index_error(e):
                    try:
                        await self.init_index()
                        if not getattr(self, "_memtxt_available", False):
                            raise RuntimeError("memtxt index disabled")
                        if qtxt is None:
                            qtxt = (Query("*")
                                    .sort_by("ts", asc=False)
                                    .return_fields("text", "ts", "chat", "uid", "topic")
                                    .dialect(2)
                                    .paging(0, int(getattr(settings, "MEMTXT_BM25_CANDIDATES", 40))))
                        res_txt = await self._fts_search(self._alias_txt, qtxt, timeout=timeout_s)
                        added = 0
                        seen_norms = set(_norm_text_key(t) for t, _ in out)
                        for d in getattr(res_txt, "docs", [])[:fetch_txt]:
                            txt = d.text if isinstance(d.text, str) else (d.text.decode("utf-8", "ignore") if d.text else "")
                            key = _norm_text_key(txt)
                            if not txt or key in seen_norms:
                                continue
                            out.append((txt, 0.51))
                            seen_norms.add(key)
                            added += 1
                            if added >= fetch_txt:
                                break
                    except Exception:
                        logger.debug("memtxt hybrid fetch retry after init_index failed", exc_info=True)
                elif "syntax" in low and any(s in low for s in ("near chat", "near uid", "near topic", " chat", " uid", " topic")):
                    try:
                        await self.init_index()
                        if qtxt is None:
                            qtxt = (Query("*")
                                    .sort_by("ts", asc=False)
                                    .return_fields("text", "ts", "chat", "uid", "topic")
                                    .dialect(2)
                                    .paging(0, int(getattr(settings, "MEMTXT_BM25_CANDIDATES", 40))))
                        res_txt = await self._fts_search(self._alias_txt, qtxt, timeout=timeout_s)
                    except Exception:
                        try:
                            if bm25_enable and tokens and fts_expr:
                                qfb = (Query(fts_expr)
                                       .return_fields("text","ts","chat","uid","topic")
                                       .dialect(2)
                                       .paging(0, int(getattr(settings, "MEMTXT_BM25_CANDIDATES", 40))))
                            else:
                                qfb = (Query("*")
                                       .sort_by("ts", asc=False)
                                       .return_fields("text","ts","chat","uid","topic")
                                       .dialect(2)
                                       .paging(0, int(getattr(settings, "MEMTXT_BM25_CANDIDATES", 40))))
                            if getattr(self, "_memtxt_available", False):
                                res_txt = await self._fts_search(self._alias_txt, qfb, timeout=timeout_s)
                            else:
                                raise RuntimeError("memtxt index disabled")
                            want_chat = self.chat
                            want_uid  = str(uid) if uid is not None else None
                            th = _topic_tagify(topic_hint) if topic_hint else None
                            added = 0
                            seen_norms = set(_norm_text_key(t) for t, _ in out)
                            for d in getattr(res_txt, "docs", []):
                                ch = d.chat.decode() if isinstance(d.chat,(bytes,bytearray)) else (d.chat or "")
                                if ch != want_chat:
                                    continue
                                if want_uid is not None:
                                    u = d.uid.decode() if isinstance(d.uid,(bytes,bytearray)) else (d.uid or "")
                                    if u != want_uid:
                                        continue
                                if th:
                                    tp = d.topic.decode() if isinstance(d.topic,(bytes,bytearray)) else (d.topic or "")
                                    if th not in (tp.split(",") if tp else []):
                                        continue
                                txt = d.text if isinstance(d.text,str) else (d.text.decode("utf-8","ignore") if d.text else "")
                                key = _norm_text_key(txt)
                                if not txt or key in seen_norms:
                                    continue
                                out.append((txt, 0.51))
                                seen_norms.add(key)
                                added += 1
                                if added >= fetch_txt:
                                    break
                        except Exception:
                            logger.debug("memtxt fallback (no-filters) failed", exc_info=True)
                else:
                    logger.debug("memtxt hybrid fetch ResponseError", exc_info=True)
            except Exception:
                logger.debug("memtxt hybrid fetch failed", exc_info=True)

        try:
            if bool(getattr(settings, "MEMTXT_PROMOTE_ON_HIT", False)):
                promote_sali = float(getattr(settings, "MEMTXT_PROMOTE_SALIENCE", 0.62))
                promote_cap  = int(getattr(settings, "MEMTXT_PROMOTE_MAX", 2))
                promoted = 0
                snapshot = list(out)
                for txt, sim in snapshot:
                    if promoted >= promote_cap or sim < 0.51:
                        continue
                    emb = await get_embedding(txt)
                    if _is_zero_embedding(emb):
                        continue
                    try:
                        await self.record(
                            text=txt,
                            embedding=emb,
                            emotions={},
                            state_metrics={},
                            uid=uid,
                            salience=promote_sali,
                            event_frame=False,
                        )
                        promoted += 1
                    except Exception:
                        logger.debug("memtxt promotion failed for one item", exc_info=True)
                if promoted:
                    logger.debug("memtxt promotion: promoted=%d", promoted)
        except Exception:
            logger.debug("memtxt promotion skipped", exc_info=True)

        return out

    async def query_time(self, embedding: bytes, event_type: str, top_k: int = 5, uid: int | None = None) -> List[Tuple[str, float]]:
        ts = time.time()
        await self.ready()
        await self._ensure_index_available()
        logger.debug("query_time: ready.wait END (t=%.3fs)", time.time() - ts)

        if not isinstance(embedding, (bytes, bytearray)) or len(embedding) != self.EMBED_DIM * 4:
            logger.debug("query_time: bad embedding size → []")
            return []
        if _is_zero_embedding(embedding, self.EMBED_DIM):
            logger.debug("query_time: zero embedding fallback → []")
            return []

        event_type = (event_type or "").strip().lower()
        val = _tag_literal(event_type)

        try:
            top_k = int(top_k)
        except Exception:
            top_k = 5
        if top_k < 1:
            top_k = 1

        ef_rt = 0
        try:
            ef_rt = int(getattr(settings, "HNSW_EF_RUNTIME", 100) or 0)
        except Exception:
            ef_rt = 0
        if self._no_ef_runtime:
            ef_rt = 0
        occ = _pool_occupancy(self._redis)
        if ef_rt > 0 and occ > 0.0:
            new_ef = max(20, int(ef_rt * (1.0 - 0.6*min(1.0, occ))))
            if new_ef != ef_rt:
                logger.debug("query_time: EF_RUNTIME reduced from %s to %s due to pool occupancy=%.2f", ef_rt, new_ef, occ)
            ef_rt = new_ef
        if ef_rt > 0:
            knn_clause = f"[KNN {top_k} @embedding $vec AS vector_score EF_RUNTIME $ef]"
            params = {"vec": embedding, "ef": ef_rt}
        else:
            knn_clause = f"[KNN {top_k} @embedding $vec AS vector_score]"
            params = {"vec": embedding}
        if not self._hybrid_supported:
            try:
                fetch = max(top_k * 5, top_k + 2)
                knn_only = (
                    Query(f"*=>[KNN {fetch} @embedding $vec AS vector_score]")
                    .sort_by("vector_score", asc=True)
                    .return_fields(
                        "vector_score", "text", "ts", "event_time",
                        "salience", "attachment", "use_count", "last_used_ts",
                        "chat", "uid", "topic", "event_type"
                    )
                    .dialect(2)
                    .paging(0, fetch)
                )
                res_all = await self._fts_search(
                    self._alias, knn_only,
                    query_params={"vec": embedding},
                    timeout=_FT_TIMEOUT
                )
                want_chat = self.chat
                want_uid  = str(uid) if uid is not None else None
                docs = _filter_knn_docs(getattr(res_all, "docs", []),
                                        want_chat=want_chat,
                                        want_uid=want_uid,
                                        want_event_type=event_type)
                class _Res:
                    pass

                res = _Res()
                res.docs = docs
            except Exception as e3:
                logger.warning("query_time: forced KNN-only failed (%s)", e3)
                return []
        else:
            filters = [f'@chat:{{{_tag_literal(self.chat)}}}', f'@event_type:{{{val}}}']
            if uid is not None:
                filters.append(f'@uid:{{{_tag_literal(str(uid))}}}')
            _lhs_time = " ".join(filters)
            chat_ev = f"({_lhs_time})" if _lhs_time else "*"
            q = (
                Query(f"{chat_ev}=>{knn_clause}")
                .sort_by("vector_score", asc=True)
                .return_fields("vector_score", "text", "ts", "event_time", "salience", "attachment", "use_count", "last_used_ts")
                .dialect(2)
                .paging(0, top_k)
            )
            try:
                res = await self._fts_search(
                    self._alias, q, query_params=params,
                    timeout=_FT_TIMEOUT
                )
                logger.debug("query_time: search END (t=%.3fs)", time.time() - ts)
            except (ResponseError, asyncio.TimeoutError, redis.exceptions.RedisError) as e:
                if ef_rt > 0:
                    try:
                        q_fallback = (
                            Query(f'{chat_ev}=>[KNN {top_k} @embedding $vec AS vector_score]')
                            .sort_by("vector_score", asc=True)
                            .return_fields(
                                "vector_score", "text", "ts", "event_time",
                                "salience", "attachment", "use_count", "last_used_ts"
                            )
                            .dialect(2)
                            .paging(0, top_k)
                        )
                        res = await self._fts_search(
                            self._alias, q_fallback, query_params={"vec": embedding},
                            timeout=_FT_TIMEOUT
                        )
                    except ResponseError as e_fb:
                        low = str(e_fb).lower()
                        if "ef_runtime" in low or "unknown argument" in low or "unexpected attribute" in low:
                            logger.debug("query_time: disabling EF_RUNTIME due to server response: %s", e_fb)
                            self._no_ef_runtime = True
                        if _is_missing_index_error(e_fb):
                            await self._ensure_index_available()
                            try:
                                res = await self._fts_search(
                                    self._alias, q_fallback, query_params={"vec": embedding},
                                    timeout=_FT_TIMEOUT
                                )
                            except Exception as e2:
                                logger.warning("PersonaMemory.query_time retry after ensure failed (%s)", e2)
                                return []
                        elif "syntax" in low and any(s in low for s in ("near chat", "near uid", "near topic", "near event_type")):
                            self._hybrid_supported = False
                            try:
                                fetch = max(top_k * 5, top_k + 2)
                                knn_only = (
                                    Query(f"*=>[KNN {fetch} @embedding $vec AS vector_score]")
                                    .sort_by("vector_score", asc=True)
                                    .return_fields(
                                        "vector_score", "text", "ts", "event_time",
                                        "salience", "attachment", "use_count", "last_used_ts",
                                        "chat", "uid", "topic", "event_type"
                                    )
                                    .dialect(2)
                                    .paging(0, fetch)
                                )
                                res_all = await self._fts_search(
                                    self._alias, knn_only,
                                    query_params={"vec": embedding},
                                    timeout=_FT_TIMEOUT
                                )
                                want_chat = self.chat
                                want_uid  = str(uid) if uid is not None else None
                                docs = _filter_knn_docs(getattr(res_all, "docs", []),
                                                        want_chat=want_chat,
                                                        want_uid=want_uid,
                                                        want_event_type=event_type)
                                class _Res:
                                    pass

                                res = _Res()
                                res.docs = docs
                                logger.debug("query_time: used KNN-only fallback, kept %d/%d docs", len(docs), len(getattr(res_all, "docs", [])))
                            except Exception as e3:
                                logger.warning("PersonaMemory.query_time KNN-only fallback failed (%s)", e3)
                                return []
                        else:
                            logger.warning("PersonaMemory.query_time fallback failed (%s)", e_fb)
                            return []
                elif _is_missing_index_error(e):
                    await self._ensure_index_available()
                    try:
                        res = await self._fts_search(
                            self._alias, q, query_params=params,
                            timeout=_FT_TIMEOUT
                        )
                    except Exception as e2:
                        logger.warning("PersonaMemory.query_time retry failed (%s)", e2)
                        return []
                else:
                    low = str(e).lower()
                    if "ef_runtime" in low or "unknown argument" in low or "unexpected attribute" in low:
                        logger.debug("query_time: disabling EF_RUNTIME due to server response: %s", e)
                        self._no_ef_runtime = True
                    if "syntax" in low and any(s in low for s in ("near chat", "near uid", "near topic", "near event_type")):
                        self._hybrid_supported = False
                        try:
                            fetch = max(top_k * 5, top_k + 2)
                            knn_only = (
                                Query(f"*=>[KNN {fetch} @embedding $vec AS vector_score]")
                                .sort_by("vector_score", asc=True)
                                .return_fields(
                                    "vector_score", "text", "ts", "event_time",
                                    "salience", "attachment", "use_count", "last_used_ts",
                                    "chat", "uid", "topic", "event_type"
                                )
                                .dialect(2)
                                .paging(0, fetch)
                            )
                            res_all = await self._fts_search(
                                self._alias, knn_only,
                                query_params={"vec": embedding},
                                timeout=_FT_TIMEOUT
                            )
                            want_chat = self.chat
                            want_uid  = str(uid) if uid is not None else None
                            docs = _filter_knn_docs(getattr(res_all, "docs", []),
                                                    want_chat=want_chat,
                                                    want_uid=want_uid,
                                                    want_event_type=event_type)
                            class _Res:
                                pass

                            res = _Res()
                            res.docs = docs
                            logger.debug("query_time: used KNN-only fallback (outer), kept %d/%d docs", len(docs), len(getattr(res_all, "docs", [])))
                        except Exception as e3:
                            logger.warning("PersonaMemory.query_time KNN-only outer fallback failed (%s)", e3)
                            return []
                    else:
                        logger.warning("PersonaMemory.query_time failed (%s)", e)
                        return []

        try:
            if event_type == "present":
                min_sim = float(settings.MIN_MEMORY_SIMILARITY_PRESENT)
            elif event_type == "future":
                min_sim = float(settings.MIN_MEMORY_SIMILARITY_FUTURE)
            else:
                min_sim = float(settings.MIN_MEMORY_SIMILARITY_PAST)
        except Exception:
            min_sim = _MIN_SIMILARITY

        parent = getattr(self, "parent", None)
        try:
            if uid is not None and parent and getattr(parent, "attachments", None) and uid in parent.attachments:
                att = float(parent.attachments[uid].get("value", 0.0))
                if event_type in ("present", "past"):
                    min_sim = max(0.45, min_sim - 0.08*att)
                else:
                    min_sim = min(0.85, min_sim + 0.05*att)
        except Exception:
            pass

        try:
            logger.debug(
                "query_time: min_sim=%.3f (event_type=%s, chat=%s, uid=%s)",
                min_sim, event_type, self.chat, str(uid) if uid is not None else ""
            )
        except Exception:
            pass

        out: List[Tuple[str, float]] = []
        ranked: list[tuple[float, str, float]] = []
        reinforce_ids: List[str] = []
        now_ts = time.time()
        for doc in res.docs:
            sim = _dist_to_sim(float(doc.vector_score))
            if sim < min_sim:
                continue
            text = doc.text if isinstance(doc.text, str) else (doc.text.decode('utf-8','ignore') if doc.text else "")
            if _RERANK_ENABLE:
                ev_raw = getattr(doc, "event_time", None)
                ts_raw = getattr(doc, "ts", None)
                comp = _rerank_score(
                    sim,
                    now_ts=now_ts,
                    event_time=_to_float(ev_raw, float("nan")) if ev_raw is not None else float("nan"),
                    ts=_to_float(ts_raw, float("nan")) if ts_raw is not None else float("nan"),
                    salience=_to_float(getattr(doc, "salience", 0.0), 0.0),
                    attachment=_to_float(getattr(doc, "attachment", 0.0), 0.0),
                    use_count=_to_int(getattr(doc, "use_count", 0), 0),
                    last_used_ts=_to_float(getattr(doc, "last_used_ts", 0.0), 0.0),
                    consolidation_age=self.CONSOLIDATION_AGE,
                )
                ranked.append((comp, text, sim))
            else:
                out.append((text, sim))
            doc_id = doc.id.decode() if isinstance(doc.id, (bytes, bytearray)) else str(doc.id)
            reinforce_ids.append(doc_id)
        if _RERANK_ENABLE and ranked:
            ranked.sort(key=lambda x: x[0], reverse=True)
            out = [(t, s) for _, t, s in ranked[:top_k]]
        if reinforce_ids:
            await self._reinforce_ids(reinforce_ids, now_ts)
            try:
                await asyncio.gather(
                    *[self._clamped_hincr(did, "salience", _SALIENCE_REINFORCE_STEP) for did in reinforce_ids]
                )
            except Exception:
                logger.debug("reinforcement (query_time) salience clamp failed", exc_info=True)
        return out

    async def count_entries(self) -> int:
        await self.ready()
        return await self._redis.zcard(self.ZSET_IDS)


    async def _forget_if_needed(self):
        ts = time.time()
        await self.ready()
        logger.debug("_forget_if_needed: ready.wait END (t=%.3fs)", time.time() - ts)
        now = time.time()
        total = await self._redis.zcard(self.ZSET_IDS)
        if total <= self.MAX_ENTRIES:
            return
        try:
            CHUNK = int(getattr(settings, "FORGET_CHUNK_SIZE", 1500))
        except Exception:
            CHUNK = 1500
        need_del = max(0, total - self.MAX_ENTRIES)
        if total == 0:
            return

        end = min(total - 1, max(CHUNK - 1, need_del * 2 - 1))
        head_ids = await self._redis.zrange(self.ZSET_IDS, 0, end)

        rand_ids: list = []
        try:
            rnd_take = max(0, min(CHUNK, total - len(head_ids)))
            if rnd_take > 0:
                rand_res = await self._redis.execute_command("ZRANDMEMBER", self.ZSET_IDS, rnd_take)
                if rand_res:
                    if isinstance(rand_res, (bytes, bytearray)):
                        rand_ids = [rand_res]
                    else:
                        rand_ids = list(rand_res)
        except Exception:
            logger.debug("forget: ZRANDMEMBER not available or failed — continue with head-only sample", exc_info=True)

        ids = []
        seen = set()
        for e in (head_ids + rand_ids):
            k = _b2s(e, str(e))
            if k not in seen:
                seen.add(k)
                ids.append(k.encode() if isinstance(e, (bytes, bytearray)) else k)
        logger.debug("_forget_if_needed: sampled %d ids (head=%d, rand=%d, total=%d)", len(ids), len(head_ids), len(rand_ids), total)
        
        scores = []
        eid_uid_map: Dict[str, str] = {}
        pipe = self._redis.pipeline()
        for eid in ids:
            eid_s = _b2s(eid, str(eid))
            key = f"memory:{self.chat}:{eid_s}"
            pipe.hmget(key, "emotions", "event_time", "use_count", "last_used_ts", "salience", "attachment", "uid")
            pipe.exists(key)
        rows = await pipe.execute()

        it = iter(rows)
        stale_ids = []
        for idx, eid in enumerate(ids, start=1):
            hm = next(it, None)
            exists_flag = next(it, 0)
            if hm is None:
                hm = [None] * 7
            raw, ts_raw, uc_raw, lu_raw, sal_raw, att_raw, uid_raw = hm
            eid_s = _b2s(eid, str(eid))
            if not exists_flag:
                stale_ids.append(eid_s)
                continue
            uid_str = _b2s(uid_raw, "").strip()
            if uid_str:
                eid_uid_map[eid_s] = uid_str
            if raw:
                try:
                    emo_dict = json.loads(_b2s(raw, "{}")) or {}
                except Exception:
                    emo_dict = {}
            else:
                emo_dict = {}
            try:
                emo_vals = [float(v) for v in emo_dict.values() if isinstance(v, (int, float, str))]
            except Exception:
                emo_vals = []
            emo_score = (sum(emo_vals) / max(1, len(emo_vals))) if emo_vals else 0.0
            ts_ = _to_float(ts_raw, now - self.CONSOLIDATION_AGE * 2)
            recency = math.exp(-(now - ts_) / max(1.0, self.CONSOLIDATION_AGE))
            use_count    = _to_int(uc_raw, 0)
            last_used_ts = _to_float(lu_raw, 0.0)
            salience     = _to_float(sal_raw, 0.0)
            salience = max(0.0, min(1.0, salience))
            attachment   = _to_float(att_raw, 0.0)
            attachment = max(0.0, min(1.0, attachment))
            use_bonus = math.log1p(max(0, use_count)) / math.log1p(10)
            lu_tau = max(1.0, float(getattr(settings, "FORGET_LAST_USED_TAU", 7*86400)))
            last_used_bonus = math.exp(-max(0.0, now - last_used_ts)/lu_tau) if last_used_ts > 0 else 0.0

            pos_norm = idx / len(ids)
            sem_tail = pos_norm 
            score = (_EMOTION_WEIGHT*emo_score +
                     _RECENCY_WEIGHT*recency +
                     _FORGET_SALIENCE_WEIGHT*salience +
                     _FORGET_ATTACHMENT_WEIGHT*attachment +
                     0.1*sem_tail +
                     float(getattr(settings, "FORGET_USE_COUNT_WEIGHT", 0.08)) * use_bonus +
                     float(getattr(settings, "FORGET_LAST_USED_WEIGHT", 0.08)) * last_used_bonus)
            scores.append((score, eid_s))

        scores.sort(key=lambda x: x[0])
        to_remove = list(stale_ids)
        left_need = max(0, need_del - len(to_remove))
        if left_need > 0:
            extra_ids = [eid for _, eid in scores[:left_need]]
            to_remove.extend(extra_ids)
            left_need = max(0, need_del - len(to_remove))
            if left_need > 0:
                weak_ids = [eid for sc, eid in scores if sc < self.FORGET_THRESHOLD and eid not in to_remove]
                to_remove.extend(weak_ids[:left_need])
        if to_remove:
            try:
                uidsets = await self._redis.smembers(f"memory:uidsets:{self.chat}")
                uidset_names = [
                    (u.decode() if isinstance(u, (bytes, bytearray)) else str(u)) for u in (uidsets or [])
                ]
            except Exception:
                uidset_names = []
            pipe = self._redis.pipeline(transaction=True)
            for eid_s in to_remove:
                key = f"memory:{self.chat}:{eid_s}"
                pipe.delete(key)
                pipe.zrem(self.ZSET_IDS, eid_s)
                uid_str = eid_uid_map.get(eid_s, "")
                if uid_str:
                    pipe.zrem(f"memory:ids:{self.chat}:{uid_str}", eid_s)
                else:
                    for z in uidset_names:
                        pipe.zrem(z, eid_s)
            await pipe.execute()

    async def _periodic_maintenance(self):
        
        await self.ready()
        token = f"{os.getpid()}:{time.time():.6f}"
        while True:
            cycle_start = time.time()
            try:
                await self._maintenance_cycle(token)
            except asyncio.CancelledError:
                logger.info("maintenance task cancelled — exit")
                break
            except Exception:
                logger.exception("maintenance cycle failed")

            leader_key = self.MAINT_LEADER_KEY_TPL.format(chat=self.chat)
            try:
                am_leader = await self._redis.set(
                    leader_key,
                    token,
                    ex=max(30, int(self.MAINT_LEADER_TTL)),
                    nx=True,
                )
            except Exception:
                logger.debug("maintenance: leader probe failed, skipping this cycle", exc_info=True)
                am_leader = False

            if am_leader:
                cutoff = time.time() - self.CONSOLIDATION_AGE
                lock_key = f"{self.ZSET_IDS}:consolidate_lock"
                got = await self._redis.set(
                    lock_key,
                    "1",
                    ex=max(30, int(self.MAINT_INTERVAL) - 1),
                    nx=True,
                )
                if got:
                    try:
                        total_main = await self._redis.zcard(self.ZSET_IDS)
                        sample = await self._redis.zrange(self.ZSET_IDS, 0, min(500, total_main - 1)) if total_main else []
                        if sample:
                            pipe = self._redis.pipeline()
                            for eid in sample:
                                eid_s = _b2s(eid, str(eid))
                                pipe.exists(f"memory:{self.chat}:{eid_s}")
                            exists_flags = await pipe.execute()
                            pipe = self._redis.pipeline(transaction=True)
                            for eid, ok in zip(sample, exists_flags):
                                if not ok:
                                    eid_s = _b2s(eid, str(eid))
                                    pipe.zrem(self.ZSET_IDS, eid_s)
                            await pipe.execute()
                    except Exception:
                        logger.debug("ZSET stale cleanup skipped", exc_info=True)
                    
                    try:
                        txt_z = f"memtxt:ids:{self.chat}"
                        total_txt = await self._redis.zcard(txt_z)
                        if total_txt:
                            sample_txt = await self._redis.zrange(txt_z, 0, min(500, total_txt - 1))
                            if sample_txt:
                                pipe = self._redis.pipeline()
                                for doc_key in sample_txt:
                                    k = _b2s(doc_key, str(doc_key))
                                    pipe.exists(k)
                                exists_flags = await pipe.execute()
                                pipe = self._redis.pipeline(transaction=True)
                                for doc_key, ok in zip(sample_txt, exists_flags):
                                    if not ok:
                                        k = _b2s(doc_key, str(doc_key))
                                        pipe.zrem(txt_z, k)
                                await pipe.execute()
                    except Exception:
                        logger.debug("memtxt ZSET stale cleanup skipped", exc_info=True)

                    try:
                        processed_sets = 0
                        removed_uid_total = 0
                        async for z in self._redis.scan_iter(match=f"memtxt:ids:{self.chat}:*", count=1000):
                            if processed_sets >= _MEMTXT_PER_UID_CLEAN_MAXSETS:
                                break
                            processed_sets += 1
                            zname = z.decode() if isinstance(z, (bytes, bytearray)) else str(z)
                            total_uid = await self._redis.zcard(zname)
                            if not total_uid:
                                continue

                            sample_uid = await self._redis.zrange(zname, 0, min(500, total_uid - 1))
                            if not sample_uid:
                                continue

                            pipe = self._redis.pipeline()
                            for doc_key in sample_uid:
                                k = _b2s(doc_key, str(doc_key))
                                pipe.exists(k)
                            flags = await pipe.execute()

                            removed = 0
                            pipe = self._redis.pipeline(transaction=True)
                            for doc_key, ok in zip(sample_uid, flags):
                                if not ok:
                                    k = _b2s(doc_key, str(doc_key))
                                    pipe.zrem(zname, k)
                                    removed += 1
                            await pipe.execute()
                            removed_uid_total += removed

                        logger.debug("memtxt per-uid cleanup: processed_sets=%d, removed=%d",
                                    processed_sets, removed_uid_total)
                    except Exception:
                        logger.debug("memtxt per-uid ZSET stale cleanup skipped", exc_info=True)
    
                    try:
                        uidsets_key = f"memory:uidsets:{self.chat}"
                        znames = await self._redis.smembers(uidsets_key)
                        if znames:
                            pipe = self._redis.pipeline()
                            zlist = []
                            for z in znames:
                                zname = z.decode() if isinstance(z, (bytes, bytearray)) else str(z)
                                zlist.append(zname)
                                pipe.zcard(zname)
                            counts = await pipe.execute()
                            pipe2 = self._redis.pipeline(transaction=True)
                            for zname, cnt in zip(zlist, counts):
                                if not cnt:
                                    pipe2.delete(zname)
                                    pipe2.srem(uidsets_key, zname)
                            await pipe2.execute()
                    except Exception:
                        logger.debug("memory:uidsets cleanup skipped", exc_info=True)

                    try:
                        if bool(getattr(settings, "PERSONA_SUMMARIZE_ENABLE", True)):
                            old = await self._redis.zrangebyscore(self.ZSET_IDS, "-inf", cutoff)
                        else:
                            old = []

                        if len(old) >= 2:

                            try:
                                from app.tasks.celery_app import celery
                            except Exception as e:
                                celery = None
                                logger.warning("_periodic_maintenance: celery import failed, skip scheduling: %s", e)

                            try:
                                BATCH = int(getattr(settings, "CONSOLIDATION_BATCH", 300))
                            except Exception:
                                BATCH = 300

                            for i in range(0, len(old), BATCH):
                                chunk = old[i:i+BATCH]
                                pipe = self._redis.pipeline()
                                keys = []
                                for eid in chunk:
                                    eid_s = _b2s(eid, str(eid))
                                    keys.append(eid_s)
                                    pipe.hget(f"memory:{self.chat}:{eid_s}", "text")
                                    pipe.hget(f"memory:{self.chat}:{eid_s}", "consolidation_scheduled_ts")
                                rows = await pipe.execute()
                                it = iter(rows)
                                texts, keys_sched = [], []
                                now_ts = time.time()

                                try:
                                    stale_sec = int(getattr(settings, "CONSOLIDATION_SCHEDULED_STALE_SEC", 6*3600))
                                except Exception:
                                    stale_sec = 6*3600

                                for eid_s in keys:
                                    r_text = next(it, None)
                                    r_flag = next(it, None)
                                    if not r_text:
                                        continue
                                    if r_flag:
                                        try:
                                            flag_ts = float(_b2s(r_flag, "0") or 0)
                                        except Exception:
                                            flag_ts = 0.0
                                        if flag_ts > 0.0 and (now_ts - flag_ts) < stale_sec:
                                            continue
                                    txt = r_text.decode() if isinstance(r_text, (bytes,bytearray)) else r_text
                                    texts.append(txt)
                                    keys_sched.append(eid_s)
                                if not texts:
                                    continue
                                if celery:
                                    celery.send_task("persona.summarize_memory", args=[int(self.chat), texts, keys_sched])
                                    try:
                                        pset = self._redis.pipeline(transaction=True)
                                        for eid_s in keys_sched:
                                            pset.hset(f"memory:{self.chat}:{eid_s}", mapping={"consolidation_scheduled_ts": now_ts})
                                        await pset.execute()
                                    except Exception:
                                        logger.debug("mark consolidation_scheduled_ts failed", exc_info=True)
                            logger.info(
                                "_periodic_maintenance: scheduled summarize for %d old entries (t=%.3fs)",
                                len(old),
                                time.time() - cycle_start,
                            )
                    except Exception:
                        logger.exception("PersonaMemory maintenance error")

            delay = max(0.0, self.MAINT_INTERVAL - (time.time() - cycle_start))
            await asyncio.sleep(delay)
            
    async def _maintenance_cycle(self, token: str) -> None:
        await self._forget_if_needed()
