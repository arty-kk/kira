#app/services/addons/passive_moderation.py
from __future__ import annotations

import logging
import contextlib
import re
import time
import asyncio
import hashlib
import unicodedata
import json

from urllib.parse import urlparse
from typing import Any, Literal, List, Optional
from contextvars import ContextVar

from redis.exceptions import RedisError
from aiogram.enums import ChatType

from app.core.memory import load_context, get_redis
from app.config import settings
from app.clients.telegram_client import get_bot
from app.clients.openai_client import get_openai, _call_openai_with_retry, _get_output_text

logger = logging.getLogger(__name__)

_LIGHT_SEMAPHORE = asyncio.Semaphore(10)
MAX_URLS = getattr(settings, "MOD_MAX_URLS", 10)
MAX_PROMPT_TEXT = getattr(settings, "MOD_PROMPT_TEXT_LIMIT", 2000)
DEEP_HISTORY = getattr(settings, "MOD_DEEP_HISTORY", 20)
_PIPELINE_TIMEOUT = float(getattr(settings, "REDIS_PIPELINE_TIMEOUT", 1.0))

_LAST_AI_MODERATION_CATEGORY: ContextVar[str] = ContextVar("last_ai_moderation_category", default="")


def get_last_ai_moderation_category() -> str:
    return str(_LAST_AI_MODERATION_CATEGORY.get("") or "").strip()


def _to_plain_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        with contextlib.suppress(Exception):
            data = obj.model_dump()
            if isinstance(data, dict):
                return data
    if hasattr(obj, "dict"):
        with contextlib.suppress(Exception):
            data = obj.dict()
            if isinstance(data, dict):
                return data
    return {}


def _primary_ai_category(categories: dict[str, Any], category_scores: dict[str, Any], flagged: bool) -> str:
    scored: list[tuple[str, float]] = []
    for key, value in category_scores.items():
        if isinstance(value, (int, float)):
            scored.append((str(key), float(value)))

    threshold = float(getattr(settings, "MODERATION_TOXICITY_THRESHOLD", 0.9) or 0.9)
    flagged_candidates = [
        (key, score)
        for key, score in scored
        if bool(categories.get(key, False)) or score >= threshold
    ]
    if flagged_candidates:
        flagged_candidates.sort(key=lambda item: item[1], reverse=True)
        return flagged_candidates[0][0]

    if flagged and scored:
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[0][0]

    if flagged:
        for key, value in categories.items():
            if bool(value):
                return str(key)

    return ""


TELEGRAM_DOMAINS = [d.lower() for d in getattr(
    settings, "MODERATION_TELEGRAM_DOMAINS",
    ["t.me", "telegram.me", "telegram.dog"]
)]

SANITIZE_REPLACE_ANY_LINK = getattr(settings, "SANITIZE_REPLACE_ANY_LINK", "[link]")
SANITIZE_REPLACE_TG_LINK  = getattr(settings, "SANITIZE_REPLACE_TG_LINK", "[tg-link]")


def _log_message_ref(text: str) -> tuple[str, int]:
    raw = (text or "")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return digest, len(raw)


def is_telegram_link(url: str) -> bool:
    try:
        u = url.strip()
        if u.lower().startswith("tg://"):
            return True
        if "://" not in u:
            u = f"http://{u}"
        host = urlparse(u).netloc.lower().split(":", 1)[0]
        return any(host == d or host.endswith("." + d) for d in TELEGRAM_DOMAINS)
    except Exception:
        return False

_ZW_RE = re.compile(
    r"[\u200B\u200C\u200D\u2060\u180E\uFEFF\u2061\u2062\u2063\u2064]"
)
_BRACKETED_DOT_RE = re.compile(r"\[\s*\.\s*\]|\(\s*\.\s*\)|\{\s*\.\s*\}", re.IGNORECASE)
_DOT_WORD_RE = re.compile(r"\b(dot|точка|дот)\b", re.IGNORECASE)
_DOT_LIKE = "•·∙⋅∘｡。・●○◦"
_CYR_TO_LAT = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "r", "с": "c", "х": "x", "у": "y", "к": "k",
    "т": "t", "м": "m", "н": "n", "в": "v", "л": "l", "г": "g", "д": "d",
    # заглавные
    "А": "A", "Е": "E", "О": "O", "Р": "R", "С": "C", "Х": "X", "У": "Y", "К": "K",
    "Т": "T", "М": "M", "Н": "N", "В": "V", "Л": "L", "Г": "G", "Д": "D",
})

def _strip_zero_width(s: str) -> str:
    return _ZW_RE.sub("", s)

def _normalize_for_url_detection(s: str) -> str:
    if not s:
        return s
    s = unicodedata.normalize("NFKC", s)
    s = _strip_zero_width(s)
    s = _BRACKETED_DOT_RE.sub(".", s)
    s = _DOT_WORD_RE.sub(".", s)
    for ch in _DOT_LIKE:
        s = s.replace(ch, ".")
    s = s.translate(_CYR_TO_LAT)
    return s

def contains_telegram_obfuscated(text: str) -> bool:
    if not text:
        return False
    s = _normalize_for_url_detection(text)
    sep = r"[\s_\-\(\)\[\]\{\}]*"
    tg_domain = rf"(?:t{sep}\.{sep}me|telegram{sep}\.{sep}me|telegram{sep}\.{sep}dog)"
    tg_proto  = rf"(?:tg{sep}:{sep}//)"
    pat = re.compile(rf"(?<![a-z0-9])(?:{tg_domain}|{tg_proto})(?![a-z0-9])", re.IGNORECASE)
    return bool(pat.search(s))

def _normalize_for_cta_detection(text: str) -> str:
    s = unicodedata.normalize("NFKC", text or "")
    s = _strip_zero_width(s).lower().replace("ё", "е")
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


_PROFILE_CTA_PATTERNS = [
    re.compile(r"\b(?:смотри|глянь|загляни|чекаи|чекни|посмотри)\s+(?:у\s+меня\s+)?(?:в\s+)?(?:профил(?:е|ь)|био)\b", re.IGNORECASE),
    re.compile(r"\b(?:ссылка\s+)?(?:у\s+меня\s+)?(?:в\s+)?(?:профил(?:е|ь)|био)\b", re.IGNORECASE),
    re.compile(r"\bу\s+меня\s+(?:в\s+)?(?:профил(?:е|ь)|био|канале|чате)\b", re.IGNORECASE),
    re.compile(r"\b(?:в|на)\s+моем\s+(?:канале|чате|профил(?:е|ь)|био)\b", re.IGNORECASE),
    re.compile(r"\b(?:пиши|напиши)\s+мне\b", re.IGNORECASE),
]

_COMBAT_PROMO_CTA_PATTERNS = [
    re.compile(r"\b(?:контент|истории|опыт|много\s+контента)\s+(?:с|про|из)?\s*(?:фронт|передк[ае]|сво|войн[аеы]|штурм)\b", re.IGNORECASE),
]

_COMBAT_PROMO_CTA_TRIGGER_RE = re.compile(
    r"\b(?:смотри|глянь|зацени|подписываися|подписывайся|переходи|заходи|пиши|напиши)\b",
    re.IGNORECASE,
)

_COMBAT_TOPIC_RE = re.compile(
    r"\b(?:штурм|фронт|передк[ае]|сво|войн[аеы]|боев(?:ых|ые|ая)|дрон(?:ы|ов)?|боец|ветеран)\b",
    re.IGNORECASE,
)


def contains_profile_cta_without_url(text: str) -> bool:
    normalized = _normalize_for_cta_detection(text)
    if not normalized:
        return False
    has_profile_cta = any(p.search(normalized) for p in _PROFILE_CTA_PATTERNS)
    has_combat_cta = bool(_COMBAT_PROMO_CTA_TRIGGER_RE.search(normalized) and _COMBAT_TOPIC_RE.search(normalized))
    has_combat_phrase = any(p.search(normalized) for p in _COMBAT_PROMO_CTA_PATTERNS)
    return bool(has_profile_cta or has_combat_cta or has_combat_phrase)

def sanitize_for_context(
    text: str,
    entities: List[dict] | None = None,
    *,
    replace_links: bool = True,
) -> str:
    if not text:
        return text
    s = text

    try:
        if entities and replace_links:
            spans = []
            for ent in entities:
                t = str(ent.get("type") or "").lower()
                off = int(ent.get("offset", -1))
                ln  = int(ent.get("length", 0))
                if off < 0 or ln <= 0:
                    continue
                if t in ("text_link", "url"):
                    repl = SANITIZE_REPLACE_ANY_LINK
                    if t == "text_link":
                        url = str(ent.get("url") or "")
                        repl = SANITIZE_REPLACE_TG_LINK if is_telegram_link(url) else SANITIZE_REPLACE_ANY_LINK
                    spans.append((off, ln, repl))
            for off, ln, repl in sorted(spans, key=lambda x: x[0], reverse=True):
                s = s[:off] + repl + s[off+ln:]
    except Exception:
        pass

    s = _strip_zero_width(s)

    if replace_links:
        sep = r"[\s_\-\(\)\[\]\{\}\u200B\u200C\u200D\u2060\u180E\uFEFF]*"
        tg_pat = re.compile(rf"(?i)(?:tg{sep}:{sep}//|t{sep}\.{sep}me|telegram{sep}\.{sep}(?:me|dog))")
        s = tg_pat.sub(SANITIZE_REPLACE_TG_LINK, s)

        url_pat = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>'\"]+")
        s = url_pat.sub(SANITIZE_REPLACE_ANY_LINK, s)

    try:
        if replace_links:
            found = extract_urls(text, entities)
            for u in found:
                repl = SANITIZE_REPLACE_TG_LINK if is_telegram_link(u) else SANITIZE_REPLACE_ANY_LINK
                s = s.replace(u, repl)
    except Exception:
        pass

    return s


def split_context_text(
    raw_text: str,
    entities: List[dict] | None,
    *,
    allow_web: bool,
) -> tuple[str, str]:
    log_text = sanitize_for_context(raw_text, entities)
    sanitize_for_model = bool(getattr(settings, "MODERATION_SANITIZE_CONTEXT_FOR_MODEL", False))
    if not sanitize_for_model:
        return raw_text, log_text
    model_text = sanitize_for_context(raw_text, entities, replace_links=not allow_web)
    return model_text, log_text


async def is_flooding(chat_id: int, user_id: int) -> bool:

    max_msgs = int(getattr(settings, "MOD_MAX_MESSAGES", 10))
    period = int(getattr(settings, "MOD_PERIOD_SECONDS", 60))
    redis = get_redis()
    key = f"mod_flood:{chat_id}:{user_id}"
    now_ts = time.time()

    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.lpush(key, now_ts)
            pipe.ltrim(key, 0, max_msgs * 2)
            pipe.expire(key, period + 1)
            pipe.lrange(key, 0, max_msgs * 2)
            result = await asyncio.wait_for(pipe.execute(), timeout=_PIPELINE_TIMEOUT)
        timestamps = result[-1] or []
    except RedisError:
        logger.warning("is_flooding: Redis error for chat %s user %s", chat_id, user_id)
        return False
    except asyncio.TimeoutError:
        logger.warning("is_flooding: Redis pipeline timeout for chat %s user %s", chat_id, user_id)
        return False

    threshold = now_ts - period
    valid = []
    for ts in timestamps:
        try:
            if isinstance(ts, (bytes, bytearray)):
                ts = ts.decode()
            t = float(ts)
            if t >= threshold:
                valid.append(t)
        except Exception:
            continue

    return len(valid) > max_msgs


def extract_urls(text: str, entities: List[dict] | None = None) -> List[str]:
    pattern = r"(?:https?://[^\s<>'\"]+|www\.[^\s<>'\"]+|t\.me/[^\s<>'\"]+|telegram\.me/[^\s<>'\"]+|tg://[^\s<>'\"]+)"
    strip_trail = '.,;!?)]}\'"'
    urls = [m.group(0).rstrip(strip_trail) for m in re.finditer(pattern, text or "", flags=re.IGNORECASE)]
    if entities:
        for ent in entities:
            t = (str(ent.get("type") or "")).lower()
            if t == "url":
                off, length = ent["offset"], ent["length"]
                snippet = text[off:off + length].rstrip(strip_trail)
                if snippet and snippet not in urls:
                    urls.append(snippet)
            elif t == "text_link" and ent.get("url"):
                u = str(ent["url"]).rstrip(strip_trail)
                if u and u not in urls:
                    urls.append(u)
    norm = _normalize_for_url_detection(text or "")

    hidden_tg = re.findall(r"(?:t\.me/[^\s<>'\"]+|telegram\.me/[^\s<>'\"]+|tg://[^\s<>'\"]+)", norm, flags=re.IGNORECASE)
    for u in hidden_tg:
        if u not in urls:
            urls.append(u)

    domain_re = re.compile(
        r"(?<!@)\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,24}|xn--[a-z0-9-]{2,60})"
        r"(?:/[^\s<>'\"]*)?",
        re.IGNORECASE,
    )
    for m in domain_re.finditer(norm):
        candidate = m.group(0).rstrip(strip_trail)

        if len(candidate) < 4:
            continue
        if candidate not in urls:
            urls.append(candidate)

    return list(dict.fromkeys(urls))[:MAX_URLS]


async def extract_external_mentions(
    chat_id: int,
    text: str,
    entities: List[dict] | None = None,
) -> List[str]:
    """Return @usernames that resolve to external channels/bots."""
    if not text or not entities:
        return []

    def _norm_uname(u: str) -> str:
        return (u or "").lstrip("@").strip().lower()

    redis = get_redis()
    bot = get_bot()
    own_bot_username = str(getattr(settings, "TELEGRAM_BOT_USERNAME", "") or "").lstrip("@").strip().lower()
    external: list[str] = []
    usernames: list[str] = []

    for ent in entities:
        t = str(ent.get("type") or "").lower()
        if t == "text_mention":
            continue
        if t != "mention":
            continue
        off = int(ent.get("offset", -1))
        ln = int(ent.get("length", 0))
        if off < 0 or ln <= 0:
            continue
        uname = _norm_uname(text[off:off + ln])
        if not uname:
            continue
        if own_bot_username and uname == own_bot_username:
            continue
        usernames.append(uname)

    usernames = list(dict.fromkeys(usernames))
    if not usernames:
        return []

    resolve_timeout = float(getattr(settings, "MOD_MENTION_RESOLVE_TIMEOUT", 1.5))
    resolve_concurrency = int(max(1, int(getattr(settings, "MOD_MENTION_RESOLVE_CONCURRENCY", 3))))
    ttl_pos = int(max(1, int(getattr(settings, "MOD_MENTION_RESOLVE_TTL_POS", 3600))))
    ttl_neg = int(max(1, int(getattr(settings, "MOD_MENTION_RESOLVE_TTL_NEG", 300))))
    semaphore = asyncio.Semaphore(resolve_concurrency)

    async def _resolve_outcome(uname: str) -> str:
        cache_key = f"mod:mention_resolve:{uname}"
        try:
            cached = await redis.hget(f"user_map:{chat_id}", uname)
        except RedisError:
            logger.warning("extract_external_mentions: Redis error for chat %s", chat_id)
            cached = None
        if cached:
            return "ok_user"

        try:
            cached_outcome = await redis.get(cache_key)
            if cached_outcome:
                if isinstance(cached_outcome, (bytes, bytearray)):
                    cached_outcome = cached_outcome.decode("utf-8", "ignore")
                cached_outcome = str(cached_outcome).strip().lower()
                if cached_outcome in {"ok_user", "channel", "bot", "unknown_error"}:
                    return cached_outcome
        except Exception:
            logger.debug("extract_external_mentions: mention cache read failed", exc_info=True)

        outcome = "unknown_error"
        try:
            async with semaphore:
                chat = await asyncio.wait_for(bot.get_chat(f"@{uname}"), timeout=resolve_timeout)
            if not chat:
                outcome = "unknown_error"
                logger.warning(
                    "extract_external_mentions: unknown_error empty chat response chat_id=%s uname=%s",
                    chat_id,
                    uname,
                )
            elif getattr(chat, "type", None) == ChatType.CHANNEL:
                outcome = "channel"
            elif getattr(chat, "is_bot", False):
                outcome = "bot"
            else:
                outcome = "ok_user"
        except Exception:
            outcome = "unknown_error"
            logger.warning(
                "extract_external_mentions: unknown_error on get_chat chat_id=%s uname=%s",
                chat_id,
                uname,
                exc_info=True,
            )

        try:
            ttl = ttl_pos if outcome == "ok_user" else ttl_neg
            await redis.set(cache_key, outcome, ex=ttl)
        except Exception:
            logger.debug("extract_external_mentions: mention cache write failed", exc_info=True)
        return outcome

    outcomes = await asyncio.gather(*(_resolve_outcome(uname) for uname in usernames), return_exceptions=True)
    for uname, outcome in zip(usernames, outcomes):
        if isinstance(outcome, Exception):
            logger.warning(
                "extract_external_mentions: unknown_error gather exception chat_id=%s uname=%s",
                chat_id,
                uname,
                exc_info=(type(outcome), outcome, outcome.__traceback__),
            )
            continue
        if outcome in {"channel", "bot"}:
            external.append(uname)

    return external


def contains_any_link_obfuscated(text: str) -> bool:

    if not text:
        return False
    norm = _normalize_for_url_detection(text)

    if contains_telegram_obfuscated(text):
        return True

    domain_re = re.compile(
        r"(?<!@)\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,24}|xn--[a-z0-9-]{2,60})",
        re.IGNORECASE,
    )
    return bool(domain_re.search(norm))


def url_is_unwanted(url: str, *, policy: dict[str, Any] | None = None) -> bool:
    try:
        u = url if '://' in url else f'http://{url}'
        netloc = urlparse(u).netloc.lower().split(':', 1)[0]
    except Exception:
        return True

    link_policy = str((policy or {}).get("link_policy", "group_default") or "group_default").strip().lower()
    if any(netloc == d or netloc.endswith("." + d) for d in TELEGRAM_DOMAINS):
        return link_policy != "relaxed"

    for kw in getattr(settings, "MODERATION_ALLOWED_LINK_KEYWORDS", []):
        kw = (kw or "").lower().strip(".")
        if not kw:
            continue
        if netloc == kw or netloc.endswith("." + kw):
            return False

    return True


def _profile_nsfw_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "is_nude_female": {"type": "boolean"},
            "is_sexualized": {"type": "boolean"},
            "is_suggestive": {"type": "boolean"},
            "risk_level": {"type": "string", "enum": ["low", "medium", "high"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "answer": {"type": "string", "enum": ["yes", "no"]},
        },
        "required": [
            "is_nude_female",
            "is_sexualized",
            "is_suggestive",
            "risk_level",
            "confidence",
            "answer",
        ],
        "additionalProperties": False,
    }


async def classify_profile_nsfw_fast(*, image_b64: str, image_mime: str = "image/jpeg") -> bool:
    payload = (image_b64 or "").strip()
    if not payload:
        return False

    system_prompt = (
        "Ты модератор аватаров. Оцени изображение по нескольким NSFW-сигналам: "
        "1) явная женская обнаженность; "
        "2) сексуализированная подача (эротические позы, акценты на интимных зонах); "
        "3) suggestive-контент (белье/купальник любой расцветки) с сексуализированным контекстом. "
        "Верни только JSON по схеме. "
        "При сомнениях выбирай консервативный безопасный вариант: answer='no', risk_level='low', "
        "is_nude_female=false, is_sexualized=false."
    )
    user_prompt = (
        "Analyze the profile image and return JSON only. "
        "Set is_nude_female/is_sexualized/is_suggestive and final answer (yes|no) with risk_level (low|medium|high)."
    )

    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=str(getattr(settings, "MODERATION_PROFILE_NSFW_MODEL", "gpt-5-nano") or "gpt-5-nano"),
                instructions=system_prompt,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": user_prompt},
                            {"type": "input_image", "image_url": f"data:{image_mime};base64,{payload}"},
                        ],
                    }
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "profile_nsfw_label",
                        "schema": _profile_nsfw_schema(),
                        "strict": True,
                    }
                },
                max_output_tokens=64,
                reasoning={"effort": "low"},
            ),
            timeout=10.0,
        )
    except Exception:
        logger.exception("classify_profile_nsfw_fast: openai call failed")
        return False

    raw = (_get_output_text(resp) or "").strip()
    if not raw:
        return False

    try:
        obj = json.loads(raw)
    except Exception:
        logger.debug("classify_profile_nsfw_fast: invalid JSON output=%r", raw)
        return False

    answer = str(obj.get("answer", "")).strip().lower()
    if answer in ("да", "нет"):
        answer = "yes" if answer == "да" else "no"
    if answer not in ("yes", "no"):
        return False

    is_nude_female = bool(obj.get("is_nude_female", False))
    if is_nude_female and answer == "yes":
        return True

    risk_level = str(obj.get("risk_level", "")).strip().lower()
    if risk_level not in {"low", "medium", "high"}:
        return False

    is_sexualized = bool(obj.get("is_sexualized", False))
    return risk_level == "high" and is_sexualized


async def moderate_with_openai(
    text: str,
    *,
    image_b64: Optional[str] = None,
    image_mime: Optional[str] = None,
) -> bool:

    _LAST_AI_MODERATION_CATEGORY.set("")

    if not settings.ENABLE_AI_MODERATION:
        return False

    if not text or not text.strip():
        if not image_b64:
            return False
        text = ""
    trimmed = text[:MAX_PROMPT_TEXT]

    model_tag = settings.MODERATION_MODEL
    tox_tag = str(getattr(settings, "MODERATION_TOXICITY_THRESHOLD", ""))
    img_tag = ""
    if image_b64:
        try:
            img_tag = hashlib.sha256(image_b64.encode("utf-8")).hexdigest()[:16]
        except Exception:
            img_tag = "img"

    cache_key = "mod:cache:" + hashlib.sha256(
        (model_tag + "|" + tox_tag + "|" + trimmed + "|" + img_tag).encode("utf-8")
    ).hexdigest()[:48]
    msg_hash, msg_len = _log_message_ref(trimmed)

    redis = get_redis()
    try:
        cached = await redis.get(cache_key)
        if cached is not None:
            if isinstance(cached, (bytes, bytearray)):
                cached = cached.decode("utf-8", "ignore")
            cached_raw = str(cached).strip()

            cached_flagged = cached_raw == "1"
            cached_category = ""
            if cached_raw.startswith("{"):
                with contextlib.suppress(Exception):
                    data = json.loads(cached_raw)
                    if isinstance(data, dict):
                        cached_flagged = bool(data.get("flagged", False))
                        cached_category = str(data.get("category") or "").strip()

            _LAST_AI_MODERATION_CATEGORY.set(cached_category if cached_flagged else "")
            logger.info(
                "moderation result (cache): model=%s flagged=%s category=%s message_hash=%s message_len=%s",
                settings.MODERATION_MODEL,
                cached_flagged,
                cached_category or "-",
                msg_hash,
                msg_len,
            )
            return cached_flagged
    except Exception:
        logger.debug("moderate_with_openai: cache lookup failed")

    client = get_openai()

    if image_b64:
        input_payload = [
            {"type": "text", "text": trimmed or "(no text)"},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{image_mime or 'image/jpeg'};base64,{(image_b64 or '').strip()}",
                },
            },
        ]
    else:
        input_payload = trimmed

    async with _LIGHT_SEMAPHORE:
        try:
            resp = await asyncio.wait_for(
                client.moderations.create(
                    model=settings.MODERATION_MODEL,
                    input=input_payload,
                ),
                timeout=10.0,
            )
        except Exception:
            logger.exception("moderate_with_openai: moderation API error")
            return False

    results = getattr(resp, "results", None)
    if not results or not isinstance(results, list):
        logger.error("moderate_with_openai: unexpected response %r", resp)
        return False

    result = results[0]
    flagged = bool(getattr(result, "flagged", False))

    categories_obj = getattr(result, "categories", None)
    category_scores_obj = getattr(result, "category_scores", None)
    categories_dump = _to_plain_dict(categories_obj)
    category_scores_dump = _to_plain_dict(category_scores_obj)
    primary_category = _primary_ai_category(categories_dump, category_scores_dump, flagged)

    try:
        ttl = int(max(1, int(getattr(settings, "MODERATION_CACHE_TTL", 3600))))
        payload = json.dumps({
            "flagged": bool(flagged),
            "category": primary_category,
        }, ensure_ascii=False)
        await asyncio.wait_for(
            redis.set(cache_key, payload, ex=ttl, nx=True),
            timeout=0.5,
        )
    except Exception:
        logger.debug("moderate_with_openai: failed to cache result")

    logger.info(
        "moderation result: model=%s flagged=%s category=%s message_hash=%s message_len=%s categories=%s category_scores=%s",
        settings.MODERATION_MODEL,
        flagged,
        primary_category or "-",
        msg_hash,
        msg_len,
        categories_dump,
        category_scores_dump,
    )

    if flagged:
        _LAST_AI_MODERATION_CATEGORY.set(primary_category)
        return True

    for category, score in category_scores_dump.items():
        if isinstance(score, (int, float)) and score >= settings.MODERATION_TOXICITY_THRESHOLD:
            logger.debug("moderation: flagged by %s=%.2f", category, score)
            _LAST_AI_MODERATION_CATEGORY.set(str(category))
            try:
                ttl = int(max(1, int(getattr(settings, "MODERATION_CACHE_TTL", 3600))))
                payload = json.dumps({"flagged": True, "category": str(category)}, ensure_ascii=False)
                await redis.set(cache_key, payload, ex=ttl)
            except Exception:
                pass
            return True

    _LAST_AI_MODERATION_CATEGORY.set("")
    return False


async def check_light(
    chat_id: int,
    user_id: int,
    text: str,
    entities: List[dict] | None = None,
    source: Literal["user", "bot", "channel"] = "user",
    allow_ai_for_source: bool | None = None,
    policy: dict[str, Any] | None = None,
    *,
    image_b64: Optional[str] = None,
    image_mime: Optional[str] = None,
) -> Literal["clean", "flood", "spam_links", "spam_mentions", "link_violation", "promo", "promo_profile_cta", "toxic"]:

    if not settings.ENABLE_MODERATION or ((not text or not text.strip()) and not image_b64):
        return "clean"

    # Channel/bot sources are checked with link-policy only in light mode.
    if source == "user" and await is_flooding(chat_id, user_id):
        return "flood"

    urls = extract_urls(text or "", entities)
    logger.debug("check_light: urls=%r", urls)
    link_policy = str((policy or {}).get("link_policy", "group_default") or "group_default").strip().lower()
    links_blocked = link_policy != "relaxed"

    mention_count = sum(1 for ent in (entities or []) if str(ent.get("type") or "").lower() == "mention")
    if mention_count > int(getattr(settings, "MODERATION_SPAM_MENTION_THRESHOLD", 5)):
        return "spam_mentions"

    if len(urls) > int(getattr(settings, "MODERATION_SPAM_LINK_THRESHOLD", 5)):
        return "spam_links"

    external_mentions: list[str] = []
    if links_blocked:
        external_mentions = await extract_external_mentions(chat_id, text or "", entities)
        if external_mentions:
            logger.debug("check_light: external_mentions=%r", external_mentions)
            return "link_violation"

    if links_blocked and contains_telegram_obfuscated(text or ""):
        return "link_violation"

    for u in urls:
        if links_blocked and url_is_unwanted(u, policy=policy):
            return "link_violation"

    if not urls and not contains_telegram_obfuscated(text or "") and contains_profile_cta_without_url(text or ""):
        return "promo_profile_cta"

    # NOTE: Separate AI promo-content detection is currently not implemented in the light pipeline.
    ai_allowed = (source == "user") if allow_ai_for_source is None else bool(allow_ai_for_source)
    if ai_allowed and await moderate_with_openai(text or "", image_b64=image_b64, image_mime=image_mime):
        return "toxic"

    return "clean"


async def check_deep(
    chat_id: int,
    user_id: int,
    text: str,
    source: Literal["user", "bot", "channel"] = "user",
    allow_ai_for_source: bool | None = None,
    *,
    image_b64: Optional[str] = None,
    image_mime: Optional[str] = None,
) -> bool:

    if not settings.ENABLE_AI_MODERATION:
        return False

    ai_allowed = (source == "user") if allow_ai_for_source is None else bool(allow_ai_for_source)
    if not ai_allowed:
        return False

    include_history = bool(getattr(settings, "MODERATION_DEEP_INCLUDE_HISTORY", False))
    history = []
    if source == "user" and include_history:
        try:
            history = await load_context(chat_id, user_id)
        except Exception:
            logger.exception("check_deep: load_context error for chat %s", chat_id)
            history = []

    def _as_resp_msg(m):
        if not isinstance(m, dict):
            return None
        role = m.get("role")
        if role not in ("system", "user", "assistant"):
            return None
        content = m.get("content") or m.get("text")
        if content is None:
            return None
        if isinstance(content, (list, dict)):
            content = str(content)
        return {"role": role, "content": content}

    snippet = [x for x in map(_as_resp_msg, history[-DEEP_HISTORY:]) if x]

    ctx_parts: List[str] = []
    for m in snippet:
        try:
            role = (m.get("role") or "user").upper()
            content = (m.get("content") or "")
            if content:
                ctx_parts.append(f"{role}: {content}")
        except Exception:
            continue
    ctx = "\n".join(ctx_parts)
    if source == "user" and include_history:
        combined = (ctx + "\n\nNEW MESSAGE:\n" + (text or "")).strip()
    else:
        combined = (text or "").strip()

    try:
        blocked = await moderate_with_openai(combined, image_b64=image_b64, image_mime=image_mime)
        msg_hash, msg_len = _log_message_ref(combined)
        logger.info(
            "check_deep moderation: chat_id=%s user_id=%s source=%s include_history=%s blocked=%s message_hash=%s message_len=%s",
            chat_id,
            user_id,
            source,
            include_history,
            blocked,
            msg_hash,
            msg_len,
        )
        return blocked
    except Exception:
        logger.exception("check_deep: moderate_with_openai failed for chat %s", chat_id)
        return False
