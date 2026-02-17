#app/services/addons/passive_moderation.py
from __future__ import annotations

import logging
import re
import time
import asyncio
import hashlib
import unicodedata

from urllib.parse import urlparse
from typing import Literal, List, Optional

from redis.exceptions import RedisError
from aiogram.enums import ChatType

from app.core.memory import load_context, get_redis
from app.config import settings
from app.clients.telegram_client import get_bot
from app.clients.openai_client import get_openai

logger = logging.getLogger(__name__)

_LIGHT_SEMAPHORE = asyncio.Semaphore(10)
MAX_URLS = getattr(settings, "MOD_MAX_URLS", 10)
MAX_PROMPT_TEXT = getattr(settings, "MOD_PROMPT_TEXT_LIMIT", 2000)
DEEP_HISTORY = getattr(settings, "MOD_DEEP_HISTORY", 20)
_PIPELINE_TIMEOUT = float(getattr(settings, "REDIS_PIPELINE_TIMEOUT", 1.0))

TELEGRAM_DOMAINS = [d.lower() for d in getattr(
    settings, "MODERATION_TELEGRAM_DOMAINS",
    ["t.me", "telegram.me", "telegram.dog"]
)]

SANITIZE_REPLACE_ANY_LINK = getattr(settings, "SANITIZE_REPLACE_ANY_LINK", "[link]")
SANITIZE_REPLACE_TG_LINK  = getattr(settings, "SANITIZE_REPLACE_TG_LINK", "[tg-link]")


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
    """Return @usernames that resolve to external channels/bots or unknown chats."""
    if not text or not entities:
        return []

    def _norm_uname(u: str) -> str:
        return (u or "").lstrip("@").strip().lower()

    redis = get_redis()
    bot = get_bot()
    external = []

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
        try:
            cached = await redis.hget(f"user_map:{chat_id}", uname)
        except RedisError:
            logger.warning("extract_external_mentions: Redis error for chat %s", chat_id)
            cached = None
        if cached:
            continue
        try:
            chat = await bot.get_chat(f"@{uname}")
            if not chat:
                external.append(uname)
            elif getattr(chat, "type", None) == ChatType.CHANNEL:
                external.append(uname)
            elif getattr(chat, "is_bot", False):
                external.append(uname)
        except Exception:
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


async def moderate_with_openai(
    text: str,
    *,
    image_b64: Optional[str] = None,
    image_mime: Optional[str] = None,
) -> bool:

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

    redis = get_redis()
    try:
        cached = await redis.get(cache_key)
        if cached is not None:
            if isinstance(cached, (bytes, bytearray)):
                cached = cached.decode("utf-8", "ignore")
            return str(cached) == "1"
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

    try:
        ttl = int(max(1, int(getattr(settings, "MODERATION_CACHE_TTL", 3600))))
        await asyncio.wait_for(
            redis.set(cache_key, "1" if flagged else "0", ex=ttl, nx=True),
            timeout=0.5,
        )
    except Exception:
        logger.debug("moderate_with_openai: failed to cache result")

    if flagged:
        return True

    scores = getattr(result, "category_scores", None)
    items = scores.dict().items() if hasattr(scores, "dict") else getattr(scores, "items", lambda: [])()
    for category, score in items:
        if isinstance(score, (int, float)) and score >= settings.MODERATION_TOXICITY_THRESHOLD:
            logger.debug("moderation: flagged by %s=%.2f", category, score)
            try:
                ttl = int(max(1, int(getattr(settings, "MODERATION_CACHE_TTL", 3600))))
                await redis.set(cache_key, "1", ex=ttl)
            except Exception:
                pass
            return True

    return False

async def is_promo_via_ai(text: str, urls: List[str]) -> bool:
    return False

async def check_light(
    chat_id: int,
    user_id: int,
    text: str,
    entities: List[dict] | None = None,
    source: Literal["user", "bot", "channel"] = "user",
    policy: dict[str, Any] | None = None,
    *,
    image_b64: Optional[str] = None,
    image_mime: Optional[str] = None,
) -> Literal["clean", "flood", "spam_links", "link_violation", "promo", "toxic"]:

    if not settings.ENABLE_MODERATION or ((not text or not text.strip()) and not image_b64):
        return "clean"

    # Channel/bot sources are checked with link-policy only in light mode.
    if source == "user" and await is_flooding(chat_id, user_id):
        return "flood"

    urls = extract_urls(text or "", entities)
    logger.debug("check_light: urls=%r", urls)
    link_policy = str((policy or {}).get("link_policy", "group_default") or "group_default").strip().lower()
    links_blocked = link_policy != "relaxed"

    if len(urls) > int(getattr(settings, "MODERATION_SPAM_LINK_THRESHOLD", 5)):
        return "spam_links"

    external_mentions = await extract_external_mentions(chat_id, text or "", entities)
    if external_mentions and links_blocked:
        logger.debug("check_light: external_mentions=%r", external_mentions)
        return "link_violation"

    if links_blocked and contains_telegram_obfuscated(text or ""):
        return "link_violation"

    for u in urls:
        if links_blocked and url_is_unwanted(u, policy=policy):
            return "link_violation"

    # Keep toxicity checks user-scoped to avoid applying user-specific heuristics to channel/bot sources.
    if source == "user" and await moderate_with_openai(text or "", image_b64=image_b64, image_mime=image_mime):
        return "toxic"

    return "clean"


async def check_deep(
    chat_id: int,
    user_id: int,
    text: str,
    source: Literal["user", "bot", "channel"] = "user",
    *,
    image_b64: Optional[str] = None,
    image_mime: Optional[str] = None,
) -> bool:

    if not settings.ENABLE_AI_MODERATION:
        return False

    # Deep context moderation is user-only; channel/bot sources skip history-based checks.
    if source != "user":
        return False

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
    combined = (ctx + "\n\nNEW MESSAGE:\n" + (text or "")).strip()

    try:
        return await moderate_with_openai(combined, image_b64=image_b64, image_mime=image_mime)
    except Exception:
        logger.exception("check_deep: moderate_with_openai failed for chat %s", chat_id)
        return False
