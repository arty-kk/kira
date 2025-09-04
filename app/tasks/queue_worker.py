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

from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter, TelegramNetworkError, TelegramForbiddenError
from redis.asyncio import Redis, from_url
from redis.exceptions import RedisError

from app.config import settings
from app.clients.telegram_client import get_bot
from app.services.responder import respond_to_user


logger = logging.getLogger(__name__)


TG_TEXT_LIMIT: int = int(getattr(settings, "TG_TEXT_LIMIT", 4096))

try:
    REDIS_QUEUE: Redis = from_url(
        settings.REDIS_URL_QUEUE,
        decode_responses=True,
        max_connections=settings.REDIS_MAX_CONNECTIONS,
        health_check_interval=30,
    )
    logger.info("Connected to Redis queue at %s", settings.REDIS_URL_QUEUE)
except RedisError as e:
    logger.critical("Failed to connect to Redis: %s", e)
    sys.exit(1)

BOT = get_bot()

PROCESSING_TASKS: set[asyncio.Task] = set()
MAX_INFLIGHT_TASKS: int = int(getattr(settings, "WORKER_MAX_INFLIGHT_TASKS", settings.OPENAI_MAX_CONCURRENT_REQUESTS * 2))
openai_sem = asyncio.Semaphore(settings.OPENAI_MAX_CONCURRENT_REQUESTS)
chat_locks: Dict[int, asyncio.Lock] = {}

JOB_KEY_PREFIX = "q:job:"
JOB_PROCESSING_TTL = int(getattr(settings, "JOB_PROCESSING_TTL", 300))
JOB_DONE_TTL = int(getattr(settings, "JOB_DONE_TTL", 86400))
JOB_HEARTBEAT_INTERVAL = int(getattr(settings, "JOB_HEARTBEAT_INTERVAL", 25))


def _get_chat_lock(chat_id: int) -> asyncio.Lock:
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
    script = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end"
    try:
        return int(await redis.eval(script, 1, key, expected_value) or 0)
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
        r"&lt;a href=&quot;((?:[Hh][Tt][Tt][Pp][Ss]?|[Tt][Gg])://[^\"<>\s]{1,200})&quot;&gt;",
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


async def handle_job(raw: str, processing_key: str) -> None:

    redis: Redis = REDIS_QUEUE
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
        if (val or "").startswith("done"):
            with suppress(Exception):
                await REDIS_QUEUE.lrem(processing_key, 1, raw)
            logger.info("Drop duplicate: already done %s", dedupe_id)
        else:
            with suppress(Exception):
                await REDIS_QUEUE.lrem(processing_key, 1, raw)
            logger.info("Drop duplicate: already inflight %s (removed from processing)", dedupe_id)
        return

    lock = _get_chat_lock(chat_id)

    async with lock:

        async with openai_sem:
            typing_task = asyncio.create_task(_delayed_typing(chat_id))
            hb_task = asyncio.create_task(
                _heartbeat_inflight(REDIS_QUEUE, job_key, value, JOB_HEARTBEAT_INTERVAL, JOB_PROCESSING_TTL)
            )
            try:
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
            except asyncio.CancelledError:
                remove_from_processing = False
                with suppress(Exception):
                    await _delete_if_inflight(REDIS_QUEUE, job_key, value)
                raise
            finally:
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


async def queue_worker(stop_evt: asyncio.Event) -> None:

    global REDIS_QUEUE
    queue_key      = settings.QUEUE_KEY
    processing_key = queue_key + ":processing"

    pending = await REDIS_QUEUE.lrange(processing_key, 0, -1)
    if pending:
        await REDIS_QUEUE.rpush(queue_key, *pending)
        await REDIS_QUEUE.delete(processing_key)
    logger.info("Starting queue_worker on Redis key '%s'", queue_key)

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
            t = asyncio.create_task(handle_job(raw, processing_key))
            PROCESSING_TASKS.add(t)
            def _done(_t: asyncio.Task) -> None:
                PROCESSING_TASKS.discard(_t)
            t.add_done_callback(_done)

        except RedisError as e:
            logger.error("RedisError in queue_worker: %s — reconnecting", e)
            try:
                await REDIS_QUEUE.close()
            except Exception:
                pass
            await asyncio.sleep(1)
            try:
                new_conn = from_url(
                    settings.REDIS_URL_QUEUE,
                    decode_responses=True,
                    max_connections=settings.REDIS_MAX_CONNECTIONS,
                    health_check_interval=30,
                )
                REDIS_QUEUE = new_conn
            except Exception as ex:
                logger.critical("Failed to reconnect to Redis: %s", ex)
                await asyncio.sleep(5)

        except asyncio.CancelledError:
            logger.info("queue_worker received shutdown signal")
            break

        except Exception as e:
            logger.exception("Unexpected error in queue_worker: %s", e)
            await asyncio.sleep(1)


async def _async_main() -> None:

    stop_evt = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_evt.set)

    worker = asyncio.create_task(queue_worker(stop_evt))
    logger.info("queue_worker task launched, entering event loop")

    await stop_evt.wait()
    logger.info("Shutdown signal received → cancelling worker")

    worker.cancel()
    with suppress(asyncio.CancelledError):
        await worker

    with suppress(Exception):
        await BOT.session.close()

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