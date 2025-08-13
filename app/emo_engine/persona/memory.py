cat >app/emo_engine/persona/memory.py<< 'EOF'
#app/emo_engine/persona/memory.py
import json
import time
import math
import base64
import hashlib
import yake
import asyncio
import logging
import numpy as np
import redis.exceptions

from functools import partial
from concurrent.futures import ThreadPoolExecutor
from typing import List, Tuple, Dict
from dateparser import parse as dp_parse

from redis.commands.search.field import (
    TextField, NumericField, TagField, VectorField
)
from redis.commands.search.indexDefinition import IndexDefinition, IndexType
from redis.commands.search.query import Query
from redis.exceptions import ResponseError

from app.core.memory import get_redis
from app.clients.openai_client import _call_openai_with_retry
from app.config import settings


logger = logging.getLogger(__name__)


_EMOTION_WEIGHT = getattr(settings, "EMOTION_WEIGHT", 0.6)
_RECENCY_WEIGHT = getattr(settings, "RECENCY_WEIGHT", 0.4)
_DUP_DIST_MAX = settings.DUPLICATE_DISTANCE_MAX
_MIN_SIMILARITY = settings.MIN_MEMORY_SIMILARITY
_DP_EXECUTOR = ThreadPoolExecutor(max_workers=8)


def _tag_literal(s: str) -> str:
    s = (s or "")
    s = (s.replace("\\", "\\\\")
           .replace("|", r"\|")
           .replace(",", r"\,")
           .replace("{", r"\{")
           .replace("}", r"\}"))
    return f'"{s}"' if any(ch.isspace() for ch in s) else s


async def get_embedding(text: str) -> bytes:
    
    rds = get_redis()
    md5_key = "emb:" + hashlib.md5(text.encode("utf-8")).hexdigest()
    if cached := await rds.get(md5_key):
        return base64.b64decode(cached)

    try:
        resp = await _call_openai_with_retry(
            endpoint="embeddings.create",
            kwargs={"input": text, "model": settings.EMBEDDING_MODEL},
            timeout=settings.EMBEDDING_TIMEOUT,
        )
    except Exception as e:
        logger.warning("get_embedding: OpenAI embed failed, returning zeros: %s", e)
        empty = np.zeros(settings.EMBED_DIM, dtype=np.float32).tobytes()
        return empty

    vec = resp.data[0].embedding
    if len(vec) != settings.EMBED_DIM:
        logger.warning("get_embedding: dim mismatch %d vs %d", len(vec), settings.EMBED_DIM)
    arr = np.asarray(vec[:settings.EMBED_DIM], dtype=np.float32)
    if arr.shape[0] < settings.EMBED_DIM:
        arr = np.pad(arr, (0, settings.EMBED_DIM - arr.shape[0]))
    arr = arr.tobytes()
    try:
        await rds.set(md5_key, base64.b64encode(arr), ex=86400)
    except redis.exceptions.RedisError as e:
        logger.warning("get_embedding: Redis cache store failed: %s", e)
    return arr


def _dist_to_sim(d: float) -> float:
    return max(0.0, 1.0 - d)


class PersonaMemory:
    INDEX_NAME = "idx:memory"
    ZSET_IDS = "memory:ids"
    MAX_ENTRIES = settings.MEMORY_MAX_ENTRIES
    FORGET_THRESHOLD = settings.FORGET_THRESHOLD
    CONSOLIDATION_AGE = settings.CONSOLIDATION_AGE
    MAINT_INTERVAL = settings.MEMORY_MAINTENANCE_INTERVAL
    EMBED_DIM = settings.EMBED_DIM

    def __init__(self):
        if settings.EMBED_DIM <= 0:
            raise RuntimeError("EMBED_DIM must be positive; check settings")
        self._redis = get_redis()
        self._ready = asyncio.Event()
        asyncio.create_task(self._initialize())

    async def _initialize(self):

        ts = time.time()
        try:
            await self.init_index()
            logger.info("PersonaMemory.init_index END (t=%.3fs)", time.time() - ts)

            await self._ensure_index_dim()
            logger.info("PersonaMemory._ensure_index_dim END (t=%.3fs)", time.time() - ts)
        except asyncio.TimeoutError:
            logger.error("init_index/_ensure_index_dim timeout")
        except redis.exceptions.RedisError as e:
            logger.error("init/ensure failed due to Redis error: %s", e)
        self._ready.set()

        asyncio.create_task(self._periodic_maintenance())


    async def _ensure_index_dim(self) -> None:

        try:
            info = await self._redis.ft(self.INDEX_NAME).info()
        except (ResponseError, redis.exceptions.RedisError) as e:
            logger.warning("_ensure_index_dim skipped: Redis error: %s", e)
            return

        if isinstance(info, list):
            info = {
                (k.decode() if isinstance(k, (bytes, bytearray)) else k): v
                for k, v in zip(info[::2], info[1::2])
            }

        attrs = info.get("fields") or info.get("attributes") or []

        attr = None
        for a in attrs:
            if isinstance(a, dict):
                name = a.get("attribute") or a.get(b"attribute")
                if name in ("embedding", b"embedding"):
                    attr = {
                        (k.decode() if isinstance(k, (bytes, bytearray)) else k):
                        (v.decode() if isinstance(v, (bytes, bytearray)) else v)
                        for k, v in a.items()
                    }
                    break
            elif isinstance(a, list):
                for i in range(0, len(a) - 1, 2):
                    key = a[i]
                    val = a[i + 1]
                    if key in (b"attribute", "attribute") and val in (b"embedding", "embedding"):
                        attr = {
                            (a[j].decode() if isinstance(a[j], (bytes, bytearray)) else a[j]):
                            (a[j+1].decode() if isinstance(a[j+1], (bytes, bytearray)) else a[j+1])
                            for j in range(0, len(a) - 1, 2)
                        }
                        break
                if attr:
                    break

        if not attr:
            logger.warning("Index %s: field 'embedding' not found in info()", self.INDEX_NAME)
            return

        if "attributes" in attr and isinstance(attr["attributes"], dict):
            attrs_raw = attr["attributes"]
        else:
            attrs_raw = attr

        if isinstance(attrs_raw, dict):
            dim_seen = int(attrs_raw.get("DIM", 0))
        elif isinstance(attrs_raw, list):
            dim_seen = 0
            for i in range(0, len(attrs_raw) - 1, 2):
                if attrs_raw[i] in (b"DIM", "DIM"):
                    dim_seen = int(attrs_raw[i + 1])
                    break
        else:
            dim_seen = 0
        if dim_seen != self.EMBED_DIM:
            logger.warning(
                "Index DIM mismatch: Redis=%d vs settings.EMBED_DIM=%d → recreate",
                dim_seen, self.EMBED_DIM,
            )
            await self._redis.ft(self.INDEX_NAME).dropindex(delete_documents=True)
            try:
                await self._redis.delete(self.ZSET_IDS)
                await self._redis.delete("memory:next_id")
            except Exception:
                logger.warning("Failed to delete %s after index drop", self.ZSET_IDS, exc_info=True)
            await self.init_index()

    async def init_index(self):

        try:
            await self._redis.ft(self.INDEX_NAME).info()
            logger.debug("RedisSearch index %s already exists", self.INDEX_NAME)
            return
        except ResponseError:
            pass
        except redis.exceptions.RedisError as e:
            logger.error("init_index skipped due to Redis error: %s", e)
            return

        fields = [
            TextField("text"),
            NumericField("ts", sortable=True),
            NumericField("event_time", sortable=True),
            TagField("event_type"),
            TagField("topic"),
            TextField("emotions"),
            VectorField(
                "embedding",
                "HNSW",
                {
                    "TYPE":            "FLOAT32",
                    "DIM":             settings.EMBED_DIM,
                    "DISTANCE_METRIC": "COSINE",
                    "INITIAL_CAP":     settings.EMBED_INITIAL_CAP,
                    "M":                getattr(settings, "HNSW_M", 16),
                    "EF_CONSTRUCTION":  getattr(settings, "HNSW_EF_CONSTRUCTION", 200),
                },
            ),
        ]
        definition = IndexDefinition(prefix=["memory:"], index_type=IndexType.HASH)
        try:
            await self._redis.ft(self.INDEX_NAME).create_index(fields, definition=definition)
        except ResponseError as e:
            if "Index already exists" in str(e) or "already exists" in str(e):
                logger.warning(
                    "RedisSearch index %s already exists, skipping create_index", 
                    self.INDEX_NAME
                )
                return
            raise
        

    async def ready(self) -> None:
        await self._ready.wait()


    @staticmethod
    def _extract_topics(txt: str, top_k: int = 3) -> List[str]:

        try:
            from langdetect import detect
            lang = detect(txt)
        except Exception:
            lang = "en"

        try:
            kw = yake.KeywordExtractor(lan=lang, top=top_k)
        except Exception:
            kw = yake.KeywordExtractor(lan="en", top=top_k)

        try:
            return [w for w, _ in kw.extract_keywords(txt)]
        except Exception:
            return []


    async def record(self, text: str, embedding: bytes, emotions: Dict[str, float], state_metrics: Dict[str, float]):

        ts = time.time()
        
        await self._ready.wait()
        eid = await self._redis.incr("memory:next_id")
        key = f"memory:{eid}"
        try:
            dt = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    _DP_EXECUTOR,
                    partial(
                        dp_parse,
                        text,
                        settings={'PREFER_DATES_FROM': 'past', 'REQUIRE_PARTS': ['day', 'month']},
                    )
                ),
                timeout=1.0
            )
        except asyncio.TimeoutError:
            dt = None
        except Exception:
            logger.exception("date parsing failed")
            dt = None
        event_time = dt.timestamp() if dt else time.time()

        if abs(event_time - time.time()) < 3600:
            etype = "present"
        elif event_time < time.time():
            etype = "past"
        else:
            etype = "future"

        k    = settings.REDISSEARCH_KNN_K
        tout = settings.REDISSEARCH_TIMEOUT

        q = (
            Query(f"(*)=>[KNN {k} @embedding $vec AS vector_score]")
            .sort_by("vector_score")
            .return_fields("vector_score")
            .dialect(2)
        )
        try:
            res = await asyncio.wait_for(
                self._redis.ft(self.INDEX_NAME)
                    .search(q, query_params={"vec": embedding}),
                timeout=tout,
            )
            logger.info("record: dedupe search END (t=%.3fs)", time.time() - ts)
        except Exception as e:
            logger.warning("record: dedupe search failed (%s), skipping deduplication", e)
            res = None

        pipe = self._redis.pipeline(transaction=True)
        if res:
            for doc in res.docs:
                try:
                    dist = float(doc.vector_score)
                except (TypeError, ValueError):
                    continue
                if dist <= _DUP_DIST_MAX:
                    doc_id = doc.id.decode() if isinstance(doc.id, (bytes, bytearray)) else str(doc.id)
                    if ":" in doc_id:
                        old = doc_id.split(":", 1)[1]
                    else:
                        old = doc_id
                    pipe.delete(f"memory:{old}")
                    pipe.zrem(self.ZSET_IDS, old)

        topics_raw = self._extract_topics(text)
        topics = [ (w or "").replace(",", " ").replace('"', " ").replace("|", " ").strip() for w in topics_raw if w ]
        data = {
            "text":       text,
            "ts":         time.time(),
            "event_time": event_time,
            "event_type": etype,
            "emotions":   json.dumps(emotions),
            "topic":      ",".join(topics) if topics else "",
            "embedding":  embedding,
        }
        data.update({k: v for k, v in state_metrics.items()})
        pipe.hset(key, mapping=data)
        pipe.zadd(self.ZSET_IDS, {str(eid): event_time})
        await pipe.execute()
        logger.info("record: hset/zadd END (t=%.3fs)", time.time() - ts)

        count = await self._redis.zcard(self.ZSET_IDS)
        if count > self.MAX_ENTRIES:
            await self._forget_if_needed()

    async def query(self, embedding: bytes, top_k: int = 5, topic_hint: str | None = None) -> List[Tuple[str, float]]:

        ts = time.time()
        
        await self._ready.wait()
        logger.debug("query: ready.wait END (t=%.3fs)", time.time() - ts)

        if topic_hint:
            qbase = f'(@topic:{{{_tag_literal(topic_hint)}}})'
        else:
            qbase = "(*)"
        q = (
            Query(f"{qbase}=>[KNN {top_k} @embedding $vec AS vector_score]")
            .sort_by("vector_score")
            .return_fields("vector_score", "text")
            .dialect(2)
        )
        try:
            res = await asyncio.wait_for(
                self._redis.ft(self.INDEX_NAME).search(
                    q, query_params={"vec": embedding}
                ),
                timeout=settings.REDISSEARCH_TIMEOUT
            )
            logger.info("query: search END (t=%.3fs)", time.time() - ts)
        except Exception as e:
            logger.warning("PersonaMemory.query failed (%s)", e)
            return []
        out: List[Tuple[str, float]] = []
        for doc in res.docs:
            sim = _dist_to_sim(float(doc.vector_score))
            if sim < _MIN_SIMILARITY:
                continue
            text = doc.text
            if isinstance(text, (bytes, bytearray)):
                text = text.decode('utf-8', 'ignore')
            out.append((text, sim))
        return out

    async def query_time(self, embedding: bytes, event_type: str, top_k: int = 5) -> List[Tuple[str, float]]:

        ts = time.time()
        
        await self._ready.wait()
        logger.debug("query_time: ready.wait END (t=%.3fs)", time.time() - ts)
        val = _tag_literal(event_type)
        q = (
            Query(
                f'(@event_type:{{{val}}})=>[KNN {top_k} @embedding $vec AS vector_score]'
            )
            .sort_by("vector_score")
            .return_fields("vector_score", "text")
            .dialect(2)
        )
        try:
            res = await asyncio.wait_for(
                self._redis.ft(self.INDEX_NAME).search(
                    q, query_params={"vec": embedding}
                ),
                timeout=settings.REDISSEARCH_TIMEOUT
            )
            logger.info("query_time: search END (t=%.3fs)", time.time() - ts)
        except Exception as e:
            logger.warning("PersonaMemory.query_time failed (%s)", e)
            return []
        out: List[Tuple[str, float]] = []
        for doc in res.docs:
            sim = _dist_to_sim(float(doc.vector_score))
            if sim < _MIN_SIMILARITY:
                continue
            text = doc.text
            if isinstance(text, (bytes, bytearray)):
                text = text.decode("utf-8", "ignore")
            out.append((text, sim))
        return out

    async def count_entries(self) -> int:
        await self._ready.wait()
        return await self._redis.zcard(self.ZSET_IDS)


    async def _forget_if_needed(self):

        ts = time.time()
        
        await self._ready.wait()
        logger.debug("_forget_if_needed: ready.wait END (t=%.3fs)", time.time() - ts)
        now = time.time()
        ids = await self._redis.zrange(self.ZSET_IDS, 0, -1)
        logger.debug("_forget_if_needed: fetched %d ids", len(ids))
        scores = []
        pipe = self._redis.pipeline()
        for eid in ids:
            eid_s = eid.decode() if isinstance(eid, (bytes, bytearray)) else str(eid)
            key = f"memory:{eid_s}"
            pipe.hget(key, "emotions")
            pipe.hget(key, "event_time")
        rows = await pipe.execute()

        it = iter(rows)
        for idx, eid in enumerate(ids, start=1):
            raw = next(it, None)
            ts_raw = next(it, None)
            if raw:
                try:
                    raw_str = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
                    emo_dict = json.loads(raw_str) or {}
                except Exception:
                    emo_dict = {}
            else:
                emo_dict = {}
            try:
                emo_vals = [float(v) for v in emo_dict.values()]
            except Exception:
                emo_vals = []
            emo_score = (sum(emo_vals) / max(1, len(emo_vals))) if emo_vals else 0.0
            if ts_raw is None:
                ts_ = now
            else:
                ts_str = ts_raw.decode() if isinstance(ts_raw, (bytes, bytearray)) else ts_raw
                ts_ = float(ts_str)
            recency = math.exp(-(now - ts_)/self.CONSOLIDATION_AGE)
            pos_norm = idx/len(ids)
            sem_tail = 1.0 - pos_norm
            total = (_EMOTION_WEIGHT*emo_score +
                     _RECENCY_WEIGHT*recency    +
                     0.1*sem_tail)
            eid_s = eid.decode() if isinstance(eid, (bytes, bytearray)) else str(eid)
            scores.append((total, eid_s))
        scores.sort(key=lambda x: x[0])
        extra = scores[: max(0, len(scores) - self.MAX_ENTRIES)]
        weak  = [p for p in scores if p[0] < self.FORGET_THRESHOLD]
        to_remove = list({eid for _, eid in (extra + weak)})
        if to_remove:
            pipe = self._redis.pipeline(transaction=True)
            for eid_s in to_remove:
                pipe.delete(f"memory:{eid_s}")
                pipe.zrem(self.ZSET_IDS, eid_s)
            await pipe.execute()

    async def _periodic_maintenance(self):
        
        await self._ready.wait()
        while True:
            try:
                cycle_ts = time.time()
                cutoff = time.time() - self.CONSOLIDATION_AGE
                old = await self._redis.zrangebyscore(self.ZSET_IDS, "-inf", cutoff)
                if len(old) >= 2:

                    texts = []
                    for eid in old:
                        eid_s = eid.decode() if isinstance(eid, (bytes, bytearray)) else str(eid)
                        t = await self._redis.hget(f"memory:{eid_s}", "text")
                        if t:
                            texts.append(t.decode() if isinstance(t, (bytes,bytearray)) else t)

                    keys = [eid.decode() if isinstance(eid, (bytes, bytearray)) else str(eid) for eid in old]
                    
                    from app.tasks.celery_app import celery
                    celery.send_task("persona.summarize_memory", args=[texts, keys])

                    pipe_del = self._redis.pipeline(transaction=True)
                    for eid in old:
                        eid_s = eid.decode() if isinstance(eid, (bytes, bytearray)) else str(eid)
                        pipe_del.delete(f"memory:{eid_s}")
                        pipe_del.zrem(self.ZSET_IDS, eid_s)
                    await pipe_del.execute()
                    logger.info("_periodic_maintenance: cleaned %d old entries (t=%.3fs)",
                                len(old), time.time() - cycle_ts)
            except Exception:
                logger.exception("PersonaMemory maintenance error")
            await asyncio.sleep(self.MAINT_INTERVAL)
EOF