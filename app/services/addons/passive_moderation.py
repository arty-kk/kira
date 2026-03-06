#app/services/addons/passive_moderation.py
from __future__ import annotations

import logging
import re
import time
import asyncio
import hashlib
import json
import unicodedata

from urllib.parse import urlparse
from typing import Any, Literal, List, Optional
from contextvars import ContextVar

from redis.exceptions import RedisError
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest

from app.core.memory import load_context, get_redis
from app.config import settings
from app.clients.telegram_client import get_bot
from app.clients.openai_client import _call_openai_with_retry, _get_output_text

logger = logging.getLogger(__name__)

_LIGHT_SEMAPHORE = asyncio.Semaphore(10)
MAX_URLS = getattr(settings, "MOD_MAX_URLS", 10)
MAX_PROMPT_TEXT = getattr(settings, "MOD_PROMPT_TEXT_LIMIT", 6000)
DEEP_HISTORY = getattr(settings, "MOD_DEEP_HISTORY", 20)
_PIPELINE_TIMEOUT = float(getattr(settings, "REDIS_PIPELINE_TIMEOUT", 1.0))

_LAST_AI_MODERATION_CATEGORY: ContextVar[str] = ContextVar("last_ai_moderation_category", default="")
_LAST_AI_MODERATION_FLAGS: ContextVar[tuple[str, ...]] = ContextVar("last_ai_moderation_flags", default=())


def get_last_ai_moderation_category() -> str:
    return str(_LAST_AI_MODERATION_CATEGORY.get("") or "").strip()


def get_last_ai_moderation_flags() -> tuple[str, ...]:
    return tuple(_LAST_AI_MODERATION_FLAGS.get(()) or ())


def should_delete_ai_flagged_message(flags: tuple[str, ...]) -> bool:
    if not flags:
        return False
    if not bool(getattr(settings, "MODERATION_DELETE_ON_AI_FLAG", True)):
        return False
    disable_insult_threat = bool(getattr(settings, "MODERATION_DISABLE_INSULT_THREAT_AI", True))
    delete_policy = {
        "regular_promo": bool(getattr(settings, "MODERATION_DELETE_FLAG_REGULAR_PROMO", True)),
        "income_promo": bool(getattr(settings, "MODERATION_DELETE_FLAG_INCOME_PROMO", True)),
        "promo_war": bool(getattr(settings, "MODERATION_DELETE_FLAG_PROMO_WAR", True)),
        "insult_abuse": (False if disable_insult_threat else bool(getattr(settings, "MODERATION_DELETE_FLAG_INSULT_ABUSE", True))),
        "threat_abuse": (False if disable_insult_threat else bool(getattr(settings, "MODERATION_DELETE_FLAG_THREAT_ABUSE", True))),
        "sex_abuse": bool(getattr(settings, "MODERATION_DELETE_FLAG_SEX_ABUSE", True)),
    }
    return any(delete_policy.get(flag, False) for flag in flags)




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
_ORDER_UUID_RE = re.compile(r"\b([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12})\b")


def extract_order_uuid(text: str) -> str | None:
    m = _ORDER_UUID_RE.search(text or "")
    if not m:
        return None
    return str(m.group(1)).lower()


def contains_order_uuid(text: str) -> bool:
    return extract_order_uuid(text) is not None


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

_EMOJI_FLOOD_RE = re.compile(
    "["
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\u2600-\u26FF"
    "\u2700-\u27BF"
    "]"
)


def is_emoji_flood_text(text: str) -> bool:
    raw = _strip_zero_width(unicodedata.normalize("NFKC", text or ""))
    compact = "".join(ch for ch in raw if not ch.isspace())
    if len(compact) < 24:
        return False

    emoji_count = sum(1 for ch in compact if _EMOJI_FLOOD_RE.match(ch))
    if emoji_count < int(getattr(settings, "MODERATION_EMOJI_FLOOD_MIN_COUNT", 16)):
        return False

    emoji_ratio = emoji_count / max(1, len(compact))
    if emoji_ratio >= float(getattr(settings, "MODERATION_EMOJI_FLOOD_MIN_RATIO", 0.55)):
        return True

    max_run = 1
    cur = 1
    for i in range(1, len(compact)):
        if compact[i] == compact[i - 1]:
            cur += 1
            max_run = max(max_run, cur)
        else:
            cur = 1
    return max_run >= int(getattr(settings, "MODERATION_EMOJI_FLOOD_MAX_RUN", 10))


def is_symbol_noise_text(text: str) -> bool:
    raw = _strip_zero_width(unicodedata.normalize("NFKC", text or ""))
    compact = "".join(ch for ch in raw if not ch.isspace())
    if len(compact) < int(getattr(settings, "MODERATION_SYMBOL_NOISE_MIN_LEN", 48)):
        return False

    alnum = sum(1 for ch in compact if ch.isalnum())
    symbol = sum(1 for ch in compact if unicodedata.category(ch).startswith(("P", "S")))
    digit = sum(1 for ch in compact if ch.isdigit())
    unique = len(set(compact))

    max_run = 1
    cur = 1
    for i in range(1, len(compact)):
        if compact[i] == compact[i - 1]:
            cur += 1
            max_run = max(max_run, cur)
        else:
            cur = 1

    symbol_ratio = symbol / max(1, len(compact))
    alnum_ratio = alnum / max(1, len(compact))
    digit_ratio = digit / max(1, len(compact))

    if max_run >= int(getattr(settings, "MODERATION_SYMBOL_NOISE_MAX_RUN", 14)):
        return True
    if symbol_ratio >= float(getattr(settings, "MODERATION_SYMBOL_NOISE_MIN_SYMBOL_RATIO", 0.6)) and alnum_ratio <= 0.4:
        return True
    if len(compact) >= 80 and unique <= int(getattr(settings, "MODERATION_SYMBOL_NOISE_MAX_UNIQUE", 10)) and digit_ratio >= 0.45:
        return True
    return False


def count_message_emojis(text: str, entities: List[dict] | None = None) -> int:
    raw = _strip_zero_width(unicodedata.normalize("NFKC", text or ""))
    unicode_emoji_count = sum(1 for ch in raw if _EMOJI_FLOOD_RE.match(ch))
    custom_emoji_count = sum(1 for ent in (entities or []) if str(ent.get("type") or "").lower() == "custom_emoji")
    return unicode_emoji_count + custom_emoji_count

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
) -> tuple[List[str], bool]:
    """Return (external_mentions, has_unresolved_mentions)."""
    if not text or not entities:
        return [], False

    def _norm_uname(u: str) -> str:
        return (u or "").lstrip("@").strip().lower()

    redis = get_redis()
    bot = get_bot()
    own_bot_username = str(getattr(settings, "TELEGRAM_BOT_USERNAME", "") or "").lstrip("@").strip().lower()
    external: list[str] = []
    has_unresolved = False
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
        return [], False

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
                if cached_outcome in {"ok_user", "channel", "bot", "not_found", "unknown_error"}:
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
        except TelegramBadRequest as exc:
            msg = str(getattr(exc, "message", exc) or "").strip().lower()
            if "chat not found" in msg:
                outcome = "not_found"
                logger.debug(
                    "extract_external_mentions: not_found on get_chat chat_id=%s uname=%s",
                    chat_id,
                    uname,
                )
            else:
                outcome = "unknown_error"
                logger.warning(
                    "extract_external_mentions: unknown_error on get_chat chat_id=%s uname=%s",
                    chat_id,
                    uname,
                    exc_info=True,
                )
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
            has_unresolved = True
            logger.warning(
                "extract_external_mentions: unknown_error gather exception chat_id=%s uname=%s",
                chat_id,
                uname,
                exc_info=(type(outcome), outcome, outcome.__traceback__),
            )
            continue
        if outcome in {"channel", "bot"}:
            external.append(uname)
        elif outcome == "unknown_error":
            has_unresolved = True

    return external, has_unresolved


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



_INCOME_PROMO_OFFER_RE = re.compile(
    r"\b(?:заработ(?:ок|ка|ать)|доход|подработк[аеи]|инвестиц(?:ия|ии|ий)|ваканси[яи]|работа|"
    r"earn(?:ings|ing)?|income|invest(?:ment|ing)?|vacancy|job|remote\s+work|passive\s+income)\b",
    re.IGNORECASE,
)

_INCOME_PROMO_SUPPORT_RE = re.compile(
    r"\b(?:возврат|верн(?:ите|и|уть|ут|ется)|refund|chargeback|"
    r"не\s*достав(?:или|лен|ка)|недостав(?:или|ка)?|заказ|поддержк[аеи]|админ(?:ов|ы)?|"
    r"купибонус(?:ы)?|бонус(?:ы)?|код|не\s*работ(?:ает|ал)|бабл[оа]|деньг(?:и|ами|ах))\b",
    re.IGNORECASE,
)


def _should_suppress_income_promo(*, text: str, parsed: dict[str, bool]) -> bool:
    if not parsed.get("income_promo", False):
        return False
    raw = (text or "").strip()
    if not raw:
        return False
    if not _INCOME_PROMO_SUPPORT_RE.search(raw):
        return False
    return not bool(_INCOME_PROMO_OFFER_RE.search(raw))

def _coerce_ai_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        raw = value.strip().lower()
        if raw in {"true", "1", "yes", "y", "on"}:
            return True
        if raw in {"false", "0", "no", "n", "off", "", "null", "none"}:
            return False
        return False
    return False

def _parse_ai_moderation_json(raw: str) -> dict[str, bool]:
    disable_insult_threat = bool(getattr(settings, "MODERATION_DISABLE_INSULT_THREAT_AI", True))
    try:
        payload = json.loads(raw or "{}")
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        "regular_promo": _coerce_ai_bool(payload.get("regular_promo", False)),
        "income_promo": _coerce_ai_bool(payload.get("income_promo", False)),
        "promo_war": _coerce_ai_bool(payload.get("promo_war", False)),
        "sex_abuse": _coerce_ai_bool(payload.get("sex_abuse", False)),
        "insult_abuse": (False if disable_insult_threat else _coerce_ai_bool(payload.get("insult_abuse", False))),
        "threat_abuse": (False if disable_insult_threat else _coerce_ai_bool(payload.get("threat_abuse", False)))
    }


async def classify_profile_nsfw_fast(*, image_b64: str, image_mime: str = "image/jpeg") -> bool:
    payload = (image_b64 or "").strip()
    if not payload:
        return False

    system_prompt = (
        "Ты — строгий модератор визуального контента. Классифицируй изображение для NSFW-поиска.\n\n"
        "Верни только один ярлык:\n"
        "- NSFW — любая нагота, купальники, бельё, прозрачная или откровенная одежда, акцент на интимных зонах, эротические позы, сексуальный контент.\n"
        "- SFW — только полностью неоткровенный, неэротичный контент.\n\n"
        "Правила:\n"
        "1. Будь максимально строгим.\n"
        "2. При сомнении выбирай NSFW.\n"
        "3. Не объясняй решение. Верни только ярлык."
    )


    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                prompt_profile="app.services.addons.passive_moderation",
                model=settings.BASE_MODEL,
                model_role="base",
                input=[
                    {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
                    {
                        "role": "user",
                        "content": [{"type": "input_image", "image_url": f"data:{image_mime};base64,{payload}"}],
                    },
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "profile_nsfw_moderation",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {
                                "label": {
                                    "type": "string",
                                    "enum": ["NSFW", "SFW"],
                                    "description": "Строковый ярлык классификации профиля: только 'NSFW' или 'SFW'.",
                                },
                            },
                            "required": ["label"],
                            "additionalProperties": False,
                        },
                    }
                },
                max_output_tokens=32,
            ),
            timeout=10.0,
        )
    except Exception:
        logger.exception("classify_profile_nsfw_fast: responses API error")
        return False

    try:
        payload = json.loads(_get_output_text(resp) or "{}")
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    return str(payload.get("label", "")).strip().upper() == "NSFW"


async def moderate_with_openai(
    text: str,
    *,
    image_b64: Optional[str] = None,
    image_mime: Optional[str] = None,
) -> bool:

    _LAST_AI_MODERATION_CATEGORY.set("")
    _LAST_AI_MODERATION_FLAGS.set(())

    if not settings.ENABLE_AI_MODERATION:
        return False

    if not text or not text.strip():
        if not image_b64:
            return False
        text = ""
    trimmed = text[:MAX_PROMPT_TEXT]

    msg_hash, msg_len = _log_message_ref(trimmed)

    disable_insult_threat = bool(getattr(settings, "MODERATION_DISABLE_INSULT_THREAT_AI", True))
    min_ai_text_len = max(0, int(getattr(settings, "MODERATION_AI_MIN_TEXT_LEN", 3) or 0))
    if contains_order_uuid(text or ""):
        return False
    if (not image_b64) and (len((text or "").strip()) < min_ai_text_len):
        return False

    moderation_prompt_parts = [
        "Ты — профессиональный модератор чатов и комментариев Telegram-сообществ компании kupikod.com. "
        "Заполни поля, если найдёшь явные и недвусмысленные признаки, опасные для сообщества, в котором в том числе присутствуют дети:\n",
        "- если сообщение содержит прямую или намеренно завуалированную (обфускацию) рекламу, призыв вступить в сторонние сообщества, какие-то группировки, меньшинства, или перейти во внешние источники с целью продвижения → поставь regular_promo=true. Если уровень уверенности ниже 70% → установи regular_promo=false.\n",
        "- если сообщение содержит прямое или намеренно завуалированное (обфускацию) предложение заработка, инвестиций или работы → поставь income_promo=true. Если уровень уверенности ниже 70% → установи income_promo=false.\n",
        "- если сообщение содержит сексуализированный контент или сексуальное насилие (в т.ч. намёки/описания, неприемлемые для чатов с детьми) → поставь sex_abuse=true. Если уровень уверенности ниже 70% → установи sex_abuse=false.\n"
        "- НЕ ставь income_promo для жалоб в поддержку о возврате денег, недоставке, ожидании возврата или нерабочем коде/товаре без предложения заработать.\n",
        "- если сообщение содержит прямую или намеренно завуалированную (обфускацию) рекламу войны, военного контента, участия в боевых действиях, службы, вербовки, призывов вступить в ряды бойцов, продвижение связанных с войной каналов/чатов/профилей, либо призыв перейти во внешние источники за военными материалами, кадрами насилия, крови, боёв или «правдой о войне» → поставь promo_war=true. Если уровень уверенности ниже 70% → установи promo_war=false.\n",
        "- Ставь promo_war=true в случаях, когда военная тематика подаётся как личный опыт участника боевых действий, как приглашение смотреть контент с фронта, подписаться, зайти в профиль, канал, чат, сообщество или перейти по ссылке.\n",
        "- Cтавь promo_war=false для нейтрального обсуждения новостей, истории, политики, осуждения войны, сообщений о личных переживаниях без продвижения, а также для упоминания армии, фронта или боевых действий без рекламы, вербовки или призыва перейти во внешние источники.\n",
    ]
    if not disable_insult_threat:
        moderation_prompt_parts.insert(
            3,
            "- если сообщение содержит прямое или намеренно завуалированное (обфускацию) оскорбление конкретных лиц или участников обсуждения с использованием уничижительной или ненормативной лексики → поставь insult_abuse=true. Если уровень уверенности ниже 70% → установи insult_abuse=false.\n",
        )
        moderation_prompt_parts.insert(
            4,
            "- если сообщение содержит прямую или намеренно завуалированную (обфускацию) угрозу причинения вреда жизни, здоровью или репутации конкретных лиц → поставь threat_abuse=true. Если уровень уверенности ниже 70% → установи threat_abuse=false.\n",
        )
    moderation_prompt = "".join(moderation_prompt_parts)

    user_content: list[dict[str, Any]] = []
    if trimmed:
        user_content.append({"type": "input_text", "text": trimmed})
    if image_b64:
        user_content.append({"type": "input_image", "image_url": f"data:{image_mime or 'image/jpeg'};base64,{(image_b64 or '').strip()}"})

    moderation_schema_properties: dict[str, dict[str, str]] = {
        "regular_promo": {
            "type": "boolean",
            "description": "Реклама/CTA во внешние источники с уверенностью >=70%.",
        },
        "income_promo": {
            "type": "boolean",
            "description": "Предложения заработка/инвестиций/работы с уверенностью >=70%.",
        },
        "promo_war": {
            "type": "boolean",
            "description": "Реклама войны/военного контента/вербовки и перехода во внешние источники с уверенностью >=70%.",
        },
        "sex_abuse": {
            "type": "boolean",
            "description": "Сексуализированный контент или сексуальное насилие с уверенностью >=70%.",
        },
    }

    if not disable_insult_threat:
        moderation_schema_properties["insult_abuse"] = {
            "type": "boolean",
            "description": "Оскорбление конкретных лиц с уверенностью >=70%.",
        }
        moderation_schema_properties["threat_abuse"] = {
            "type": "boolean",
            "description": "Угроза причинения вреда конкретным лицам с уверенностью >=70%.",
        }

    moderation_schema_required = list(moderation_schema_properties.keys())

    async with _LIGHT_SEMAPHORE:
        try:
            resp = await asyncio.wait_for(
                _call_openai_with_retry(
                    endpoint="responses.create",
                    prompt_profile="app.services.addons.passive_moderation",
                    model=settings.BASE_MODEL,
                    model_role="base",
                    input=[
                        {"role": "system", "content": [{"type": "input_text", "text": moderation_prompt}]},
                        {"role": "user", "content": user_content},
                    ],
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": "chat_moderation",
                            "strict": True,
                            "schema": {
                                "type": "object",
                                "properties": moderation_schema_properties,
                                "required": moderation_schema_required,
                                "additionalProperties": False,
                            },
                        }
                    },
                    max_output_tokens=64,
                ),
                timeout=10.0,
            )
        except Exception:
            logger.exception("moderate_with_openai: responses API error")
            return False

    parsed = _parse_ai_moderation_json(_get_output_text(resp) or "")
    if _should_suppress_income_promo(text=trimmed, parsed=parsed):
        parsed["income_promo"] = False

    flag_order = (
        "regular_promo",
        "income_promo",
        "promo_war",
        "sex_abuse",
        "insult_abuse",
        "threat_abuse"
    )

    triggered_flags = tuple(flag for flag in flag_order if parsed.get(flag, False))
    primary_category = triggered_flags[0] if triggered_flags else ""
    flagged = bool(triggered_flags)

    logger.info(
        "moderation result: model=%s flagged=%s category=%s message_hash=%s message_len=%s flags=%s",
        settings.BASE_MODEL,
        flagged,
        primary_category or "-",
        msg_hash,
        msg_len,
        triggered_flags,
    )

    if flagged:
        _LAST_AI_MODERATION_FLAGS.set(triggered_flags)
        _LAST_AI_MODERATION_CATEGORY.set(primary_category or ",".join(triggered_flags))
        return True

    _LAST_AI_MODERATION_CATEGORY.set("")
    _LAST_AI_MODERATION_FLAGS.set(())
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
) -> Literal["clean", "flood", "spam_links", "spam_mentions", "link_violation", "toxic", "emoji_flood", "symbol_noise", "custom_emoji_spam", "emoji_overlimit"]:

    if not settings.ENABLE_MODERATION or ((not text or not text.strip()) and not image_b64):
        return "clean"

    if contains_order_uuid(text or ""):
        return "clean"

    # Channel/bot sources are checked with link-policy only in light mode.
    if source == "user" and await is_flooding(chat_id, user_id):
        return "flood"

    max_emoji_per_message = int(getattr(settings, "MODERATION_MAX_EMOJI_PER_MESSAGE", 12) or 0)
    if source == "user" and max_emoji_per_message > 0 and count_message_emojis(text or "", entities) > max_emoji_per_message:
        return "emoji_overlimit"

    if source == "user" and is_emoji_flood_text(text or ""):
        return "emoji_flood"

    if source == "user" and is_symbol_noise_text(text or ""):
        return "symbol_noise"

    urls = extract_urls(text or "", entities)
    logger.debug("check_light: urls=%r", urls)
    link_policy = str((policy or {}).get("link_policy", "group_default") or "group_default").strip().lower()
    links_blocked = link_policy != "relaxed"

    mention_count = sum(1 for ent in (entities or []) if str(ent.get("type") or "").lower() == "mention")
    if mention_count > int(getattr(settings, "MODERATION_SPAM_MENTION_THRESHOLD", 5)):
        return "spam_mentions"

    custom_emoji_count = sum(1 for ent in (entities or []) if str(ent.get("type") or "").lower() == "custom_emoji")
    custom_emoji_threshold = int(getattr(settings, "MODERATION_CUSTOM_EMOJI_SPAM_THRESHOLD", 12) or 0)
    if custom_emoji_threshold > 0 and custom_emoji_count >= custom_emoji_threshold:
        return "custom_emoji_spam"

    if len(urls) > int(getattr(settings, "MODERATION_SPAM_LINK_THRESHOLD", 5)):
        return "spam_links"

    external_mentions: list[str] = []
    has_unresolved_mentions = False
    if links_blocked:
        external_mentions, has_unresolved_mentions = await extract_external_mentions(chat_id, text or "", entities)
        if external_mentions:
            logger.debug("check_light: external_mentions=%r", external_mentions)
            return "link_violation"
        if has_unresolved_mentions and bool(getattr(settings, "MODERATION_DELETE_UNRESOLVED_MENTIONS", False)):
            logger.debug("check_light: unresolved_mentions link_violation")
            return "link_violation"

    if links_blocked and contains_telegram_obfuscated(text or ""):
        return "link_violation"

    for u in urls:
        if links_blocked and url_is_unwanted(u, policy=policy):
            return "link_violation"

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
    if contains_order_uuid(text or ""):
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
