#app/tasks/queue_worker.py
from __future__ import annotations

import asyncio
import json
import signal
import random
import time
import html
import traceback
import logging
import os
import re
import tempfile

from contextlib import suppress
from typing import Optional, Dict
from collections import defaultdict

from aiogram.enums import ChatAction
from aiogram.types import Message as TgMessage
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter, TelegramNetworkError, TelegramForbiddenError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import settings
from app.bot.utils.debouncer import compute_typing_delay
import app.bot.components.constants as consts
from app.clients.telegram_client import get_bot
from app.clients.openai_client import get_openai
from app.services.responder import respond_to_user
from app.services.addons.voice_generator import (
    maybe_tts_and_send, shutdown_tts,
    will_speak, is_tts_eligible_short
)
from app.services.addons.passive_moderation import split_context_text
from app.services.addons.analytics import record_timeout
from app.core.memory import get_redis, get_redis_queue, close_redis_pools, SafeRedis, push_message
from app.services.user.user_service import confirm_reservation_by_id, refund_reservation_by_id


logger = logging.getLogger(__name__)


class ReplyTerminalError(Exception):
    """Terminal delivery outcome: do not retry/requeue this job."""

BOT = get_bot()

CHATTY_MODE: bool = bool(getattr(settings, "CHATTY_MODE", True))
_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?…])\s+')
_BULLET_LINE_RE = re.compile(r'^\s*(?:[-*•]\s+|\d+[.)]\s+)')
EMOJI_TAIL_RE = re.compile(
    r'^(.*?)(?:\s*)([\U0001F300-\U0001FAFF\U00002700-\U000027BF]+)$'
)
_EMOJI_INLINE_RE = re.compile(
    r'[\U0001F300-\U0001FAFF\U00002700-\U000027BF]'
)
EMOJI_ONLY_RE = re.compile(
    r'^[\U0001F300-\U0001FAFF\U00002700-\U000027BF]+$'
)
INTERJECTION_SPLIT_RE = re.compile(
    r'^(?P<word>Окей|Ок|Да|Нет|Ладно|Ага|Угу|Понял[аи]?|Супер|Круто|Ясно|Верно|Точно|'
    r'Okay|Ok|Yeah|Yep|Yup|Yes|No|Alright|All right|Sure|Right|Gotcha|Got it|'
    r'Cool|Great|Nice|Fine|Understood)'
    r'\b'
    r'(?P<punc>[.!?…]+(?:\s*[\U0001F300-\U0001FAFF\U00002700-\U000027BF]+)*)'
    r'\s+(?P<rest>.+)$',
    re.IGNORECASE,
)


ENABLE_RICH_HTML = bool(getattr(settings, "ENABLE_RICH_HTML", True))
_IS_MENTION_RE = re.compile(r'(?<!\S)@\w+\b')

TG_TEXT_LIMIT: int = int(getattr(settings, "TG_TEXT_LIMIT", 4096))

REDIS_QUEUE: SafeRedis = get_redis_queue()
logger.info("Configured Redis queue at %s", getattr(settings, "REDIS_URL_QUEUE", settings.REDIS_URL))

PROCESSING_TASKS: set[asyncio.Task] = set()
MAX_INFLIGHT_TASKS: int = int(getattr(settings, "WORKER_MAX_INFLIGHT_TASKS", settings.OPENAI_MAX_CONCURRENT_REQUESTS * 2))

chat_locks: Dict[int, asyncio.Lock] = {}
chat_locks_last_used: Dict[int, float] = {}
pending_per_chat: Dict[int, int] = defaultdict(int)
MAX_PENDING_PER_CHAT: int = int(getattr(settings, "MAX_PENDING_PER_CHAT", 15))

CHAT_LOCK_TTL = int(getattr(settings, "CHAT_LOCK_TTL", 3600))
PROCESSING_SWEEP_INTERVAL = int(getattr(settings, "PROCESSING_SWEEP_INTERVAL_SEC", 5))
PROCESSING_SWEEP_BATCH = int(getattr(settings, "PROCESSING_SWEEP_BATCH", 200))
JOB_RECLAIM_TTL = int(getattr(settings, "JOB_RECLAIM_TTL", 120))
TYPING_ENABLED = bool(getattr(settings, "TYPING_ENABLED", True))
TYPING_SKIP_BACKLOG = int(getattr(settings, "TYPING_SKIP_BACKLOG", 30))
TYPING_SKIP_GROUPS = bool(getattr(settings, "TYPING_SKIP_GROUPS", False))

VOICE_TRANSCRIPTION_TIMEOUT = int(getattr(settings, "VOICE_TRANSCRIPTION_TIMEOUT", 90))

TTS_SKIP_BACKLOG = int(getattr(settings, "TTS_SKIP_BACKLOG", 0))
TTS_TIMEOUT_SEC = float(os.environ.get("TTS_TIMEOUT_SEC", "12"))
JOB_KEY_PREFIX = "q:job:"
RESPOND_TIMEOUT = int(getattr(settings, "RESPOND_TIMEOUT", 150))
JOB_PROCESSING_TTL = max(int(getattr(settings, "JOB_PROCESSING_TTL", 0)), RESPOND_TIMEOUT + 30)
TTS_REPLY_TO_VOICE_IN_GROUPS = bool(getattr(
    settings, "TTS_REPLY_TO_VOICE_IN_GROUPS", os.environ.get("TTS_REPLY_TO_VOICE_IN_GROUPS", "0") not in ("0","false","False")))
JOB_DONE_TTL = int(getattr(settings, "JOB_DONE_TTL", 86400))
JOB_HEARTBEAT_INTERVAL = int(getattr(settings, "JOB_HEARTBEAT_INTERVAL", 10))

TG_GLOBAL_RPS = int(getattr(settings, "TG_GLOBAL_RPS", 27))
TG_GLOBAL_BURST = int(getattr(settings, "TG_GLOBAL_BURST", 45))
TG_CHAT_RPS = float(getattr(settings, "TG_CHAT_RPS", 1.0))
TG_CHAT_BURST = int(getattr(settings, "TG_CHAT_BURST", 3))

_TG_BUCKET_LUA = """
local key   = KEYS[1]
local rate  = tonumber(ARGV[1])   -- tokens per second
local burst = tonumber(ARGV[2])   -- bucket size
local now   = tonumber(ARGV[3])   -- ms
local cost  = 1

if not rate or rate <= 0 then
  redis.call('PEXPIRE', key, 1000)
  return 1
end
if not burst or burst <= 0 then
  burst = 1
end

local data  = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1]) or burst
local ts     = tonumber(data[2]) or now
if now > ts then
  local delta = now - ts
  if delta < 0 then delta = 0 end
  tokens = math.min(burst, tokens + (delta * rate / 1000.0))
end

local allowed = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
end
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
local ttl = math.ceil((burst / rate) * 1000)
if ttl < 100 then ttl = 100 end
redis.call('PEXPIRE', key, ttl)
return allowed
"""
_CHAT_BUCKET_LUA = _TG_BUCKET_LUA

def _mk_ctx_payload(role: str, text: str, *, speaker_id: int | None = None) -> str:
    r = (role or "").strip().lower()
    if r not in ("user", "assistant", "system"):
        r = "user"
    t = (text or "").strip()
    payload: dict = {"role": r, "text": t}
    if speaker_id is not None:
        try:
            payload["speaker_id"] = int(speaker_id)
        except Exception:
            pass
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

def _is_effectively_empty(s: str) -> bool:
    t = _IS_MENTION_RE.sub(' ', (s or ''))
    t = re.sub(r'\s+', ' ', t).strip()
    return t == ''


def _is_bullet_list_text(text: str) -> bool:
    lines = [(ln or "").strip() for ln in (text or "").splitlines() if (ln or "").strip()]
    if len(lines) < 2:
        return False

    bullet_lines = sum(1 for ln in lines if _BULLET_LINE_RE.match(ln))
    return bullet_lines >= 2 and bullet_lines == len(lines)


def _split_reply_into_messages(text: str) -> list[str]:

    if not text:
        return []

    chunks: list[str] = []

    for block in text.splitlines():
        block = block.strip()
        if not block:
            continue

        parts = _SENTENCE_SPLIT_RE.split(block)
        for part in parts:
            part = part.strip()
            if not part:
                continue

            tail_match = EMOJI_TAIL_RE.match(part)
            subparts: list[str] = []
            if tail_match:
                head = (tail_match.group(1) or "").strip()
                emojis = (tail_match.group(2) or "").strip()
                if head and emojis:
                    subparts.append(head)
                    subparts.append(emojis)
                else:
                    subparts.append(part)
            else:
                subparts.append(part)

            for sub in subparts:
                sub = sub.strip()
                if not sub:
                    continue

                m = INTERJECTION_SPLIT_RE.match(sub)
                if m:
                    word = m.group("word") or ""
                    punc = m.group("punc") or ""
                    rest = m.group("rest") or ""
                    first = f"{word}{punc}".strip()
                    rest = rest.strip()
                    if rest and not EMOJI_ONLY_RE.fullmatch(rest):
                        if first:
                            chunks.append(first)
                        if rest:
                            chunks.append(rest)
                    else:
                        chunks.append(sub)
                else:
                    chunks.append(sub)
    return chunks


def _classify_chunk(chunk: str) -> tuple[bool, bool, bool]:

    s = (chunk or "").strip()
    if not s:
        return False, False, False

    words = [w for w in s.split() if w]
    is_one_word = (len(words) == 1)

    has_emoji = bool(_EMOJI_INLINE_RE.search(s))

    tail = s.rstrip()
    last_char = tail[-1] if tail else ""

    m = re.search(r'([.!?…]+)$', tail)
    punct_cluster = m.group(1) if m else ""

    is_question = "?" in punct_cluster
    is_excl = "!" in punct_cluster
    is_ellipsis = "…" in punct_cluster or "..." in s

    expressive = False
    if punct_cluster:
        if re.search(r'(\?\?|!!|!\?|!\?|\?!|\?!)', punct_cluster):
            expressive = True

    is_special = (
        is_one_word
        or has_emoji
        or is_question
        or is_excl
        or is_ellipsis
        or expressive
    )

    is_neutral_end = (
        not is_special
        and last_char in ".:;"
    )

    return is_special, is_neutral_end, is_one_word


def _group_chatty_chunks(chunks: list[str]) -> list[str]:

    if not chunks:
        return []

    result: list[str] = []
    n = len(chunks)
    i = 0

    while i < n:
        s = (chunks[i] or "").strip()
        if not s:
            i += 1
            continue

        is_special, is_neutral, is_one_word = _classify_chunk(s)

        if is_one_word:
            run = [s]
            j = i + 1
            while j < n:
                sj = (chunks[j] or "").strip()
                if not sj:
                    j += 1
                    continue
                _, _, one_j = _classify_chunk(sj)
                if not one_j:
                    break
                run.append(sj)
                j += 1

            if len(run) >= 2:
                result.append(" ".join(run))
                i = j
                continue

        if is_special:
            result.append(s)
            i += 1
            continue

        if is_neutral:
            run = [s]
            j = i + 1
            while j < n:
                sj = (chunks[j] or "").strip()
                if not sj:
                    j += 1
                    continue
                sp, np, _ = _classify_chunk(sj)
                if sp or not np:
                    break
                run.append(sj)
                j += 1

            if len(run) >= 2:
                result.append(" ".join(run))
            else:
                result.append(run[0])
            i = j
            continue

        result.append(s)
        i += 1

    return result

def _split_into_two_by_sentences(sentences: list[str]) -> list[str]:
    sents = [(s or "").strip() for s in (sentences or []) if (s or "").strip()]
    n = len(sents)
    if n < 2:
        return [" ".join(sents).strip()] if sents else []

    mid = (n + 1) // 2

    a = " ".join(sents[:mid]).strip()
    b = " ".join(sents[mid:]).strip()

    if not a or not b:
        return [" ".join(sents).strip()]
    return [a, b]


async def _send_chatty_reply(
    chat_id: int,
    text: str,
    reply_to: Optional[int],
    msg_id: Optional[int],
    merged_ids: Optional[list[int]] = None,
    user_id: Optional[int] = None,
    enable_typing: bool = True,
) -> None:

    text = (text or "").strip()
    if not text:
        return
    delivered_first = False

    if len(text) >= 350 or _is_bullet_list_text(text):
        chunks = [text]
    else:
        base_chunks = _split_reply_into_messages(text)
        if not base_chunks:
            return
        chunks = _group_chatty_chunks(base_chunks)

        if len(chunks) == 1 and len(base_chunks) >= 3:
            forced = _split_into_two_by_sentences(base_chunks)
            if len(forced) == 2:
                chunks = forced

    multi_chunk = len(chunks) > 1
    long_single = (len(text) >= 350 and not multi_chunk)

    first_reply_target: Optional[int] = reply_to
    for idx, chunk in enumerate(chunks):
        chunk = (chunk or "").strip()
        if not chunk:
            continue

        if idx == 0:
            if long_single:
                delay = compute_typing_delay(chunk)
                if delay > 0:
                    if enable_typing:
                        await _typing_for_duration(chat_id, _jitter(delay, 0.25))
                    else:
                        await asyncio.sleep(_jitter(delay, 0.25))

            await _send_reply(
                chat_id=chat_id,
                text=chunk,
                reply_to=first_reply_target,
                msg_id=msg_id,
                merged_ids=merged_ids,
                user_id=user_id,
                skip_dedupe=False,
            )
            delivered_first = True
            first_reply_target = None
            continue

        delay = compute_typing_delay(chunk) if multi_chunk else 0.0
        if delay > 0:
            if enable_typing:
                await _typing_for_duration(chat_id, _jitter(delay, 0.25))
            else:
                await asyncio.sleep(_jitter(delay, 0.25))
        
        try:
            await _send_reply(
                chat_id=chat_id,
                text=chunk,
                reply_to=None,
                msg_id=msg_id,
                merged_ids=merged_ids,
                user_id=user_id,
                skip_dedupe=True,
            )
        except ReplyTerminalError:
            if delivered_first:
                logger.info("Terminal delivery outcome for subsequent chatty chunk chat=%s msg_id=%s idx=%s", chat_id, msg_id, idx)
                return
            raise
        except Exception:
            if delivered_first:
                logger.warning("Failed to send subsequent chatty chunk chat=%s msg_id=%s idx=%s", chat_id, msg_id, idx, exc_info=True)
                return
            raise


async def _transcribe_voice_file_id(file_id: str, model: str | None = None) -> str:
    tmp_path = None
    try:
        model = model or getattr(settings, "TRANSCRIPTION_MODEL", "whisper-1")
        f = await asyncio.wait_for(BOT.get_file(file_id), timeout=60)
        with tempfile.NamedTemporaryFile(suffix=".oga", delete=False) as tmp:
            tmp_path = tmp.name
        await asyncio.wait_for(BOT.download(f, tmp_path), timeout=120)
        client = get_openai()

        async def _do_transcribe() -> str:
            with open(tmp_path, "rb") as audio:
                resp = await client.audio.transcriptions.create(
                    model=model,
                    file=audio,
                    response_format="text"
                )
            text_inner = (resp if isinstance(resp, str) else getattr(resp, "text", "")).strip()
            return text_inner

        resp_text = await asyncio.wait_for(_do_transcribe(), timeout=VOICE_TRANSCRIPTION_TIMEOUT)
        return resp_text
    except Exception as e:
        logger.warning("voice transcription failed: %s", e)
        return ""
    finally:
        if tmp_path and os.path.exists(tmp_path):
            with suppress(Exception):
                os.remove(tmp_path)


async def _tg_acquire_permit() -> None:
    key = "ratelimit:tg:global"
    delay = 0.02
    for _ in range(100):
        now_ms = int(time.time() * 1000)
        try:
            ok = int(await REDIS_QUEUE.eval(_TG_BUCKET_LUA, 1, key, TG_GLOBAL_RPS, TG_GLOBAL_BURST, now_ms) or 0)
        except Exception:
            ok = 1
        if ok == 1:
            return
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, 1.0)


async def _tg_acquire_chat_permit(chat_id: int) -> None:
    key = f"ratelimit:tg:chat:{chat_id}"
    delay = 0.02
    for _ in range(50):
        now_ms = int(time.time() * 1000)
        try:
            ok = int(await REDIS_QUEUE.eval(_CHAT_BUCKET_LUA, 1, key, TG_CHAT_RPS, TG_CHAT_BURST, now_ms) or 0)
        except Exception:
            ok = 1
        if ok == 1:
            return
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, 0.5)


def _get_chat_lock(chat_id: int) -> asyncio.Lock:
    chat_locks_last_used[chat_id] = time.time()
    return chat_locks.setdefault(chat_id, asyncio.Lock())


def _jitter(base: float, spread: float = 0.3) -> float:
    try:
        return max(0.0, base * (1.0 + (random.random() * 2 - 1) * spread))
    except Exception:
        return max(0.0, base)


async def _mark_done_if_inflight(redis: Redis, key: str, expected_value: str, ttl: int) -> int:

    script = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
        return redis.call('SET', KEYS[1], 'done', 'EX', tonumber(ARGV[2])) and 1 or 0
    else
        return 0
    end
    """
    try:
        return int(await redis.eval(script, 1, key, expected_value, ttl) or 0)
    except Exception as e:
        logger.warning("mark_done_if_inflight eval failed for %s: %s", key, e)
        return 0


async def _delete_if_inflight(redis: Redis, key: str, expected_value: str) -> int:
    return await _delete_if_value(redis, key, expected_value)


async def _delete_if_value(redis: Redis, key: str, expected_value: str) -> int:
    script = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then 
        return redis.call('DEL', KEYS[1]) 
    else 
        return 0 
    end"""
    try:
        return int(await redis.eval(script, 1, key, expected_value) or 0)
    except Exception:
        return 0


# anchor: chatbusy lock release helper
async def _delete_if_chatbusy_owner(redis: Redis, key: str, expected_value: str) -> int:
    return await _delete_if_value(redis, key, expected_value)


async def _claim_if_reclaimed(redis: Redis, key: str, new_value: str, ttl: int) -> int:

    script = """
    local v = redis.call('GET', KEYS[1])
    if v and string.sub(v, 1, 17) == 'inflight:reclaim:' then
        return redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[2])) and 1 or 0
    else
        return 0
    end
    """
    try:
        return int(await redis.eval(script, 1, key, new_value, ttl) or 0)
    except Exception:
        return 0


async def _typing_for_duration(
    chat_id: int,
    total_duration: float,
    action: ChatAction = ChatAction.TYPING,
) -> None:

    try:
        total_duration = float(total_duration or 0.0)
    except Exception:
        total_duration = 0.0
    if total_duration <= 0:
        return

    if not TYPING_ENABLED:
        await asyncio.sleep(max(0.0, total_duration))
        return

    max_duration = int(getattr(settings, "TYPING_MAX_DURATION_SEC", 60))
    if max_duration > 0:
        total_duration = min(total_duration, max_duration)

    end_ts = time.time() + total_duration
    try:
        while True:
            now = time.time()
            if now >= end_ts:
                break

            try:
                await _tg_acquire_permit()
                await _tg_acquire_chat_permit(chat_id)
                await BOT.send_chat_action(chat_id, action)
                delay = 5.0
            except TelegramRetryAfter as e:
                delay = max(1.0, float(getattr(e, "retry_after", 1)))
            except (TelegramNetworkError, asyncio.TimeoutError):
                delay = _jitter(2.0, 0.5)
            except (TelegramBadRequest, TelegramForbiddenError) as e:
                logger.debug("typing_for_duration stopped for chat_id=%s: %s", chat_id, e)
                break

            remaining = end_ts - time.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(delay, max(0.0, remaining)))
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("typing_for_duration error for chat_id=%s: %s", chat_id, e)


async def _typing_during_generation(
    chat_id: int,
    initial_delay: float = 3,
    max_total: float = RESPOND_TIMEOUT,
) -> None:

    try:
        try:
            initial_delay = float(initial_delay or 0.0)
        except Exception:
            initial_delay = 0.0

        if initial_delay > 0:
            await asyncio.sleep(max(0.0, initial_delay))

        remaining = (max_total or 0) - initial_delay
        if remaining > 0:
            await _typing_for_duration(chat_id, remaining)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("typing_during_generation error for chat_id=%s: %s", chat_id, e)


async def _get_backlog(redis: Redis, queue_key: str, processing_key: str) -> int:
    try:
        qlen, plen = await asyncio.gather(redis.llen(queue_key), redis.llen(processing_key))
        return int(qlen or 0) + int(plen or 0)
    except Exception:
        return 0


async def _heartbeat_inflight(redis: Redis, key: str, expected_value: str, interval: int, ttl: int) -> None:

    script = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
        return redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
    else
        return 0
    end
    """
    try:
        start = time.time()
        while True:
            await asyncio.sleep(interval)
            if time.time() - start > ttl:
                return
            try:
                ok = await redis.eval(script, 1, key, expected_value, ttl)
                if not ok:
                    return
            except Exception as e:
                logger.warning("heartbeat eval failed for %s: %s", key, e)
                return
    except asyncio.CancelledError:
        pass


# anchor: _heartbeat_key
async def _heartbeat_key(redis: Redis, key: str, expected_value: str, interval: int, ttl: int) -> None:
    script = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
        return redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[2]))
    else
        return 0
    end
    """
    try:
        start = time.time()
        while True:
            await asyncio.sleep(interval)
            if time.time() - start > ttl:
                return
            try:
                res = await redis.eval(script, 1, key, expected_value, ttl * 1000)
                if not res:
                    return
            except Exception as e:
                logger.warning("heartbeat key eval failed for %s: %s", key, e)
                return
    except asyncio.CancelledError:
        pass


def _allow_telegram_html(escaped: str) -> str:

    simple_tags = ["b", "strong", "i", "em", "u", "s", "del", "code", "pre", "tg-spoiler"]
    for tag in simple_tags:
        escaped = re.sub(fr"&lt;{tag}&gt;", f"<{tag}>", escaped, flags=re.IGNORECASE)
        escaped = re.sub(fr"&lt;/{tag}&gt;", f"</{tag}>", escaped, flags=re.IGNORECASE)

    def _unescape_a(m):
        url = m.group(1)
        if not re.match(r"^(https?|tg)://[^\s\"'<>]{1,200}$", url, re.I):
            return m.group(0)
        safe_url = html.escape(url, quote=True)
        return f'<a href="{safe_url}">'

    escaped = re.sub(
        r"&lt;a href=(?:&quot;|&#39;)((?:[Hh][Tt][Tt][Pp][Ss]?|[Tt][Gg])://[^\"'<>\s]{1,200})(?:&quot;|&#39;)&gt;",
        _unescape_a,
        escaped,
    )
    escaped = re.sub(r"&lt;/a&gt;", "</a>", escaped, flags=re.IGNORECASE)
    return escaped


async def _send_reply(
    chat_id: int,
    text: str,
    reply_to: Optional[int],
    msg_id: Optional[int],
    merged_ids: Optional[list[int]] = None,
    user_id: Optional[int] = None,
    skip_dedupe: bool = False,
) -> None:
   
    dedupe_set = False
    try:
        if (not skip_dedupe) and (msg_id is not None):
            sent = await REDIS_QUEUE.set(
                f"sent_reply:{chat_id}:{msg_id}", 1, nx=True, ex=JOB_DONE_TTL
            )
            if not sent:
                logger.info("Skip duplicate reply chat=%s msg_id=%s", chat_id, msg_id)
                return
            dedupe_set = True

        if len(text) > TG_TEXT_LIMIT - 10:
            text = text[: TG_TEXT_LIMIT - 10] + "…"

        text_safe = html.escape(text)
        if ENABLE_RICH_HTML:
            text_safe = _allow_telegram_html(text_safe)

        if len(text_safe) > TG_TEXT_LIMIT:
            text_safe = text_safe[: TG_TEXT_LIMIT - 1] + "…"

        kwargs = dict(
            chat_id=chat_id,
            text=text_safe,
            disable_web_page_preview=True,
            allow_sending_without_reply=True,
        )
        if reply_to:
            kwargs["reply_to_message_id"] = reply_to

        async def _send_with_retries(pm: Optional[str], kw: dict, raw_text_for_plain: str) -> TgMessage | None:
            attempts = 3
            removed_reply = False
            for i in range(attempts):
                try:
                    if pm:
                        return await BOT.send_message(parse_mode=pm, **kw)
                    else:
                        return await BOT.send_message(
                            text=raw_text_for_plain,
                            **{k: v for k, v in kw.items() if k != "text"},
                        )
                except TelegramRetryAfter as e:
                    delay = max(1.0, float(getattr(e, "retry_after", 1)))
                    logger.warning("Rate limited (%ss), attempt %d/%d (chat_id=%s)", delay, i+1, attempts, chat_id)
                    await asyncio.sleep(_jitter(delay, 0.25))
                    continue
                except TelegramBadRequest as e:
                    if "reply" in str(e).lower() and "reply_to_message_id" in kw and not removed_reply:
                        kw = dict(kw)
                        kw.pop("reply_to_message_id", None)
                        removed_reply = True
                        continue
                    if pm:
                        logger.warning("HTML send failed: %s — falling back to plain", e)
                        return None
                    raise
                except TelegramForbiddenError as e:
                    logger.info("Forbidden for chat_id=%s, treating delivery as terminal: %s", chat_id, e)
                    try:
                        if user_id is not None and (int(chat_id) == int(user_id)):
                            from app.services.addons.personal_ping import purge_user_state
                            asyncio.create_task(purge_user_state(int(user_id), "blocked bot (reply)"))
                    except Exception:
                        logger.debug("schedule purge_user_state failed", exc_info=True)
                    raise ReplyTerminalError("telegram forbidden") from e
                except (TelegramNetworkError, asyncio.TimeoutError) as e:
                    backoff = _jitter(min(4.0, 2.0 ** i), 0.35)
                    logger.warning("Network error (%s), backoff %ss, attempt %d/%d", e, backoff, i+1, attempts)
                    await asyncio.sleep(backoff)
                    continue
            return None

        await _tg_acquire_permit()
        await _tg_acquire_chat_permit(chat_id)

        plain = text if len(text) <= TG_TEXT_LIMIT - 1 else (text[:TG_TEXT_LIMIT - 1] + "…")
        sent_msg = await _send_with_retries("HTML", dict(kwargs), text) or \
                   await _send_with_retries(None,  dict(kwargs), plain)
        if not sent_msg:
            raise RuntimeError("Message send failed in both HTML and plain modes")

        try:
            assistant_mid = int(getattr(sent_msg, "message_id", 0) or 0)
            if assistant_mid > 0:
                ttl = int(getattr(settings, "REPLY_CONTEXT_TTL_SEC", 86400))
                bot_sid = None
                with suppress(Exception):
                    bot_sid = int(getattr(consts, "BOT_ID", None) or 0) or None
                ctx_payload = _mk_ctx_payload("assistant", text, speaker_id=bot_sid)
                ctx_redis = get_redis()
                await ctx_redis.set(f"msg:{chat_id}:{assistant_mid}", ctx_payload, ex=ttl)
        except Exception:
            logger.debug("Failed to store assistant reply context", exc_info=True)

        try:
            raw_mids = merged_ids if isinstance(merged_ids, (list, tuple)) else []
            mids: list[int] = []
            for mid in raw_mids:
                try:
                    mi = int(mid)
                except Exception:
                    continue
                if msg_id is not None and mi == msg_id:
                    continue
                mids.append(mi)
            mids = mids[:200]
            if mids:
                async with REDIS_QUEUE.pipeline() as p:
                    for mid in mids:
                        p.set(f"sent_reply:{chat_id}:{mid}", 1, nx=True, ex=JOB_DONE_TTL)
                    await p.execute()
        except Exception as e:
            logger.warning("failed to mark merged sent_reply keys: %s", e)
    except ReplyTerminalError:
        raise
    except Exception as e:
        logger.error(
            "Failed to send message to chat_id=%s (reply_to=%s): %s",
            chat_id, reply_to, e,
        )
        if msg_id is not None and dedupe_set:
            with suppress(Exception):
                await REDIS_QUEUE.delete(f"sent_reply:{chat_id}:{msg_id}")
        logger.debug(traceback.format_exc())
        raise


async def _mark_sent_reply_keys(chat_id: int, msg_id: int | None, merged_ids: list[int] | None) -> None:
    if msg_id is None:
        return
    try:
        await REDIS_QUEUE.set(f"sent_reply:{chat_id}:{msg_id}", 1, nx=True, ex=JOB_DONE_TTL)
    except Exception:
        pass
    try:
        raw_mids = merged_ids if isinstance(merged_ids, (list, tuple)) else []
        mids: list[int] = []
        for mid in raw_mids:
            try:
                mi = int(mid)
                if mi != msg_id:
                    mids.append(mi)
            except Exception:
                continue
        if mids:
            async with REDIS_QUEUE.pipeline() as p:
                for mi in mids[:200]:
                    p.set(f"sent_reply:{chat_id}:{mi}", 1, nx=True, ex=JOB_DONE_TTL)
                await p.execute()
    except Exception:
        pass


def _register_task(chat_id: Optional[int], t: asyncio.Task) -> None:
    if chat_id is not None:
        pending_per_chat[chat_id] += 1
    def _done(_t: asyncio.Task, _chat_id=chat_id) -> None:
        PROCESSING_TASKS.discard(_t)
        if _chat_id is not None:
            pending_per_chat[_chat_id] = max(0, pending_per_chat[_chat_id] - 1)
            if pending_per_chat[_chat_id] == 0:
                pending_per_chat.pop(_chat_id, None)
    t.add_done_callback(_done)


async def _try_start_task_or_requeue(raw, queue_key: str, processing_key: str) -> bool:
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except Exception:
            logger.error("Failed to decode queue item (bytes); dropping from processing")
            with suppress(Exception):
                await REDIS_QUEUE.lrem(processing_key, 1, raw)
            return False

    chat_id: Optional[int] = None
    try:
        job = json.loads(raw)
        chat_id = int(job.get("chat_id"))
    except Exception:
        chat_id = None
    if chat_id is not None and pending_per_chat.get(chat_id, 0) >= MAX_PENDING_PER_CHAT:
        try:
            async with REDIS_QUEUE.pipeline() as p:
                p.lrem(processing_key, 1, raw)
                p.lpush(queue_key, raw)
                await p.execute()
            logger.debug("Requeued (cap-per-chat) chat_id=%s back to %s", chat_id, queue_key)
        except Exception as e:
            logger.warning("Failed to requeue (cap-per-chat) chat_id=%s: %s", chat_id, e)
        return False
    try:
        t = asyncio.create_task(handle_job(raw, processing_key))
    except Exception:
        if chat_id is not None:
            await REDIS_QUEUE.delete(f"chatbusy:{chat_id}")
            pending_per_chat.pop(chat_id, None)
        raise

    PROCESSING_TASKS.add(t)
    _register_task(chat_id, t)
    return True


# anchor: handle_job
async def handle_job(raw, processing_key: str) -> None:

    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except Exception:
            logger.error("Invalid queue payload (bytes)") 
            with suppress(Exception):
                await REDIS_QUEUE.lrem(processing_key, 1, raw)
            return
    
    redis = get_redis()
    queue_key = settings.QUEUE_KEY

    try:
        job = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("Invalid JSON from queue: %s", raw)

        try:
            await REDIS_QUEUE.lrem(processing_key, 1, raw)
        except Exception as exc:
            logger.warning("Failed to lrem invalid job: %s", exc)
        return

    chat_id    = job.get("chat_id")
    text       = (job.get("text") or "").strip()
    user_id    = job.get("user_id")
    reply_to   = job.get("reply_to")
    is_group   = job.get("is_group", False)
    is_channel = job.get("is_channel_post", False)
    chan_title = job.get("channel_title")
    msg_id     = job.get("msg_id")
    voice_in   = bool(job.get("voice_in"))
    voice_file_id = job.get("voice_file_id")
    merged_ids = job.get("merged_msg_ids")
    image_b64  = job.get("image_b64")
    image_mime = job.get("image_mime")
    trigger    = job.get("trigger")  # 'mention' | 'check_on_topic' | 'channel_post'
    enforce_on_topic = bool(job.get("enforce_on_topic", False))
    allow_web  = bool(job.get("allow_web", False))
    billing_tier = job.get("billing_tier")
    if isinstance(billing_tier, (bytes, bytearray)):
        billing_tier = billing_tier.decode("utf-8", "ignore")
    if billing_tier is not None and not isinstance(billing_tier, str):
        billing_tier = str(billing_tier)
    billing_tier = (billing_tier or "").strip().lower() or None
    if billing_tier not in ("paid", "free", "none"):
        billing_tier = None
    entities   = job.get("entities") or []
    reservation_id = job.get("reservation_id")
    try:
        reservation_id = int(reservation_id) if reservation_id is not None else 0
    except Exception:
        reservation_id = 0

    async def _confirm_reservation() -> None:
        if reservation_id <= 0:
            return
        try:
            await confirm_reservation_by_id(reservation_id)
        except Exception:
            logger.exception("Failed to confirm reservation_id=%s", reservation_id)

    async def _refund_reservation() -> None:
        if reservation_id <= 0:
            return
        try:
            await refund_reservation_by_id(reservation_id)
        except Exception:
            logger.exception("Failed to refund reservation_id=%s", reservation_id)

    try:
        msg_id = int(msg_id) if msg_id is not None else None
    except Exception:
        msg_id = None

    try:
        reply_to = int(reply_to) if reply_to is not None else None
    except Exception:
        reply_to = None

    has_tg_reply_to = ("tg_reply_to" in job)
    tg_reply_to_raw = job.get("tg_reply_to")
    try:
        tg_reply_to = int(tg_reply_to_raw) if tg_reply_to_raw is not None else 0
    except Exception:
        tg_reply_to = 0

    pm_chat = (chat_id == user_id)
    reply_target = reply_to if pm_chat else msg_id  # keep for context logic

    if has_tg_reply_to:
        send_reply_target: Optional[int] = (tg_reply_to if tg_reply_to > 0 else None)
    else:
        send_reply_target = reply_target

    if not (isinstance(chat_id, int) and isinstance(user_id, int) and (text is not None or image_b64 or voice_file_id)):
        logger.error(
            "Skipping job with missing fields: chat_id=%s user_id=%s text_len=%d has_image=%s",
            chat_id, user_id, len(text or "") if isinstance(text, str) else -1, bool(image_b64),
        )
        await REDIS_QUEUE.lrem(processing_key, 1, raw)
        await _refund_reservation()
        return

    if msg_id is None:
        logger.error(
            "Dropping job without msg_id (chat=%s user=%s): text=%r",
            chat_id, user_id, text
        )
        with suppress(Exception):
            await REDIS_QUEUE.lrem(processing_key, 1, raw)
        await _refund_reservation()
        return

    try:
        sent_key = f"sent_reply:{chat_id}:{msg_id}"
        if await REDIS_QUEUE.get(sent_key):
            with suppress(Exception):
                await REDIS_QUEUE.lrem(processing_key, 1, raw)
            logger.debug("Drop job as already answered: chat=%s msg_id=%s", chat_id, msg_id)
            await _confirm_reservation()
            return
    except Exception:
        pass

    dedupe_id = f"{chat_id}:{msg_id}"
    job_key = JOB_KEY_PREFIX + dedupe_id
    if trigger:
        logger.debug("queue_worker picked job %s trigger=%s", dedupe_id, trigger)

    token = f"{os.getpid()}:{id(asyncio.current_task())}:{time.time():.3f}"
    remove_from_processing = True
    value = f"inflight:{token}"
    try:
        acquired = await REDIS_QUEUE.set(job_key, value, ex=JOB_PROCESSING_TTL, nx=True)
    except Exception as exc:
        logger.warning("Failed to set inflight key %s: %s", job_key, exc)
        acquired = False

    if not acquired:
        try:
            val = await REDIS_QUEUE.get(job_key)
        except Exception:
            val = None

        if isinstance(val, str) and val.startswith("inflight:reclaim:"):
            claimed = await _claim_if_reclaimed(REDIS_QUEUE, job_key, value, JOB_PROCESSING_TTL)
            if claimed:
                acquired = True

        if not acquired:
            if isinstance(val, str) and val.startswith("done"):
                with suppress(Exception):
                    await REDIS_QUEUE.lrem(processing_key, 1, raw)
                logger.debug("Drop duplicate: already done %s", dedupe_id)
            elif isinstance(val, str) and val.startswith("inflight:"):
                with suppress(Exception):
                    await REDIS_QUEUE.lrem(processing_key, 1, raw)
                logger.debug("Drop duplicate: already inflight %s (removed from processing)", dedupe_id)
            else:
                logger.debug("Defer job %s: job_key is None/absent; keep in :processing for sweeper", dedupe_id)
            return


    lock_started: float | None = None
    busy_key = f"chatbusy:{chat_id}"
    busy_token = f"busy:{os.getpid()}:{id(asyncio.current_task())}:{time.time():.3f}"
    if not await REDIS_QUEUE.set(busy_key, busy_token, nx=True, ex=JOB_PROCESSING_TTL):
        await _delete_if_inflight(REDIS_QUEUE, job_key, value)
        await REDIS_QUEUE.lrem(processing_key, 1, raw)
        await REDIS_QUEUE.lpush(queue_key, raw)
        return

    busy_hb: Optional[asyncio.Task] = None
    lock = _get_chat_lock(chat_id)

    async with lock:
        lock_started = time.monotonic()
        busy_hb = asyncio.create_task(
            _heartbeat_key(
                REDIS_QUEUE,
                busy_key,
                busy_token,
                JOB_HEARTBEAT_INTERVAL,
                JOB_PROCESSING_TTL,
            )
        )
        hb_task = asyncio.create_task(
            _heartbeat_inflight(REDIS_QUEUE, job_key, value, JOB_HEARTBEAT_INTERVAL, JOB_PROCESSING_TTL)
        )
        try:
            backlog = 0
            if TYPING_ENABLED and not (TYPING_SKIP_GROUPS and (is_group or is_channel)):
                try:
                    backlog = await _get_backlog(REDIS_QUEUE, settings.QUEUE_KEY, processing_key)
                except Exception:
                    backlog = 0


            try:
                pref = await REDIS_QUEUE.get(f"tts:pref:{user_id}") or ""
                if isinstance(pref, (bytes, bytearray)):
                    pref = pref.decode()
                pref = pref.strip().lower()
            except Exception:
                pref = ""

            base_expect_voice = True if pref == "always" else (False if pref == "never" else will_speak(voice_in=voice_in))
            chat_voice_disabled = bool(await REDIS_QUEUE.get(f"vmsg:disabled:chat:{chat_id}") or 0)
            expect_voice_out_flag = base_expect_voice and (not chat_voice_disabled)

            if is_channel or (not (pm_chat or voice_in)) or (is_group and voice_in and not TTS_REPLY_TO_VOICE_IN_GROUPS):
                expect_voice_out_flag = False

            if (TTS_SKIP_BACKLOG > 0) and (backlog > TTS_SKIP_BACKLOG) and not voice_in:
                expect_voice_out_flag = False

            allow_typing_before_send = (
                TYPING_ENABLED
                and not (TYPING_SKIP_GROUPS and (is_group or is_channel))
                and not expect_voice_out_flag
                and ((TYPING_SKIP_BACKLOG <= 0) or (backlog <= TYPING_SKIP_BACKLOG))
                and not (is_group and ((trigger == "check_on_topic") or enforce_on_topic))
            )

            typing_task: Optional[asyncio.Task] = None
            if allow_typing_before_send:
                typing_task = asyncio.create_task(
                    _typing_during_generation(
                        chat_id=chat_id,
                        initial_delay=3,
                        max_total=RESPOND_TIMEOUT,
                    )
                )

            if (not isinstance(text, str) or not text.strip()) and voice_file_id:
                text = await _transcribe_voice_file_id(
                    voice_file_id,
                    getattr(settings, "TRANSCRIPTION_MODEL", "whisper-1"),
                )
                if not text:
                    if pm_chat or (trigger == "mention") or is_channel:
                        await _send_reply(
                            chat_id,
                            "⚠️ Voice recognition failed. Please try again.",
                            send_reply_target,
                            msg_id,
                            merged_ids,
                            user_id=user_id,
                        )
                        await _confirm_reservation()
                    with suppress(Exception):
                        await _mark_done_if_inflight(REDIS_QUEUE, job_key, value, JOB_DONE_TTL)
                    return

            if image_b64:
                cap = (text or "").strip()
                tagged = "[Image]" + (f" {cap}" if cap else "")
                with suppress(Exception):
                    await redis.set(
                        f"msg:{chat_id}:{msg_id}",
                        _mk_ctx_payload("user", tagged, speaker_id=int(user_id)),
                        ex=int(getattr(settings, "REPLY_CONTEXT_TTL_SEC", 86400)),
                    )

            if voice_in and isinstance(text, str) and text.strip():
                tagged = f"[Voice→Text] {text.strip()}"
                with suppress(Exception):
                    await redis.set(
                        f"msg:{chat_id}:{msg_id}",
                        _mk_ctx_payload("user", tagged, speaker_id=int(user_id)),
                        ex=int(getattr(settings, "REPLY_CONTEXT_TTL_SEC", 86400)),
                    )

            if isinstance(text, str):
                try:
                    text, _ = split_context_text(text, entities, allow_web=allow_web)
                except Exception:
                    pass

            if is_group and (not is_channel) and (trigger in ("mention", "check_on_topic")):
                if _is_effectively_empty(text or ""):
                    await _mark_done_if_inflight(REDIS_QUEUE, job_key, value, JOB_DONE_TTL)
                    return

            soft_reply_context = bool(job.get("soft_reply_context"))
            
            resp_task = asyncio.create_task(
                respond_to_user(
                    text, chat_id, user_id,
                    trigger=trigger,
                    group_mode=is_group,
                    is_channel_post=is_channel,
                    channel_title=chan_title,
                    reply_to=reply_to,
                    msg_id=msg_id,
                    voice_in=voice_in,
                    image_b64=image_b64,
                    image_mime=image_mime,
                    allow_web=allow_web,
                    enforce_on_topic=enforce_on_topic,
                    expect_voice_out=expect_voice_out_flag,
                    billing_tier=billing_tier,
                    persona_owner_id=None,
                    memory_uid=None,
                    soft_reply_context=soft_reply_context,
                )
            )

            resp_task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
            
            try:
                reply_text = await asyncio.wait_for(resp_task, timeout=RESPOND_TIMEOUT)
            except asyncio.TimeoutError:
                logger.error(
                    "respond_to_user timeout after %ss (chat=%s user=%s)",
                    RESPOND_TIMEOUT, chat_id, user_id
                )
                resp_task.cancel()
                with suppress(Exception):
                    await asyncio.wait({resp_task}, timeout=2)
                reply_text = (
                    "⏳ Sorry, I was thinking longer than usual. "
                    "Try asking the question again."
                )
                with suppress(Exception):
                    await record_timeout(chat_id)
            finally:
                if typing_task is not None:
                    typing_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await typing_task

            if is_group and ((trigger == "check_on_topic") or enforce_on_topic):
                rt = (reply_text or "").strip()
                if (rt == "" or rt == "…" or rt.startswith("⏳") or "something went wrong" in rt.lower()):
                    with suppress(Exception):
                        await _mark_done_if_inflight(REDIS_QUEUE, job_key, value, JOB_DONE_TTL)
                    return

            reply_text = (reply_text or "").strip() or "Sorry, I’ve got nothing to add 😅"
            try:
                eligible_short = is_tts_eligible_short(reply_text)

                want_caption = bool(getattr(settings, "TTS_VOICE_CAPTION_ENABLED",
                                   os.environ.get("TTS_VOICE_CAPTION_ENABLED", "0") not in ("0","false","False")))
                caption_len = int(getattr(settings, "TTS_VOICE_CAPTION_LEN",
                                   os.environ.get("TTS_VOICE_CAPTION_LEN", 160)))

                sent_voice = False
                if expect_voice_out_flag and eligible_short:
                    effective_voice_in = voice_in and (not is_group or TTS_REPLY_TO_VOICE_IN_GROUPS)
                    try:
                        sent_voice = await asyncio.wait_for(
                            maybe_tts_and_send(
                                chat_id=chat_id,
                                user_id=user_id,
                                reply_text=reply_text,
                                voice_in=effective_voice_in,
                                force=True,
                                reply_to=send_reply_target,
                                exclusive=not want_caption,
                                caption_max=(caption_len if want_caption else 0),
                                user_text_hint=text,
                            ),
                            timeout=TTS_TIMEOUT_SEC,
                        )
                    except asyncio.TimeoutError:
                        logger.warning("TTS timed out for chat_id=%s; falling back to text", chat_id)
                        sent_voice = False
                    except Exception as e:
                        logger.warning("TTS error: %s; falling back to text", e)
                        sent_voice = False
                if sent_voice:
                    await _mark_sent_reply_keys(chat_id, msg_id, merged_ids)
                    with suppress(Exception):
                        await _mark_done_if_inflight(REDIS_QUEUE, job_key, value, JOB_DONE_TTL)
                    await _confirm_reservation()
                    try:
                        await push_message(
                            chat_id,
                            "system",
                            "[Delivery] The previous assistant reply was sent as a voice message.",
                            user_id=user_id,
                        )
                    except Exception:
                        logger.debug("Failed to push voice-delivery meta", exc_info=True)
                    try:
                        await REDIS_QUEUE.set(f"last_out_mode:{chat_id}:{user_id}", "voice", ex=3600)
                    except Exception:
                        pass
                else:
                    if CHATTY_MODE:
                        await _send_chatty_reply(
                            chat_id=chat_id,
                            text=reply_text,
                            reply_to=send_reply_target,
                            msg_id=msg_id,
                            merged_ids=merged_ids,
                            user_id=user_id,
                            enable_typing=allow_typing_before_send,
                        )
                    else:
                        if len(reply_text) >= 350 and allow_typing_before_send:
                            delay = compute_typing_delay(reply_text)
                            if delay > 0:
                                await _typing_for_duration(chat_id, _jitter(delay, 0.25))

                        await _send_reply(
                            chat_id=chat_id,
                            text=reply_text,
                            reply_to=send_reply_target,
                            msg_id=msg_id,
                            merged_ids=merged_ids,
                            user_id=user_id,
                        )


                    with suppress(Exception):
                        await _mark_done_if_inflight(REDIS_QUEUE, job_key, value, JOB_DONE_TTL)
                    await _confirm_reservation()
                    try:
                        await REDIS_QUEUE.set(f"last_out_mode:{chat_id}:{user_id}", "text", ex=3600)
                    except Exception:
                        pass
            except ReplyTerminalError:
                with suppress(Exception):
                    await _mark_done_if_inflight(REDIS_QUEUE, job_key, value, JOB_DONE_TTL)
                await _refund_reservation()
                return
            except Exception:
                with suppress(Exception):
                    await _delete_if_inflight(REDIS_QUEUE, job_key, value)
                requeue_guard_key = f"{job_key}:requeued"
                try:
                    did_set = await REDIS_QUEUE.set(requeue_guard_key, 1, ex=300, nx=True)
                except Exception:
                    did_set = False
                if did_set:
                    try:
                        async with REDIS_QUEUE.pipeline() as p:
                            p.lrem(processing_key, 1, raw)
                            p.lpush(queue_key, raw)
                            await p.execute()
                        logger.warning("Requeued job after send failure %s", dedupe_id)
                        remove_from_processing = False
                        return
                    except Exception as ex:
                        logger.error("Failed to requeue after send failure: %s", ex)
                with suppress(Exception):
                    await _mark_done_if_inflight(REDIS_QUEUE, job_key, value, JOB_DONE_TTL)
                await _refund_reservation()
                logger.warning(
                    "Dropped job after send failure: requeue guard exhausted",
                    extra={
                        "chat_id": chat_id,
                        "msg_id": msg_id,
                        "reservation_id": reservation_id,
                        "dedupe_id": dedupe_id,
                    },
                )
                return
        except asyncio.CancelledError:
            remove_from_processing = False
            with suppress(Exception):
                await _delete_if_inflight(REDIS_QUEUE, job_key, value)
            raise
        except ReplyTerminalError:
            with suppress(Exception):
                await _mark_done_if_inflight(REDIS_QUEUE, job_key, value, JOB_DONE_TTL)
            await _refund_reservation()
            return
        except Exception as e:
            logger.error(
                "respond_to_user failed/timeout chat=%s user=%s: %s",
                chat_id, user_id, e
            )
            reply_text = (
                "⏳ Sorry, I was thinking longer than usual. "
                "Try asking the question again."
            )
            if is_group and ((trigger == "check_on_topic") or enforce_on_topic):
                with suppress(Exception):
                    await _mark_done_if_inflight(REDIS_QUEUE, job_key, value, JOB_DONE_TTL)
                return
            try:
                await _send_reply(chat_id, reply_text, send_reply_target, msg_id, merged_ids, user_id=user_id)
                with suppress(Exception):
                    await _mark_done_if_inflight(REDIS_QUEUE, job_key, value, JOB_DONE_TTL)
                await _confirm_reservation()
            except ReplyTerminalError:
                with suppress(Exception):
                    await _mark_done_if_inflight(REDIS_QUEUE, job_key, value, JOB_DONE_TTL)
                await _refund_reservation()
                return
            except Exception:
                with suppress(Exception):
                    await _delete_if_inflight(REDIS_QUEUE, job_key, value)
                requeue_guard_key = f"{job_key}:requeued"
                try:
                    did_set = await REDIS_QUEUE.set(requeue_guard_key, 1, ex=300, nx=True)
                except Exception:
                    did_set = False
                if did_set:
                    try:
                        async with REDIS_QUEUE.pipeline() as p:
                            p.lrem(processing_key, 1, raw)
                            p.lpush(queue_key, raw)
                            await p.execute()
                        logger.warning("Requeued job after fallback send failure %s", dedupe_id)
                        remove_from_processing = False
                        return
                    except Exception as ex:
                        logger.error("Failed to requeue after fallback send failure: %s", ex)
                with suppress(Exception):
                    await _mark_done_if_inflight(REDIS_QUEUE, job_key, value, JOB_DONE_TTL)
                await _refund_reservation()
                logger.warning(
                    "Dropped job after fallback send failure: requeue guard exhausted",
                    extra={
                        "chat_id": chat_id,
                        "msg_id": msg_id,
                        "reservation_id": reservation_id,
                        "dedupe_id": dedupe_id,
                    },
                )
                return
        finally:
            hb_task.cancel()
            with suppress(asyncio.CancelledError):
                await hb_task
            if busy_hb:
                busy_hb.cancel()
                with suppress(asyncio.CancelledError):
                    await busy_hb

            if remove_from_processing:
                try:
                    await REDIS_QUEUE.lrem(processing_key, 1, raw)
                except Exception as exc:
                    logger.warning("Failed to lrem processed job: %s", exc)

            if lock_started is not None:
                hold = time.monotonic() - lock_started
                if hold > (RESPOND_TIMEOUT + 30):
                    logger.warning(
                        "Chat lock held too long: %.2fs (chat_id=%s)",
                        hold,
                        chat_id,
                    )

            if busy_key:
                await _delete_if_chatbusy_owner(REDIS_QUEUE, busy_key, busy_token)


async def _sweep_processing(redis: Redis, queue_key: str, processing_key: str, batch: int) -> None:
    try:
        plen = await redis.llen(processing_key)
        if not plen:
            return
        start = max(0, plen - batch)
        items = await redis.lrange(processing_key, start, -1)
        if not items:
            return
        for raw in items:
            if isinstance(raw, (bytes, bytearray)):
                try:
                    raw = raw.decode("utf-8")
                except Exception:
                    with suppress(Exception):
                        await redis.lrem(processing_key, 1, raw)
                    continue
            try:
                job = json.loads(raw)
                chat_id = int(job.get("chat_id"))
                msg_id = int(job.get("msg_id"))
                dedupe_id = f"{chat_id}:{msg_id}"
                job_key = JOB_KEY_PREFIX + dedupe_id
            except Exception:
                with suppress(Exception):
                    await redis.lrem(processing_key, 1, raw)
                continue

            try:
                val = await redis.get(job_key)
            except Exception:
                val = None

            busy_key = f"chatbusy:{chat_id}"
            try:
                chat_is_busy = bool(await redis.exists(busy_key))
            except Exception:
                chat_is_busy = False

            if chat_is_busy:
                continue

            if not val:
                token = f"reclaim:{os.getpid()}:{time.time():.3f}"
                try:
                    ok = await redis.set(job_key, f"inflight:reclaim:{token}", ex=JOB_RECLAIM_TTL, nx=True)
                except Exception:
                    ok = False
                if not ok:
                    continue
                try:
                    removed = await redis.lrem(processing_key, 1, raw)
                    if removed:
                        await redis.rpush(queue_key, raw)
                        logger.info("Reclaimed stuck job %s → %s", dedupe_id, queue_key)
                except Exception as e:
                    logger.warning("Failed to reclaim %s: %s", dedupe_id, e)
            elif isinstance(val, str) and val.startswith("done"):
                with suppress(Exception):
                    await redis.lrem(processing_key, 1, raw)
            else:
                continue
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.warning("sweep_processing error: %s", e)


async def _sweep_chatbusy(redis: Redis) -> None:
    async for key in redis.scan_iter(match="chatbusy:*", count=500):
        ttl_ms = await redis.pttl(key)
        if ttl_ms > 0:
            continue
        try:
            chat_id = int(key.split(":")[1])
        except Exception:
            await redis.delete(key)
            continue

        has_inflight = False
        async for job_key in redis.scan_iter(match=f"q:job:{chat_id}:*", count=100):
            try:
                val = await redis.get(job_key)
            except Exception:
                val = None

            if isinstance(val, bytes):
                with suppress(Exception):
                    val = val.decode("utf-8", "ignore")

            if isinstance(val, str) and val.startswith("inflight:"):
                has_inflight = True
                break

        if not has_inflight:
            await redis.delete(key)


async def _sweeper_loop(stop_evt: asyncio.Event, queue_key: str, processing_key: str) -> None:
    try:
        while not stop_evt.is_set():
            await _sweep_processing(
                REDIS_QUEUE, queue_key, processing_key, PROCESSING_SWEEP_BATCH
            )
            await _sweep_chatbusy(REDIS_QUEUE)
            await asyncio.sleep(_jitter(PROCESSING_SWEEP_INTERVAL, 0.1))
    except asyncio.CancelledError:
        pass


async def _cleanup_chat_locks_loop(stop_evt: asyncio.Event) -> None:
    try:
        while not stop_evt.is_set():
            await asyncio.sleep(60)
            now = time.time()
            stale = [cid for cid, ts in list(chat_locks_last_used.items())
                     if (now - ts) > CHAT_LOCK_TTL and pending_per_chat.get(cid, 0) == 0]
            for cid in stale:
                lock = chat_locks.get(cid)
                if lock and not lock.locked():
                    chat_locks.pop(cid, None)
                    chat_locks_last_used.pop(cid, None)
    except asyncio.CancelledError:
        pass


async def queue_worker(stop_evt: asyncio.Event) -> None:

    global REDIS_QUEUE
    queue_key      = settings.QUEUE_KEY
    processing_key = queue_key + ":processing"

    requeue_lock_key = f"{processing_key}:requeue_lock"
    try:
        if await REDIS_QUEUE.set(requeue_lock_key, os.getpid(), nx=True, ex=60):
            pending = await REDIS_QUEUE.lrange(processing_key, 0, -1)
            if pending:
                await REDIS_QUEUE.rpush(queue_key, *pending)
                await REDIS_QUEUE.delete(processing_key)
            logger.info("Requeue on start done by pid=%s", os.getpid())
        else:
            logger.info("Skip requeue on start (another worker holds the lock)")
    except Exception as e:
        logger.warning("Requeue-on-start skipped: %s", e)
    logger.info("Starting queue_worker on Redis key '%s'", queue_key)

    sweeper = asyncio.create_task(_sweeper_loop(stop_evt, queue_key, processing_key))
    try:
        while not stop_evt.is_set():
            try:
                while (len(PROCESSING_TASKS) >= MAX_INFLIGHT_TASKS) and (not stop_evt.is_set()):
                    if PROCESSING_TASKS:
                        done, _ = await asyncio.wait(
                            PROCESSING_TASKS, return_when=asyncio.FIRST_COMPLETED, timeout=1
                        )
                    else:
                        await asyncio.sleep(0.2)
                raw = await REDIS_QUEUE.brpoplpush(queue_key, processing_key, timeout=1)
                if stop_evt.is_set():
                    break
                if not raw:
                    continue

                logger.debug("BRPOPLPUSH → %r", raw)
                await _try_start_task_or_requeue(raw, queue_key, processing_key)

            except RedisError as e:
                logger.error("RedisError in queue_worker: %s — reconnecting", e)
                with suppress(Exception):
                    await close_redis_pools()
                await asyncio.sleep(_jitter(1.0, 0.5))
                try:
                    REDIS_QUEUE = get_redis_queue()
                except Exception as ex:
                    logger.critical("Failed to recreate Redis client: %s", ex)
                    await asyncio.sleep(_jitter(5.0, 0.5))

            except asyncio.CancelledError:
                logger.info("queue_worker received shutdown signal")
                break

            except Exception as e:
                logger.exception("Unexpected error in queue_worker: %s", e)
                await asyncio.sleep(1)
    finally:
        sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await sweeper


async def _async_main() -> None:

    stop_evt = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_evt.set)

    worker = asyncio.create_task(queue_worker(stop_evt))
    cleanup_task = asyncio.create_task(_cleanup_chat_locks_loop(stop_evt))
    logger.info("queue_worker task launched, entering event loop")

    await stop_evt.wait()
    logger.info("Shutdown signal received → cancelling worker")

    worker.cancel()
    with suppress(asyncio.CancelledError):
        await worker

    cleanup_task.cancel()
    with suppress(asyncio.CancelledError):
        await cleanup_task

    try:
        if PROCESSING_TASKS:
            logger.info("Waiting for %d in-flight job(s) to finish...", len(PROCESSING_TASKS))
            done, pending = await asyncio.wait(PROCESSING_TASKS, timeout=15)
            if pending:
                logger.info("Cancelling %d stuck job(s)...", len(pending))
                for t in list(pending):
                    t.cancel()
                with suppress(asyncio.CancelledError):
                    await asyncio.gather(*pending)
    except Exception as e:
        logger.warning("Error while draining tasks on shutdown: %s", e)

    with suppress(Exception):
        await BOT.session.close()

    try:
        await shutdown_tts()
    except Exception:
        pass
    with suppress(Exception):
        await close_redis_pools()
    logger.info("Redis connections closed, bye!")


def main() -> None:
    level = os.environ.get("QUEUE_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s:%(lineno)d  %(message)s",
        force=True,
    )
    asyncio.run(_async_main())

if __name__ == "__main__":
    main()
