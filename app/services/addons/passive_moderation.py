cat >app/services/addons/passive_moderation.py<< 'EOF'
#app/services/addons/passive_moderation.py

from __future__ import annotations

import logging
import re
import time
import asyncio

from urllib.parse import urlparse
from typing import Literal, List

from redis.exceptions import RedisError

from app.core.memory import load_context, get_redis
from app.config import settings
from app.clients.openai_client import get_openai

logger = logging.getLogger(__name__)

_LIGHT_SEMAPHORE = asyncio.Semaphore(10)
_DEEP_SEMAPHORE = asyncio.Semaphore(3)
MAX_URLS = getattr(settings, "MOD_MAX_URLS", 10)
MAX_PROMPT_TEXT = getattr(settings, "MOD_PROMPT_TEXT_LIMIT", 2000)
DEEP_HISTORY = getattr(settings, "MOD_DEEP_HISTORY", 20)

async def is_flooding(chat_id: int, user_id: int) -> bool:

    redis = get_redis()
    key = f"mod_flood:{chat_id}:{user_id}"
    now_ts = time.time()

    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.lpush(key, now_ts)
            pipe.ltrim(key, 0, settings.MOD_MAX_MESSAGES * 2)
            pipe.expire(key, settings.MOD_PERIOD_SECONDS + 1)
            pipe.lrange(key, 0, settings.MOD_MAX_MESSAGES * 2)
            result = await pipe.execute()
        timestamps = result[-1]
    except RedisError:
        logger.warning("is_flooding: Redis error for chat %s user %s", chat_id, user_id)
        return False
    except asyncio.TimeoutError:
        logger.warning("is_flooding: Redis pipeline timeout for chat %s user %s", chat_id, user_id)
        return False

    threshold = now_ts - settings.MOD_PERIOD_SECONDS
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

    return len(valid) > settings.MOD_MAX_MESSAGES


def extract_urls(text: str, entities: List[dict] | None = None) -> List[str]:

    pattern = r"https?://[\w\-\.\?=/#%]+|www\.[\w\-\.\?=/#%]+"
    urls = [m.group(0).rstrip('.,;!?') for m in re.finditer(pattern, text)]
    if entities:
        for ent in entities:
            if ent.get("type") == "url":
                off, length = ent["offset"], ent["length"]
                snippet = text[off:off + length].rstrip('.,;!?')
                if snippet and snippet not in urls:
                    urls.append(snippet)
    return list(dict.fromkeys(urls))[:MAX_URLS]


def url_is_unwanted(url: str) -> bool:

    try:
        netloc = urlparse(url).netloc.lower().split(':', 1)[0]
    except Exception:
        return True
    for kw in settings.MODERATION_ALLOWED_LINK_KEYWORDS:
        if kw and kw.lower() in netloc:
            return False
    return True


async def moderate_with_openai(text: str) -> bool:

    if not text or not text.strip():
        return False
    trimmed = text[:MAX_PROMPT_TEXT]

    cache_key = f"mod:cache:{hash(text)}"
    redis = get_redis()
    try:
        cached = await redis.get(cache_key)
        if cached is not None:
            return cached == "1"
    except Exception:
        logger.debug("moderate_with_openai: cache lookup failed")

    async with _LIGHT_SEMAPHORE:
        client = get_openai()
        resp = None
        for attempt in range(2):
            try:
                resp = await asyncio.wait_for(
                    client.moderations.create(
                        model=settings.MODERATION_MODEL,
                        input=trimmed,
                    ),
                    timeout=10.0
                )
                break
            except asyncio.TimeoutError:
                logger.warning("moderate_with_openai: timeout, attempt %d", attempt + 1)
                if attempt == 1:
                    return False
            except Exception:
                logger.exception("moderate_with_openai: API error, attempt %d", attempt + 1)
                if attempt == 1:
                    return False

    results = getattr(resp, 'results', None)

    if not results or not isinstance(results, list):
        logger.error("moderate_with_openai: unexpected response %r", resp)
        return False

    result = results[0]
    flagged = bool(getattr(result, "flagged", False))
    try:
        await asyncio.wait_for(
            redis.set(
                cache_key, "1" if flagged else "0",
                ex=settings.MODERATION_CACHE_TTL, 
                nx=True
            ),
            timeout=0.5
        )
    except asyncio.TimeoutError:
        logger.warning("moderate_with_openai: cache write timeout")
    except Exception:
        logger.debug("moderate_with_openai: failed to cache result")

    if flagged:
        return True

    scores = getattr(result, 'category_scores', None)
    items = scores.dict().items() if hasattr(scores, 'dict') else getattr(scores, 'items', lambda: [])()
    for category, score in items:
        if isinstance(score, (int, float)) and score >= settings.MODERATION_TOXICITY_THRESHOLD:
            logger.debug("moderation: flagged by %s=%.2f", category, score)
            try:
                await redis.set(cache_key, "1", ex=settings.MODERATION_CACHE_TTL)
            except Exception:
                pass
            return True

    return False


async def is_promo_via_ai(text: str, urls: List[str]) -> bool:

    if not urls:
        return False
    urls = urls[:MAX_URLS]
    text = text[:MAX_PROMPT_TEXT]

    prompt = (
        "The user sent the following message and URLs:\n\n"
        f"{text}\n\nURLs:\n" + "\n".join(urls) +
        "\n\nRespond with YES if these URLs are promotional or advertising, otherwise respond with NO."
    )

    async with _DEEP_SEMAPHORE:
        client = get_openai()
        try:
            resp = await asyncio.wait_for(
                client.chat.completions.create(
                    model=settings.BASE_MODEL,
                    messages=[
                        {"role": "system", "content": "You are a helpful assistant for moderation."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.0,
                    max_completion_tokens=4,
                ),
                timeout=15.0
            )
        except asyncio.TimeoutError:
            logger.warning("is_promo_via_ai: timeout")
            return False
        except Exception:
            logger.exception("is_promo_via_ai: API error")
            return False

    reply = resp.choices[0].message.content.strip().upper()
    return reply.startswith("YES")


async def check_light(
    chat_id: int,
    user_id: int,
    text: str,
    entities: List[dict] | None = None,
    source: Literal["user", "bot"] = "user"
) -> Literal["clean", "flood", "spam_links", "promo", "toxic"]:

    if not settings.ENABLE_MODERATION or not text:
        return "clean"

    if source == "user" and await is_flooding(chat_id, user_id):
        return "flood"

    urls = extract_urls(text, entities)
    logger.debug("check_light: urls=%r", urls)

    if source == "user" and urls and await is_promo_via_ai(text, urls):
        return "promo"

    if len(urls) > settings.MODERATION_SPAM_LINK_THRESHOLD:
        return "spam_links"

    for u in urls:
        if url_is_unwanted(u):
            return "promo"

    if source == "user" and await moderate_with_openai(text):
        return "toxic"

    return "clean"


async def check_deep(
    chat_id: int,
    user_id: int,
    text: str,
    source: Literal["user", "bot"] = "user"
) -> bool:

    if source != "user":
        return False

    try:
        history = await load_context(chat_id, user_id)
    except Exception:
        logger.exception("check_deep: load_context error for chat %s", chat_id)
        history = []

    snippet = history[-DEEP_HISTORY:]
    prompt_messages: List[dict[str, str]] = [
        {"role": "system", "content": (
            "You are a context-aware moderator. Given recent conversation, "
            "respond ONLY 'BLOCK' if the message is toxic or violates rules, "
            "otherwise respond ONLY 'ALLOW'."
        )},
        *snippet,
        {"role": "user", "content": text},
    ]

    async with _DEEP_SEMAPHORE:
        client = get_openai()
        try:
            resp = await asyncio.wait_for(
                client.chat.completions.create(
                    model=settings.BASE_MODEL,
                    messages=prompt_messages,
                    temperature=0.0,
                    max_completion_tokens=16,
                ),
                timeout=20.0
            )
        except asyncio.TimeoutError:
            logger.warning("check_deep: timeout for chat %s", chat_id)
            return False
        except Exception:
            logger.exception("check_deep: API error for chat %s", chat_id)
            return False

    ans = ""
    try:
        ans = resp.choices[0].message.content.strip().upper()
    except Exception:
        logger.error("check_deep: unexpected response %r", resp)
        return False
    logger.debug("check_deep answer=%r", ans)

    return ans.startswith("BLOCK")
EOF