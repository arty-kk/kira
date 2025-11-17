#app/bot/utils/spam_guard.py
from __future__ import annotations

import json
import logging
import time
import uuid
import unicodedata

from typing import Optional, Tuple, Awaitable, Callable

from app.config import settings
from app.core.memory import get_redis

logger = logging.getLogger(__name__)

ANTI_SPAM_EMOJI_LIMIT: int = int(getattr(settings, "ANTI_SPAM_EMOJI_LIMIT", 5))
ANTI_SPAM_PM_BURST_LIMIT: int = int(getattr(settings, "ANTI_SPAM_PM_BURST_LIMIT", 3))
ANTI_SPAM_PM_BURST_WINDOW_MS: int = int(getattr(settings, "ANTI_SPAM_PM_BURST_WINDOW_MS", 1000))
ANTI_SPAM_BLOCK_TTL: int = int(getattr(settings, "ANTI_SPAM_BLOCK_TTL", 14_400))
ANTI_SPAM_MAX_TEXT_CHARS: int = int(getattr(settings, "ANTI_SPAM_MAX_TEXT_CHARS", 4000))
ANTI_SPAM_DISTINCT_SYMBOLS_LIMIT: int = int(getattr(settings, "ANTI_SPAM_DISTINCT_SYMBOLS_LIMIT", 10))

def _k_pm_block(user_id: int) -> str:
    return f"pm:block:{user_id}"

def _k_pm_block_warned(user_id: int) -> str:
    return f"pm:blockwarned:{user_id}"

def _k_pm_rate(user_id: int) -> str:
    return f"pm:rate:{user_id}"

def _is_variation_selector(cp: int) -> bool:
    return 0xFE00 <= cp <= 0xFE0F

def _is_skin_tone_modifier(cp: int) -> bool:
    return 0x1F3FB <= cp <= 0x1F3FF

def _is_zwj(cp: int) -> bool:
    return cp == 0x200D

def _is_regional_indicator(cp: int) -> bool:
    return 0x1F1E6 <= cp <= 0x1F1FF

def _is_keycap_base(cp: int) -> bool:
    return cp == 0x0023 or cp == 0x002A or (0x0030 <= cp <= 0x0039)

def _is_emoji_base(cp: int) -> bool:
    return (
        0x1F600 <= cp <= 0x1F64F
        or 0x1F300 <= cp <= 0x1F5FF
        or 0x1F680 <= cp <= 0x1F6FF
        or 0x1F900 <= cp <= 0x1F9FF
        or 0x1FA70 <= cp <= 0x1FAFF
        or 0x2600  <= cp <= 0x26FF
        or 0x2700  <= cp <= 0x27BF
    )

def count_emojis(text: str) -> int:
    if not text:
        return 0
    s = text
    n = len(s)
    i = 0
    total = 0
    while i < n:
        cp = ord(s[i])
        if _is_keycap_base(cp):
            if i + 2 < n and ord(s[i + 1]) == 0xFE0F and ord(s[i + 2]) == 0x20E3:
                total += 1
                i += 3
                continue
            if i + 1 < n and ord(s[i + 1]) == 0x20E3:
                total += 1
                i += 2
                continue
        if _is_regional_indicator(cp):
            if i + 1 < n and _is_regional_indicator(ord(s[i + 1])):
                total += 1
                i += 2
                continue
        if _is_emoji_base(cp):
            total += 1
            i += 1
            while i < n:
                cp2 = ord(s[i])
                if _is_variation_selector(cp2) or _is_skin_tone_modifier(cp2):
                    i += 1
                    continue
                if _is_zwj(cp2):
                    if i + 1 < n and _is_emoji_base(ord(s[i + 1])):
                        i += 2
                        continue
                    else:
                        i += 1
                        break
                break
            continue
        i += 1
    return total

def count_distinct_symbols(text: str) -> int:
    if not text:
        return 0
    uniq: set[str] = set()
    for ch in text:
        cat = unicodedata.category(ch)
        if cat and cat[0] in ("P", "S"):
            uniq.add(ch)
    return len(uniq)

async def _pm_block(user_id: int, reason: str) -> None:
    redis = get_redis()
    payload = json.dumps(
        {"reason": reason, "ts": time.time(), "ttl": ANTI_SPAM_BLOCK_TTL},
        ensure_ascii=False,
    )
    try:
        await redis.set(_k_pm_block(user_id), payload, ex=ANTI_SPAM_BLOCK_TTL)
    except Exception:
        logger.exception("pm_block: failed to set block for user=%s", user_id)

async def _pm_block_status(user_id: int) -> Tuple[bool, Optional[str], Optional[int]]:
    redis = get_redis()
    try:
        ttl = await redis.ttl(_k_pm_block(user_id))
        if ttl is None or ttl < 0:
            raw = await redis.get(_k_pm_block(user_id))
            if not raw:
                return False, None, None
            await redis.expire(_k_pm_block(user_id), ANTI_SPAM_BLOCK_TTL)
            ttl = ANTI_SPAM_BLOCK_TTL
        try:
            raw = await redis.get(_k_pm_block(user_id))
            reason = None
            if raw:
                s = raw.decode("utf-8", "ignore") if isinstance(raw, (bytes, bytearray)) else raw
                obj = json.loads(s)
                reason = obj.get("reason")
            return True, reason, int(ttl)
        except Exception:
            return True, None, int(ttl or ANTI_SPAM_BLOCK_TTL)
    except Exception:
        logger.exception("pm_block_status: failed for user=%s", user_id)
        return False, None, None

async def _pm_should_notify(user_id: int, ttl_hint: Optional[int]) -> bool:
    redis = get_redis()
    ttl = int(ttl_hint or ANTI_SPAM_BLOCK_TTL)
    try:
        ok = await redis.set(_k_pm_block_warned(user_id), 1, nx=True, ex=ttl)
        return bool(ok)
    except Exception:
        logger.exception("pm_should_notify: failed for user=%s", user_id)
        return False

async def check_and_enforce_pm_limits(
    user_id: int,
    text: Optional[str],
) -> Tuple[bool, bool, Optional[str]]:

    blocked, _reason, ttl = await _pm_block_status(user_id)
    if blocked:
        notify = await _pm_should_notify(user_id, ttl)
        return True, notify, "blocked"

    if text:
        try:
            if len(text) > ANTI_SPAM_MAX_TEXT_CHARS:
                return True, True, "length"
        except Exception:
            logger.exception("length check failed for user=%s", user_id)

    if text:
        try:
            if count_distinct_symbols(text) > ANTI_SPAM_DISTINCT_SYMBOLS_LIMIT:
                await _pm_block(user_id, reason="symbols")
                await _pm_should_notify(user_id, ANTI_SPAM_BLOCK_TTL)
                return True, True, "symbols"
        except Exception:
            logger.exception("symbols distinct check failed for user=%s", user_id)

    if text:
        try:
            if count_emojis(text) > ANTI_SPAM_EMOJI_LIMIT:
                await _pm_block(user_id, reason="emoji")
                await _pm_should_notify(user_id, ANTI_SPAM_BLOCK_TTL)
                return True, True, "emoji"
        except Exception:
            logger.exception("emoji check failed for user=%s", user_id)

    now_ms = int(time.time() * 1000)
    try:
        redis = get_redis()
        key = _k_pm_rate(user_id)
        async with redis.pipeline(transaction=True) as p:
            p.zremrangebyscore(key, 0, now_ms - ANTI_SPAM_PM_BURST_WINDOW_MS)
            member = f"{now_ms}:{uuid.uuid4().hex}"
            p.zadd(key, {member: now_ms})
            p.zcard(key)
            p.expire(key, 5)
            _, _, count, _ = await p.execute()
        if int(count or 0) > ANTI_SPAM_PM_BURST_LIMIT:
            await _pm_block(user_id, reason="rate")
            await _pm_should_notify(user_id, ANTI_SPAM_BLOCK_TTL)
            return True, True, "rate"
    except Exception:
        logger.exception("rate window check failed for user=%s", user_id)

    return False, False, None

async def pm_block_guard(
    bot,
    t_func: Callable[[int, str], Awaitable[str]],
    *,
    user_id: int,
    chat_id: int,
    text: Optional[str] = None,
) -> bool:
    try:
        blocked, should_notify, reason = await check_and_enforce_pm_limits(user_id, text)
    except Exception:
        logger.exception("pm_block_guard failed for user=%s", user_id)
        return False

    if not blocked:
        return False

    if should_notify:
        try:
            if reason == "length":
                msg = await t_func(user_id, "private.pm_too_long")
                if not msg:
                    msg = f"⚠️ Your message is too long. Max length is {ANTI_SPAM_MAX_TEXT_CHARS} characters."
            else:
                msg = await t_func(user_id, "private.pm_blocked")
                if not msg:
                    msg = "🚫 You are temporarily blocked from sending messages due to spam."
            await bot.send_message(chat_id, msg, parse_mode="HTML")
        except Exception:
            logger.debug("Failed to send block notice to user=%s", user_id, exc_info=True)

    return True