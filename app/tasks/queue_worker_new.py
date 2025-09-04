cat >app/tasks/queue_worker.py<< 'EOF'
# app/tasks/queue_worker.py
import asyncio
import json
import signal
import random
import sys
import time
import html
import traceback
import logging
import os
import re

from contextlib import suppress
from typing import Optional, Dict
from collections import defaultdict

from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter, TelegramNetworkError, TelegramForbiddenError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import settings
from app.clients.telegram_client import get_bot
from app.services.responder import respond_to_user
from app.core.memory import get_redis_queue, close_redis_pools, SafeRedis


logger = logging.getLogger(__name__)


TG_TEXT_LIMIT: int = int(getattr(settings, "TG_TEXT_LIMIT", 4096))

REDIS_QUEUE: SafeRedis = get_redis_queue()
logger.info("Configured Redis queue at %s", getattr(settings, "REDIS_URL_QUEUE", settings.REDIS_URL))

BOT = get_bot()

PROCESSING_TASKS: set[asyncio.Task] = set()
MAX_INFLIGHT_TASKS: int = int(getattr(settings, "WORKER_MAX_INFLIGHT_TASKS", settings.OPENAI_MAX_CONCURRENT_REQUESTS * 2))
openai_sem = asyncio.Semaphore(settings.OPENAI_MAX_CONCURRENT_REQUESTS)
chat_locks: Dict[int, asyncio.Lock] = {}
chat_locks_last_used: Dict[int, float] = {}
pending_per_chat: Dict[int, int] = defaultdict(int)
MAX_PENDING_PER_CHAT: int = int(getattr(settings, "MAX_PENDING_PER_CHAT", 3))

CHAT_LOCK_TTL = int(getattr(settings, "CHAT_LOCK_TTL", 3600))
PROCESSING_SWEEP_INTERVAL = int(getattr(settings, "PROCESSING_SWEEP_INTERVAL_SEC", 5))
PROCESSING_SWEEP_BATCH = int(getattr(settings, "PROCESSING_SWEEP_BATCH", 200))
JOB_RECLAIM_TTL = int(getattr(settings, "JOB_RECLAIM_TTL", 120))
TYPING_ENABLED = bool(getattr(settings, "TYPING_ENABLED", True))
TYPING_SKIP_BACKLOG = int(getattr(settings, "TYPING_SKIP_BACKLOG", 0))
TYPING_SKIP_GROUPS = bool(getattr(settings, "TYPING_SKIP_GROUPS", True))

JOB_KEY_PREFIX = "q:job:"
JOB_PROCESSING_TTL = int(getattr(settings, "JOB_PROCESSING_TTL", 300))
JOB_DONE_TTL = int(getattr(settings, "JOB_DONE_TTL", 86400))
JOB_HEARTBEAT_INTERVAL = int(getattr(settings, "JOB_HEARTBEAT_INTERVAL", 25))

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

async def _tg_acquire_permit() -> None:
    key = "ratelimit:tg:global"
    for _ in range(100):
        now_ms = int(time.time() * 1000)
        try:
            ok = int(await REDIS_QUEUE.eval(_TG_BUCKET_LUA, 1, key, TG_GLOBAL_RPS, TG_GLOBAL_BURST, now_ms) or 0)
        except Exception:
            ok = 1
        if ok == 1:
            return
        await asyncio.sleep(0.02)

async def _tg_acquire_chat_permit(chat_id: int) -> None:
    key = f"ratelimit:tg:chat:{chat_id}"
    for _ in range(50):
        now_ms = int(time.time() * 1000)
        try:
            ok = int(await REDIS_QUEUE.eval(_CHAT_BUCKET_LUA, 1, key, TG_CHAT_RPS, TG_CHAT_BURST, now_ms) or 0)
        except Exception:
            ok = 1
        if ok == 1:
            return
        await asyncio.sleep(0.02)

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

async def _typing_loop(chat_id: int) -> None:

    try:
        while True:
            try:
                await BOT.send_chat_action(chat_id, ChatAction.TYPING)
                await asyncio.sleep(5)
            except TelegramRetryAfter as e:
                delay = max(1.0, float(getattr(e, "retry_after", 1)))
                logger.debug("Typing rate-limited for chat_id=%s, sleeping %ss", chat_id, delay)
                await asyncio.sleep(_jitter(delay, 0.25))
            except (TelegramNetworkError, asyncio.TimeoutError, TimeoutError):
                await asyncio.sleep(_jitter(2.0, 0.5))
            except (TelegramBadRequest, TelegramForbiddenError) as e:
                logger.debug("Typing stopped for chat_id=%s: %s", chat_id, e)
                break
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("Typing loop error for chat_id=%s: %s", chat_id, e)

async def _get_backlog(redis: Redis, queue_key: str, processing_key: str) -> int:
    try:
        qlen, plen = await asyncio.gather(redis.llen(queue_key), redis.llen(processing_key))
        return int(qlen or 0) + int(plen or 0)
    except Exception:
        return 0

async def _delayed_typing(chat_id: int, delay: float = 1.5) -> None:
    
    try:
        await asyncio.sleep(_jitter(delay, 0.25))
        await _typing_loop(chat_id)
    except asyncio.CancelledError:
        raise


async def _heartbeat_inflight(redis: Redis, key: str, expected_value: str, interval: int, ttl: int) -> None:

    script = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
        return redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
    else
        return 0
    end
    """
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                await redis.eval(script, 1, key, expected_value, ttl)
            except Exception as e:
                logger.warning("heartbeat eval failed for %s: %s", key, e)
    except asyncio.CancelledError:
        pass


def _allow_telegram_html(escaped: str) -> str:

    simple_tags = ["b", "strong", "i", "em", "u", "s", "del", "code", "pre"]
    for tag in simple_tags:
        escaped = re.sub(fr"&lt;{tag}&gt;", f"<{tag}>", escaped, flags=re.IGNORECASE)
        escaped = re.sub(fr"&lt;/{tag}&gt;", f"</{tag}>", escaped, flags=re.IGNORECASE)

    def _unescape_a(m):
        url = m.group(1)
        if url.startswith(("http://", "https://", "tg://")):
            return f'<a href="{url}">'
        return m.group(0)

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
) -> None:
   
    try:
        if msg_id is not None:
            try:
                sent = await REDIS_QUEUE.set(
                    f"sent_reply:{chat_id}:{msg_id}",
                    1,
                    nx=True,
                    ex=JOB_DONE_TTL,
                )
                if not sent:
                    logger.info("Skip duplicate reply chat=%s msg_id=%s", chat_id, msg_id)
                    return
            except Exception as e:
                logger.warning("failed to set sent_reply key: %s", e)

        if len(text) > TG_TEXT_LIMIT - 10:
            text = text[: TG_TEXT_LIMIT - 10] + "…"

        text_safe = html.escape(text)
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

        async def _send_with_retries(pm: Optional[str], kw: dict, raw_text_for_plain: str) -> bool:
            attempts = 3
            removed_reply = False
            for i in range(attempts):
                try:
                    if pm:
                        await BOT.send_message(parse_mode=pm, **kw)
                    else:
                        await BOT.send_message(text=raw_text_for_plain, **{k: v for k, v in kw.items() if k != "text"})
                    return True
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
                        return False
                    raise
                except TelegramForbiddenError as e:
                    logger.info("Forbidden for chat_id=%s, skipping send and marking done: %s", chat_id, e)
                    return True
                except (TelegramNetworkError, asyncio.TimeoutError, TimeoutError) as e:
                    backoff = _jitter(min(4.0, 2.0 ** i), 0.35)
                    logger.warning("Network error (%s), backoff %ss, attempt %d/%d", e, backoff, i+1, attempts)
                    await asyncio.sleep(backoff)
                    continue
            return False

        await _tg_acquire_permit()
        await _tg_acquire_chat_permit(chat_id)

        ok = await _send_with_retries("HTML", dict(kwargs), text)
        if not ok:
            ok = await _send_with_retries(None, dict(kwargs), text)
        if not ok:
            raise RuntimeError("Message send failed in both HTML and plain modes")

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
    except Exception as e:
        logger.error(
            "Failed to send message to chat_id=%s (reply_to=%s): %s",
            chat_id, reply_to, e,
        )
        if msg_id is not None:
            try:
                await REDIS_QUEUE.delete(f"sent_reply:{chat_id}:{msg_id}")
                for mid in (merged_ids or []):
                    if mid is None:
                        continue
                    await REDIS_QUEUE.delete(f"sent_reply:{chat_id}:{mid}")
            except Exception:
                pass
        logger.debug(traceback.format_exc())

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

async def _try_start_task_or_requeue(raw: str, queue_key: str, processing_key: str) -> bool:
    chat_id: Optional[int] = None
    try:
        job = json.loads(raw)
        chat_id = int(job.get("chat_id"))
    except Exception:
        chat_id = None
    if (chat_id is not None) and (pending_per_chat.get(chat_id, 0) >= MAX_PENDING_PER_CHAT):
        try:
            async with REDIS_QUEUE.pipeline() as p:
                p.lrem(processing_key, 1, raw)
                p.rpush(queue_key, raw)
                await p.execute()
            logger.debug("Requeued (cap-per-chat) chat_id=%s back to %s", chat_id, queue_key)
        except Exception as e:
            logger.warning("Failed to requeue (cap-per-chat) chat_id=%s: %s", chat_id, e)
        return False
    t = asyncio.create_task(handle_job(raw, processing_key))
    PROCESSING_TASKS.add(t)
    _register_task(chat_id, t)
    return True

async def handle_job(raw: str, processing_key: str) -> None:

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
    merged_ids = job.get("merged_msg_ids")
    image_b64  = job.get("image_b64")
    image_mime = job.get("image_mime")

    try:
        msg_id = int(msg_id) if msg_id is not None else None
    except Exception:
        msg_id = None

    if not (isinstance(chat_id, int) and isinstance(user_id, int) and (text or image_b64)):
        logger.error(
            "Skipping job with missing fields: chat_id=%s user_id=%s text_len=%d has_image=%s",
            chat_id, user_id, len(text), bool(image_b64),
        )
        await REDIS_QUEUE.lrem(processing_key, 1, raw)
        return

    if msg_id is None:
        logger.error(
            "Dropping job without msg_id (chat=%s user=%s): text=%r",
            chat_id, user_id, text
        )
        with suppress(Exception):
            await REDIS_QUEUE.lrem(processing_key, 1, raw)
        return
    dedupe_id = f"{chat_id}:{msg_id}"
    job_key = JOB_KEY_PREFIX + dedupe_id

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

    lock = _get_chat_lock(chat_id)

    async with lock:
        typing_task = None
        hb_task = asyncio.create_task(
            _heartbeat_inflight(REDIS_QUEUE, job_key, value, JOB_HEARTBEAT_INTERVAL, JOB_PROCESSING_TTL)
        )
        try:
            async with openai_sem:
                if TYPING_ENABLED and not (TYPING_SKIP_GROUPS and (is_group or is_channel)):
                    try:
                        backlog = await _get_backlog(REDIS_QUEUE, settings.QUEUE_KEY, processing_key)
                    except Exception:
                        backlog = 0
                    if (TYPING_SKIP_BACKLOG <= 0) or (backlog <= TYPING_SKIP_BACKLOG):
                        with suppress(Exception):
                            await BOT.send_chat_action(chat_id, ChatAction.TYPING)
                        typing_task = asyncio.create_task(_delayed_typing(chat_id, delay=0.0))
                reply_text = await asyncio.wait_for(
                    respond_to_user(
                        text, chat_id, user_id,
                        group_mode=is_group,
                        is_channel_post=is_channel,
                        channel_title=chan_title,
                        reply_to=reply_to,
                        msg_id=msg_id,
                        voice_in=voice_in,
                        image_b64=image_b64,
                        image_mime=image_mime,
                    ),
                    timeout=180,
                )
                reply_text = (reply_text or "").strip() or "Sorry, I’ve got nothing to add 😅"
                try:
                    await _send_reply(chat_id, reply_text, reply_to, msg_id, merged_ids)
                    with suppress(Exception):
                        await _mark_done_if_inflight(REDIS_QUEUE, job_key, value, JOB_DONE_TTL)
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
                                p.rpush(queue_key, raw)
                                await p.execute()
                            logger.warning("Requeued job after send failure %s", dedupe_id)
                            remove_from_processing = False
                            return
                        except Exception as ex:
                            logger.error("Failed to requeue after send failure: %s", ex)
                            remove_from_processing = False
        except asyncio.CancelledError:
            remove_from_processing = False
            with suppress(Exception):
                await _delete_if_inflight(REDIS_QUEUE, job_key, value)
            raise
        except Exception as e:
            logger.error(
                "respond_to_user failed/timeout chat=%s user=%s: %s",
                chat_id, user_id, e
            )
            reply_text = (
                "⏳ Sorry, I was thinking longer than usual. "
                "Try asking the question again."
            )
            try:
                await _send_reply(chat_id, reply_text, reply_to, msg_id, merged_ids)
                with suppress(Exception):
                    await _mark_done_if_inflight(REDIS_QUEUE, job_key, value, JOB_DONE_TTL)
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
                            p.rpush(queue_key, raw)
                            await p.execute()
                        logger.warning("Requeued job after fallback send failure %s", dedupe_id)
                        remove_from_processing = False
                        return
                    except Exception as ex:
                        logger.error("Failed to requeue after fallback send failure: %s", ex)
                        remove_from_processing = False
        finally:
            if typing_task:
                typing_task.cancel()
                with suppress(asyncio.CancelledError):
                    await typing_task
                
            hb_task.cancel()
            with suppress(asyncio.CancelledError):
                await hb_task

            if remove_from_processing:
                try:
                    await REDIS_QUEUE.lrem(processing_key, 1, raw)
                except Exception as exc:
                    logger.warning("Failed to lrem processed job: %s", exc)

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

            if not val:
                token = f"reclaim:{os.getpid()}:{time.time():.3f}"
                try:
                    ok = await redis.set(job_key, f"inflight:{token}", ex=JOB_RECLAIM_TTL, nx=True)
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

async def _sweeper_loop(stop_evt: asyncio.Event, queue_key: str, processing_key: str) -> None:
    try:
        while not stop_evt.is_set():
            await _sweep_processing(REDIS_QUEUE, queue_key, processing_key, PROCESSING_SWEEP_BATCH)
            await asyncio.sleep(_jitter(PROCESSING_SWEEP_INTERVAL, 0.1))
    except asyncio.CancelledError:
        pass

async def _cleanup_chat_locks_loop(stop_evt: asyncio.Event) -> None:
    try:
        while not stop_evt.is_set():
            await asyncio.sleep(60)
            now = time.time()
            stale = [cid for cid, ts in list(chat_locks_last_used.items()) if (now - ts) > CHAT_LOCK_TTL]
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
                if stop_evt.is_set(): break
                if not raw:
                    continue

                logger.debug("BRPOPLPUSH → %r", raw)
                await _try_start_task_or_requeue(raw, queue_key, processing_key)

                filled = 0
                while (len(PROCESSING_TASKS) < MAX_INFLIGHT_TASKS) and (not stop_evt.is_set()):
                    extra = await REDIS_QUEUE.rpoplpush(queue_key, processing_key)
                    if not extra:
                        break
                    started = await _try_start_task_or_requeue(extra, queue_key, processing_key)
                    if started:
                        filled += 1
                        if (filled % 50) == 0:
                            await asyncio.sleep(0)

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

    await REDIS_QUEUE.close()
    await REDIS_QUEUE.connection_pool.disconnect(inuse_connections=True)
    logger.info("Redis connection closed, bye!")


def main() -> None:
    level = os.environ.get("QUEUE_LOG_LEVEL", "DEBUG").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s:%(lineno)d  %(message)s",
        force=True,
    )
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
EOF