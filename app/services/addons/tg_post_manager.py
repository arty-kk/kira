#app/services/addons/tg_post_manager.py
from __future__ import annotations

import textwrap
import urllib.request
import asyncio
import hashlib
import json
import logging
import random
import time
import re
import secrets
import base64
import binascii
import struct
import zlib

from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from app.clients.openai_client import _call_openai_with_retry, _get_output_text
from app.clients.telegram_client import get_bot
from app.services.responder.prompt_builder import build_system_prompt
from app.emo_engine import get_persona
from app.core.memory import load_context, push_message, get_redis
from app.config import settings

logger = logging.getLogger(__name__)

TIME_BUCKET_LABELS = {
    "morning": "утренний обзор",
    "day": "дневной разбор",
    "evening": "вечерняя колонка",
    "night": "вне коридора постинга",
}

MAX_HISTORY = 24
RECENT_POSTS_FOR_CONTEXT = 10

DEFAULT_REDIS_LOCK_TTL_SEC = 600

DEFAULT_MIN_GAP_MINUTES_FLOOR = 25
DEFAULT_MAX_GAP_MINUTES_CAP = 140

REDIS_KEY_PREFIX = "tg_post_manager"

POST_CHAR_LIMIT = 600
TRIM_SUFFIX = "..."

MAX_TEMPERATURE = 0.78
MIN_TEMPERATURE = 0.52
TOP_P_MIN = 0.80
TOP_P_MAX = 0.98

DEFAULT_NEWS_LOOKBACK_HOURS = 12
DEFAULT_NEWS_MAX_ITEMS = 12

DEFAULT_IMAGE_ENABLED = True
DEFAULT_IMAGE_PROB = 0.33
DEFAULT_IMAGE_TIMEOUT_SEC = 180
DEFAULT_IMAGE_MODEL = "gpt-image-1"
DEFAULT_IMAGE_SIZE = "1024x1024"
DEFAULT_IMAGE_QUALITY = "low"         # low|medium|high|auto
DEFAULT_IMAGE_FORMAT = "png"          # png|jpeg|webp
DEFAULT_IMAGE_COMPRESSION = 60        # 0..100 (только для jpeg/webp)
DEFAULT_IMAGE_BACKGROUND = "auto"     # auto|transparent|opaque
DEFAULT_REQUIRE_IMAGE = False
DEFAULT_REQUIRE_IMAGE_STRICT = False
DEFAULT_FALLBACK_IMAGE_PATH = "app/assets/fallback_post.png"

IMAGE_DISABLED_TTL_SEC = 600

DEFAULT_INCLUDE_SOURCE = False
DEFAULT_INCLUDE_URL = False
DEFAULT_STORY_ALTS = 3

DEFAULT_CANDIDATE_COUNT = 4
DEFAULT_EVAL_ENABLED = True
DEFAULT_POLISH_ENABLED = True

DEFAULT_RUBRICS_PER_RUN = 2

DEFAULT_HUMOR_RATE = 0.35
DEFAULT_EVENING_HUMOR_BOOST = 0.15
DEFAULT_HUMOR_MAX = 0.70

CTA_MARKER_PREFIX = "__DM_CTA_AT__"
META_MARKER_PREFIX = "__TG_META__"

DEFAULT_MODS = {
    "creativity_mod": 0.55,
    "sarcasm_mod": 0.30,
    "enthusiasm_mod": 0.55,
    "confidence_mod": 0.65,
    "precision_mod": 0.80,
    "fatigue_mod": 0.0,
    "stress_mod": 0.0,
    "valence_mod": 0.05,
}

FORBIDDEN_CALLS = [
    "подписывайтесь", "подпишитесь", "ставьте лайк", "лайк", "репост", "перешлите", "делитесь",
    "жмите", "переходите по ссылке", "ссылка в описании", "комментируйте", "напишите в комментариях",
    "купите", "покупайте", "закажите", "оформите", "инвестируйте", "купить токен", "покупай токен",
    "гарантированная прибыль", "заработаете", "рост акций",
    "приходите", "вступайте", "регистрируйтесь",
]

FORBIDDEN_OPENERS = [
    "ну что,", "ну что ",
    "привет,", "привет ",
    "а вот и",
    "новости дня",
    "срочные новости",
    "всем привет",
    "разбираем новости",
    "давайте разберем",
    "давайте разберём",
]

TRIGGER_KEYWORDS_STRONG = [
    "релиз", "модель", "агент", "утечк", "инцидент", "уязвим", "взлом", "персональн", "данн",
    "регуля", "штраф", "запрет", "безопасн", "prompt injection", "инъекц", "eval", "оценк",
]
TRIGGER_KEYWORDS_MEDIUM = [
    "rag", "контекст", "инференс", "стоимост", "пилот", "внедрен", "интеграц", "copilot",
    "оркестрац", "векторн", "эмбед", "обучен", "fine-tun", "дообуч", "комплаенс", "политик",
]

TOKEN_RE = re.compile(r"[a-zа-яё0-9]{4,}", re.IGNORECASE)
SHORT_TECH_RE = re.compile(r"\b(?:rag|llm|api|gpt|gpu|cpu|soc|tls|ssl|k8s)\b", re.IGNORECASE)
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
LIST_LINE_RE = re.compile(r"(?m)^\s*(?:[-•*]|(?:\d{1,2}[.)]))\s+")
HASHTAG_RE = re.compile(r"#\w+", re.UNICODE)
FIRST_SENTENCE_RE = re.compile(r"^(.{1,240}?)([.!?…]\s|\n|$)", re.DOTALL)
SENT_SPLIT_RE = re.compile(r"[.!?…](?:[)\]»”\"']*)?(?:\s+|$)")
EMOJI_RE = re.compile(
    "[" +
    "\U0001F300-\U0001FAFF"
    "\U00002700-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "]+",
    flags=re.UNICODE,
)

SOURCE_FOOTER_RE = re.compile(r"(?is)(?:\n\s*источник:\s*.*)$")
SOURCE_LINE_RE = re.compile(r"(?im)^\s*источник\s*:", re.UNICODE)
SENT_END_RE = re.compile(r"[.!?…](?:[)\]»”\"']*)?(?:\s+|$)")
AI_SLASH_RE = re.compile(r"(?i)\bии\s*/\s*ai\b|\bai\s*/\s*ии\b")
AI_TOKEN_RE = re.compile(r"(?i)\bai\b(?!\s+act\b)")

def _normalize_compact(text: str) -> str:
    s = (text or "").lower().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9]+", "", s)

STRONG_TRIGGERS_NORM = sorted({ _normalize_compact(t) for t in TRIGGER_KEYWORDS_STRONG if t })
MEDIUM_TRIGGERS_NORM = sorted({ _normalize_compact(t) for t in TRIGGER_KEYWORDS_MEDIUM if t })

ALL_RUBRICS = [
    "news_explainer",
    "practical_takeaway",
    "anti_hype",
    "tool_tip",
    "risk_alert",
    "market_signal",
    "case_mini",
    "editor_note",
    "field_note",
    "myth_bust",
    "light_observation",
]

HUMOR_FRIENDLY_RUBRICS = {"editor_note", "light_observation", "field_note", "anti_hype", "myth_bust"}

@dataclass
class NewsItem:
    id: str
    type: str  # PRODUCT|RESEARCH|BUSINESS|POLICY|INCIDENT|TOOL|MIXED
    title: str
    what: str
    why: str
    source: str
    url: str | None
    published_at: datetime | None
    confidence: str  # high|medium|low


DEFAULT_WEEKLY_RUBRIC_PLAN = {
    "mon": ["practical_takeaway", "tool_tip"],
    "tue": ["tool_tip", "news_explainer"],
    "wed": ["myth_bust", "practical_takeaway"],
    "thu": ["news_explainer", "anti_hype"],
    "fri": ["field_note", "anti_hype"],
    "sat": ["light_observation", "editor_note"],
    "sun": ["editor_note", "myth_bust"],
}

def _weekday_key(local_now: datetime) -> str:
    return ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][local_now.weekday()]

def _corridor_duration_minutes(start_hour: int, end_hour: int) -> int:
    start_hour = int(start_hour) % 24
    end_hour = int(end_hour) % 24
    if start_hour == end_hour:
        return 0
    if end_hour > start_hour:
        return (end_hour - start_hour) * 60
    return ((24 - start_hour) + end_hour) * 60

def _corridor_elapsed_minutes(local_now: datetime, start_hour: int, end_hour: int) -> int:
    start_hour = int(start_hour) % 24
    end_hour = int(end_hour) % 24
    cur = local_now.hour * 60 + local_now.minute
    start = start_hour * 60
    if end_hour > start_hour:
        return max(0, cur - start)
    if cur >= start:
        return cur - start
    return (24 * 60 - start) + cur

def _local_day_key(local_now: datetime) -> str:
    return local_now.strftime("%Y%m%d")

def _coerce_int(value: Any, default: int) -> int:
    try:
        if value is None:
            return int(default)
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, str):
            s = value.strip()
            if not s:
                return int(default)
            # allow "60.0"
            if any(ch in s for ch in ".eE"):
                return int(float(s))
            return int(s)
        return int(value)
    except Exception:
        return int(default)

def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off", ""}:
            return False
        return bool(default)
    return bool(value)

def _normalize_ai_terms(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return s
    s = AI_SLASH_RE.sub("ИИ", s)
    s = AI_TOKEN_RE.sub("ИИ", s)
    return s

def _guess_image_ext(data: bytes) -> str:
    if not data:
        return "png"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "png"

def _looks_like_org_verification_error(exc: Exception) -> bool:

    msg = (str(exc) or "").lower()
    if "verify organization" in msg:
        return True
    if "must be verified" in msg and "organization" in msg:
        return True
    if "organization must be verified" in msg:
        return True
    if "org" in msg and "verif" in msg:
        return True
    if "not authorized" in msg and ("image" in msg or "gpt-image" in msg):
        return True
    return False

def _image_disabled_key(channel_id: int) -> str:
    return f"{REDIS_KEY_PREFIX}:image_disabled:{int(channel_id)}"

async def _get_image_disabled_reason(redis, channel_id: int) -> str | None:
    if not redis:
        return None
    try:
        raw = await redis.get(_image_disabled_key(channel_id))
        if not raw:
            return None
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "ignore")
        s = str(raw).strip()
        return s or "disabled"
    except Exception:
        return None

async def _download_bytes(url: str, timeout: float = 20.0) -> bytes | None:
    def _do() -> bytes | None:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.read()
        except Exception:
            return None
    return await asyncio.to_thread(_do)

async def _mark_image_disabled(redis, channel_id: int, reason: str) -> None:
    if not redis:
        return
    try:
        await redis.set(_image_disabled_key(channel_id), str(reason or "disabled"), ex=IMAGE_DISABLED_TTL_SEC)
    except Exception:
        logger.debug("tg_post_manager: cannot set image_disabled marker", exc_info=True)

def _redis_to_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", "replace")
        except Exception:
            return None
    return str(value)

async def _acquire_redis_lock(redis, channel_id: int) -> tuple[str, str] | None:

    if not redis:
        return None
    key = f"{REDIS_KEY_PREFIX}:lock:{int(channel_id)}"
    ttl = _coerce_int(getattr(settings, "TG_POST_REDIS_LOCK_TTL_SEC", None), DEFAULT_REDIS_LOCK_TTL_SEC)
    post_timeout = _coerce_int(getattr(settings, "POST_MODEL_TIMEOUT", None), 60)
    ttl = max(ttl, post_timeout + 420)
    token = secrets.token_urlsafe(16)
    try:
        ok = await redis.set(key, token, nx=True, ex=ttl)
        return (key, token) if ok else None
    except Exception:
        logger.debug("tg_post_manager redis lock unavailable (redis error)", exc_info=True)
        return None

async def _release_redis_lock(redis, lock_key: str, token: str) -> None:

    if not redis or not lock_key or not token:
        return
    lua = """
    if redis.call("GET", KEYS[1]) == ARGV[1] then
      return redis.call("DEL", KEYS[1])
    else
      return 0
    end
    """
    try:
        await redis.eval(lua, 1, lock_key, token)
    except Exception:
        logger.debug("tg_post_manager redis lock release failed; rely on TTL", exc_info=True)

async def _get_daily_pacing_state(redis, channel_id: int, local_now: datetime) -> tuple[int, float | None]:
    if not redis:
        return 0, None
    day = _local_day_key(local_now)
    k_count = f"{REDIS_KEY_PREFIX}:count:{int(channel_id)}:{day}"
    k_last = f"{REDIS_KEY_PREFIX}:last_ts:{int(channel_id)}"
    try:
        raw_c, raw_last = await asyncio.gather(redis.get(k_count), redis.get(k_last))
        c = _coerce_int(_redis_to_str(raw_c), 0)
        last = None
        s_last = _redis_to_str(raw_last)
        if s_last:
            try:
                last = float(s_last)
            except Exception:
                last = None
        return max(0, c), last
    except Exception:
        logger.debug("tg_post_manager get pacing state failed", exc_info=True)
        return 0, None

async def _bump_daily_pacing_state(redis, channel_id: int, local_now: datetime) -> None:
    if not redis:
        return
    day = _local_day_key(local_now)
    k_count = f"{REDIS_KEY_PREFIX}:count:{int(channel_id)}:{day}"
    k_last = f"{REDIS_KEY_PREFIX}:last_ts:{int(channel_id)}"
    try:
        pipe = redis.pipeline()
        pipe.incr(k_count)
        pipe.expire(k_count, 3 * 86400)
        pipe.set(k_last, str(time.time()), ex=7 * 86400)
        await pipe.execute()
    except Exception:
        logger.debug("tg_post_manager bump pacing state failed", exc_info=True)

def _normalize_opening(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    s = URL_RE.sub(" ", s)
    s = HASHTAG_RE.sub(" ", s)
    s = s.lower().replace("ё", "е")
    s = re.sub(r"[^a-zа-я0-9\s]", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _opening_text(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    m = FIRST_SENTENCE_RE.match(t)
    opener = (m.group(1) if m else t[:240]).strip()
    return opener

def _opening_prefix(text: str, n_words: int = 4) -> str:
    opener = _normalize_opening(_opening_text(text))
    if not opener:
        return ""
    words = opener.split()
    return " ".join(words[: max(1, int(n_words))])[:80]

def _opening_fingerprint(text: str) -> str:
    opener = _normalize_opening(_opening_text(text))
    if not opener:
        return ""
    opener = opener[:120]
    return hashlib.sha1(opener.encode("utf-8")).hexdigest()[:16]

def _opener_key(channel_id: int, day_key: str) -> str:
    return f"{REDIS_KEY_PREFIX}:openers:{int(channel_id)}:{day_key}"

async def _is_recent_opener(redis, channel_id: int, local_now: datetime, fp: str) -> bool:
    if not redis or not fp:
        return False
    day = _local_day_key(local_now)
    prev = _local_day_key(local_now - timedelta(days=1))
    try:
        k1 = _opener_key(channel_id, day)
        k2 = _opener_key(channel_id, prev)
        a, b = await asyncio.gather(redis.sismember(k1, fp), redis.sismember(k2, fp))
        return bool(a or b)
    except Exception:
        logger.debug("tg_post_manager opener check failed", exc_info=True)
        return False

async def _remember_opener(redis, channel_id: int, local_now: datetime, fp: str) -> None:
    if not redis or not fp:
        return
    day = _local_day_key(local_now)
    k = _opener_key(channel_id, day)
    try:
        pipe = redis.pipeline()
        pipe.sadd(k, fp)
        pipe.expire(k, 3 * 86400)
        await pipe.execute()
    except Exception:
        logger.debug("tg_post_manager opener remember failed", exc_info=True)

async def _rephrase_opening_once(
    draft: str,
    story_block: str,
    rubric: str,
    char_limit: int,
    avoid_prefix: str,
) -> str | None:

    model = getattr(settings, "POST_MODEL", None)
    if not model:
        return None

    avoid_line = f'Не начинай с: «{avoid_prefix}». ' if avoid_prefix else ""
    user_prompt = (
        "Перепиши пост так, чтобы он начинался по-другому (другой первый заход/первая фраза), "
        "но смысл и факты сохрани. "
        f"{avoid_line}"
        "Никаких списков, нумерации, эмодзи, хэштегов, призывов к действиям. "
        f"Длина до {char_limit} символов.\n\n"
        "Факты можно брать только отсюда:\n"
        f"{story_block}\n\n"
        "Текущий текст:\n"
        f"{draft}"
    )
    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=model,
                input=_to_responses_input(
                    [
                        {"role": "system", "content": "Ты аккуратно редактируешь текст по-русски, соблюдая требования к стилю."},
                        {"role": "user", "content": user_prompt},
                    ]
                ),
                temperature=0.35,
                max_output_tokens=520,
                total_timeout=240,
            ),
            timeout=_coerce_float(getattr(settings, "POST_MODEL_TIMEOUT", None), 60.0),
        )
        out = _get_output_text(resp) or ""
        out = out.strip()
        if not out:
            return None
        out = _clamp_text_len(out, char_limit)
        if LIST_LINE_RE.search(out):
            return None
        return out
    except Exception:
        logger.debug("tg_post_manager rephrase opening failed", exc_info=True)
        return None

def _load_weekly_plan_from_settings() -> dict[str, list[str]]:

    raw = getattr(settings, "TG_POST_WEEKLY_PLAN_JSON", None)
    if not raw:
        return DEFAULT_WEEKLY_RUBRIC_PLAN

    try:
        obj = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        logger.warning("tg_post_manager: invalid TG_POST_WEEKLY_PLAN_JSON, using default")
        return DEFAULT_WEEKLY_RUBRIC_PLAN

    if not isinstance(obj, dict):
        return DEFAULT_WEEKLY_RUBRIC_PLAN

    out: dict[str, list[str]] = {}
    for k, v in obj.items():
        if not isinstance(k, str):
            continue
        kk = k.strip().lower()
        if kk not in {"mon","tue","wed","thu","fri","sat","sun"}:
            continue

        vals: list[str] = []
        if isinstance(v, str):
            vals = [v]
        elif isinstance(v, list):
            vals = [x for x in v if isinstance(x, str)]
        vals = [x.strip() for x in vals if x and x.strip() in ALL_RUBRICS]
        if vals:
            out[kk] = vals[:3]

    return out or DEFAULT_WEEKLY_RUBRIC_PLAN

def _planned_rubrics_for_today(local_now: datetime) -> list[str]:
    plan = _load_weekly_plan_from_settings()
    key = _weekday_key(local_now)
    vals = plan.get(key) or []
    return [r for r in vals if r in ALL_RUBRICS]

def _context_override_rubrics(
    planned: list[str],
    mood: str,
    story: NewsItem | None,
) -> list[str]:

    mood = (mood or "").lower()
    t = story.type if story else None

    out = list(planned)

    def _ensure_front(r: str) -> None:
        if r in ALL_RUBRICS:
            if r in out:
                out.remove(r)
            out.insert(0, r)

    if mood == "cautious" or t == "INCIDENT":
        _ensure_front("risk_alert")
        if "news_explainer" not in out:
            out.insert(1, "news_explainer")

    if mood == "controversial" or (story and story.type == "POLICY"):
        _ensure_front("news_explainer")
        if "anti_hype" in out and "news_explainer" in out and out.index("anti_hype") < out.index("news_explainer"):
            out.remove("anti_hype")
            out.insert(1, "anti_hype")

    uniq: list[str] = []
    for r in out:
        if r not in uniq:
            uniq.append(r)
    return uniq[:3]

def _allocate_counts(total: int, n: int) -> list[int]:
    total = max(1, int(total))
    n = max(1, int(n))
    counts = [0] * n
    for i in range(total):
        counts[i % n] += 1
    return counts

def _strip_source_footer(text: str) -> str:
    return SOURCE_FOOTER_RE.sub("", (text or "").strip()).strip()

def _passes_hard_constraints(text: str, allow_url: bool, story: NewsItem | None = None) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if _contains_direct_call_to_action(t):
        return False
    if _has_listish_format(t):
        return False
    if _has_emoji_or_hashtags(t):
        return False
    if (not allow_url) and URL_RE.search(t):
        return False
    sc = _sentence_count(t)
    if sc < 3 or sc > 7:
        return False
    if not _sentences_on_new_lines_ok(t):
        return False

    if story:
        cand_t = _tokens(t)
        story_t = _tokens(f"{story.title} {story.what}")
        if story_t:
            inter = len(cand_t & story_t)
            need = 1 if len(story_t) < 6 else 2
            if inter < need:
                return False

    return True

def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)

def _tokens(text: str) -> set[str]:
    s = (text or "").lower().replace("ё", "е")
    base = set(TOKEN_RE.findall(s))
    base |= set(SHORT_TECH_RE.findall(s))
    return base

def _extract_recent_posts(history: list[dict], limit: int = RECENT_POSTS_FOR_CONTEXT) -> str:
    posts: list[str] = []
    for m in reversed(history):
        if m.get("role") != "assistant":
            continue
        content = m.get("content", "")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts: list[str] = []
            for p in content:
                if isinstance(p, dict) and isinstance(p.get("text"), str):
                    parts.append(p["text"])
            if parts:
                text = " ".join(parts)
        if text:
            short = text.replace("\n", " ").strip()
            if len(short) > 220:
                short = short[:217].rstrip() + "..."
            posts.append(short)
        if len(posts) >= limit:
            break

    if not posts:
        return ""
    posts.reverse()
    return "\n".join(posts)

def _strip_forbidden_openers(text: str) -> str:
    if not text:
        return text
    prefix_ws_len = len(text) - len(text.lstrip())
    head = text[prefix_ws_len:].lower().replace("ё", "е")

    for opener in FORBIDDEN_OPENERS:
        op_norm = opener.lower().replace("ё", "е")
        if head.startswith(op_norm):
            cut_from = prefix_ws_len + len(opener)
            while cut_from < len(text) and text[cut_from] in " :—-–":
                cut_from += 1
            return text[:prefix_ws_len] + text[cut_from:].lstrip()
    return text

def _contains_direct_call_to_action(text: str) -> bool:
    low = (text or "").lower()
    return any(phrase in low for phrase in FORBIDDEN_CALLS)

def _merge_and_clamp_mods(style_mods: dict | None) -> dict:
    mods = DEFAULT_MODS.copy()
    if not isinstance(style_mods, dict):
        return mods

    def _pick(*keys: str, fallback: Any) -> Any:
        for k in keys:
            if k in style_mods:
                return style_mods.get(k)
        return fallback

    for key in mods.keys():
        base = key[:-4] if key.endswith("_mod") else key
        if key == "valence_mod":
            raw = _pick("valence_mod", "valence", base, fallback=mods[key])
            x = _coerce_float(raw, mods[key])
            mods[key] = max(-1.0, min(1.0, x))
        else:
            raw = _pick(key, base, fallback=mods[key])
            x = _coerce_float(raw, mods[key])
            mods[key] = max(0.0, min(1.0, x))
    return mods

def _to_responses_input(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages or []:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, str):
            out.append(
                {
                    "role": role,
                    "content": [
                        {
                            "type": "input_text",
                            "text": content,
                        }
                    ],
                }
            )
        elif isinstance(content, list):
            norm_parts: list[dict] = []
            for p in content:
                if isinstance(p, dict):
                    t = p.get("type")
                    if t == "text" or (t is None and "text" in p):
                        p = {
                            "type": "input_text",
                            "text": p.get("text"),
                        }
                norm_parts.append(p)
            out.append({"role": role, "content": norm_parts})
        else:
            out.append({"role": role, "content": [{"type": "input_text", "text": str(content)}]})
    return out

def _get_local_now() -> datetime:
    tz_raw = getattr(settings, "DEFAULT_TZ", "UTC")
    try:
        offset_hours = int(tz_raw)
    except (TypeError, ValueError):
        offset_hours = None

    if offset_hours is not None:
        tz = timezone(timedelta(hours=offset_hours))
        return datetime.now(tz)

    try:
        tz = ZoneInfo(str(tz_raw))
    except Exception:
        tz = timezone.utc

    return datetime.now(tz)

def _get_time_bucket() -> tuple[str, str]:
    local_now = _get_local_now()
    hour = local_now.hour

    start_hour = _coerce_int(getattr(settings, "SCHED_TG_START_HOUR", None), 8)
    end_hour = _coerce_int(getattr(settings, "SCHED_TG_END_HOUR", None), 23)

    if start_hour == end_hour:
        bucket = "night"
    else:
        if end_hour > start_hour:
            in_window = start_hour <= hour < end_hour
            if not in_window:
                bucket = "night"
            else:
                duration = end_hour - start_hour
                morning_span = max(1, int(duration * 0.25))
                evening_span = max(1, int(duration * 0.25))
                morning_end = start_hour + morning_span
                evening_start = end_hour - evening_span
                if hour < morning_end:
                    bucket = "morning"
                elif hour < evening_start:
                    bucket = "day"
                else:
                    bucket = "evening"
        else:
            in_window = (hour >= start_hour) or (hour < end_hour)
            if not in_window:
                bucket = "night"
            else:
                rel_hour = (hour - start_hour) % 24
                duration = (end_hour - start_hour) % 24 or 24
                morning_span = max(1, int(duration * 0.25))
                evening_span = max(1, int(duration * 0.25))
                morning_end = morning_span
                evening_start = duration - evening_span
                if rel_hour < morning_end:
                    bucket = "morning"
                elif rel_hour < evening_start:
                    bucket = "day"
                else:
                    bucket = "evening"

    label = TIME_BUCKET_LABELS.get(bucket, "общая рубрика")
    return bucket, label

def _hits_any_compact(text: str, triggers_norm: list[str]) -> int:
    s = _normalize_compact(text or "")
    hits = 0
    for t in triggers_norm:
        if t and t in s:
            hits += 1
    return hits

def _should_post_now(
    time_bucket: str,
    mood: str,
    intensity: str,
    keywords: str,
    story_text: str,
    mods: dict,
) -> bool:
    tb = (time_bucket or "").lower()
    mood = (mood or "").lower()
    intensity = (intensity or "").lower()

    combined = " ".join([keywords or "", story_text or ""]).strip()
    score = 0.0

    if intensity == "high":
        score += 0.60
    elif intensity == "medium":
        score += 0.35
    else:
        score += 0.18

    if mood in {"breakthrough", "controversial"}:
        score += 0.10
    elif mood in {"cautious"}:
        score += 0.06
    elif mood in {"quiet"}:
        score -= 0.08

    if tb == "evening":
        score += 0.08
    elif tb == "morning":
        score += 0.06
    elif tb == "night":
        score -= 0.20

    strong_hits = _hits_any_compact(combined, STRONG_TRIGGERS_NORM)
    medium_hits = _hits_any_compact(combined, MEDIUM_TRIGGERS_NORM)

    if strong_hits:
        score += 0.16 + 0.05 * (strong_hits - 1)
    if medium_hits:
        score += 0.08 + 0.03 * (medium_hits - 1)

    sarcasm = float(mods.get("sarcasm_mod", 0.3) or 0.3)
    enthusiasm = float(mods.get("enthusiasm_mod", 0.55) or 0.55)
    fatigue = float(mods.get("fatigue_mod", 0.0) or 0.0)
    stress = float(mods.get("stress_mod", 0.0) or 0.0)

    score += 0.04 * (sarcasm - 0.3)
    score += 0.05 * (enthusiasm - 0.5)
    score += 0.05 * stress
    score -= 0.10 * fatigue

    score = max(0.0, min(score, 1.0))

    if score >= 0.86:
        return True
    if score <= 0.16:
        return False
    return random.random() <= score

def _safe_json_extract(text: str) -> Any | None:
    if not text:
        return None
    s = text.strip()

    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
        s = s.strip()

    try:
        return json.loads(s)
    except Exception:
        pass

    lbr = s.find("[")
    rbr = s.rfind("]")
    if 0 <= lbr < rbr:
        try:
            return json.loads(s[lbr : rbr + 1])
        except Exception:
            pass

    lbr = s.find("{")
    rbr = s.rfind("}")
    if 0 <= lbr < rbr:
        try:
            return json.loads(s[lbr : rbr + 1])
        except Exception:
            pass

    return None

def _parse_dt(dt_raw: Any) -> datetime | None:
    if not dt_raw:
        return None
    if isinstance(dt_raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(dt_raw), tz=timezone.utc)
        except Exception:
            return None
    if isinstance(dt_raw, str):
        s = dt_raw.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None
    return None

def _coerce_news_items(raw: Any) -> list[NewsItem]:
    items: list[NewsItem] = []
    if not raw:
        return items

    if isinstance(raw, dict) and "items" in raw:
        raw = raw.get("items")

    if not isinstance(raw, list):
        return items

    for i, obj in enumerate(raw):
        if not isinstance(obj, dict):
            continue

        itype = str(obj.get("type") or "").strip().upper()
        if itype not in {"PRODUCT", "RESEARCH", "BUSINESS", "POLICY", "INCIDENT", "TOOL"}:
            itype = "MIXED"

        title = str(obj.get("title") or "").strip()
        what = str(obj.get("what") or obj.get("summary") or "").strip()
        why = str(obj.get("why") or obj.get("impact") or "").strip()
        source = str(obj.get("source") or "").strip()
        url = str(obj.get("url") or "").strip() or None
        published_at = _parse_dt(obj.get("published_at") or obj.get("published_at_utc") or obj.get("published_at_iso"))
        confidence = str(obj.get("confidence") or "medium").strip().lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "medium"

        if not title or not what:
            continue

        items.append(
            NewsItem(
                id=str(obj.get("id") or f"n{i+1}"),
                type=itype,
                title=title,
                what=what,
                why=why,
                source=source or "источник не указан",
                url=url,
                published_at=published_at,
                confidence=confidence,
            )
        )

    return items

async def _fetch_ai_news_digest(local_now: datetime) -> list[NewsItem]:
    
    model = getattr(settings, "RESPONSE_MODEL", None) or getattr(settings, "POST_MODEL", None)
    if not model:
        return []

    lookback_h = int(getattr(settings, "AI_NEWS_LOOKBACK_HOURS", DEFAULT_NEWS_LOOKBACK_HOURS) or DEFAULT_NEWS_LOOKBACK_HOURS)
    max_items = int(getattr(settings, "AI_NEWS_MAX_ITEMS", DEFAULT_NEWS_MAX_ITEMS) or DEFAULT_NEWS_MAX_ITEMS)

    user_prompt = (
        "Собери компактный дайджест событий по индустрии ИИ за последние "
        f"{lookback_h} часов относительно времени: {local_now.isoformat()}.\n"
        "В тексте используй индустриальные термины, названия компаний, технологий в международном формате (AI, Nvidia, RAG и т.д.).\n\n"
        "Требования:\n"
        f"- верни JSON-массив из 0–{max_items} объектов (не заполняй ради количества);\n"
        "- каждый объект: {\n"
        '  "id": "n1",\n'
        '  "type": "PRODUCT|RESEARCH|BUSINESS|POLICY|INCIDENT|TOOL",\n'
        '  "title": "короткий заголовок по-русски",\n'
        '  "what": "что произошло (1–2 предложения, без воды)",\n'
        '  "why": "почему важно для практики/рынка (1 предложение)",\n'
        '  "source": "название источника (издание/блог/регулятор)",\n'
        '  "url": "https://...",\n'
        '  "published_at": "ISO-8601 UTC",\n'
        '  "confidence": "high|medium|low"\n'
        "}\n"
        "- если точная дата/время неочевидны — ставь confidence=low и обозначь неопределённость в what;\n"
        "- отсекай явный маркетинг, реферальные ссылки, курсы и инвестиционный контент;\n"
        "- не выдумывай цифры, цитаты и «слухи».\n"
    )

    system_text = (
        "Ты — техредактор по индустрии ИИ. Твоя цель — достать факты и оформить их в строгий JSON. "
        "Используй web_search. Если источники противоречат — confidence=low и никаких выводов."
    )

    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=model,
                input=_to_responses_input(
                    [
                        {"role": "system", "content": system_text},
                        {"role": "user", "content": user_prompt},
                    ]
                ),
                tools=[{"type": "web_search"}],
                tool_choice="required",
                max_output_tokens=1600,
                temperature=0.2,
                total_timeout=240,
            ),
            timeout=250,
        )
        text = (_get_output_text(resp) or "").strip()
    except asyncio.TimeoutError:
        logger.warning("tg_post_manager AI news digest timed out")
        return []
    except Exception:
        logger.exception("tg_post_manager failed to fetch AI news digest")
        return []

    raw = _safe_json_extract(text)
    items = _coerce_news_items(raw)
    if len(items) > max_items:
        items = items[:max_items]
    return items

async def _summarize_keywords(items: list[NewsItem]) -> str:
    model = getattr(settings, "RESPONSE_MODEL", getattr(settings, "POST_MODEL", None))
    if not model or not items:
        return ""

    compact = "\n".join(f"- [{it.type}] {it.title}: {it.what}" for it in items[:10])
    prompt = (
        "Дай 3–6 коротких русских тем (через запятую), которые описывают общий набор новостей.\n"
        "Без воды. Можно 1–2 технических термина (RAG/eval), если это реально тема дня.\n"
        "Верни только строку."
    )

    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=model,
                input=_to_responses_input(
                    [
                        {"role": "system", "content": "Ты аккуратно выделяешь темы и не выдумываешь факты."},
                        {"role": "user", "content": f"{prompt}\n\nНОВОСТИ:\n{compact}"},
                    ]
                ),
                max_output_tokens=70,
                temperature=0.2,
                total_timeout=240,
            ),
            timeout=min(_coerce_float(getattr(settings, "RESPONSE_MODEL_TIMEOUT", None), 15.0), 30.0),
        )
        out = (_get_output_text(resp) or "").strip()
    except Exception:
        logger.exception("tg_post_manager _summarize_keywords failed")
        return ""

    out = out.replace("\n", " ").strip(" ,")
    return out[:160]

def _analyze_ai_day(items: list[NewsItem]) -> dict[str, str]:
    if not items:
        return {"focus": "MIXED", "mood": "quiet", "intensity": "low", "keywords": ""}

    weights = {"PRODUCT": 1.0, "TOOL": 0.9, "RESEARCH": 0.85, "INCIDENT": 1.15, "POLICY": 0.95, "BUSINESS": 0.8}
    score_by_type: dict[str, float] = {}
    incident_count = 0
    policy_count = 0
    research_count = 0

    for it in items:
        t = it.type
        score_by_type[t] = score_by_type.get(t, 0.0) + weights.get(t, 0.8)
        if t == "INCIDENT":
            incident_count += 1
        elif t == "POLICY":
            policy_count += 1
        elif t == "RESEARCH":
            research_count += 1

    focus = max(score_by_type, key=score_by_type.get)

    if incident_count >= 1:
        mood = "cautious"
    elif research_count >= 3 and research_count >= policy_count:
        mood = "breakthrough"
    elif focus in {"PRODUCT", "TOOL"}:
        mood = "practical"
    elif policy_count >= 2:
        mood = "controversial"
    else:
        mood = "practical"

    if len(items) >= 10 or incident_count >= 2 or (incident_count >= 1 and policy_count >= 1):
        intensity = "high"
    elif len(items) >= 6:
        intensity = "medium"
    else:
        intensity = "low"

    return {"focus": focus, "mood": mood, "intensity": intensity, "keywords": ""}

def _last_meta(history: list[dict]) -> dict[str, Any] | None:
    for m in reversed(history or []):
        if m.get("role") != "system":
            continue
        content = m.get("content") or ""
        if isinstance(content, str) and content.startswith(META_MARKER_PREFIX):
            raw = content[len(META_MARKER_PREFIX):].strip()
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                return None
    return None

def _pick_story(items: list[NewsItem], recent_posts: str, last_story_id: str | None) -> NewsItem | None:
    if not items:
        return None

    now_utc = datetime.now(timezone.utc)
    recent_tokens = _tokens(recent_posts or "")

    type_weight = {"INCIDENT": 1.00, "PRODUCT": 0.82, "TOOL": 0.78, "RESEARCH": 0.74, "POLICY": 0.70, "BUSINESS": 0.62, "MIXED": 0.60}

    def recency_bonus(dt: datetime | None) -> float:
        if not dt:
            return 0.04
        age_h = max(0.0, (now_utc - dt).total_seconds() / 3600.0)
        return max(0.0, 0.22 * (1.0 - min(age_h / 24.0, 1.0)))

    def novelty_penalty(title: str, what: str) -> float:
        t = _tokens(f"{title} {what}")
        if not t or not recent_tokens:
            return 0.0
        inter = len(t & recent_tokens)
        return min(0.30, 0.05 * inter)

    best: tuple[float, NewsItem] | None = None
    seen: set[str] = set()
    if last_story_id:
        seen.add(str(last_story_id))
    for it in items:
        if str(it.id) in seen:
            continue

        base = type_weight.get(it.type, 0.6)
        base += recency_bonus(it.published_at)
        if it.confidence == "high":
            base += 0.05
        elif it.confidence == "low":
            base -= 0.14
        base -= novelty_penalty(it.title, it.what)
        base += random.random() * 0.02

        if best is None or base > best[0]:
            best = (base, it)

    return best[1] if best else None

def _pick_story_candidates(
    items: list[NewsItem],
    recent_posts: str,
    exclude_ids: set[str] | None,
    limit: int = DEFAULT_STORY_ALTS,
) -> list[NewsItem]:

    if not items:
        return []
    limit = max(1, min(int(limit or DEFAULT_STORY_ALTS), 6))

    now_utc = datetime.now(timezone.utc)
    recent_tokens = _tokens(recent_posts or "")
    exclude = {str(x) for x in (exclude_ids or set()) if x is not None}

    type_weight = {
        "INCIDENT": 1.00,
        "PRODUCT": 0.82,
        "TOOL": 0.78,
        "RESEARCH": 0.74,
        "POLICY": 0.70,
        "BUSINESS": 0.62,
        "MIXED": 0.60,
    }

    def recency_bonus(dt: datetime | None) -> float:
        if not dt:
            return 0.04
        age_h = max(0.0, (now_utc - dt).total_seconds() / 3600.0)
        return max(0.0, 0.22 * (1.0 - min(age_h / 24.0, 1.0)))

    def novelty_penalty(title: str, what: str) -> float:
        t = _tokens(f"{title} {what}")
        if not t or not recent_tokens:
            return 0.0
        inter = len(t & recent_tokens)
        return min(0.30, 0.05 * inter)

    ranked: list[tuple[float, NewsItem]] = []
    for it in items:
        sid = str(it.id)
        if sid in exclude:
            continue

        s = type_weight.get(it.type, 0.6)
        s += recency_bonus(it.published_at)
        if it.confidence == "high":
            s += 0.05
        elif it.confidence == "low":
            s -= 0.14
        s -= novelty_penalty(it.title, it.what)
        s += random.random() * 0.01

        ranked.append((s, it))

    ranked.sort(key=lambda x: x[0], reverse=True)
    out: list[NewsItem] = []
    for _, it in ranked:
        out.append(it)
        if len(out) >= limit:
            break
    return out

def _recent_metas(history: list[dict], limit: int = 10) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in reversed(history or []):
        if m.get("role") != "system":
            continue
        content = m.get("content") or ""
        if not isinstance(content, str):
            continue
        if not content.startswith(META_MARKER_PREFIX):
            continue
        raw = content[len(META_MARKER_PREFIX):].strip()
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                out.append(obj)
        except Exception:
            continue
        if len(out) >= limit:
            break
    return out

def _rubric_for_context(
    time_bucket: str,
    mood: str,
    intensity: str,
    last_rubric: str | None,
    recent_rubrics: list[str] | None = None,
) -> str:
    tb = (time_bucket or "").lower()
    mood = (mood or "").lower()
    intensity = (intensity or "").lower()

    candidates: list[tuple[str, float]] = [
        ("news_explainer", 1.0),
        ("practical_takeaway", 1.15),
        ("anti_hype", 0.95),
        ("tool_tip", 0.85),
        ("risk_alert", 0.85),
        ("market_signal", 0.75),
        ("case_mini", 0.70),
        ("editor_note", 0.70),
        ("field_note", 0.65),
        ("myth_bust", 0.75),
        ("light_observation", 0.55),
    ]

    if last_rubric:
        candidates = [(k, w * (0.35 if k == last_rubric else 1.0)) for k, w in candidates]

    if recent_rubrics:
        freq: dict[str, int] = {}
        for r in recent_rubrics[:10]:
            if isinstance(r, str) and r in ALL_RUBRICS:
                freq[r] = freq.get(r, 0) + 1
        if freq:
            candidates = [
                (k, w * max(0.55, 1.0 - 0.14 * float(freq.get(k, 0))))
                for k, w in candidates
            ]

    if mood == "cautious":
        candidates = [(k, w + (0.45 if k in {"risk_alert", "news_explainer"} else 0.0)) for k, w in candidates]
        candidates = [(k, w * (0.75 if k in {"light_observation"} else 1.0)) for k, w in candidates]
    if mood == "breakthrough":
        candidates = [(k, w + (0.25 if k in {"news_explainer", "editor_note"} else 0.0)) for k, w in candidates]
    if intensity == "high":
        candidates = [(k, w + (0.20 if k in {"news_explainer", "risk_alert"} else 0.0)) for k, w in candidates]
    if tb == "evening":
        candidates = [(k, w + (0.15 if k in {"editor_note", "anti_hype", "field_note"} else 0.0)) for k, w in candidates]
    if tb == "morning":
        candidates = [(k, w + (0.20 if k in {"news_explainer", "practical_takeaway"} else 0.0)) for k, w in candidates]

    total = sum(w for _, w in candidates)
    r = random.random() * total
    acc = 0.0
    for key, w in candidates:
        acc += w
        if r <= acc:
            return key
    return "news_explainer"


def _pick_rubrics(
    time_bucket: str,
    mood: str,
    intensity: str,
    last_rubric: str | None,
    count: int,
    recent_rubrics: list[str] | None = None,
) -> list[str]:
    count = max(1, min(count, 3))
    first = _rubric_for_context(time_bucket, mood, intensity, last_rubric, recent_rubrics=recent_rubrics)
    if count == 1:
        return [first]

    second = _rubric_for_context(time_bucket, mood, intensity, first, recent_rubrics=recent_rubrics)
    if second == first:
        pool = [r for r in ALL_RUBRICS if r != first]
        second = random.choice(pool) if pool else first

    if count == 2:
        return [first, second]

    third = _rubric_for_context(time_bucket, mood, intensity, second, recent_rubrics=recent_rubrics)
    if third in {first, second}:
        pool = [r for r in ALL_RUBRICS if r not in {first, second}]
        third = random.choice(pool) if pool else second
    return [first, second, third]


def _format_story_for_prompt(story: NewsItem) -> str:
    dt = story.published_at.isoformat().replace("+00:00", "Z") if story.published_at else "неясно"
    url = story.url or ""
    return (
        f"TYPE: {story.type}\n"
        f"TITLE: {story.title}\n"
        f"WHAT: {story.what}\n"
        f"WHY: {story.why or '—'}\n"
        f"SOURCE: {story.source}\n"
        f"PUBLISHED_AT_UTC: {dt}\n"
        f"URL: {url}"
    )


def _clamp_text_len(text: str, limit: int = POST_CHAR_LIMIT) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = limit - len(TRIM_SUFFIX)
    return (text[:cut].rstrip() + TRIM_SUFFIX) if cut > 0 else TRIM_SUFFIX


def _strip_urls_if_disallowed(text: str, allow_url: bool) -> str:
    if allow_url:
        return text
    out = URL_RE.sub("", text)
    out = re.sub(r"\s{2,}", " ", out)
    return out.strip()


def _has_listish_format(text: str) -> bool:
    return bool(LIST_LINE_RE.search(text or ""))


def _has_emoji_or_hashtags(text: str) -> bool:
    return bool(EMOJI_RE.search(text or "")) or bool(HASHTAG_RE.search(text or ""))


def _sentence_count(text: str) -> int:
    s = (text or "").strip()
    if not s:
        return 0
    parts = [p.strip() for p in SENT_SPLIT_RE.split(s) if p.strip()]
    return len(parts)

def _split_sentences_keep(text: str) -> list[str]:
    s = (text or "").strip()
    if not s:
        return []
    out: list[str] = []
    start = 0
    for m in re.finditer(r"[.!?…](?:[)\]»”\"']*)?(?:\s+|$)", s):
        end = m.end()
        chunk = s[start:end].strip()
        if chunk:
            out.append(chunk)
        start = end
    tail = s[start:].strip()
    if tail:
        out.append(tail)
    return out

def _ensure_sentence_per_line(text: str) -> str:

    t = (text or "").strip()
    if not t:
        return t
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    flat = re.sub(r"\s+", " ", t.replace("\n", " ")).strip()
    sents = _split_sentences_keep(flat)
    return "\n".join(sents).strip()

def _sentences_on_new_lines_ok(text: str) -> bool:
    body = _strip_source_footer(text or "")
    body = body.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not body:
        return False
    lines = [ln.strip() for ln in body.split("\n") if ln.strip()]
    if any(_sentence_count(ln) != 1 for ln in lines):
        return False
    flat = re.sub(r"\s+", " ", body.replace("\n", " ")).strip()
    return len(lines) == len(_split_sentences_keep(flat))

def _ensure_two_paragraphs(text: str, limit: int = POST_CHAR_LIMIT) -> str:
    t = (text or "").strip()
    if not t:
        return t
    t = t.replace("\r\n", "\n").replace("\r", "\n")

    if "\n\n" in t:
        return _clamp_text_len(t, limit)

    if "\n" in t:
        t = re.sub(r"\n{1,}", "\n\n", t).strip()
        return _clamp_text_len(t, limit)

    sents = _split_sentences_keep(t)
    if len(sents) <= 1:
        return _clamp_text_len(t, limit)

    first_n = 1
    if len(sents[0]) < 140 and len(sents) >= 3:
        first_n = 2
    p1 = " ".join(sents[:first_n]).strip()
    p2 = " ".join(sents[first_n:]).strip()
    out = (p1 + "\n\n" + p2).strip()
    return _clamp_text_len(out, limit)

def _append_source_footer(
    text: str,
    story: NewsItem | None,
    allow_source: bool,
    allow_url: bool,
    limit: int = POST_CHAR_LIMIT,
) -> str:
    if not allow_source or not story:
        return text
    if SOURCE_LINE_RE.search(text or ""):
        return text

    footer = f"Источник: {story.source}"
    if allow_url and story.url:
        footer = f"{footer} {story.url}"
    footer = footer.strip()
    if not footer:
        return text

    glue = "\n\n"
    total_len = len(text) + len(glue) + len(footer)
    if total_len <= limit:
        return text + glue + footer

    room_for_body = limit - len(glue) - len(footer)
    if room_for_body <= 0:
        return _clamp_text_len(footer, limit)

    trimmed_body = _clamp_text_len(text, room_for_body).rstrip()
    return trimmed_body + glue + footer


def _compute_humor_target(time_bucket: str, mood: str, story: NewsItem | None, mods: dict) -> float:
    base = _coerce_float(getattr(settings, "TG_POST_HUMOR_RATE", None), DEFAULT_HUMOR_RATE)
    boost = _coerce_float(getattr(settings, "TG_POST_EVENING_HUMOR_BOOST", None), DEFAULT_EVENING_HUMOR_BOOST)
    maxv = _coerce_float(getattr(settings, "TG_POST_HUMOR_MAX", None), DEFAULT_HUMOR_MAX)

    tb = (time_bucket or "").lower()
    mood = (mood or "").lower()

    sarcasm = float(mods.get("sarcasm_mod", 0.30) or 0.30)
    valence = float(mods.get("valence_mod", 0.05) or 0.05)
    precision = float(mods.get("precision_mod", 0.80) or 0.80)

    x = base
    if tb == "evening":
        x += boost
    elif tb == "morning":
        x -= 0.05

    x += 0.20 * (sarcasm - 0.30)
    x += 0.08 * max(-0.5, min(0.5, valence))
    x -= 0.10 * max(0.0, precision - 0.75)

    if mood == "cautious":
        x *= 0.70
    if story and story.type == "INCIDENT":
        x *= 0.65

    try:
        if _get_local_now().weekday() in (5, 6):
            x += 0.08
    except Exception:
        pass

    return max(0.0, min(maxv, x))


def _build_bonnie_style_block(
    rubric: str,
    time_bucket_label: str,
    mood: str,
    intensity: str,
    char_limit: int,
    allow_source: bool,
    allow_url: bool,
    humor_on: bool,
    story_type: str | None,
) -> str:

    hook_styles = [
        "короткий парадокс (1 фраза)",
        "контраст «обещали vs получилось» (1 фраза)",
        "мини-сцена из практики (1 фраза, без персонажей по имени)",
        "осторожный риторический вопрос (1 фраза, без призывов)",
        "сухая тех-ирония (1 фраза, без мемов)",
    ]
    hook = random.choice(hook_styles)

    rubric_desc = {
        "news_explainer": "крючок → факт → почему важно → короткий вывод",
        "practical_takeaway": "факт → практический смысл → как проверить/применить → вывод",
        "anti_hype": "что обещают → что реально работает → граница/подвох → вывод",
        "tool_tip": "факт → инструмент/паттерн → где экономит время → вывод",
        "risk_alert": "факт → риск → как снизить риск (без призывов) → вывод",
        "market_signal": "факт → сигнал рынка → последствия → вывод",
        "case_mini": "боль → решение → эффект → нюанс/ограничение",
        "editor_note": "наблюдение → пример → точный парадокс → вывод",
        "field_note": "мини-сцена/наблюдение из практики → факт → вывод",
        "myth_bust": "миф → что на самом деле → как проверить → вывод",
        "light_observation": "короткая человеческая зарисовка → факт → вывод",
    }.get(rubric, "крючок → факт → смысл → вывод")

    source_line = ""
    if allow_source:
        source_line = "- источник оформит система после генерации, поэтому не вставляй «Источник: ...» сам(а);\n"
        if not allow_url:
            source_line += "- ссылки не вставляй;\n"

    humor_line = ""
    if humor_on:
        humor_line = (
            "- добавь ОДИН лёгкий юмористический штрих: сухая ирония или метафора (одна фраза), "
            "без издёвки и без мемов;\n"
        )
        if story_type == "INCIDENT":
            humor_line += "- если сюжет про инцидент/утечку: юмор только про процессы/системы, не про людей;\n"
    else:
        humor_line = "- тон спокойный, без попытки «шутить ради шутки»;\n"

    return (
        "\nТы — Bonnie, женская ИИ-персона проекта Synchatica. Ты ведёшь публичный телеграм-канал о применении "
        "и развитии ИИ-технологий.\n"
        "Тон: умная редакторка и практик. Главная ценность: превращать шум новостей в понятный смысл и практический вывод.\n\n"
        "Термины:\n"
        "- в тексте используй оригинальные названия компаний, терминологию и названия технологий (в международном формате: AI, RAG, Apple и т.д.);\n\n"
        f"Рубрика: {rubric} (структура: {rubric_desc}).\n"
        f"Контекст: mood={mood}, intensity={intensity}, слот={time_bucket_label}.\n\n"
        "Требования:\n"
        "- до 7 коротких предложений, которые просто читать;\n"
        "- каждое предложение - с новой строки;\n"
        "- один главный сюжет и целостное содержание;\n"
        "- факты можно брать только из story_block;\n"
        "- если деталей мало — прямо скажи, что деталей пока мало;\n"
        "- без списков, нумерации, маркеров;\n"
        "- без эмодзи и хэштегов;\n"
        "- без призывов к действиям, без продаж и инвестиционных советов;\n"
        f"- стартовый ход: {hook};\n"
        f"{humor_line}"
        f"{source_line}"
        f"- лимит: до {char_limit} символов.\n"
    )


def _post_fallbacks() -> list[str]:
    return [
        "Сегодня в ИИ-ленте тише — и это редкий момент, когда полезно заняться не «новой магией», а качеством данных и проверкой ответов. Кажется скучно, но именно это потом спасает прод. В итоге выигрывает не самая громкая модель, а самая дисциплинированная система.",
        "ИИ-индустрия любит обещать автопилот, а потом выясняется, что автопилоту нужен ремень безопасности и техосмотр. На практике всё упирается в доступы, логирование и оценку качества. Ирония в том, что это самое «не-вау», но без него вау заканчивается быстро.",
        "Когда очередной релиз называют «революцией», я машинально ищу две вещи: как мерили качество и где границы применения. Если ответ расплывчатый — значит, революция пока на слайдах. В проде побеждает не громкость, а проверяемость.",
    ]

def _story_fallback(story: NewsItem) -> str:

    def _first_sentence(s: str) -> str:
        s = " ".join((s or "").split()).strip()
        if not s:
            return ""
        m = SENT_END_RE.search(s)
        if m:
            return s[: m.end()].strip()
        return s

    def _clean(s: str) -> str:
        return " ".join((s or "").split()).strip()

    def _ensure_punct(s: str) -> str:
        s = _clean(s)
        if not s:
            return ""
        if s[-1] not in ".!?":
            s += "."
        return s

    parts: list[str] = []
    parts.append(_ensure_punct(_first_sentence(story.title)))
    parts.append(_ensure_punct(_first_sentence(story.what)))
    if story.why:
        parts.append(_ensure_punct(_first_sentence(story.why)))

    if story.confidence == "low":
        parts.append("Деталей пока немного, поэтому это скорее сигнал, чем готовая картина.")

    parts.append("В итоге ценность таких новостей обычно проявляется не в демо, а в границах применения и проверяемом эффекте.")

    out = " ".join(p for p in parts if p).strip()
    return _clamp_text_len(out, POST_CHAR_LIMIT)

async def _rewrite_post_without_calls(post_text: str, char_limit: int = POST_CHAR_LIMIT) -> str | None:
    model = getattr(settings, "POST_MODEL", None)
    if not model:
        return None

    system_prompt = (
        "Ты получаешь текст поста для телеграм-канала про индустрию ИИ.\n"
        "- Перепиши на русском в том же тоне, но убери прямые призывы к действиям "
        "(подписаться, лайкнуть, репостнуть, перейти по ссылке, купить, зарегистрироваться, инвестировать и т.п.).\n"
        "- Не добавляй новых фактов.\n"
        "- Верни только переписанный текст."
    )

    user_prompt = f"Исходный текст:\n{post_text}\n\nПерепиши по правилам."

    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=model,
                input=_to_responses_input(
                    [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]
                ),
                temperature=0.2,
                max_output_tokens=450,
                total_timeout=240,
            ),
            timeout=min(_coerce_float(getattr(settings, "POST_MODEL_TIMEOUT", None), 60.0), 60.0),
        )
    except Exception:
        logger.exception("tg_post_manager: rewrite without calls failed")
        return None

    new_text = (_get_output_text(resp) or "").strip()
    return _clamp_text_len(new_text, char_limit) if new_text else None


async def _judge_candidate_llm(candidate: str, story_block: str, rubric: str, recent_posts: str) -> dict[str, Any] | None:
    model = getattr(settings, "RESPONSE_MODEL", getattr(settings, "POST_MODEL", None))
    if not model:
        return None

    system_text = (
        "Ты — строгий редактор качества телеграм-постов про индустрию ИИ. "
        "Оценивай фактологию и качество текста. Никаких советов по продвижению."
    )

    user_text = (
        "Оцени кандидат на пост.\n\n"
        "Правила:\n"
        "- факты можно брать только из story_block;\n"
        "- если кандидат содержит утверждения, которых нет в story_block — это риск галлюцинации;\n"
        "- без списков, эмодзи, хэштегов, CTA.\n\n"
        f"RUBRIC: {rubric}\n\n"
        f"STORY_BLOCK:\n{story_block}\n\n"
        f"RECENT_POSTS:\n{recent_posts}\n\n"
        f"CANDIDATE:\n{candidate}\n\n"
        "Верни ТОЛЬКО JSON:\n"
        '{ "score": 0-100, "hallucination_risk": "low|medium|high", '
        '"has_cta": true|false, "has_list": true|false, "has_emoji_or_hashtags": true|false, '
        '"too_similar": true|false, "tone": "ok|too_dry|too_funny", '
        '"notes": ["короткие замечания"] }'
    )

    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=model,
                input=_to_responses_input([{"role": "system", "content": system_text}, {"role": "user", "content": user_text}]),
                temperature=0.1,
                max_output_tokens=280,
                total_timeout=240,
            ),
            timeout=min(_coerce_float(getattr(settings, "RESPONSE_MODEL_TIMEOUT", None), 15.0), 30.0),
        )
    except Exception:
        logger.exception("tg_post_manager _judge_candidate_llm failed")
        return None

    raw = (_get_output_text(resp) or "").strip()
    obj = _safe_json_extract(raw)
    return obj if isinstance(obj, dict) else None


def _heuristic_score_candidate(candidate: str, recent_posts: str, story: NewsItem | None, allow_url: bool) -> tuple[int, list[str]]:
    notes: list[str] = []
    text = (candidate or "").strip()
    if not text:
        return 0, ["пустой текст"]

    score = 60

    if _contains_direct_call_to_action(text):
        return 0, ["CTA/призыв к действию"]

    if _has_listish_format(text):
        score -= 25
        notes.append("похоже на список/маркеры")

    if _has_emoji_or_hashtags(text):
        score -= 25
        notes.append("эмодзи/хэштеги (не надо)")

    if not allow_url and URL_RE.search(text or ""):
        score -= 18
        notes.append("вставлена ссылка при запрете ссылок")

    sc = _sentence_count(text)
    if 3 <= sc <= 7:
        score += 10
    else:
        score -= 8
        notes.append(f"предложений={sc} (норма 3–7)")

    ln = len(text)
    if ln < 160:
        score -= 10
        notes.append("слишком коротко")
    elif 260 <= ln <= 560:
        score += 6
    elif ln > POST_CHAR_LIMIT:
        score -= 20
        notes.append("превышен лимит")

    practical_markers = ["на практике", "в итоге", "по факту", "провер", "оцен", "риск", "важно", "упирается", "поэтому"]
    if any(m in text.lower() for m in practical_markers):
        score += 6

    humor_markers = ["как будто", "похоже на", "впечатление", "ирония", "забавно", "смешно", "в реальности"]
    if any(m in text.lower() for m in humor_markers):
        score += 3

    cand_t = _tokens(text)
    recent_t = _tokens(recent_posts or "")
    if cand_t and recent_t:
        inter = len(cand_t & recent_t)
        union = len(cand_t | recent_t)
        jacc = inter / max(1, union)
        if jacc >= 0.22:
            score -= 18
            notes.append("слишком похоже на недавние посты")
        elif jacc >= 0.15:
            score -= 10
            notes.append("похоже на недавние посты")

    if story:
        story_t = _tokens(f"{story.title} {story.what}")
        if cand_t and story_t and len(cand_t & story_t) >= 2:
            score += 8
        elif story.confidence == "low":
            score -= 6
            notes.append("низкая уверенность в факте")

    return max(0, min(100, score)), notes


async def _polish_post(draft: str, story_block: str, rubric: str, char_limit: int) -> str | None:
    model = getattr(settings, "POST_MODEL", None)
    if not model:
        return None

    system_text = (
        "Ты — редактор, улучшающий телеграм-пост про индустрию ИИ. "
        "Нельзя добавлять новые факты: допускается только переформулировать и сделать текст лучше."
    )

    user_text = (
        "Улучши текст: сделай его более цепким, но спокойным и умным. "
        "Сохрани структуру рубрики. Оставь один главный факт из story_block. "
        "Если в story_block мало деталей — добавь короткую оговорку про недостаток деталей, без фантазии.\n\n"
        f"RUBRIC: {rubric}\n\n"
        f"STORY_BLOCK:\n{story_block}\n\n"
        f"DRAFT:\n{draft}\n\n"
        f"Ограничения: 3–7 предложений, без списков/эмодзи/хэштегов/CTA, до {char_limit} символов.\n"
        "Верни только улучшенный текст."
    )

    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=model,
                input=_to_responses_input([{"role": "system", "content": system_text}, {"role": "user", "content": user_text}]),
                temperature=0.25,
                max_output_tokens=520,
                total_timeout=240,
            ),
            timeout=min(_coerce_float(getattr(settings, "POST_MODEL_TIMEOUT", None), 60.0), 60.0),
        )
    except Exception:
        logger.exception("tg_post_manager _polish_post failed")
        return None

    out = (_get_output_text(resp) or "").strip()
    return _clamp_text_len(out, char_limit) if out else None

async def _send_telegram_with_retry(
    chat_id: int,
    text: str,
    *,
    image_bytes: bytes | None = None,
    parse_mode: str | None = None,
    disable_web_page_preview: bool = True,
) -> bool:
    bot = get_bot()
    attempt = 1
    while True:
        try:
            if image_bytes:
                try:
                    from aiogram.types import BufferedInputFile
                    ext = _guess_image_ext(image_bytes)
                    photo = BufferedInputFile(image_bytes, filename=f"post.{ext}")
                except Exception:
                    logger.exception("Telegram: cannot build photo file object (BufferedInputFile missing?)")
                    return False
                await bot.send_photo(chat_id, photo=photo, caption=text, parse_mode=parse_mode)
            else:
                await bot.send_message(
                    chat_id,
                    text,
                    disable_web_page_preview=disable_web_page_preview,
                    parse_mode=parse_mode,
                )
            return True
        except TelegramRetryAfter as e:
            delay = max(1, int(getattr(e, "retry_after", 5)))
            logger.warning("tg_post_manager TelegramRetryAfter, sleep %ss (attempt %d)", delay, attempt)
            await asyncio.sleep(delay)
            attempt += 1
        except TelegramBadRequest as e:
            logger.warning("tg_post_manager TelegramBadRequest: %s", e)
            return False
        except Exception as e:
            if attempt >= 3:
                logger.exception("tg_post_manager send_message failed after %d attempts: %s", attempt, e)
                return False
            await asyncio.sleep(1.5 * attempt)
            attempt += 1

def _build_image_prompt(story: NewsItem | None, post_text: str, mood: str) -> str:

    base = (
        "Create a vivid, high-quality illustration for a Telegram post about AI industry news. "
        "No text, no captions, no watermarks, no logos. "
        "Cinematic lighting, rich colors, high detail, editorial style."
    )
    mood_line = ""
    m = (mood or "").lower()
    if m == "cautious":
        mood_line = " Slightly tense but professional tone, focus on security/process."
    elif m == "breakthrough":
        mood_line = " Optimistic, innovative tone, futuristic but realistic."
    elif m == "controversial":
        mood_line = " Neutral analytical tone, regulatory/market vibe."

    if story:
        subject = f" Topic: {story.title}. Key idea: {story.what}."
    else:
        subject = " Topic: practical AI engineering (quality evaluation, safety, data, monitoring)."

    return f"{base}{mood_line}{subject}"

async def _build_image_prompt_grounded(story: NewsItem | None, post_text: str, mood: str) -> str:

    if not story:
        return _build_image_prompt(story, post_text, mood)

    model = getattr(settings, "RESPONSE_MODEL", None) or getattr(settings, "POST_MODEL", None)
    if not model:
        return _build_image_prompt(story, post_text, mood)

    story_block = _format_story_for_prompt(story)
    sys = (
        "You create concise visual briefs for illustrations. "
        "Never include any text/captions/logos/watermarks. "
        "Avoid brand names and recognizable trademarks."
    )
    usr = textwrap.dedent(f"""
    Use ONLY the facts below. Describe ONE illustration concept (1–2 English sentences):
    - what objects should be visible (concrete nouns)
    - what environment (office/lab/server room/regulatory hearing/etc.)
    - mood (matching: {mood})
    Constraints: no text, no UI text, no logos, no watermarks, no brand names.

    FACTS (story_block):
    {story_block}
    """).strip()

    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=model,
                input=_to_responses_input(
                    [{"role": "system", "content": sys}, {"role": "user", "content": usr}]
                ),
                max_output_tokens=120,
                temperature=0.2,
                total_timeout=240,
            ),
            timeout=25,
        )
        brief = (_get_output_text(resp) or "").strip()
        if not brief:
            return _build_image_prompt(story, post_text, mood)
    except Exception:
        logger.debug("tg_post_manager: visual brief build failed; fallback prompt", exc_info=True)
        return _build_image_prompt(story, post_text, mood)

    base = (
        "Create a vivid, high-quality illustration for a Telegram post about AI industry news. "
        "No text, no captions, no watermarks, no logos. "
        "Cinematic lighting, high detail, editorial style. "
    )
    return base + "Scene: " + brief

def _extract_image_b64(resp: Any) -> str | None:

    def _norm_b64(s: str) -> str:
        s = (s or "").strip()
        if not s:
            return ""
        if s.startswith("data:") and "base64," in s:
            s = s.split("base64,", 1)[1].strip()
        return s

    if resp is None:
        return None

    try:
        out = getattr(resp, "output", None)
        if isinstance(out, list):
            for item in out:
                itype = getattr(item, "type", None)
                if itype == "image_generation_call":
                    res = getattr(item, "result", None)
                    if isinstance(res, str):
                        b = _norm_b64(res)
                        if b:
                            return b
                    if isinstance(res, dict):
                        b = res.get("b64_json") or res.get("image_base64") or res.get("b64")
                        if isinstance(b, str):
                            b = _norm_b64(b)
                            if b:
                                return b
    except Exception:
        pass

    try:
        data = getattr(resp, "data", None)
        if isinstance(data, list) and data:
            first = data[0]
            b64 = getattr(first, "b64_json", None) or getattr(first, "b64", None)
            if isinstance(b64, str) and b64.strip():
                return b64.strip()
    except Exception:
        pass

    obj = resp
    if not isinstance(obj, dict):
        try:
            obj = getattr(resp, "__dict__", None) or resp
        except Exception:
            obj = resp
    if isinstance(obj, dict):
        data = obj.get("data")
        if isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, dict):
                b64 = first.get("b64_json") or first.get("b64")
                if isinstance(b64, str) and b64.strip():
                    b = _norm_b64(b64)
                    return b or None
        out = obj.get("output")
        if isinstance(out, list):
            for item in out:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "image_generation_call":
                    res = item.get("result")
                    if isinstance(res, str):
                        b = _norm_b64(res)
                        if b:
                            return b
                    if isinstance(res, dict):
                        b = res.get("b64_json") or res.get("image_base64") or res.get("b64")
                        if isinstance(b, str):
                            b = _norm_b64(b)
                            if b:
                                return b
                b64 = item.get("b64_json") or item.get("image_base64") or item.get("b64")
                if isinstance(b64, str) and b64.strip():
                    b = _norm_b64(b64)
                    if b:
                        return b
                content = item.get("content")
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict):
                            b = c.get("b64_json") or c.get("image_base64") or c.get("b64")
                            if isinstance(b, str) and b.strip():
                                bb = _norm_b64(b)
                                if bb:
                                    return bb
    return None

def _extract_image_url(resp: Any) -> str | None:
    if resp is None:
        return None

    try:
        data = getattr(resp, "data", None)
        if isinstance(data, list) and data:
            first = data[0]
            url = getattr(first, "url", None)
            if isinstance(url, str) and url.strip():
                return url.strip()
    except Exception:
        pass

    obj = resp
    if not isinstance(obj, dict):
        try:
            obj = getattr(resp, "__dict__", None) or resp
        except Exception:
            obj = resp
    if isinstance(obj, dict):
        data = obj.get("data")
        if isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, dict):
                url = first.get("url")
                if isinstance(url, str) and url.strip():
                    return url.strip()
    return None

def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = binascii.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack("!I", len(data)) + chunk_type + data + struct.pack("!I", crc)

def _builtin_fallback_png_bytes(width: int = 768, height: int = 768) -> bytes:

    width = max(256, min(int(width), 1024))
    height = max(256, min(int(height), 1024))

    r_line = [int(x * 255 / max(1, width - 1)) for x in range(width)]
    b_const = 48

    raw_rows: list[bytes] = []
    for y in range(height):
        g = int(y * 255 / max(1, height - 1))
        row = bytearray(1 + 3 * width)
        row[0] = 0
        off = 1
        for x in range(width):
            row[off] = r_line[x]
            row[off + 1] = g
            row[off + 2] = b_const
            off += 3
        raw_rows.append(bytes(row))

    raw = b"".join(raw_rows)
    comp = zlib.compress(raw, level=6)

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit, RGB
    return (
        signature
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", comp)
        + _png_chunk(b"IEND", b"")
    )

def _load_fallback_image_bytes() -> bytes | None:
    raw = getattr(settings, "TG_POST_FALLBACK_IMAGE_PATH", None) or DEFAULT_FALLBACK_IMAGE_PATH
    raw = str(raw).strip()

    here = Path(__file__).resolve()
    try:
        project_root = here.parents[3]
    except Exception:
        project_root = here.parents[2]

    candidates: list[Path] = []
    p = Path(raw)
    if p.is_absolute():
        candidates.append(p)
    else:
        candidates.append((project_root / p).resolve())
        candidates.append((Path.cwd() / p).resolve())
        try:
            parts = list(p.parts)
            if parts and parts[0] == "app":
                candidates.append((project_root / Path(*parts[1:])).resolve())
        except Exception:
            pass
        candidates.append((project_root / "assets" / p.name).resolve())

    seen: set[str] = set()
    for cand in candidates:
        key = str(cand)
        if key in seen:
            continue
        seen.add(key)
        try:
            if cand.exists() and cand.is_file():
                b = cand.read_bytes()
                if b:
                    return b
        except Exception:
            continue

    logger.info("tg_post_manager: fallback image not found/readable; tried=%s", [str(x) for x in candidates])
    logger.warning("tg_post_manager: fallback image not found on disk; using built-in fallback PNG")
    return _builtin_fallback_png_bytes(width=1024, height=1024)

async def _generate_post_image_bytes(redis, channel_id: int, story: NewsItem | None, post_text: str, mood: str) -> bytes | None:
    enabled = _coerce_bool(getattr(settings, "TG_POST_IMAGE_ENABLED", None), DEFAULT_IMAGE_ENABLED)
    if not enabled:
        return None

    disabled_reason = await _get_image_disabled_reason(redis, channel_id)
    if disabled_reason:
        logger.warning("tg_post_manager: image disabled via redis marker: %r", disabled_reason)
        return None

    prob = _coerce_float(getattr(settings, "TG_POST_IMAGE_PROB", None), DEFAULT_IMAGE_PROB)
    if prob < 1.0 and random.random() > max(0.0, min(1.0, prob)):
        return None

    model = getattr(settings, "TG_POST_IMAGE_MODEL", None) or DEFAULT_IMAGE_MODEL
    size = getattr(settings, "TG_POST_IMAGE_SIZE", None) or DEFAULT_IMAGE_SIZE
    timeout_s = _coerce_float(getattr(settings, "TG_POST_IMAGE_TIMEOUT_SEC", None), DEFAULT_IMAGE_TIMEOUT_SEC)

    quality = (getattr(settings, "TG_POST_IMAGE_QUALITY", None) or DEFAULT_IMAGE_QUALITY)
    quality = str(quality).strip().lower()
    if quality not in {"low", "medium", "high", "auto"}:
        quality = DEFAULT_IMAGE_QUALITY

    fmt = (getattr(settings, "TG_POST_IMAGE_FORMAT", None) or DEFAULT_IMAGE_FORMAT)
    fmt = str(fmt).strip().lower()
    if fmt not in {"png", "jpeg", "webp"}:
        fmt = DEFAULT_IMAGE_FORMAT

    if fmt == "webp":
        fmt = "png"

    background = (getattr(settings, "TG_POST_IMAGE_BACKGROUND", None) or DEFAULT_IMAGE_BACKGROUND)
    background = str(background).strip().lower()
    if background not in {"auto", "transparent", "opaque"}:
        background = DEFAULT_IMAGE_BACKGROUND

    compression = _coerce_int(getattr(settings, "TG_POST_IMAGE_COMPRESSION", None), DEFAULT_IMAGE_COMPRESSION)
    compression = max(0, min(100, int(compression)))

    if background == "transparent" and fmt == "jpeg":
        fmt = "png"

    prompt = await _build_image_prompt_grounded(story, post_text, mood)

    try:
        kwargs = dict(
            endpoint="images.generate",
            model=model,
            prompt=prompt,
            size=size,
            quality=quality,
            background=background,
            output_format=fmt,
            total_timeout=240,
        )
        if fmt in {"jpeg", "webp"}:
            kwargs["output_compression"] = compression
        resp = await asyncio.wait_for(
            _call_openai_with_retry(**kwargs),
            timeout=timeout_s,
        )
        b64 = _extract_image_b64(resp)
        if b64:
            return base64.b64decode(b64)
        url = _extract_image_url(resp)
        if url:
            b = await _download_bytes(url, timeout=20.0)
            if b:
                return b
    except Exception as e:
        if _looks_like_org_verification_error(e):
            await _mark_image_disabled(redis, channel_id, "org_verification_required")
            logger.warning("tg_post_manager: image generation disabled (org verification required)")
            return None
        logger.info("tg_post_manager: images.generate failed, fallback to responses image tool", exc_info=True)

    tool_model = getattr(settings, "RESPONSE_MODEL", None) or getattr(settings, "POST_MODEL", None)
    if not tool_model:
        return None

    try:
        tool_def: dict[str, Any] = {"type": "image_generation", "background": background, "quality": quality, "size": size, "output_format": fmt}
        if fmt in {"jpeg", "webp"}:
            tool_def["output_compression"] = compression
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=tool_model,
                input=_to_responses_input(
                    [
                        {"role": "system", "content": "Generate one image. No text, no logos, no watermarks."},
                        {"role": "user", "content": prompt},
                    ]
                ),
                tools=[tool_def],
                tool_choice="required",
                max_output_tokens=600,
                total_timeout=240,
            ),
            timeout=timeout_s,
        )
        b64 = _extract_image_b64(resp)
        if b64:
            return base64.b64decode(b64)
    except Exception as e:
        if _looks_like_org_verification_error(e):
            await _mark_image_disabled(redis, channel_id, "org_verification_required")
            logger.warning("tg_post_manager: responses image tool disabled (org verification required)")
            return None
        logger.info("tg_post_manager: responses image tool failed", exc_info=True)

    return None

async def generate_and_post_tg() -> None:
    persona_chat_id = getattr(settings, "TG_PERSONA_CHAT_ID", None)
    if not persona_chat_id:
        logger.warning("tg_post_manager persona_chat_id is not configured; skip")
        return

    raw_channel_id = getattr(settings, "TG_CHANNEL_ID", None)
    if not raw_channel_id:
        logger.warning("tg_post_manager TG_CHANNEL_ID is not configured; skip")
        return

    try:
        channel_id = int(raw_channel_id)
    except (TypeError, ValueError):
        logger.warning("tg_post_manager invalid channel id value %r; skip", raw_channel_id)
        return

    post_model = getattr(settings, "POST_MODEL", None)
    if not post_model:
        logger.warning("tg_post_manager POST_MODEL is not configured; skip")
        return

    post_timeout = _coerce_float(getattr(settings, "POST_MODEL_TIMEOUT", None), 60.0)

    time_bucket, time_bucket_label = _get_time_bucket()
    start_hour = _coerce_int(getattr(settings, "SCHED_TG_START_HOUR", None), 8)
    end_hour = _coerce_int(getattr(settings, "SCHED_TG_END_HOUR", None), 23)

    if time_bucket == "night":
        logger.info(
            "tg_post_manager skip posting – outside corridor %02d:00–%02d:00 (bucket=%s)",
            start_hour,
            end_hour,
            time_bucket,
        )
        return

    redis = get_redis()
    if not redis:
        logger.info("tg_post_manager skip: redis lock unavailable (redis not configured)")
        return
    lock = await _acquire_redis_lock(redis, channel_id)
    if not lock:
        try:
            await redis.ping()
        except Exception:
            logger.info("tg_post_manager skip: redis lock unavailable (redis error)", exc_info=True)
            return
        logger.info("tg_post_manager skip: redis lock busy (channel=%s)", channel_id)
        return
    lock_key, lock_token = lock

    persona = await get_persona(persona_chat_id)
    try:
        await asyncio.wait_for(persona._restored_evt.wait(), timeout=5.0)
    except Exception:
        logger.exception("tg_post_manager persona restore failed")

    try:
        history = await load_context(persona_chat_id, persona_chat_id)
    except Exception:
        logger.exception("tg_post_manager load_context failed")
        history = []

    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]

    recent_posts = _extract_recent_posts(history, limit=RECENT_POSTS_FOR_CONTEXT)
    meta = _last_meta(history) or {}
    last_rubric = meta.get("rubric")
    last_story_id = meta.get("story_id")

    metas = _recent_metas(history, limit=10)
    recent_rubrics: list[str] = []
    recent_story_ids: set[str] = set()
    if metas:
        for m in metas:
            r = m.get("rubric")
            if isinstance(r, str) and r in ALL_RUBRICS:
                recent_rubrics.append(r)
            sid = m.get("story_id")
            if sid:
                recent_story_ids.add(str(sid))
        if not last_rubric and recent_rubrics:
            last_rubric = recent_rubrics[0]
        if not last_story_id and recent_story_ids:
            last_story_id = next(iter(recent_story_ids))

    try:
        style_mods = persona._mods_cache or await asyncio.wait_for(persona.style_modifiers(), 30)
    except Exception:
        logger.exception("tg_post_manager style_modifiers acquisition failed")
        style_mods = {}
    mods = _merge_and_clamp_mods(style_mods)

    try:
        guidelines = await persona.style_guidelines(persona_chat_id)
    except Exception:
        logger.exception("tg_post_manager style_guidelines acquisition failed")
        guidelines = []

    novelty = 0.35 * mods["creativity_mod"] + 0.20 * mods["sarcasm_mod"] + 0.45 * mods["enthusiasm_mod"]
    coherence = (
        0.55 * mods["precision_mod"]
        + 0.25 * mods["confidence_mod"]
        + 0.10 * (1 - mods["fatigue_mod"])
        + 0.10 * (1 - mods["stress_mod"])
    )

    alpha = 1.6
    dynamic_temperature = MIN_TEMPERATURE + (MAX_TEMPERATURE - MIN_TEMPERATURE) * (novelty**alpha)
    dynamic_top_p = TOP_P_MIN + (TOP_P_MAX - TOP_P_MIN) * (1.0 - coherence)

    try:
        dynamic_temperature *= 1.0 + 0.08 * float(mods["valence_mod"])
    except Exception:
        pass

    if time_bucket == "morning":
        dynamic_temperature = max(0.52, min(dynamic_temperature + 0.02, 0.76))

    dynamic_temperature = max(0.50, min(dynamic_temperature, 0.80))
    dynamic_top_p = max(0.85, min(dynamic_top_p, 0.98))

    include_source = _coerce_bool(getattr(settings, "TG_POST_INCLUDE_SOURCE", None), DEFAULT_INCLUDE_SOURCE)
    include_url = _coerce_bool(getattr(settings, "TG_POST_INCLUDE_URL", None), DEFAULT_INCLUDE_URL)

    candidate_count = _coerce_int(getattr(settings, "TG_POST_CANDIDATES", None), DEFAULT_CANDIDATE_COUNT)
    eval_enabled = _coerce_bool(getattr(settings, "TG_POST_EVAL_ENABLED", None), DEFAULT_EVAL_ENABLED)
    polish_enabled = _coerce_bool(getattr(settings, "TG_POST_POLISH_ENABLED", None), DEFAULT_POLISH_ENABLED)

    rubrics_per_run = _coerce_int(getattr(settings, "TG_POST_RUBRICS_PER_RUN", None), DEFAULT_RUBRICS_PER_RUN)
    rubrics_per_run = max(1, min(3, rubrics_per_run))

    local_now = _get_local_now()

    min_posts = int(getattr(settings, "SCHED_TG_MIN_POSTS", 15) or 15)
    max_posts = int(getattr(settings, "SCHED_TG_MAX_POSTS", 21) or 21)
    max_posts = max(min_posts, max_posts)

    count_today, last_ts = await _get_daily_pacing_state(redis, channel_id, local_now)
    duration_min = _corridor_duration_minutes(int(start_hour), int(end_hour))
    elapsed_min = _corridor_elapsed_minutes(local_now, int(start_hour), int(end_hour)) if duration_min else 0
    remaining_min = max(0, duration_min - elapsed_min)

    items = await _fetch_ai_news_digest(local_now)
    ctx = _analyze_ai_day(items)
    keywords = await _summarize_keywords(items) if items else ""
    ctx["keywords"] = keywords or ctx.get("keywords") or ""

    focus = ctx.get("focus", "MIXED")
    mood = ctx.get("mood", "quiet")
    intensity = ctx.get("intensity", "low")
    kw = ctx.get("keywords", "")

    if not items:
        mood = "quiet"
        intensity = "low"
        kw = kw or "спокойный день, качество, данные, оценка"

    try:
        if intensity == "high":
            target_posts = max_posts
        elif intensity == "low":
            target_posts = min_posts
        else:
            target_posts = int(round((min_posts + max_posts) / 2.0))
        target_posts = max(1, int(target_posts))
    except Exception:
        target_posts = max(1, int(round((min_posts + max_posts) / 2.0)))

    if count_today >= max_posts:
        logger.info(
            "tg_post_manager skip: daily cap reached (%d/%d)", count_today, max_posts
        )
        await _release_redis_lock(redis, lock_key, lock_token)
        return

    if duration_min > 0:
        desired_gap = float(duration_min) / float(max(1, target_posts))
    else:
        desired_gap = 60.0

    gap_floor = _coerce_int(getattr(settings, "TG_POST_MIN_GAP_MINUTES_FLOOR", None), DEFAULT_MIN_GAP_MINUTES_FLOOR)
    gap_cap = _coerce_int(getattr(settings, "TG_POST_MAX_GAP_MINUTES_CAP", None), DEFAULT_MAX_GAP_MINUTES_CAP)
    min_gap_min = max(gap_floor, int(desired_gap * 0.60))
    max_gap_min = min(gap_cap, max(min_gap_min + 10, int(desired_gap * 1.45)))

    last_age_min = None
    if last_ts:
        try:
            last_age_min = max(0.0, (time.time() - float(last_ts)) / 60.0)
        except Exception:
            last_age_min = None

    must_catch_up = False

    if last_age_min is not None and last_age_min > float(max_gap_min):
        must_catch_up = True

    if duration_min > 0 and target_posts > 1:
        expected_now = int(round((float(elapsed_min) / float(duration_min)) * float(target_posts)))
        if count_today + 1 < expected_now:
            must_catch_up = True

    if last_age_min is not None and last_age_min < float(min_gap_min):
        need_left = max(0, int(target_posts) - int(count_today))
        tight = remaining_min < int(need_left * min_gap_min * 1.20)
        if not (must_catch_up and tight):
            logger.info(
                "tg_post_manager skip: min gap not met (age=%.1fmin < %dmin, catch_up=%s)",
                last_age_min, min_gap_min, must_catch_up
            )
            await _release_redis_lock(redis, lock_key, lock_token)
            return

    story_alts = int(getattr(settings, "TG_POST_STORY_ALTS", DEFAULT_STORY_ALTS) or DEFAULT_STORY_ALTS)
    story_alts = max(1, min(6, story_alts))

    story_candidates: list[NewsItem] = []
    if items:
        exclude_ids = set(recent_story_ids)
        if last_story_id:
            exclude_ids.add(str(last_story_id))
        story_candidates = _pick_story_candidates(items, recent_posts, exclude_ids, limit=story_alts)
        if not story_candidates:
            st = _pick_story(items, recent_posts, last_story_id)
            if st:
                story_candidates = [st]

    story = (story_candidates[0] if story_candidates else None) if items else None

    story_text_for_pacing = ""
    if story:
        story_text_for_pacing = f"{story.type} {story.title} {story.what} {story.why}"

    if (not must_catch_up) and (not _should_post_now(time_bucket, mood, intensity, kw, story_text_for_pacing, mods)):
        logger.info(
            "tg_post_manager skip posting by contextual pacing "
            "(bucket=%s focus=%s mood=%s intensity=%s keywords=%r)",
            time_bucket,
            focus,
            mood,
            intensity,
            kw,
        )
        await _release_redis_lock(redis, lock_key, lock_token)
        return

    if intensity == "high":
        dynamic_temperature = min(dynamic_temperature + 0.03, 0.80)
    elif intensity == "low":
        dynamic_temperature = max(dynamic_temperature - 0.03, 0.50)

    planned_base = _planned_rubrics_for_today(local_now)

    rubrics: list[str] = []

    try:
        system_base = await build_system_prompt(persona, guidelines, user_gender=None)
    except Exception:
        logger.exception("tg_post_manager build_system_prompt failed")
        system_base = "Ты — Bonnie. Пиши по-русски, коротко и по делу."

    history_block = ""
    if recent_posts:
        history_block = (
            "Краткая хроника последних постов (для уникальности формулировок):\n"
            f"{recent_posts}\n\n"
            "Не повторяй те же заходы и обороты.\n\n"
        )

    focus_label = {
        "PRODUCT": "релизы и продуктовые обновления",
        "TOOL": "инструменты и практики",
        "RESEARCH": "исследования и результаты",
        "INCIDENT": "инциденты и безопасность",
        "POLICY": "регуляторика и нормы",
        "BUSINESS": "рынок и компании",
        "MIXED": "смешанная повестка",
    }.get(focus, "повестка дня")

    if story:
        story_block = (
            "story_block (единственный источник фактов):\n"
            f"{_format_story_for_prompt(story)}\n\n"
        )
    else:
        story_block = (
            "story_block: сегодня нет явного главного сюжета. "
            "Пиши evergreen-заметку про практику внедрения ИИ (качество, оценка, безопасность, данные), "
            "без новых фактов и без упоминаний «сегодня в новостях».\n\n"
        )

    attempts: list[NewsItem | None]
    if items:
        attempts = list(story_candidates) if story_candidates else [story]
    else:
        attempts = [None]

    last_fail_reason = ""
    for i_try, story in enumerate(attempts, start=1):
        _ = await _get_image_disabled_reason(redis, channel_id)

        planned = _context_override_rubrics(list(planned_base), mood, story)

        rubrics = []
        for r in planned:
            if r not in rubrics:
                rubrics.append(r)
        while len(rubrics) < rubrics_per_run:
            r = _rubric_for_context(
                time_bucket,
                mood,
                intensity,
                (rubrics[-1] if rubrics else last_rubric),
                recent_rubrics=recent_rubrics,
            )
            if r not in rubrics:
                rubrics.append(r)

        if story:
            story_block = (
                "story_block (единственный источник фактов):\n"
                f"{_format_story_for_prompt(story)}\n\n"
            )
        else:
            story_block = (
                "story_block: сегодня нет явного главного сюжета. "
                "Пиши evergreen-заметку про практику внедрения ИИ (качество, оценка, безопасность, данные), "
                "без новых фактов и без упоминаний «сегодня в новостях».\n\n"
            )

        user_prompt = (
            history_block
            + story_block
            + "Задача: напиши один пост для публичного телеграм-канала.\n"
            "Требования:\n"
            "- начни сразу с мысли, без приветствий;\n"
            "- один главный сюжет;\n"
            "- 3–7 предложений;\n"
            "- встрой 1 конкретный факт из story_block (если story_block содержит новость);\n"
            "- затем: что это значит и практический вывод;\n"
            "- без списков/нумераций/эмодзи/хэштегов/CTA;\n"
            f"Контекст дня: {focus_label}; темы: {kw or '—'}.\n"
            "Верни только готовый текст поста.\n"
        )

        include_url_local = include_url
        humor_target = _compute_humor_target(time_bucket, mood, story, mods)
        humor_max = float(getattr(settings, "TG_POST_HUMOR_MAX", DEFAULT_HUMOR_MAX) or DEFAULT_HUMOR_MAX)

        total_candidates = max(1, int(candidate_count))
        counts = _allocate_counts(total_candidates, len(rubrics))

        generated: list[tuple[str, str]] = []  # (text, rubric_used)
        for idx_r, rubric in enumerate(rubrics):
            k = counts[idx_r]
            if k <= 0:
                continue

            p_humor = float(humor_target)
            if rubric in HUMOR_FRIENDLY_RUBRICS:
                p_humor = min(humor_max, p_humor + 0.12)
            if mood in {"controversial"} or (story and story.type == "POLICY"):
                p_humor *= 0.85
            humor_on = random.random() < max(0.0, min(humor_max, p_humor))

            style_block = _build_bonnie_style_block(
                rubric=rubric,
                time_bucket_label=time_bucket_label,
                mood=mood,
                intensity=intensity,
                char_limit=POST_CHAR_LIMIT,
                allow_source=include_source,
                allow_url=include_url_local,
                humor_on=humor_on,
                story_type=(story.type if story else None),
            )
            system_msg = {"role": "system", "content": system_base + style_block}
            messages = [system_msg, {"role": "user", "content": user_prompt}]

            for _ in range(k):
                t = max(0.50, min(0.80, dynamic_temperature + random.uniform(-0.03, 0.03)))
                p = max(0.85, min(0.98, dynamic_top_p + random.uniform(-0.02, 0.02)))
                try:
                    resp = await asyncio.wait_for(
                        _call_openai_with_retry(
                            endpoint="responses.create",
                            model=post_model,
                            input=_to_responses_input(messages),
                            temperature=t,
                            top_p=p,
                            max_output_tokens=560,
                            total_timeout=240,
                        ),
                        timeout=post_timeout,
                    )
                    txt = (_get_output_text(resp) or "").strip()
                except Exception:
                    logger.exception("tg_post_manager candidate generation failed rubric=%s", rubric)
                    continue

                if not txt:
                    continue
                txt = _strip_forbidden_openers(txt)
                txt = _strip_source_footer(txt)
                txt = _strip_urls_if_disallowed(txt, include_url_local)
                txt = _clamp_text_len(txt, POST_CHAR_LIMIT)
                generated.append((txt, rubric))

        post_text: str
        chosen_rubric: str = rubrics[0] if rubrics else "news_explainer"

        if not generated:
            post_text = _story_fallback(story) if story else random.choice(_post_fallbacks())
            post_text = _clamp_text_len(post_text, POST_CHAR_LIMIT)
            post_text = _ensure_sentence_per_line(post_text)
            post_text = _clamp_text_len(post_text, POST_CHAR_LIMIT)
        else:
            scored: list[tuple[float, str, str, dict[str, Any] | None, list[str]]] = []
            for cand, rubric_used in generated:
                h_score, h_notes = _heuristic_score_candidate(cand, recent_posts, story, include_url_local)

                llm_judge = None
                llm_score = None
                if eval_enabled:
                    llm_judge = await _judge_candidate_llm(cand, story_block, rubric_used, recent_posts)
                    if isinstance(llm_judge, dict) and isinstance(llm_judge.get("score"), (int, float)):
                        llm_score = float(llm_judge["score"])
                        if llm_judge.get("has_cta") is True:
                            llm_score = 0.0
                        risk = (llm_judge.get("hallucination_risk") or "").lower()
                        if risk == "high":
                            llm_score -= 25.0
                        elif risk == "medium":
                            llm_score -= 8.0
                        if llm_judge.get("has_list") is True:
                            llm_score -= 15.0
                        if llm_judge.get("has_emoji_or_hashtags") is True:
                            llm_score -= 15.0
                        if llm_judge.get("too_similar") is True:
                            llm_score -= 12.0

                if llm_score is None:
                    final = float(h_score)
                else:
                    final = 0.45 * float(h_score) + 0.55 * llm_score
                    tone = (llm_judge or {}).get("tone")
                    if tone == "too_dry":
                        final -= 3.0
                    elif tone == "too_funny":
                        final -= 6.0

                scored.append((final, cand, rubric_used, llm_judge, h_notes))

            scored.sort(key=lambda x: x[0], reverse=True)
            best_row = None
            for row in scored:
                _, cand, _, _, _ = row
                if _passes_hard_constraints(cand, include_url_local, story):
                    best_row = row
                    break

            if best_row is None:
                post_text = _story_fallback(story) if story else random.choice(_post_fallbacks())
                post_text = _strip_source_footer(post_text)
                post_text = _strip_forbidden_openers(post_text)
                post_text = _strip_urls_if_disallowed(post_text, include_url_local)
                post_text = _clamp_text_len(post_text, POST_CHAR_LIMIT)
                chosen_rubric = rubrics[0] if rubrics else "news_explainer"
                best_score = 0.0
                best_judge = {}
                best_h_notes = ["все кандидаты провалили hard-constraints"]
            else:
                best_score, post_text, chosen_rubric, best_judge, best_h_notes = best_row

            logger.info(
                "tg_post_manager candidate selection: best_score=%.1f rubric=%s story=%s judge=%s h_notes=%s",
                best_score,
                chosen_rubric,
                (story.id if story else None),
                (best_judge or {}),
                best_h_notes,
            )

            if (not eval_enabled) and polish_enabled:
                best_score = best_score or 0.0
                post_text = post_text or ""
                forced = await _polish_post(post_text, story_block, chosen_rubric, POST_CHAR_LIMIT)
                if forced and _passes_hard_constraints(forced, include_url_local, story):
                    post_text = forced
            elif polish_enabled and best_score < 84:
                polished = await _polish_post(post_text, story_block, chosen_rubric, POST_CHAR_LIMIT)
                if polished:
                    polished = _strip_forbidden_openers(polished)
                    polished = _strip_source_footer(polished)
                    polished = _strip_urls_if_disallowed(polished, include_url_local)
                    polished = _clamp_text_len(polished, POST_CHAR_LIMIT)
                    if _passes_hard_constraints(polished, include_url_local, story):
                        post_text = polished

        post_text = _strip_forbidden_openers(post_text)
        post_text = _strip_source_footer(post_text)
        post_text = _strip_urls_if_disallowed(post_text, include_url_local)
        post_text = _clamp_text_len(post_text, POST_CHAR_LIMIT)

        if _contains_direct_call_to_action(post_text):
            rewritten = await _rewrite_post_without_calls(post_text, POST_CHAR_LIMIT)
            if rewritten and not _contains_direct_call_to_action(rewritten):
                post_text = rewritten
            else:
                logger.warning("tg_post_manager: failed to rewrite CTA out, skip sending")
                await _release_redis_lock(redis, lock_key, lock_token)
                return

        if not _passes_hard_constraints(post_text, include_url_local, story):
            logger.warning("tg_post_manager: hard-constraints violation after cleanup; fallback to evergreen")
            post_text = _story_fallback(story) if story else random.choice(_post_fallbacks())
            post_text = _strip_source_footer(post_text)
            post_text = _strip_forbidden_openers(post_text)
            post_text = _strip_urls_if_disallowed(post_text, include_url_local)
            post_text = _clamp_text_len(post_text, POST_CHAR_LIMIT)
            post_text = _ensure_sentence_per_line(post_text)
            post_text = _clamp_text_len(post_text, POST_CHAR_LIMIT)

        post_text = _append_source_footer(
            post_text,
            story=story,
            allow_source=include_source,
            allow_url=include_url_local,
            limit=POST_CHAR_LIMIT,
        )
        post_text = _clamp_text_len(post_text, POST_CHAR_LIMIT)

        logger.info(
            "tg_post_manager final post length=%d chars focus=%s mood=%s intensity=%s rubric=%s story=%s try=%d/%d text=%r",
            len(post_text),
            focus,
            mood,
            intensity,
            chosen_rubric,
            (story.id if story else None),
            i_try,
            len(attempts),
            post_text,
        )

        opener_fp = _opening_fingerprint(post_text)
        opener_pref = _opening_prefix(post_text, n_words=4)
        if redis and opener_fp:
            try:
                if await _is_recent_opener(redis, channel_id, local_now, opener_fp):
                    logger.info(
                        "tg_post_manager duplicate opener detected (fp=%s pref=%r) -> rephrase once (try=%d/%d)",
                        opener_fp, opener_pref, i_try, len(attempts)
                    )
                    rephrased = await _rephrase_opening_once(
                        draft=post_text,
                        story_block=story_block,
                        rubric=chosen_rubric,
                        char_limit=POST_CHAR_LIMIT,
                        avoid_prefix=opener_pref,
                    )
                    if rephrased:
                        new_fp = _opening_fingerprint(rephrased)
                        new_pref = _opening_prefix(rephrased, n_words=4)
                        if new_fp and (not await _is_recent_opener(redis, channel_id, local_now, new_fp)):
                            post_text = rephrased
                            opener_fp = new_fp
                            opener_pref = new_pref
                            logger.info("tg_post_manager opener rephrase accepted (new_fp=%s)", new_fp)
                        else:
                            last_fail_reason = "opener_duplicate_after_rephrase"
                            logger.info("tg_post_manager opener still duplicates; try next story (plan B)")
                            continue
                    else:
                        last_fail_reason = "opener_rephrase_failed"
                        logger.info("tg_post_manager opener rephrase failed; try next story (plan B)")
                        continue
            except Exception:
                logger.debug("tg_post_manager opener anti-dup flow failed; proceed", exc_info=True)

        try:
            image_bytes = None
            try:
                image_bytes = await _generate_post_image_bytes(redis, channel_id, story, post_text, mood)
            except Exception:
                logger.info("tg_post_manager: image generation pipeline failed", exc_info=True)
                image_bytes = None

            require_image = _coerce_bool(getattr(settings, "TG_POST_REQUIRE_IMAGE", None), DEFAULT_REQUIRE_IMAGE)
            strict = _coerce_bool(getattr(settings, "TG_POST_REQUIRE_IMAGE_STRICT", None), DEFAULT_REQUIRE_IMAGE_STRICT)

            if require_image and not image_bytes:
                image_bytes = _load_fallback_image_bytes()
                if not image_bytes:
                    if strict:
                        logger.warning("tg_post_manager: require_image=true but no image (gen+fallback failed) -> skip post")
                        await _release_redis_lock(redis, lock_key, lock_token)
                        return
                    logger.warning("tg_post_manager: no image available; sending text-only")
                    image_bytes = None

            ok = await _send_telegram_with_retry(channel_id, post_text, image_bytes=image_bytes)
        except Exception:
            logger.exception("tg_post_manager failed to send Telegram message")
            await _release_redis_lock(redis, lock_key, lock_token)
            return
        if not ok:
            logger.warning("tg_post_manager: telegram send failed; skip persisting meta/pacing")
            await _release_redis_lock(redis, lock_key, lock_token)
            return

        await _bump_daily_pacing_state(redis, channel_id, local_now)
        await _remember_opener(redis, channel_id, local_now, opener_fp)

        try:
            meta_obj = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "rubric": chosen_rubric,
                "planned_rubrics": planned,
                "rubrics_considered": rubrics,
                "story_id": (story.id if story else None),
                "story_type": (story.type if story else None),
                "story_source": (story.source if story else None),
                "opener_fp": opener_fp,
                "opener_prefix": opener_pref,
                "has_image": bool(image_bytes),
            }
            await asyncio.gather(
                persona.process_interaction(persona_chat_id, post_text),
                push_message(persona_chat_id, "assistant", post_text, user_id=persona_chat_id),
                push_message(persona_chat_id, "system", META_MARKER_PREFIX + json.dumps(meta_obj, ensure_ascii=False), user_id=persona_chat_id),
            )
        except Exception:
            logger.exception("tg_post_manager saving to memory failed")
        await _release_redis_lock(redis, lock_key, lock_token)
        return

    logger.info("tg_post_manager: all story attempts exhausted without unique opener (last_fail=%s)", last_fail_reason)
    await _release_redis_lock(redis, lock_key, lock_token)
    return
