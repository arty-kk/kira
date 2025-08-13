cat >app/tasks/queue_worker.py<< 'EOF'
# app/tasks/queue_worker.py
import asyncio
import json
import signal
import sys
import time
import html
import traceback
import logging
import os
import weakref

from contextlib import suppress
from typing import Optional, Dict

from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
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


openai_sem = asyncio.Semaphore(settings.OPENAI_MAX_CONCURRENT_REQUESTS)
chat_locks: "weakref.WeakValueDictionary[int, asyncio.Lock]" = weakref.WeakValueDictionary()

JOB_KEY_PREFIX = "q:job:"
JOB_PROCESSING_TTL = int(getattr(settings, "JOB_PROCESSING_TTL", 300))
JOB_DONE_TTL = int(getattr(settings, "JOB_DONE_TTL", 86400))
JOB_HEARTBEAT_INTERVAL = int(getattr(settings, "JOB_HEARTBEAT_INTERVAL", 25))


async def _typing_loop(chat_id: int) -> None:

    try:
        while True:
            await BOT.send_chat_action(chat_id, ChatAction.TYPING)
            await asyncio.sleep(5)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("Typing loop error for chat_id=%s: %s", chat_id, e)


async def _delayed_typing(chat_id: int, delay: float = 1.5) -> None:
    
    try:
        await asyncio.sleep(delay)
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


async def _send_reply(chat_id: int, text: str, reply_to: Optional[int], msg_id: Optional[int]) -> None:
   
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

        if len(text_safe) > TG_TEXT_LIMIT:
            text_safe = text_safe[: TG_TEXT_LIMIT - 1] + "…"

        kwargs = dict(
            chat_id=chat_id,
            text=text_safe,
            disable_web_page_preview=True,
        )
        if reply_to:
            kwargs["reply_to_message_id"] = reply_to

        try:
            await BOT.send_message(parse_mode="HTML", **kwargs)
        except TelegramBadRequest as bad:
            logger.warning("HTML send failed: %s — retrying plain text", bad)
            await BOT.send_message(**kwargs)
    except Exception as e:
        logger.error(
            "Failed to send message to chat_id=%s (reply_to=%s): %s",
            chat_id, reply_to, e,
        )
        if msg_id is not None:
            try:
                await REDIS_QUEUE.delete(f"sent_reply:{chat_id}:{msg_id}")
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

    try:
        msg_id = int(msg_id) if msg_id is not None else None
    except Exception:
        msg_id = None

    if not (isinstance(chat_id, int) and isinstance(user_id, int) and text):
        logger.error(
            "Skipping job with missing fields: chat_id=%s user_id=%s text_len=%d",
            chat_id, user_id, len(text),
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
        with suppress(Exception):
            await REDIS_QUEUE.lrem(processing_key, 1, raw)
        if (val or "").startswith("done"):
            logger.info("Drop duplicate: already done %s", dedupe_id)
        else:
            logger.info("Drop duplicate: already inflight %s", dedupe_id)
        return

    lock = chat_locks.setdefault(chat_id, asyncio.Lock())

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
                    ),
                    timeout=180,
                )
                await _send_reply(chat_id, reply_text, reply_to, msg_id)
                with suppress(Exception):
                    await REDIS_QUEUE.set(job_key, "done", ex=JOB_DONE_TTL)
            except Exception as e:
                logger.error(
                    "respond_to_user failed/timeout chat=%s user=%s: %s",
                    chat_id, user_id, e
                )
                reply_text = (
                    "⏳ Sorry, I was thinking longer than usual."
                    "Try asking the question again."
                )
                try:
                    await _send_reply(chat_id, reply_text, reply_to, msg_id)
                    with suppress(Exception):
                        await REDIS_QUEUE.set(job_key, "done", ex=JOB_DONE_TTL)
                except Exception:
                    with suppress(Exception):
                        await REDIS_QUEUE.delete(job_key)
            finally:
                typing_task.cancel()
                with suppress(asyncio.CancelledError):
                    await typing_task
                    
                hb_task.cancel()
                with suppress(asyncio.CancelledError):
                    await hb_task

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
        await REDIS_QUEUE.lpush(queue_key, *pending)
        await REDIS_QUEUE.delete(processing_key)
    logger.info("Starting queue_worker on Redis key '%s'", queue_key)

    while not stop_evt.is_set():
        try:
            raw = await REDIS_QUEUE.brpoplpush(queue_key, processing_key, timeout=0)
            if not raw:
                continue

            logger.debug("BRPOPLPUSH → %r", raw)
            asyncio.create_task(handle_job(raw, processing_key))

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

    await REDIS_QUEUE.close()
    await REDIS_QUEUE.connection_pool.disconnect(inuse_connections=True)
    logger.info("Redis connection closed, bye!")


def main() -> None:
    level = os.environ.get("LOG_LEVEL", "DEBUG").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s:%(lineno)d  %(message)s",
        force=True,
    )
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
EOF