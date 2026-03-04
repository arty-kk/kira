#app/emo_engine/persona/ltm.py
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import os
import math
import time
import unicodedata

from typing import Dict, List, Optional, Tuple, Callable
from collections import OrderedDict

from dateparser import parse as dp_parse
from dateutil.parser import isoparse
from dateutil.tz import UTC
from redis.exceptions import ResponseError
from redis.commands.search.field import (
    NumericField, TagField, VectorField, TextField
)
try:
    from redis.commands.search.indexDefinition import IndexDefinition, IndexType
except Exception:
    from redis.commands.search.index_definition import IndexDefinition, IndexType
from redis.commands.search.query import Query

from app.config import settings
from app.prompts_base import ltm_extract_system_prompt, ltm_extract_user_prompt
from app.core.memory import get_redis_vector
from app.clients.openai_client import _call_openai_with_retry, _get_output_text
from .memory import get_embedding, _fallback_rel, _DIM as _MEM_EMBED_DIM

logger = logging.getLogger(__name__)

_LTM_LAST_ACTIVE_Z = "ltm:last_active"
_DP_SETTINGS = {'PREFER_DATES_FROM':'future','TIMEZONE':'UTC','RETURN_AS_TIMEZONE_AWARE':True}
_LOG1P_10 = math.log1p(10.0)
_EMB_TIMEOUT = float(getattr(settings, "EMBED_TIMEOUT_SECS", 10.0))
_EMBED_MAX_CHARS = int(getattr(settings, "EMBED_MAX_CHARS", 1024))
_RS_TIMEOUT = int(getattr(settings, "REDISSEARCH_TIMEOUT", 3))
try:
    _RS_SERVER_TIMEOUT_MS = int(getattr(settings, "REDISSEARCH_SERVER_TIMEOUT_MS", 0) or 0)
except Exception:
    _RS_SERVER_TIMEOUT_MS = 0
try:
    _LTM_DEMOTE_USEDCOUNT_PROTECT = int(getattr(settings, "LTM_DEMOTE_USEDCOUNT_PROTECT", 3))
except Exception:
    _LTM_DEMOTE_USEDCOUNT_PROTECT = 3

def _fmt_utc(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts)) + "Z"

# --- Rerank weights---
_LTM_W_CONF = float(getattr(settings, "LTM_W_CONF", 0.15))
_LTM_W_RECENCY = float(getattr(settings, "LTM_W_RECENCY", 0.10))
_LTM_W_USE = float(getattr(settings, "LTM_W_USE", 0.08))
_LTM_W_LASTUSED = float(getattr(settings, "LTM_W_LASTUSED", 0.08))
_LTM_LAST_USED_TAU = float(getattr(settings, "LTM_LAST_USED_TAU", 7*86400))
_LTM_RECENCY_TAU = float(getattr(settings, "LTM_RECENCY_TAU", 14*86400))
_LTM_W_TIME = float(getattr(settings, "LTM_W_TIME", 0.25))
_LTM_PLAN_TIME_TAU_SECS= float(getattr(settings, "LTM_PLAN_TIME_TAU_SECS", 7*86400))

_WS_RE = re.compile(r"\s+")
_WINDOW_SPLIT_RE = re.compile(
    r"\s*(?:\bto\b|\buntil\b|\btill\b|до|по|\bhasta\b|\bau\b|\bal\b|\bbis\b|"
    r"\bat\u00E9\b|\bjusqu(?:'|’)?(?:a|à)\b|\bfino a\b|(?<=\s)[-–—](?=\s))\s*",
    re.IGNORECASE
)

_CANON_KEYS: tuple[str, ...] = (
    # Identity & comms
    "name_to_call","first_name","last_name","full_name","pronouns","gender","age","birthday","timezone",
    "languages","native_language","locale","country","city","nationality","citizenship",
    "occupation","role","job_title","profession","industry","seniority","company","employer",
    "work_mode","work_hours","work_days","meeting_pref","preferred_contact_time",
    "education","degree","marital_status","relationship_status","kids","has_children",
    "pets","pet_names",
    # Contact / social
    "email","phone","messengers","social_media",
    # Preferences: comms & style
    "address_style","formality","form_of_address","communication_style","voice_pref","call_pref",
    "no_smalltalk","no_emojis","no_links","no_voice","short_answers","summary_pref","dm_ok","email_ok",
    # Daily routines
    "wake_time","sleep_time","sleep_schedule","lunch_time","gym_time","fitness_routine","focus_hours",
    "commute_time","weekend_prefs",
    # Tech stack
    "devices","os_pref","editor_ide","terminal","cloud","tools","messaging_tools","calendar_tool",
    "browser","search_engine","notifications_pref",
    # Dev / DS
    "stack","frontend","backend","mobile","ml_libs","db","ci_cd","hosting",
    # Content & media
    "music_genres","artists","favorite_bands","podcasts","movies","movie_genres","series","series_genres",
    "books","book_genres","authors","games","game_genres","youtube_channels","news_sources",
    # Food & life
    "diet","cuisine","allergies","food_likes","food_dislikes","lactose_intolerant","gluten_free",
    "coffee_pref","tea_pref","alcohol_pref","drinker","smoker","spicy_tolerance",
    # Leisure & sports
    "hobbies","interests","sports","favorite_sport","sports_team","travel_pref","travel_frequency",
    "transport_pref","visa_status",
    # Finance / ops
    "currency","budget_sensitivity","spending_limit","payment_methods","income_bracket",
    # Beliefs / views
    "religion","politics",
)

_KEY_SYNONYMS_EN: dict[str, str] = {
    "name":"name_to_call","nickname":"name_to_call","preferred_name":"name_to_call","display_name":"name_to_call","handle":"name_to_call","call_me":"name_to_call",
    "firstname":"first_name","first-name":"first_name","last-name":"last_name","lastname":"last_name","surname":"last_name",
    "full name":"full_name","fullname":"full_name",
    "sex":"gender","pronoun":"pronouns","pronouns":"pronouns","dob":"birthday","birthdate":"birthday",
    "tz":"timezone","time_zone":"timezone","time-zone":"timezone",
    "langs":"languages","language":"languages","native_lang":"native_language","nation":"country","city_town":"city",
    "locale":"locale","location_city":"city","location_country":"country",
    "job":"occupation","position":"role","title":"job_title","level":"seniority","company_name":"company","workplace":"employer",
    "remote":"work_mode","hybrid":"work_mode","office":"work_mode","meeting_days":"work_days",
    "manager":"employer",
    "education_level":"education","degree_name":"degree","marriage":"marital_status","children":"kids","kids_count":"kids",
    "mail":"email","email_address":"email","phone_number":"phone","im":"messengers","socials":"social_media","social":"social_media",
    "tone":"address_style","style":"address_style","formality_level":"formality","form_of_address":"address_style",
    "communication_style":"address_style","call_pref":"voice_pref",
    "no small talk":"no_smalltalk","no smalltalk":"no_smalltalk","no emoji":"no_emojis","no emojis":"no_emojis",
    "concise":"short_answers","short_replies":"short_answers","summaries":"summary_pref","ok_to_dm":"dm_ok",
    "wake":"wake_time","sleep":"sleep_time","sleep_schedule":"sleep_time","work":"work_hours","lunch":"lunch_time","gym":"gym_time","focus":"focus_hours",
    "commute":"commute_time","weekend":"weekend_prefs",
    "os":"os_pref","ide":"editor_ide","code_editor":"editor_ide","shell":"terminal","clouds":"cloud","apps":"tools",
    "chat_apps":"messaging_tools","messaging":"messaging_tools","calendar":"calendar_tool",
    "tech_stack":"stack","fe":"frontend","be":"backend","dbms":"db","cicd":"ci_cd","deploy":"hosting",
    "music":"music_genres","singers":"artists","bands":"favorite_bands","films":"movies","movies_genres":"movie_genres",
    "tv":"series","tv_series":"series","series_genres":"series_genres","authors_list":"authors","gaming":"games","games_genres":"game_genres",
    "channels":"youtube_channels","news":"news_sources","book_genres":"book_genres",
    "food":"diet","kitchen":"cuisine","allergy":"allergies","likes_food":"food_likes","dislikes_food":"food_dislikes",
    "coffee":"coffee_pref","tea":"tea_pref","alcohol":"alcohol_pref","drinks_alcohol":"drinker",
    "lactose":"lactose_intolerant","gluten":"gluten_free","spicy":"spicy_tolerance",
    "pastime":"hobbies","interest":"interests","sport":"sports","favorite_sport":"favorite_sport","team":"sports_team","travel":"travel_pref",
    "travel_style":"travel_pref","transport":"transport_pref","visa":"visa_status",
    "currency_code":"currency","budget":"budget_sensitivity","limit":"spending_limit","payments":"payment_methods",
    "income":"income_bracket","religion":"religion","politics":"politics",
}

_MULTI_VAL_KEYS: set[str] = {
    "languages","hobbies","interests","music_genres","movie_genres","series_genres",
    "game_genres","book_genres","authors","favorite_bands","sports","messengers",
    "social_media","devices","tools","news_sources","cuisine","food_likes","food_dislikes",
    "movies","series","podcasts","artists","games","youtube_channels"
}

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())

def _canon_key(key: str) -> str:
    if not key:
        return ""
    k = _norm(key)
    if k in _CANON_KEYS:
        return k
    if k in _KEY_SYNONYMS_EN:
        t = _KEY_SYNONYMS_EN[k]
        return t if t in _CANON_KEYS else ""
    k2 = re.sub(r"[^a-z0-9_]+", "_", k).strip("_")
    if k2 in _CANON_KEYS:
        return k2
    if k2 in _KEY_SYNONYMS_EN:
        t2 = _KEY_SYNONYMS_EN[k2]
        return t2 if t2 in _CANON_KEYS else ""
    return ""

CANON_KEYS: tuple[str, ...] = _CANON_KEYS
canon_key = _canon_key

try:
    _SENS_RAW = list(getattr(settings, "LTM_SENSITIVE_KEYS", []))
except Exception:
    _SENS_RAW = []
SENSITIVE_KEYS = { canon_key(k) or (k or "") for k in _SENS_RAW if (canon_key(k) or (k or "")) }
try:
    SENSITIVE_ALLOWED = bool(int(os.getenv("LTM_ALLOW_SENSITIVE", "0")))
except Exception:
    SENSITIVE_ALLOWED = False

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

def _is_missing_index_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(
        s in msg
        for s in (
            "no such index",
            "unknown index",
            "unknown index name",
            "index does not exist",
            "index not found",
        )
    )

def _hget_val(rec: dict, key: str, default=None):
    if not isinstance(rec, dict):
        return default
    v = rec.get(key)
    if v is None:
        v = rec.get(key.encode("utf-8"))
    if isinstance(v, (bytes, bytearray)):
        try:
            v = v.decode("utf-8", "ignore")
        except Exception:
            pass
    return v if v is not None else default

def _hget_float(rec: dict, key: str, default: float = 0.0) -> float:
    v = _hget_val(rec, key, default)
    try:
        return float(v)
    except Exception:
        return default

def _hget_int(rec: dict, key: str, default: int = 0) -> int:
    v = _hget_val(rec, key, default)
    try:
        return int(float(v))
    except Exception:
        return default

def _as_str(x):
    return x.decode("utf-8", "ignore") if isinstance(x, (bytes, bytearray)) else x

def _as_float(x, default: float = 0.0) -> float:
    try:
        return float(_as_str(x))
    except Exception:
        return default

def _as_int(x, default: int = 0) -> int:
    try:
        return int(float(_as_str(x)))
    except Exception:
        return default

def _finite_pos(x) -> float:
    try:
        v = float(_as_str(x))
        return v if math.isfinite(v) and v > 0.0 else 0.0
    except Exception:
        return 0.0

def _excerpt(s: str, max_len: int = 240) -> str:
    s = (s or "").replace("\n", " ").replace("\r", " ")
    s = _WS_RE.sub(" ", s).strip()
    return s if len(s) <= max_len else (s[: max_len - 1] + "…")

def _clip_for_embed(s: str) -> str:
    s = unicodedata.normalize("NFKC", (s or ""))
    if len(s) <= _EMBED_MAX_CHARS:
        return s
    cut = s[:_EMBED_MAX_CHARS]
    sp = cut.rfind(" ")
    return cut[:sp] if sp > 0 and _EMBED_MAX_CHARS - sp < 40 else cut

_PII_RE = re.compile(
    r'([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})|((?<!\w)(?:\+?\d[\d\s().-]{6,}\d)(?!\w))'
)

def _redact(s: Optional[str]) -> str:
    s = s or ""
    return _PII_RE.sub("[redacted]", s)

def _tag_literal(s: str) -> str:
    s = (s or "")
    s = (s.replace("\\", "\\\\")
           .replace('"', r'\"')
           .replace("|", r"\|")
           .replace(",", r"\,")
           .replace("{", r"\{")
           .replace("}", r"\}")
           .replace("\r", " ").replace("\n", " "))
    needs_quotes = any(ch.isspace() for ch in s) or any(sym in s for sym in (",","|","{","}"))
    return f'"{s}"' if needs_quotes else s

def _now() -> float:
    return time.time()

def _snake(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", (s or "").strip().lower())
    s = re.sub(r"_{2,}", "_", s).strip("_")
    return s or "misc"

def _hash8(s: str) -> str:
    return hashlib.md5((s or "").encode("utf-8")).hexdigest()[:8]

def _dist_to_sim(d: float) -> float:

    try:
        dv = float(d)
    except Exception:
        return 0.0
    if not math.isfinite(dv) or dv < 0.0:
        return 0.0

    if dv <= 2.0:
        sim = 1.0 - dv / 2.0
    else:
        sim = 1.0 / (1.0 + dv)
    if sim < 0.0:
        return 0.0
    if sim > 1.0:
        return 1.0
    return sim

def _clamp01(x: float) -> float:
    try:
        if x is None:
            return 0.0
        v = float(x)
        if not math.isfinite(v):
            return 0.0
        if v < 0.0:
            return 0.0
        if v > 1.0:
            return 1.0
        return v
    except Exception:
        return 0.0

def _exp_decay_from(now_ts: float, ref_ts: float, tau: float) -> float:
    try:
        if ref_ts <= 0.0 or tau <= 0.0:
            return 0.0
        return math.exp(-max(0.0, now_ts - ref_ts) / max(1.0, tau))
    except Exception:
        return 0.0

def _log1p_norm10(x: int) -> float:
    return math.log1p(max(0, int(x))) / _LOG1P_10

def _norm_val(s: str) -> str:
    try:
        s = unicodedata.normalize("NFKC", s or "")
    except Exception:
        s = (s or "")
    s = s.lower()
    s = _WS_RE.sub(" ", s).strip()
    return s

def _strip_start_prep(s: str) -> str:

    return re.sub(
        r'^\s*(?:с|from|desde|de|da|dal|du|von|od)\b[:\s]*',
        '',
        (s or ''),
        flags=re.IGNORECASE
    )

def _extraction_schema() -> dict:
    str_or_null = {"anyOf": [{"type":"string"}, {"type":"null"}]}
    num01 = {"type":"number", "minimum": 0.0, "maximum": 1.0}
    key_enum = {"type": "string", "enum": list(_CANON_KEYS)}
    key_string = {"type": "string"}
    return {
        "type": "object",
        "properties": {
            "facts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key":   key_enum,
                        "value": {"type":"string"},
                        "confidence": num01,
                    },
                    "required": ["key","value","confidence"],
                    "additionalProperties": False,
                },
            },
            "boundaries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key":   key_string,
                        "value": {"type":"string"},
                        "confidence": num01,
                    },
                    "required": ["key","value","confidence"],
                    "additionalProperties": False,
                },
            },
            "plans": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type":"string"},
                        "due_iso": str_or_null,
                        "window_text": str_or_null,
                        "recurrence": str_or_null,
                        "confidence": num01,
                    },
                    "required": ["title","due_iso","window_text","recurrence","confidence"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["facts","boundaries","plans"],
        "additionalProperties": False,
    }


class LongTermMemory:

    FACTS_IX  = "idx:facts"
    PLANS_IX  = "idx:plans"
    BOUNDS_IX = "idx:bounds"
    SCHEMA_VER = 2
    INIT_LOCK_KEY = "lock:ltm:init"
    INIT_LOCK_TTL = int(getattr(settings, "INDEX_INIT_LOCK_TTL", 30))

    _IDLE_WIPE_DAYS = int(getattr(settings, "LTM_IDLE_FULL_WIPE_DAYS", 30))
    _IDLE_WIPE_BATCH = int(getattr(settings, "LTM_IDLE_WIPE_BATCH", 20))

    def __init__(self) -> None:
        self._redis = get_redis_vector()
        self._init_scheduled = False
        try:
            self._redis_loop_id = id(asyncio.get_running_loop())
        except RuntimeError:
            self._redis_loop_id = None
        self._ready = asyncio.Event()
        self._index_lock = asyncio.Lock()
        self._prune_next: Dict[int, float] = {}
        self._dim = int(getattr(settings, "EMBED_DIM", 3072))
        self._emb_fld = f"emb_{self._dim}"
        self._emb_cache: "OrderedDict[str, bytes]" = OrderedDict()
        self._emb_cache_cap = int(getattr(settings, "EMBED_LRU_CAP", 256))
        self._no_ef_runtime = False
        self._cool_secs = float(getattr(settings, "LTM_COOLDOWN_SECS", 90.0))
        self._cool_turns = int(getattr(settings, "LTM_COOLDOWN_TURNS", 2))
        self._max_per_prompt = int(getattr(settings, "LTM_MAX_PER_PROMPT", 32))
        self._min_sim = float(getattr(settings, "LTM_MIN_SIM", 0.55))
        self._ef_runtime = int(getattr(settings, "HNSW_EF_RUNTIME", 80))
        self._normalize_cosine = bool(int(os.getenv("EMBED_NORMALIZE_FOR_COSINE", "1")))
        self._plan_demote_sim_thr = float(getattr(settings, "LTM_PLAN_DEMOTE_SIM_THR", 0.75))
        try:
            self._zero_vec = b"\x00" * (4 * self._dim)
            loop = asyncio.get_running_loop()
            loop.create_task(self._initialize())
            self._init_scheduled = True
        except RuntimeError:
            pass
        if self._dim != int(_MEM_EMBED_DIM):
            msg = f"EMBED_DIM mismatch: settings={self._dim}, memory.py={_MEM_EMBED_DIM}"
            if os.getenv("LTM_ALLOW_DIM_MISMATCH", "0") != "1":
                logger.error(msg)
                raise RuntimeError(msg)
            else:
                logger.warning("%s (continuing due to LTM_ALLOW_DIM_MISMATCH=1)", msg)

    def _ensure_redis(self):
        try:
            cur = id(asyncio.get_running_loop())
        except RuntimeError:
            return self._redis
        if (self._redis is None) or (self._redis_loop_id != cur):
            self._redis = get_redis_vector()
            self._redis_loop_id = cur
        return self._redis

    def _ensure_vec(self, vec: Optional[bytes]) -> bytes:
        try:
            if isinstance(vec, memoryview):
                vec = vec.tobytes()
            elif isinstance(vec, bytearray):
                vec = bytes(vec)
            if not isinstance(vec, (bytes,)):
                return self._zero_vec
            if len(vec) != 4 * self._dim:
                return self._zero_vec
            try:
                if not any(vec):
                    return self._zero_vec
            except Exception:
                pass
            if self._normalize_cosine:
                try:
                    import array
                    import math as _m
                    a = array.array("f")
                    a.frombytes(vec)
                    s = 0.0
                    for x in a:
                        s += x * x
                    if s > 0.0:
                        if 0.98 <= s <= 1.02:
                            return bytes(vec)
                        inv = 1.0 / _m.sqrt(s)
                        for i in range(len(a)):
                            a[i] *= inv
                        return a.tobytes()
                except Exception:
                    pass
            return bytes(vec)
        except Exception:
            return self._zero_vec

    async def _embed_cached(self, text: str) -> Optional[bytes]:

        model_tag = str(getattr(settings, "EMBED_MODEL_NAME",
                                getattr(settings, "REASONING_MODEL", "")) or "")
        clipped = _clip_for_embed(text or "")
        if not clipped:
            return None
        cache_key = f"{self._dim}:{model_tag}:{clipped}"
        v = self._emb_cache.get(cache_key)
        if v is not None:
            try:
                self._emb_cache.move_to_end(cache_key)
            except Exception:
                pass
            return v
        t0 = time.perf_counter()
        try:
            emb_raw = await asyncio.wait_for(get_embedding(clipped), timeout=_EMB_TIMEOUT)
            logger.info(
                "openai.timing: embed model=%s chars=%d duration=%.3fs",
                model_tag or "unknown", len(clipped), time.perf_counter() - t0
            )
        except Exception as e:
            logger.warning(
                "openai.timing: embed model=%s chars=%d error=%s duration=%.3fs",
                model_tag or "unknown", len(clipped),
                getattr(e, "__class__", type(e)).__name__,
                time.perf_counter() - t0
            )
            emb_raw = None

        v = self._ensure_vec(emb_raw)

        if v and any(v):
            self._emb_cache[cache_key] = v
            try:
                if len(self._emb_cache) > self._emb_cache_cap:
                    self._emb_cache.popitem(last=False)
            except Exception:
                pass
            return v
        return None

    async def record_activity(self, uid: int) -> None:
        self._ensure_redis()
        try:
            now = _now()
            await self._redis.zadd(_LTM_LAST_ACTIVE_Z, {str(uid): now})
        except Exception:
            logger.debug("record_activity failed", exc_info=True)

    async def drop_user(self, uid: int) -> None:
        self._ensure_redis()
        patterns = (f"facts:{uid}:*", f"plans:{uid}:*", f"bounds:{uid}:*")
        total = 0
        try:
            for pat in patterns:
                cursor = 0
                while True:
                    try:
                        cursor, keys = await self._redis.scan(cursor=cursor, match=pat, count=1000)
                    except TypeError:
                        cursor, keys = await self._redis.scan(cursor, pat, 1000)
                    if keys:
                        pipe = self._redis.pipeline(transaction=True)
                        for k in keys:
                            try:
                                pipe.unlink(k)
                            except AttributeError:
                                pipe.delete(k)
                            total += 1
                        try:
                            await pipe.execute()
                        except Exception:
                            pass
                    if cursor == 0:
                        break
            try:
                await self._redis.zrem(_LTM_LAST_ACTIVE_Z, str(uid))
            except Exception:
                pass
            logger.info("LTM: fully wiped uid=%s total_keys=%d", uid, total)
        except Exception:
            logger.exception("drop_user failed for uid=%s", uid)

    async def maybe_prune(self, uid: int) -> None:
        self._ensure_redis()
        if hasattr(self._redis, "latency_ms") and getattr(self._redis, "latency_ms") > int(getattr(settings, "REDIS_LATENCY_ABORT_MS", 120)):
            return
        try:
            now = _now()
            next_ts = float(self._prune_next.get(uid, 0.0))
            min_period = float(getattr(settings, "LTM_PRUNE_MIN_PERIOD_SECS", 2*3600))
            prob = float(getattr(settings, "LTM_PRUNE_PROB", 0.25))
            if now < next_ts:
                return
            if random.random() > prob:
                return
            self._prune_next[uid] = now + min_period
            cutoff = now - max(1, self._IDLE_WIPE_DAYS) * 86400
            try:
                stale = await self._redis.zrangebyscore(_LTM_LAST_ACTIVE_Z, "-inf", cutoff, start=0, num=self._IDLE_WIPE_BATCH)
                for u in stale or []:
                    try:
                        su = int(_as_str(u))
                    except Exception:
                        continue
                    if su > 0:
                        await self.drop_user(su)
            except Exception:
                logger.debug("idle full-wipe scan failed", exc_info=True)
            try:
                last = await self._redis.zscore(_LTM_LAST_ACTIVE_Z, str(uid))
            except Exception:
                last = None
            if last is not None:
                try:
                    last = float(last)
                except Exception:
                    last = 0.0
            prune_idle_days = int(getattr(settings, "LTM_PRUNE_IF_IDLE_DAYS", 7))
            if not last or (now - last) >= prune_idle_days * 86400:
                await self.prune_user(
                    uid,
                    max_del=int(getattr(settings, "LTM_PRUNE_MAX_DEL", 200)),
                    conf_thr=float(getattr(settings, "LTM_PRUNE_CONF_THR", 0.12)),
                    idle_days=int(getattr(settings, "LTM_PRUNE_IDLE_DAYS", 45)),
                )
            try:
                grace = int(getattr(settings, "LTM_PLAN_AUTOCLOSE_GRACE_SECS", 2*86400))
                close_cutoff = now - max(0, grace)
                max_close = int(getattr(settings, "LTM_PLAN_AUTOCLOSE_MAX", 200))
                min_conf_close = float(getattr(settings, "LTM_PLAN_AUTOCLOSE_MIN_CONF", 0.25))
                q = Query(
                    f'(@uid:{{{_tag_literal(str(uid))}}} @status:{{active}} '
                    f'(@due_ts:[0 {close_cutoff}] | @window_end:[0 {close_cutoff}]) '
                    f'@confidence:[{min_conf_close} 1])'
                ).return_fields("due_ts","window_end","confidence").paging(0, max_close)
                if _RS_SERVER_TIMEOUT_MS > 0:
                    try:
                        q = q.timeout(_RS_SERVER_TIMEOUT_MS)
                    except Exception:
                        pass
                try:
                    res = await asyncio.wait_for(self._redis.ft(self.PLANS_IX).search(q), timeout=_RS_TIMEOUT)
                    docs = getattr(res, "docs", []) or []
                except ResponseError as e:
                    if _is_missing_index_error(e):
                        await self._ensure_indexes()
                        res = await asyncio.wait_for(self._redis.ft(self.PLANS_IX).search(q), timeout=_RS_TIMEOUT)
                        docs = getattr(res, "docs", []) or []
                    else:
                        docs = []
                if docs:
                    pipe = self._redis.pipeline(transaction=True)
                    for d in docs:
                        did = _as_str(getattr(d, "id", "")) or getattr(d, "id", "")
                        pipe.hset(did, mapping={"status": "done", "last_seen": now, "closed_ts": now})
                    try:
                        await pipe.execute()
                    except Exception:
                        pass
            except Exception:
                logger.debug("auto-close old plans failed", exc_info=True)
        except Exception:
            logger.debug("maybe_prune failed", exc_info=True)

    async def ready(self):
        if not self._init_scheduled and not self._ready.is_set():
            await self._initialize()
            self._init_scheduled = True
        await self._ready.wait()


    def _vec_opts(self) -> dict:
        return {
            "TYPE": "FLOAT32",
            "DIM": self._dim,
            "DISTANCE_METRIC": "COSINE",
            "M": int(getattr(settings, "HNSW_M", 24)),
            "EF_CONSTRUCTION": int(getattr(settings, "HNSW_EF_CONSTRUCTION", 400)),
        }

    def _vec_opts_with_cap(self, cap: int, *, use_initial_cap: bool) -> dict:
        opts = self._vec_opts()
        if use_initial_cap:
            try:
                opts["INITIAL_CAP"] = int(min(max(int(cap), 1024),
                                              int(getattr(settings, "EMBED_INITIAL_CAP", 4096))))
            except Exception:
                pass
        return opts

    def _facts_fields(self, *, use_initial_cap: bool = True) -> list:
        return [
            TagField("uid"),
            TagField("key"),
            TagField("source"),
            TextField("value", no_stem=True, no_index=True, sortable=True),
            NumericField("confidence", sortable=True),
            NumericField("first_seen", sortable=True),
            NumericField("last_seen", sortable=True),
            NumericField("counter", sortable=True),
            NumericField("used_count", sortable=True),
            NumericField("last_used_ts", sortable=True),
            NumericField("last_used_turn", sortable=True),
            NumericField("last_evidence_ts", sortable=True),
            NumericField("evidence_count", sortable=True),
            TextField("last_evidence_sample", no_stem=True, no_index=True, sortable=True),
            NumericField("schema_ver", sortable=True),
            VectorField(self._emb_fld, "HNSW", self._vec_opts_with_cap(getattr(settings, "FACTS_INITIAL_CAP", 4096), use_initial_cap=use_initial_cap)),
        ]

    def _plans_fields(self, *, use_initial_cap: bool = True) -> list:
        return [
            TagField("uid"),
            TagField("source"),
            TagField("status"),
            TagField("recurrence"),
            TextField("value", no_stem=True, no_index=True, sortable=True),
            TextField("title", no_stem=True, no_index=True, sortable=True),
            NumericField("due_ts", sortable=True),
            NumericField("window_start", sortable=True),
            NumericField("window_end", sortable=True),
            NumericField("confidence", sortable=True),
            NumericField("first_seen", sortable=True),
            NumericField("last_seen", sortable=True),
            NumericField("counter", sortable=True),
            NumericField("used_count", sortable=True),
            NumericField("last_used_ts", sortable=True),
            NumericField("last_used_turn", sortable=True),
            NumericField("schema_ver", sortable=True),
            NumericField("last_evidence_ts", sortable=True),
            TextField("last_evidence_sample", no_stem=True, no_index=True, sortable=True),
            VectorField(self._emb_fld, "HNSW", self._vec_opts_with_cap(getattr(settings, "PLANS_INITIAL_CAP", 2048), use_initial_cap=use_initial_cap)),
        ]

    def _bounds_fields(self, *, use_initial_cap: bool = True) -> list:
        return [
            TagField("uid"),
            TagField("source"),
            TagField("key"),
            TextField("value", no_stem=True, no_index=True, sortable=True),
            TextField("title", no_stem=True, no_index=True, sortable=True),
            NumericField("confidence", sortable=True),
            NumericField("first_seen", sortable=True),
            NumericField("last_seen", sortable=True),
            NumericField("counter", sortable=True),
            NumericField("evidence_count", sortable=True),
            NumericField("used_count", sortable=True),
            NumericField("last_used_ts", sortable=True),
            NumericField("last_used_turn", sortable=True),
            NumericField("last_evidence_ts", sortable=True),
            TextField("last_evidence_sample", no_stem=True, no_index=True, sortable=True),
            VectorField(self._emb_fld, "HNSW", self._vec_opts_with_cap(getattr(settings, "BOUNDS_INITIAL_CAP", 1024), use_initial_cap=use_initial_cap)),
        ]

    async def _initialize(self):
        try:
            await self._ensure_indexes()
        finally:
            self._ready.set()

    async def _ensure_indexes(self) -> None:
        await self._ensure_index_with_alias(
            self.FACTS_IX,
            f"{self.FACTS_IX}:{self._dim}:v{self.SCHEMA_VER}",
            "facts:",
            self._facts_fields
        )
        await self._ensure_index_with_alias(
            self.PLANS_IX,
            f"{self.PLANS_IX}:{self._dim}:v{self.SCHEMA_VER}",
            "plans:",
            self._plans_fields
        )
        await self._ensure_index_with_alias(
            self.BOUNDS_IX,
            f"{self.BOUNDS_IX}:{self._dim}:v{self.SCHEMA_VER}",
            "bounds:",
            self._bounds_fields
        )

    async def close(self):
        return

    async def prune_user(
        self,
        uid: int,
        *,
        max_del: int = 200,
        conf_thr: float = 0.15,
        idle_days: int = 90
    ) -> None:

        self._ensure_redis()
        try:
            if _pool_occupancy(self._redis) > 0.30:
                return
        except Exception:
            pass
        if hasattr(self._redis, "latency_ms") and getattr(self._redis, "latency_ms") > int(getattr(settings, "REDIS_LATENCY_ABORT_MS", 120)):
            return
        now = _now()
        cutoff = now - max(1, int(idle_days)) * 86400

        async def _prune_facts() -> None:
            try:
                q = Query(
                    f'(@uid:{{{_tag_literal(str(uid))}}} '
                    f'(@last_used_ts:[0 {cutoff}] | @last_seen:[0 {cutoff}]) '
                    f'@confidence:[0 {conf_thr}])'
                ).return_fields("used_count","first_seen","last_used_ts","confidence").paging(0, max_del)
                if _RS_SERVER_TIMEOUT_MS > 0:
                    try:
                        q = q.timeout(_RS_SERVER_TIMEOUT_MS)
                    except Exception:
                        pass
                try:
                    res = await asyncio.wait_for(self._redis.ft(self.FACTS_IX).search(q), timeout=_RS_TIMEOUT)
                except ResponseError as e:
                    if _is_missing_index_error(e):
                        await self._ensure_indexes()
                        if _RS_SERVER_TIMEOUT_MS > 0:
                            try:
                                q = q.timeout(_RS_SERVER_TIMEOUT_MS)
                            except Exception:
                                pass
                        res = await asyncio.wait_for(self._redis.ft(self.FACTS_IX).search(q), timeout=_RS_TIMEOUT)
                    else:
                        return
            except ResponseError as e:
                if _is_missing_index_error(e):
                    try:
                        await self._ensure_indexes()
                        res = await self._redis.ft(self.FACTS_IX).search(q)
                    except Exception:
                        return
                else:
                    return
            except Exception:
                return
            docs = getattr(res, "docs", None) or []
            if not docs:
                return
            pipe = self._redis.pipeline(transaction=True)
            deletions = 0
            for d in docs:
                try:
                    uc = _as_int(getattr(d, "used_count", 0), 0)
                    if uc > 0:
                        continue
                    did = getattr(d, "id", None) or d.id
                    try:
                        pipe.unlink(did)
                        deletions += 1
                    except AttributeError:
                        pipe.delete(did)
                        deletions += 1
                except Exception:
                    continue
            if deletions:
                try:
                    await pipe.execute()
                except Exception:
                    logger.debug("LTM prune facts: pipe.execute failed", exc_info=True)
            if deletions:
                logger.info("LTM prune: deleted %d facts for uid=%s", deletions, uid)

        async def _prune_bounds() -> None:
            try:
                q = Query(
                    f'(@uid:{{{_tag_literal(str(uid))}}} '
                    f'(@last_used_ts:[0 {cutoff}] | @last_seen:[0 {cutoff}]) '
                    f'@confidence:[0 {conf_thr}])'
                ).return_fields("used_count","first_seen","last_used_ts","confidence").paging(0, max_del)
                if _RS_SERVER_TIMEOUT_MS > 0:
                    try:
                        q = q.timeout(_RS_SERVER_TIMEOUT_MS)
                    except Exception:
                        pass
                try:
                    res = await asyncio.wait_for(self._redis.ft(self.BOUNDS_IX).search(q), timeout=_RS_TIMEOUT)
                except ResponseError as e:
                    if _is_missing_index_error(e):
                        await self._ensure_indexes()
                        if _RS_SERVER_TIMEOUT_MS > 0:
                            try:
                                q = q.timeout(_RS_SERVER_TIMEOUT_MS)
                            except Exception:
                                pass
                        res = await asyncio.wait_for(self._redis.ft(self.BOUNDS_IX).search(q), timeout=_RS_TIMEOUT)
                    else:
                        return
            except Exception:
                return
            docs = getattr(res, "docs", None) or []
            if not docs:
                return
            pipe = self._redis.pipeline(transaction=True)
            deletions = 0
            for d in docs:
                try:
                    uc = _as_int(getattr(d, "used_count", 0), 0)
                    if uc > 0:
                        continue
                    did = _as_str(getattr(d, "id", "")) or getattr(d, "id", "")
                    try:
                        pipe.unlink(did)
                        deletions += 1
                    except AttributeError:
                        pipe.delete(did)
                        deletions += 1
                except Exception:
                    continue
            if deletions:
                try:
                    await pipe.execute()
                except Exception:
                    logger.debug("LTM prune bounds: pipe.execute failed", exc_info=True)
            if deletions:
                logger.info("LTM prune: deleted %d boundaries for uid=%s", deletions, uid)

        async def _prune_plans() -> None:
            try:
                q = Query(
                    f'(@uid:{{{_tag_literal(str(uid))}}} '
                    f'(@last_used_ts:[0 {cutoff}] | @last_seen:[0 {cutoff}]) '
                    f'@confidence:[0 {conf_thr}])'
                ).return_fields("used_count","first_seen","last_used_ts","confidence").paging(0, max_del)
                if _RS_SERVER_TIMEOUT_MS > 0:
                    try:
                        q = q.timeout(_RS_SERVER_TIMEOUT_MS)
                    except Exception:
                        pass
                try:
                    res = await asyncio.wait_for(self._redis.ft(self.PLANS_IX).search(q), timeout=_RS_TIMEOUT)
                except ResponseError as e:
                    if _is_missing_index_error(e):
                        await self._ensure_indexes()
                        if _RS_SERVER_TIMEOUT_MS > 0:
                            try:
                                q = q.timeout(_RS_SERVER_TIMEOUT_MS)
                            except Exception:
                                pass
                        res = await asyncio.wait_for(self._redis.ft(self.PLANS_IX).search(q), timeout=_RS_TIMEOUT)
                    else:
                        return
            except Exception:
                return
            docs = getattr(res, "docs", None) or []
            if not docs:
                return
            pipe = self._redis.pipeline(transaction=True)
            deletions = 0
            for d in docs:
                try:
                    uc = _as_int(getattr(d, "used_count", 0), 0)
                    if uc > 0:
                        continue
                    did = _as_str(getattr(d, "id", "")) or getattr(d, "id", "")
                    try:
                        pipe.unlink(did)
                        deletions += 1
                    except AttributeError:
                        pipe.delete(did)
                        deletions += 1
                except Exception:
                    continue
            if deletions:
                try:
                    await pipe.execute()
                except Exception:
                    logger.debug("LTM prune plans: pipe.execute failed", exc_info=True)
            if deletions:
                logger.info("LTM prune: deleted %d plans for uid=%s", deletions, uid)

        await _prune_facts()
        await _prune_bounds()
        await _prune_plans()

    async def _with_init_lock(self, coro):
        token = f"{os.getpid()}:{time.time():.6f}"
        try:
            got = await self._redis.set(self.INIT_LOCK_KEY, token, ex=self.INIT_LOCK_TTL, nx=True)
        except Exception:
            got = False
        if not got:
            deadline = time.time() + max(2, int(self.INIT_LOCK_TTL))
            while time.time() < deadline:
                try:
                    await self._redis.ft(self.FACTS_IX).info()
                    await self._redis.ft(self.PLANS_IX).info()
                    await self._redis.ft(self.BOUNDS_IX).info()
                    return
                except Exception:
                    await asyncio.sleep(0.25)
            return
        try:
            async def _keeper():
                try:
                    while True:
                        await asyncio.sleep(self.INIT_LOCK_TTL * 0.5)
                        await self._redis.expire(self.INIT_LOCK_KEY, self.INIT_LOCK_TTL)
                except Exception:
                    return
            keeper = asyncio.create_task(_keeper())
            try:
                return await coro()
            finally:
                keeper.cancel()
        finally:
            lua = (
                "if redis.call('GET', KEYS[1]) == ARGV[1] "
                "then return redis.call('DEL', KEYS[1]) else return 0 end"
            )
            try:
                await self._redis.eval(lua, 1, self.INIT_LOCK_KEY, token)
            except Exception:
                pass

    async def _ensure_index_with_alias(self, alias: str, real_name: str, prefix: str, fields_builder: Callable[..., list]) -> None:

        self._ensure_redis()
        async def _create_or_update():
            async with self._index_lock:
                try:
                    info = await self._redis.ft(alias).info()
                    attrs = { (k.decode() if isinstance(k,(bytes,bytearray)) else k): v for k,v in zip(info[::2], info[1::2]) } if isinstance(info, list) else info
                    fields_meta = attrs.get("fields") or attrs.get("attributes") or []
                    dim_seen = 0
                    for a in fields_meta:
                        if not isinstance(a, dict):
                            continue
                        name = a.get("attribute") or a.get("identifier") or a.get("name")
                        if isinstance(name, (bytes, bytearray)):
                            try:
                                name = name.decode("utf-8", "ignore")
                            except Exception:
                                pass
                        if name not in ("embedding", self._emb_fld):
                            continue
                        raw = a.get("attributes") or a.get("params") or a
                        if isinstance(raw, dict):
                            dim_seen = int((raw.get("DIM") or raw.get(b"DIM") or 0))
                        elif isinstance(raw, list):
                            for i in range(0, len(raw) - 1, 2):
                                if raw[i] in ("DIM", b"DIM"):
                                    dim_seen = int(raw[i + 1])
                                    break
                        break
                    if dim_seen:
                        if int(dim_seen) != self._dim:
                            logger.warning("LTM index alias=%s DIM=%s expected=%s → repoint to %s",
                                           alias, dim_seen, self._dim, real_name)
                        else:
                            logger.debug("LTM index alias=%s OK (DIM=%s)", alias, dim_seen)
                except ResponseError:
                    pass

                try:
                    await self._redis.ft(real_name).info()
                except ResponseError:
                    try:
                        await self._redis.ft(real_name).create_index(
                            fields_builder(use_initial_cap=True),
                            definition=IndexDefinition(prefix=[prefix], index_type=IndexType.HASH),
                        )
                        logger.info("Created RediSearch index %s", real_name)
                    except ResponseError as e:
                        low = str(e).lower()
                        if "index already exists" in low:
                            logger.info("Index %s already exists (race); continuing", real_name)
                        elif any(s in low for s in ("unknown argument", "invalid argument", "unsupported", "unexpected attribute")):
                            logger.warning("FT.CREATE %s retrying without INITIAL_CAP due to server compatibility: %s", real_name, e)
                            await self._redis.ft(real_name).create_index(
                                fields_builder(use_initial_cap=False),
                                definition=IndexDefinition(prefix=[prefix], index_type=IndexType.HASH),
                            )
                            logger.info("Created RediSearch index (no INITIAL_CAP) %s", real_name)
                        else:
                            raise

                try:
                    await self._redis.execute_command("FT.ALIASUPDATE", alias, real_name)
                except ResponseError as e:
                    low = str(e).lower()
                    if "unknown alias" in low:
                        await self._redis.execute_command("FT.ALIASADD", alias, real_name)
                    elif "unknown command" in low or "syntax" in low:
                        try:
                            await self._redis.execute_command("FT.ALIASDEL", alias)
                        except Exception:
                            pass
                        await self._redis.execute_command("FT.ALIASADD", alias, real_name)
                    else:
                        raise
                logger.info("Updated alias %s -> %s", alias, real_name)
                try:
                    await self._redis.ft(alias).info()
                except Exception:
                    logger.warning("Alias %s health check failed after update", alias)

        await self._with_init_lock(_create_or_update)

    async def _hmget_map(self, key: str, fields: List[str]) -> dict:
        self._ensure_redis()
        try:
            vals = await self._redis.hmget(key, *fields)
        except Exception:
            return {}
        out = {}
        for f, v in zip(fields, vals or []):
            if isinstance(v, (bytes, bytearray)):
                try:
                    v = v.decode("utf-8", "ignore")
                except Exception:
                    pass
            out[f] = v
        return out

    async def upsert_fact(self, uid: int, key: str, value: str, *, source: str, confidence: float, evidence: str) -> None:
        self._ensure_redis()
        key_n_raw = _snake(key)
        key_n = _canon_key(key_n_raw)
        if not key_n:
            logger.debug("skip upsert_fact: non-canonical key=%r", key)
            return
        v_clean = _excerpt(value, 240)
        fid = f"{uid}:{key_n}:{_hash8(_norm_val(v_clean))}"
        now = _now()
        emb = await self._embed_cached(f"{key_n}: {v_clean}" if v_clean else key_n)
        v_clean_norm = _norm_val(v_clean)
        mapping = {
            "uid": str(uid),
            "key": key_n,
            "value": v_clean,
            "source": source or "user",
            "confidence": _clamp01(float(confidence)),
            "first_seen": now,
            "last_seen": now,
            "used_count": 0,
            "last_used_ts": 0.0,
            "last_used_turn": -9999,
            "last_evidence_ts": now,
            "last_evidence_sample": _excerpt(_redact(evidence)),
            "schema_ver": self.SCHEMA_VER,
        }
        if emb and any(emb):
            mapping[self._emb_fld] = emb
            if os.getenv("LTM_WRITE_LEGACY_EMB","0") == "1":
                mapping["embedding"] = emb
        pipe = self._redis.pipeline(transaction=True)
        cur = await self._hmget_map(
            f"facts:{fid}",
            ["uid", "confidence", "counter", "evidence_count", "first_seen", "used_count"]
        ) or {}
        exists = bool(_hget_val(cur, "uid", None))
        if exists:
            c_old   = _hget_float(cur, "confidence", 0.0)
            fs_old  = _hget_float(cur, "first_seen", now)
            usedold = _hget_int(cur, "used_count", 0)
            c_new   = 1 - (1 - max(c_old, float(confidence))) * 0.6
            mapping.update({
                "confidence": _clamp01(c_new),
                "first_seen": fs_old,
                "used_count": usedold,
                "last_evidence_ts": now,
                "last_evidence_sample": _excerpt(_redact(evidence)),
                "last_seen": now,
            })
        pipe.hset(f"facts:{fid}", mapping=mapping)
        pipe.hincrby(f"facts:{fid}", "counter", 1)
        pipe.hincrby(f"facts:{fid}", "evidence_count", 1)

        try:
            q = Query(
                f'(@uid:{{{_tag_literal(str(uid))}}} @key:{{{_tag_literal(key_n)}}})'
            ).return_fields("value", "confidence", "last_seen", "last_used_ts", "last_used_turn", "used_count").paging(0, 50)
            if _RS_SERVER_TIMEOUT_MS > 0:
                try:
                    q = q.timeout(_RS_SERVER_TIMEOUT_MS)
                except Exception:
                    pass
            try:
                res = await asyncio.wait_for(
                    self._redis.ft(self.FACTS_IX).search(q),
                    timeout=_RS_TIMEOUT
                )
            except ResponseError as e:
                if _is_missing_index_error(e):
                    await self._ensure_indexes()
                    res = await asyncio.wait_for(
                        self._redis.ft(self.FACTS_IX).search(q),
                        timeout=_RS_TIMEOUT
                    )
                else:
                    raise
            if res and getattr(res, "docs", None) and key_n not in _MULTI_VAL_KEYS and float(confidence) >= float(getattr(settings, "LTM_DEMOTE_MIN_CONF", 0.35)) and len(res.docs) >= 2:
                pipe2 = self._redis.pipeline(transaction=True)
                _ops = 0
                _now_ts = _now()
                _fresh_win = int(getattr(settings, "LTM_DEMOTE_FRESH_WINDOW_SECS", 3*86400))
                for doc in res.docs:
                    did = _as_str(getattr(doc, "id", ""))
                    if did == f"facts:{fid}":
                        continue
                    try:
                        val = doc.value if isinstance(doc.value, str) else _as_str(doc.value)
                    except Exception:
                        val = ""
                    doc_val_norm = _norm_val(_excerpt(val, 240))
                    if val and (doc_val_norm != v_clean_norm):
                        conf_old = _as_float(getattr(doc, "confidence", 0.5), 0.5)
                        ls = _as_float(getattr(doc, "last_seen", 0.0), 0.0)
                        uc = _as_int(getattr(doc, "used_count", 0), 0)
                        if uc >= _LTM_DEMOTE_USEDCOUNT_PROTECT:
                            continue
                        if (_now_ts - ls) < _fresh_win:
                            continue
                        lu_ts = _as_float(getattr(doc, "last_used_ts", 0.0), 0.0)
                        if (_now_ts - lu_ts) < _fresh_win:
                            continue
                        age_pen = 0.9 if (_now_ts - ls) > 30*86400 else 1.0
                        new_c = max(0.03, conf_old * 0.85 * age_pen)
                        pipe2.hset(did or doc.id, mapping={"confidence": new_c})
                        _ops += 1
                if _ops:
                    await pipe2.execute()
        except Exception:
            pass
        await pipe.execute()


    async def upsert_boundary(
        self, uid: int, key: str, value: str, *, confidence: float, evidence: Optional[str] = None
    ) -> None:
        self._ensure_redis()
        key_n_raw = _snake(key)
        key_n = _canon_key(key_n_raw) or key_n_raw
        bid = f"{uid}:{key_n}:{_hash8(_norm_val(_excerpt(value, 240)))}"
        now = _now()
        emb = await self._embed_cached(value or key_n)
        mapping = {
            "uid": str(uid),
            "key": key_n,
            "value": value,
            "title": value,
            "source": "user",
            "confidence": _clamp01(float(confidence)),
            "first_seen": now,
            "last_seen": now,
            "used_count": 0,
            "last_used_ts": 0.0,
            "last_used_turn": -9999,
            "last_evidence_ts": now,
            "last_evidence_sample": _excerpt(_redact(evidence) if evidence else _excerpt(value)),
            "schema_ver": self.SCHEMA_VER,
        }
        if emb and any(emb):
            mapping[self._emb_fld] = emb
            if os.getenv("LTM_WRITE_LEGACY_EMB","0") == "1":
                mapping["embedding"] = emb
        cur = await self._hmget_map(
            f"bounds:{bid}",
            ["uid", "confidence", "counter", "first_seen", "used_count"]
        ) or {}
        exists = bool(_hget_val(cur, "uid", None))
        if exists:
            c_old = _hget_float(cur, "confidence", 0.0)
            first_seen_old = _hget_float(cur, "first_seen", now)
            used_old = _hget_int(cur, "used_count", 0)
            c_new = 1 - (1 - max(c_old, float(confidence))) * 0.6
            mapping.update({"confidence": _clamp01(c_new), "first_seen": first_seen_old, "used_count": used_old})
        pipe = self._redis.pipeline(transaction=True)
        pipe.hset(f"bounds:{bid}", mapping=mapping)
        pipe.hincrby(f"bounds:{bid}", "counter", 1)
        pipe.hincrby(f"bounds:{bid}", "evidence_count", 1)
        await pipe.execute()


    async def upsert_plan(
        self, 
        uid: int, 
        title: str, 
        *, 
        due_ts: Optional[float], 
        window: Optional[Tuple[float,float]], 
        recurrence: Optional[str], 
        confidence: float,
        evidence: Optional[str] = None,
        ) -> None:
        
        self._ensure_redis()
        title_n = (title or "").strip()
        base = _hash8(_norm_val(title_n))
        time_part = ""
        if due_ts:
            q = 900
            tq = int((float(due_ts) // q) * q)
            time_part = f":d{tq}"
        elif window and (window[0] or window[1]):
            q = 300
            w0 = int((window[0] // q) * q) if window[0] else 0
            w1 = int((window[1] // q) * q) if window[1] else 0
            time_part = f":w{w0}-{w1}"
        rec_part = _hash8((recurrence or "").strip()) if recurrence else "nr"
        pid = f"{uid}:{base}{time_part}:{rec_part}"
        now = _now()
        emb = await self._embed_cached(title_n)
        mapping = {
            "uid": str(uid),
            "title": title_n,
            "status": "active",
            "source": "user",
            "recurrence": (recurrence or "").strip(),
            "due_ts": float(due_ts or 0.0),
            "window_start": float(window[0]) if window else 0.0,
            "window_end": float(window[1]) if window else 0.0,
            "confidence": _clamp01(float(confidence)),
            "first_seen": now,
            "last_seen": now,
            "used_count": 0,
            "last_used_ts": 0.0,
            "last_used_turn": -9999,
            "schema_ver": self.SCHEMA_VER,
            "last_evidence_ts": now,
            "last_evidence_sample": _excerpt(_redact(evidence or title_n)),
        }
        if emb and any(emb):
            mapping[self._emb_fld] = emb
            if os.getenv("LTM_WRITE_LEGACY_EMB","0") == "1":
                mapping["embedding"] = emb
        cur = await self._hmget_map(
            f"plans:{pid}",
            ["uid", "confidence", "counter", "first_seen", "used_count"]
        ) or {}
        exists = bool(_hget_val(cur, "uid", None))
        if exists:
            c_old = _hget_float(cur, "confidence", 0.0)
            first_seen_old = _hget_float(cur, "first_seen", now)
            used_old = _hget_int(cur, "used_count", 0)
            c_new = 1 - (1 - max(c_old, float(confidence))) * 0.6
            mapping.update({"confidence": _clamp01(c_new), "first_seen": first_seen_old, "used_count": used_old})
        pipe0 = self._redis.pipeline(transaction=True)
        pipe0.hset(f"plans:{pid}", mapping=mapping)
        pipe0.hincrby(f"plans:{pid}", "counter", 1)
        await pipe0.execute()
        
        try:
            docs = await self._knn(
                self.PLANS_IX,
                f'(@uid:{{{_tag_literal(str(uid))}}} @status:{{active}})',
                emb,
                12,
                ["title","confidence","last_seen","last_used_ts","last_used_turn","used_count"]
            )
            if docs and float(confidence) >= float(getattr(settings, "LTM_DEMOTE_MIN_CONF", 0.35)):
                pipe = self._redis.pipeline(transaction=True)
                _ops = 0
                _now_ts = _now()
                _fresh_win = int(getattr(settings, "LTM_DEMOTE_FRESH_WINDOW_SECS", 3*86400))
                for d in docs:
                    did = _as_str(getattr(d, "id", "")) or getattr(d, "id", "")
                    if did == f"plans:{pid}":
                        continue
                    try:
                        sim = _dist_to_sim(float(d.vector_score))
                    except Exception:
                        sim = 0.0
                    if sim < self._plan_demote_sim_thr:
                        continue
                    ls = _as_float(getattr(d, "last_seen", 0.0), 0.0)
                    uc = _as_int(getattr(d, "used_count", 0), 0)
                    if uc >= _LTM_DEMOTE_USEDCOUNT_PROTECT:
                        continue
                    lu = _as_float(getattr(d, "last_used_ts", 0.0), 0.0)
                    if (_now_ts - ls) < _fresh_win:
                        continue
                    if (_now_ts - lu) < _fresh_win:
                        continue
                    conf_old = _as_float(getattr(d, "confidence", 0.5), 0.5)
                    pipe.hset(did, mapping={"confidence": _clamp01(conf_old * 0.85)})
                    _ops += 1
                if _ops:
                    await pipe.execute()
        except Exception:
            logger.debug("demote conflicting plans failed", exc_info=True)

    async def extract_and_upsert(self, uid: int, text: str) -> None:

        self._ensure_redis()

        try:
            tx = unicodedata.normalize("NFKC", text or "").strip()
            low_info = (len(tx) < 8) or bool(re.fullmatch(r"[\W_]+", tx))
            if low_info:
                return
        except Exception:
            pass

        try:
            await self.ready()
        except Exception:
            logger.debug("LTM.ready() failed (continuing anyway)", exc_info=True)

        try:
            await self.record_activity(uid)
        except Exception:
            pass

        allowed_keys = ", ".join(_CANON_KEYS)
        system_prompt = ltm_extract_system_prompt(allowed_keys)

        utc_now = time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime())
        user_prompt = ltm_extract_user_prompt(utc_now, text)
        t0 = time.perf_counter()
        try:
            resp = await asyncio.wait_for(
                _call_openai_with_retry(
                    endpoint="responses.create",
                    model=settings.REASONING_MODEL,
                    model_role="regular",
                    instructions=system_prompt,
                    input=user_prompt,
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": "memory_keys",
                            "schema": _extraction_schema(),
                            "strict": True
                        }
                    },
                    temperature=0,
                    max_output_tokens=500,
                ),
                timeout=settings.REASONING_MODEL_TIMEOUT
            )
            logger.info(
                "openai.timing: responses.create model=%s input_chars=%d duration=%.3fs",
                settings.REASONING_MODEL, len(user_prompt), time.perf_counter() - t0
            )
            raw = (_get_output_text(resp) or "{}").strip()
            if raw.startswith("```"):
                nl = raw.find("\n")
                if nl != -1:
                    raw = raw[nl + 1 :]
                raw = raw.rstrip("`").strip()
            raw = raw.lstrip("\ufeff").replace("\u2028","").replace("\u2029","")
            left_idx, right_idx = raw.find("{"), raw.rfind("}")
            if left_idx != -1 and right_idx != -1 and right_idx > left_idx:
                raw = raw[left_idx : right_idx + 1]
        except Exception as e:
            logger.warning(
                "openai.timing: responses.create model=%s input_chars=%d error=%s duration=%.3fs",
                settings.REASONING_MODEL, len(user_prompt),
                getattr(e, "__class__", type(e)).__name__,
                time.perf_counter() - t0
            )
            logger.debug("LTM.extract: model call failed", exc_info=True)
            return

        try:
            data = json.loads(raw)
        except Exception:
            logger.debug("LTM.extract: JSON parse failed: %s", raw[:200])
            return

        facts = (data.get("facts") or [])[: self._max_per_prompt]
        bounds = (data.get("boundaries") or [])[: self._max_per_prompt]
        plans = (data.get("plans") or [])[: self._max_per_prompt]

        for f in facts:
            try:
                rk = (f.get("key") or "").strip()
                rv = (f.get("value") or "").strip()
                k_std = _canon_key(rk)
                if not k_std or not rv:
                    continue
                if (k_std in SENSITIVE_KEYS) and (not SENSITIVE_ALLOWED):
                    continue
                await self.upsert_fact(
                    uid,
                    k_std,
                    rv,
                    source="user",
                    confidence=float(f.get("confidence", 0.5)),
                    evidence=text,
                )
            except Exception:
                logger.debug("upsert_fact failed", exc_info=True)

        for b in bounds:
            try:
                bk_raw = (b.get("key") or "").strip()
                bv = (b.get("value") or "").strip()
                bk = _canon_key(_snake(bk_raw)) or _snake(bk_raw)
                if not bk or not bv:
                    continue
                await self.upsert_boundary(
                    uid,
                    bk,
                    bv,
                    confidence=float(b.get("confidence", 0.5)),
                    evidence=text,
                )
            except Exception:
                logger.debug("upsert_boundary failed", exc_info=True)

        for p in plans:
            try:
                title = (p.get("title") or "").strip()
                if not title:
                    continue
                due_ts = None
                win = None
                if p.get("due_iso"):
                    try:
                        dt = isoparse(p["due_iso"])
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=UTC)
                        due_ts = dt.astimezone(UTC).timestamp()
                    except Exception:
                        due_ts = None
                if (not due_ts) and p.get("window_text"):
                    try:
                        _prefs = dict(_DP_SETTINGS)
                        if re.search(r"\b(last|yesterday|прошл\w+|вчера)\b", p.get("window_text",""), flags=re.I):
                            _prefs['PREFER_DATES_FROM'] = 'past'
                        wtxt = p["window_text"]
                        dt = dp_parse(wtxt, settings=_prefs)
                        if dt:
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=UTC)
                            due_ts = dt.astimezone(UTC).timestamp()
                        m = None
                        m_between = re.search(r"\bbetween\s+(.+?)\s+\band\b\s+(.+)", wtxt, flags=re.I|re.S)
                        if m_between:
                            m = [m_between.group(1), m_between.group(2)]
                        else:
                            m = _WINDOW_SPLIT_RE.split(wtxt, maxsplit=1)
                        if len(m) == 2:
                            s = dp_parse(_strip_start_prep(m[0]), settings=_prefs)
                            e = dp_parse(_strip_start_prep(m[1]), settings=_prefs)
                            if s and e:
                                if s.tzinfo is None:
                                    s = s.replace(tzinfo=UTC)
                                if e.tzinfo is None:
                                    e = e.replace(tzinfo=UTC)
                                ws = s.astimezone(UTC).timestamp()
                                we = e.astimezone(UTC).timestamp()
                                if we < ws:
                                    ws, we = we, ws
                                win = (ws, we)
                                due_ts = None
                    except Exception:
                        pass
                if not due_ts:
                    _now_cached = _now()
                    if p.get("window_text"):
                        due_ts = _fallback_rel(p["window_text"], _now_cached)
                    if (not due_ts) and p.get("title"):
                        due_ts = _fallback_rel(p["title"], _now_cached)
                await self.upsert_plan(
                    uid,
                    title,
                    due_ts=due_ts,
                    window=win,
                    recurrence=p.get("recurrence"),
                    confidence=float(p.get("confidence", 0.5)),
                    evidence=text,
                )
            except Exception:
                logger.debug("upsert_plan failed", exc_info=True)

    async def _knn(self, ix_name: str, base_filter: str, vec: bytes, k: int, ret_fields: List[str]):
        self._ensure_redis()
        if not vec:
            return None
        if isinstance(vec, (bytes, bytearray)):
            if not any(vec):
                return None
            if len(vec) != 4 * self._dim:
                return None
        try:
            k = max(1, int(k))
        except Exception:
            k = 1
        ef = 0 if self._no_ef_runtime else self._ef_runtime
        occ = _pool_occupancy(self._redis)
        if ef > 0 and occ > 0.0:
            ef = max(20, int(ef * (1.0 - 0.6*min(1.0, occ))))
        if occ >= 0.6:
            k = max(3, int(k * (1.0 - 0.35*min(1.0, (occ-0.6)/0.4))))
        logger.debug("ltm.knn: occ=%.2f k=%d ef=%d ix=%s", occ, k, ef, ix_name)

        fld = self._emb_fld
        knn_clause = f"[KNN {k} @{fld} $vec AS vector_score EF_RUNTIME $ef]" if ef > 0 else f"[KNN {k} @{fld} $vec AS vector_score]"
        qp = {"vec": vec}
        if ef > 0:
            qp["ef"] = ef
        q = Query(f"{base_filter}=>{knn_clause}") \
            .sort_by("vector_score") \
            .return_fields(*ret_fields, "vector_score") \
            .dialect(2).paging(0, k)
        if _RS_SERVER_TIMEOUT_MS > 0:
            try:
                q = q.timeout(_RS_SERVER_TIMEOUT_MS)
            except Exception:
                pass
        try:
            res = await asyncio.wait_for(self._redis.ft(ix_name).search(q, query_params=qp), timeout=_RS_TIMEOUT)
            return res.docs
        except ResponseError as e:
            try:
                if _is_missing_index_error(e):
                    await self._ensure_indexes()
                lowe = str(e).lower()
                if ("ef_runtime" in lowe) or ("unknown argument" in lowe) or ("unexpected attribute" in lowe) or ("invalid argument" in lowe):
                    self._no_ef_runtime = True
                q2 = Query(f"{base_filter}=>[KNN {k} @{fld} $vec AS vector_score]") \
                        .sort_by("vector_score") \
                        .return_fields(*ret_fields, "vector_score") \
                        .dialect(2).paging(0, k)
                if _RS_SERVER_TIMEOUT_MS > 0:
                    try:
                        q2 = q2.timeout(_RS_SERVER_TIMEOUT_MS)
                    except Exception:
                        pass
                res = await asyncio.wait_for(
                    self._redis.ft(ix_name).search(q2, query_params={"vec": vec}),
                    timeout=_RS_TIMEOUT
                )
                return res.docs
            except Exception:
                low = str(e).lower()
                if "syntax" in low or "near" in low:
                    try:
                        fetch = min(128, max(k * 6, k + 4))
                        q3 = Query(f"{base_filter}=>[KNN {fetch} @{fld} $vec AS vector_score]") \
                                .sort_by("vector_score") \
                                .return_fields("uid","status", *ret_fields, "vector_score") \
                                .dialect(2).paging(0, fetch)
                        if _RS_SERVER_TIMEOUT_MS > 0:
                            try:
                                q3 = q3.timeout(_RS_SERVER_TIMEOUT_MS)
                            except Exception:
                                pass
                        res3 = await asyncio.wait_for(
                            self._redis.ft(ix_name).search(q3, query_params={"vec": vec}),
                            timeout=_RS_TIMEOUT
                        )
                        docs = getattr(res3, "docs", []) or []

                        def _extract_tag(filter_s: str, field: str) -> Optional[str]:
                            m = re.search(rf"@{field}:\{{([^}}]+)\}}", filter_s)
                            if not m:
                                return None
                            v = m.group(1).strip()
                            if v.startswith('"') and v.endswith('"'):
                                v = v[1:-1]
                            v = v.replace(r'\"', '"').replace(r"\|","|").replace(r"\,",
                                ",").replace(r"\{","{").replace(r"\}","}")
                            return v
                        want_uid = _extract_tag(base_filter, "uid")
                        want_status = _extract_tag(base_filter, "status")
                        filtered = []
                        for d in docs:
                            try:
                                if want_uid is not None:
                                    du = d.uid if isinstance(d.uid, str) else _as_str(getattr(d, "uid", ""))
                                    if du != want_uid:
                                        continue
                                if want_status is not None:
                                    st = d.status if isinstance(d.status, str) else _as_str(getattr(d, "status", ""))
                                    if st != want_status:
                                        continue
                            except Exception:
                                continue
                            filtered.append(d)
                        return filtered
                    except Exception:
                        return None
                return None
        except Exception:
            try:
                res = await asyncio.wait_for(self._redis.ft(ix_name).search(q, query_params=qp), timeout=_RS_TIMEOUT*2)
                return res.docs
            except Exception:
                return None

    async def pick_snippets(
        self, 
        *, 
        uid: int, 
        context: str, 
        now_ts: Optional[float], 
        turn_id: int, 
        query_vec: bytes | None = None
        ) -> Dict[str, Optional[str]]:

        self._ensure_redis()

        try:
            await self.ready()
        except Exception:
            logger.debug("LTM.ready() failed (continuing anyway)", exc_info=True)

        try:
            await self.record_activity(uid)
        except Exception:
            pass

        now_ts = now_ts or _now()
        uid_tag = _tag_literal(str(uid))
        try:
            if query_vec is not None:
                query_emb = self._ensure_vec(query_vec)
            else:
                raw = await self._embed_cached(context or " ")
                query_emb = self._ensure_vec(raw)
        except Exception:
            query_emb = self._zero_vec
        if query_emb == self._zero_vec:
            return {"fact": None, "plan": None, "boundary": None}

        out: Dict[str, Optional[str]] = {"fact": None, "plan": None, "boundary": None}

        facts_task = asyncio.create_task(
            self._knn(self.FACTS_IX, f'(@uid:{{{uid_tag}}})', query_emb, 8,
                      ["key","value","confidence","last_seen","used_count","last_used_ts","last_used_turn"])
        )
        plans_task = asyncio.create_task(
            self._knn(self.PLANS_IX, f'(@uid:{{{uid_tag}}} @status:{{active}})', query_emb, 8,
                      ["title","confidence","due_ts","window_start","window_end",
                       "last_seen","used_count","last_used_ts","last_used_turn"])
        )
        bounds_task = asyncio.create_task(
            self._knn(self.BOUNDS_IX, f'(@uid:{{{uid_tag}}})', query_emb, 6,
                      ["key","value","title","confidence","last_seen","used_count","last_used_ts","last_used_turn"])
        )

        try:
            docs_raw, plans_raw, bounds_raw = await asyncio.gather(
                facts_task, plans_task, bounds_task, return_exceptions=True
            )
            if isinstance(docs_raw, Exception):
                logger.debug("facts knn failed: %r", docs_raw)
                docs = []
            else:
                docs = docs_raw or []
            if isinstance(plans_raw, Exception):
                logger.debug("plans knn failed: %r", plans_raw)
                docs_plans = []
            else:
                docs_plans = plans_raw or []
            if isinstance(bounds_raw, Exception):
                logger.debug("bounds knn failed: %r", bounds_raw)
                docs_bounds = []
            else:
                docs_bounds = bounds_raw or []
            ranked = []
            noisy_keys = set((os.getenv("LTM_NOISY_KEYS","").split(","))) if os.getenv("LTM_NOISY_KEYS") else set()
            for d in docs or []:
                try:
                    sim = _dist_to_sim(float(d.vector_score))
                except Exception:
                    sim = 0.0
                if sim < self._min_sim:
                    continue
                key = d.key if isinstance(d.key, str) else _as_str(d.key)
                val = d.value if isinstance(d.value, str) else _as_str(d.value)
                conf = _as_float(getattr(d, "confidence", 0.5), 0.5)
                last_seen = _as_float(getattr(d, "last_seen", 0.0), 0.0)
                used_cnt  = _as_int(getattr(d, "used_count", 0), 0)
                lu_ts     = _as_float(getattr(d, "last_used_ts", 0.0), 0.0)
                lu_turn   = _as_int(getattr(d, "last_used_turn", -9999), -9999)
                rec_boost = _exp_decay_from(now_ts, last_seen, _LTM_RECENCY_TAU)
                lu_boost  = _exp_decay_from(now_ts, lu_ts, _LTM_LAST_USED_TAU)
                use_boost = _log1p_norm10(used_cnt)
                comp = sim * (1.0 + _LTM_W_CONF*conf + _LTM_W_RECENCY*rec_boost + _LTM_W_USE*use_boost + _LTM_W_LASTUSED*lu_boost)
                did = _as_str(getattr(d, "id", "")) or getattr(d, "id", "")
                if noisy_keys and key in noisy_keys:
                    comp *= 0.88
                ranked.append((comp, key, val, conf, did, lu_ts, lu_turn))
            ranked.sort(key=lambda t: t[0], reverse=True)
            chosen = None
            for comp, key, val, conf, docid, lu_ts, lu_turn in ranked:
                if (now_ts - lu_ts) < self._cool_secs or (turn_id - lu_turn) <= self._cool_turns:
                    continue
                chosen = (key, val, conf, docid)
                break
            if chosen:
                key, val, conf, docid = chosen
                logger.debug("ltm.snippet.fact picked key=%s conf=%.2f", key, conf)
                out["fact"] = f"{key}={val} (conf={conf:.2f})"
                pipe = self._redis.pipeline(transaction=True)
                pipe.hset(docid, mapping={"last_used_ts": now_ts, "last_used_turn": turn_id})
                pipe.hincrby(docid, "used_count", 1)
                await pipe.execute()
        except Exception:
            logger.debug("pick facts failed", exc_info=True)

        try:
            ranked = []
            for d in docs_plans or []:
                try:
                    sim = _dist_to_sim(float(d.vector_score))
                except Exception:
                    sim = 0.0
                if sim < (self._min_sim - 0.08):
                    continue
                title     = d.title if isinstance(d.title, str) else _as_str(d.title)
                conf      = _as_float(getattr(d, "confidence", 0.5), 0.5)
                if conf < float(getattr(settings, "LTM_PLAN_MIN_CONF_FOR_SNIPPET", 0.20)):
                    continue
                due       = _finite_pos(getattr(d, "due_ts", 0.0))
                ws        = _finite_pos(getattr(d, "window_start", 0.0))
                we        = _finite_pos(getattr(d, "window_end", 0.0))
                if ws and we and we < ws:
                    ws, we = we, ws
                last_seen = _as_float(getattr(d, "last_seen", 0.0), 0.0)
                used_cnt  = _as_int(getattr(d, "used_count", 0), 0)
                lu_ts     = _as_float(getattr(d, "last_used_ts", 0.0), 0.0)
                lu_turn   = _as_int(getattr(d, "last_used_turn", -9999), -9999)

                ref_ts  = 0.0
                if due > 0:
                    ref_ts = due
                elif ws > 0 and we > 0:
                    ref_ts = ws if abs(ws - now_ts) <= abs(we - now_ts) else we
                elif ws > 0:
                    ref_ts = ws
                elif we > 0:
                    ref_ts = we
                time_boost = math.exp(-abs(ref_ts - now_ts)/max(1.0, _LTM_PLAN_TIME_TAU_SECS)) if ref_ts > 0 else 0.0

                rec_boost = _exp_decay_from(now_ts, last_seen, _LTM_RECENCY_TAU)
                lu_boost  = _exp_decay_from(now_ts, lu_ts, _LTM_LAST_USED_TAU)
                use_boost = _log1p_norm10(used_cnt)
                comp = sim * (1.0
                              + _LTM_W_CONF*conf
                              + _LTM_W_TIME*time_boost
                              + _LTM_W_RECENCY*rec_boost
                              + _LTM_W_USE*use_boost
                              + _LTM_W_LASTUSED*lu_boost)
                did = _as_str(getattr(d, "id", "")) or getattr(d, "id", "")
                ranked.append((comp, title, conf, did, lu_ts, lu_turn, due, ws, we))
            ranked.sort(key=lambda t: t[0], reverse=True)
            chosen = None
            for comp, title, conf, docid, lu_ts, lu_turn, due, ws, we in ranked:
                if (now_ts - lu_ts) < self._cool_secs or (turn_id - lu_turn) <= self._cool_turns:
                    continue
                chosen = (title, conf, docid, due, ws, we)
                break
            if chosen:
                title, conf, docid, due, ws, we = chosen
                logger.debug("ltm.snippet.plan picked title=%r conf=%.2f", title, conf)
                if due and due > 0:
                    when_s = _fmt_utc(due)
                elif ws > 0 and we > 0:
                    when_s = f"{_fmt_utc(ws)}–{_fmt_utc(we)}"
                elif ws > 0:
                    when_s = f"from {_fmt_utc(ws)}"
                elif we > 0:
                    when_s = f"until {_fmt_utc(we)}"
                else:
                    when_s = "unscheduled"
                out["plan"] = f"{title} @ {when_s} (conf={conf:.2f})"
                pipe = self._redis.pipeline(transaction=True)
                pipe.hset(docid, mapping={"last_used_ts": now_ts, "last_used_turn": turn_id})
                pipe.hincrby(docid, "used_count", 1)
                await pipe.execute()
        except Exception:
            logger.debug("pick plans failed", exc_info=True)

        try:
            ranked = []
            for d in docs_bounds or []:
                try:
                    sim = _dist_to_sim(float(d.vector_score))
                except Exception:
                    sim = 0.0
                if sim < self._min_sim:
                    continue
                key = d.key if isinstance(d.key,str) else _as_str(d.key)
                try:
                    val = d.value if isinstance(d.value, str) else _as_str(getattr(d, "value", ""))
                except Exception:
                    val = ""
                if not val:
                    try:
                        val = d.title if isinstance(d.title, str) else _as_str(getattr(d, "title", ""))
                    except Exception:
                        val = ""
                conf = _as_float(getattr(d,"confidence", 0.5), 0.5)
                last_seen = _as_float(getattr(d, "last_seen", 0.0), 0.0)
                used_cnt  = _as_int(getattr(d, "used_count", 0), 0)
                lu_ts     = _as_float(getattr(d,"last_used_ts", 0.0), 0.0)
                lu_turn   = _as_int(getattr(d,"last_used_turn", -9999), -9999)
                rec_boost = _exp_decay_from(now_ts, last_seen, _LTM_RECENCY_TAU)
                lu_boost  = _exp_decay_from(now_ts, lu_ts, _LTM_LAST_USED_TAU)
                use_boost = _log1p_norm10(used_cnt)
                comp = sim * (1.0 + _LTM_W_CONF*conf + _LTM_W_RECENCY*rec_boost + _LTM_W_USE*use_boost + _LTM_W_LASTUSED*lu_boost)
                did = _as_str(getattr(d, "id", "")) or getattr(d, "id", "")
                ranked.append((comp, key, val, conf, did, lu_ts, lu_turn))
            ranked.sort(key=lambda t: t[0], reverse=True)
            chosen = None
            for comp, key, val, conf, docid, lu_ts, lu_turn in ranked:
                if (now_ts - lu_ts) < self._cool_secs or (turn_id - lu_turn) <= self._cool_turns:
                    continue
                chosen = (key, val, conf, docid)
                break
            if chosen:
                key, val, conf, docid = chosen
                logger.debug("ltm.snippet.boundary picked key=%s conf=%.2f", key, conf)
                out["boundary"] = f"{key}={val} (conf={conf:.2f})"
                pipe = self._redis.pipeline(transaction=True)
                pipe.hset(docid, mapping={"last_used_ts": now_ts, "last_used_turn": turn_id})
                pipe.hincrby(docid, "used_count", 1)
                await pipe.execute()
        except Exception:
            logger.debug("pick bounds failed", exc_info=True)

        return out

    async def mark_profile_used(
        self,
        uid: int,
        pairs: list[tuple[str, str]],
        *,
        turn_id: int,
        now_ts: float | None = None,
    ) -> None:

        self._ensure_redis()

        try:
            await self.record_activity(uid)
        except Exception:
            pass

        if not pairs:
            return
        now_ts = now_ts or _now()
        uid_tag = _tag_literal(str(uid))
        pipe = self._redis.pipeline(transaction=True)
        ops = 0
        for key, value in pairs[:64]:
            try:
                k = _canon_key(key)
                if not k or not value:
                    continue
                fid = f"facts:{uid}:{k}:{_hash8(_norm_val(_excerpt(value, 240)))}"
                try:
                    if await self._redis.exists(fid):
                        pipe.hset(fid, mapping={"last_used_ts": now_ts, "last_used_turn": turn_id})
                        pipe.hincrby(fid, "used_count", 1)
                        ops += 1
                        continue
                except Exception:
                    pass
                q = Query(
                    f'(@uid:{{{uid_tag}}} @key:{{{_tag_literal(k)}}})'
                ).return_fields("value").paging(0, 50)
                if _RS_SERVER_TIMEOUT_MS > 0:
                    try:
                        q = q.timeout(_RS_SERVER_TIMEOUT_MS)
                    except Exception:
                        pass
                try:
                    res = await asyncio.wait_for(
                        self._redis.ft(self.FACTS_IX).search(q),
                        timeout=_RS_TIMEOUT
                    )
                except ResponseError as e:
                    if _is_missing_index_error(e):
                        await self._ensure_indexes()
                        res = await asyncio.wait_for(
                            self._redis.ft(self.FACTS_IX).search(q),
                            timeout=_RS_TIMEOUT
                        )
                    else:
                        continue
                for d in getattr(res, "docs", []) or []:
                    try:
                        v = d.value if isinstance(d.value, str) else _as_str(d.value)
                    except Exception:
                        v = ""
                    if _norm_val(v) != _norm_val(value):
                        continue
                    did = _as_str(getattr(d, "id", "")) or getattr(d, "id", "")
                    pipe.hset(did, mapping={"last_used_ts": now_ts, "last_used_turn": turn_id})
                    pipe.hincrby(did, "used_count", 1)
                    ops += 1
                    break
            except Exception:
                continue
        if ops:
            try:
                await pipe.execute()
            except Exception:
                logger.debug("mark_profile_used: pipe.execute failed", exc_info=True)

    async def get_profile(self, uid: int, *, min_conf: float = 0.25, max_items: int = 64) -> list[tuple[str,str,float]]:

        self._ensure_redis()
        try:
            await self.ready()
        except Exception:
            pass
        try:
            q = (
                Query(f'(@uid:{{{_tag_literal(str(uid))}}} @confidence:[{min_conf} 1])')
                .return_fields("key", "value", "confidence", "last_seen", "used_count")
                .sort_by("last_seen", asc=False)
                .paging(0, max_items)
            )
            if _RS_SERVER_TIMEOUT_MS > 0:
                try:
                    q = q.timeout(_RS_SERVER_TIMEOUT_MS)
                except Exception:
                    pass
            res = await asyncio.wait_for(self._redis.ft(self.FACTS_IX).search(q), timeout=_RS_TIMEOUT)
        except ResponseError as e:
            if _is_missing_index_error(e):
                await self._ensure_indexes()
                res = await asyncio.wait_for(self._redis.ft(self.FACTS_IX).search(q), timeout=_RS_TIMEOUT)
            else:
                return []
        except Exception:
            return []
        rows = getattr(res, "docs", []) or []
        pool: dict[str, tuple[str,float,float,int]] = {}
        now = _now()
        for d in rows:
            k = _canon_key(_as_str(getattr(d,"key","")))
            v = _as_str(getattr(d,"value",""))
            c = _as_float(getattr(d,"confidence",0.5),0.5)
            ls = _as_float(getattr(d,"last_seen",0.0),0.0)
            uc = _as_int(getattr(d,"used_count",0),0)

            rec_boost = _exp_decay_from(now, ls, _LTM_RECENCY_TAU)
            score = c + 0.1*rec_boost + 0.04*math.log1p(uc)
            prev = pool.get(k)
            if (not prev) or (score > prev[2]):
                pool[k] = (v, c, score, uc)

        result = [(k, v_c[0], v_c[1], v_c[2]) for k, v_c in pool.items()]

        priority = {
            # identity
            "name_to_call": 100, "pronouns": 95, "gender": 92, "age": 90, "birthday": 88,
            "timezone": 86, "languages": 84, "native_language": 82, "locale": 80,
            "city": 78, "country": 76, "nationality": 75, "citizenship": 74,
            "marital_status": 72, "relationship_status": 72, "kids": 70, "has_children": 70, "pets": 69,
            # communication / boundaries
            "address_style": 66, "formality": 65, "voice_pref": 64, "communication_style": 64,
            # Флаги наподобие no_emojis/no_voice/no_links оставляем в facts при желании,
            # но убираем из списка приоритетов все boundary-специфичные ключи
            "no_emojis": 63, "no_voice": 61, "no_links": 60,
            # lifestyle
            "diet": 55, "allergies": 54, "coffee_pref": 53, "tea_pref": 52, "food_likes": 51, "food_dislikes": 50,
            "sleep_time": 48, "work_hours": 47, "fitness_routine": 46, "smoker": 45, "alcohol_pref": 44,
            "travel_pref": 42, "transport_pref": 41,
            # culture & hobbies
            "hobbies": 38, "interests": 37, "music_genres": 36, "favorite_bands": 35, "podcasts": 34,
            "movie_genres": 33, "series_genres": 32, "game_genres": 31, "book_genres": 30, "authors": 29,
            "sports_team": 28, "favorite_sport": 27, "news_sources": 26,
            # tech
            "devices": 20, "os_pref": 19, "messengers": 18, "social_media": 17,
            # education/work
            "education": 15, "profession": 14, "job_title": 13, "company": 12, "industry": 11, "income_bracket": 10,
            # beliefs
            "religion": 8, "politics": 7,
        }
        result.sort(key=lambda t: (priority.get(t[0], 1), t[3]), reverse=True)
        return [(k, v, c) for (k, v, c, _score) in result[:max_items]]

    async def relevant_profile(
        self,
        *,
        uid: int,
        context: str,
        top_n: int = 14,
        min_conf: float = 0.28,
        query_vec: bytes | None = None,
    ) -> list[tuple[str, str]]:

        try:
            await self.ready()
        except Exception:
            pass
        try:
            if query_vec is not None:
                q_emb = self._ensure_vec(query_vec)
            else:
                raw = await self._embed_cached(context or " ")
                q_emb = self._ensure_vec(raw)
        except Exception:
            q_emb = self._zero_vec
        docs = await self._knn(
            self.FACTS_IX,
            f'(@uid:{{{_tag_literal(str(uid))}}})',
            q_emb,
            max(8, top_n * 2),
            ["key","value","confidence","last_seen","used_count"]
        ) or []
        now = _now()
        grouped: dict[str, tuple[str,float,float]] = {}
        for d in docs:
            try:
                sim = _dist_to_sim(float(d.vector_score))
            except Exception:
                sim = 0.0
            if sim < (self._min_sim - 0.05):
                continue
            k = _canon_key(_as_str(getattr(d,"key","")))
            v = _as_str(getattr(d,"value",""))
            c = _as_float(getattr(d,"confidence",0.5),0.5)
            if c < min_conf:
                continue
            ls = _as_float(getattr(d,"last_seen",0.0),0.0)
            uc = _as_int(getattr(d,"used_count",0),0)
            rec = _exp_decay_from(now, ls, _LTM_RECENCY_TAU)
            score = sim * (1.0 + 0.14*c + 0.08*rec + 0.05*math.log1p(uc))
            prev = grouped.get(k)
            if (not prev) or (score > prev[2]):
                grouped[k] = (v, c, score)
        if not grouped:
            base = await self.get_profile(uid, min_conf=min_conf, max_items=top_n)
            return [(k,v) for (k,v,_c) in base]
        items = sorted(grouped.items(), key=lambda kv: kv[1][2], reverse=True)
        pairs: list[tuple[str,str]] = [(k, v_c[0]) for k, v_c in items[:top_n]]
        return pairs
