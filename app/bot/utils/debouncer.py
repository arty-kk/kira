#app/bot/utils/debouncer.py
import asyncio
import json
import time
import logging

from collections import defaultdict

import app.bot.components.constants as consts
from app.config import settings

logger = logging.getLogger(__name__)

message_buffers: dict[str, list[dict]] = defaultdict(list)
pending_tasks: dict[str, asyncio.Task] = {}
_locks: dict[str, asyncio.Lock] = {}

MAX_BUFFER_PER_CHAT = int(getattr(settings, "DEBOUNCE_BUFFER_PER_CHAT", 30))
GLOBAL_MAX_BUFFERS   = int(getattr(settings, "DEBOUNCE_GLOBAL_MAX", 10000))

DEBOUNCE_MODE = getattr(settings, "DEBOUNCE_MODE", "human").lower()  # human | merge | single
MAX_BATCH_CHARS = int(getattr(settings, "DEBOUNCE_MAX_BATCH_CHARS", 1800))
MIN_DELAY = float(getattr(settings, "DEBOUNCE_MIN_DELAY", 1.0))
MAX_DELAY = float(getattr(settings, "DEBOUNCE_MAX_DELAY", 4.0))
BUSY_HOLD_TIMEOUT = float(getattr(settings, "DEBOUNCE_BUSY_HOLD_TIMEOUT", 3.0))
BUSY_POLL_INTERVAL = float(getattr(settings, "DEBOUNCE_BUSY_POLL_INTERVAL", 0.25))
IDLE_GRACE = float(getattr(settings, "DEBOUNCE_IDLE_GRACE", 0.2))
CHAT_BUSY_PREFIX = "chatbusy:"

def _get_lock(key: str) -> asyncio.Lock:
    return _locks.setdefault(key, asyncio.Lock())

def compute_typing_delay(text: str) -> float:
    words = [w for w in (text or "").split() if w]
    n = len(words)
    if n <= 1:
        d = 3.0
    else:
        extra = n - 1
        base_extra = 0.1 * extra
        progressive = 0.01 * extra * (extra - 1) / 2
        d = 3 + base_extra + progressive
    return max(MIN_DELAY, min(MAX_DELAY, d))

async def _enqueue(payload: dict):
    await consts.redis_queue.lpush(settings.QUEUE_KEY, json.dumps(payload))

async def schedule_response(key: str):
    lock = _get_lock(key)
    try:
        while True:
            async with lock:
                if not message_buffers.get(key):
                    break
                last_text = (message_buffers[key][-1].get("text") or "")

            delay = compute_typing_delay(last_text)
            await asyncio.sleep(delay)

            if DEBOUNCE_MODE == "single":
                async with lock:
                    msgs = message_buffers.pop(key, [])
                for m in msgs:
                    p = m.copy()
                    p.pop("merged_msg_ids", None)
                    await _enqueue(p)
                continue

            if DEBOUNCE_MODE == "merge":
                async with lock:
                    msgs = message_buffers.pop(key, [])
                await _merge_and_send(msgs)
                continue

            async with lock:
                if not message_buffers.get(key):
                    continue
                sample = message_buffers[key][-1]

            chat_id = sample["chat_id"]
            try:
                busy = bool(int(await consts.redis_queue.get(f"{CHAT_BUSY_PREFIX}{chat_id}") or 0))
            except Exception:
                busy = False

            if busy:
                start = time.monotonic()
                while True:
                    try:
                        still_busy = bool(int(await consts.redis_queue.get(f"{CHAT_BUSY_PREFIX}{chat_id}") or 0))
                    except Exception:
                        still_busy = False
                    if not still_busy or (time.monotonic() - start) >= BUSY_HOLD_TIMEOUT:
                        break
                    await asyncio.sleep(BUSY_POLL_INTERVAL)
                await asyncio.sleep(IDLE_GRACE)

            async with lock:
                msgs = message_buffers.pop(key, [])
            if msgs:
                await _merge_and_send(msgs)
    except asyncio.CancelledError:
        return
    finally:
        current = asyncio.current_task()
        async with lock:
            has_msgs = bool(message_buffers.get(key))
            existing = pending_tasks.get(key)
            if has_msgs:
                if (existing is None) or existing.done() or (existing is current):
                    logger.debug("debounce[%s]: spawn successor; pending=%d",
                                 key, len(message_buffers.get(key, ())))
                    pending_tasks[key] = asyncio.create_task(schedule_response(key))
            else:
                if key in message_buffers or key in pending_tasks:
                    logger.debug("debounce[%s]: empty buffer → cleanup", key)
                message_buffers.pop(key, None)
                pending_tasks.pop(key, None)

        lk = _locks.get(key)
        if (lk is not None) and (not lk.locked()) and (pending_tasks.get(key) is None) and (not message_buffers.get(key)):
            logger.debug("debounce[%s]: dropping unused lock", key)
            _locks.pop(key, None)


async def _merge_and_send(msgs: list[dict]):
    if not msgs:
        return
    batch: list[dict] = []
    cur_len = 0
    out_payloads: list[dict] = []

    def _flush():
        nonlocal batch, cur_len
        if not batch:
            return
        if any(m.get("image_b64") or m.get("voice_in") for m in batch):
            for m in batch:
                p = m.copy()
                p.pop("merged_msg_ids", None)
                out_payloads.append(p)
        else:
            payload = batch[-1].copy()
            payload["text"] = "\n".join((b.get("text") or "") for b in batch).strip()
            payload["merged_msg_ids"] = [b.get("msg_id") for b in batch if b.get("msg_id") is not None]
            if any(bool(b.get("allow_web")) for b in batch):
                payload["allow_web"] = True
            first_reply_to = next((b.get("reply_to") for b in batch if b.get("reply_to")), None)
            if first_reply_to and not payload.get("reply_to"):
                payload["reply_to"] = first_reply_to
            enforce_any = any(bool(b.get("enforce_on_topic")) for b in batch)
            payload["enforce_on_topic"] = enforce_any
            out_payloads.append(payload)
        batch = []
        cur_len = 0

    for m in msgs:
        t = (m.get("text") or "").strip()
        if m.get("image_b64") or m.get("voice_in"):
            _flush()
            out_payloads.append(m.copy())
            continue

        prospective = cur_len + len(t) + (1 if batch else 0)
        if batch and prospective > MAX_BATCH_CHARS:
            _flush()
        batch.append(m)
        cur_len += len(t) + (1 if cur_len else 0)

    _flush()
    for p in out_payloads:
        await _enqueue(p)

def buffer_message_for_response(payload: dict):
    if payload.get("is_channel_post"):
        key = f"{payload['chat_id']}:channel:{payload.get('msg_id')}"
    else:
        key = f"{payload['chat_id']}:{payload['user_id']}"

    async def _append_and_schedule():
        lock = _get_lock(key)
        async with lock:
            payload.setdefault("ts", time.time())
            if len(message_buffers[key]) >= MAX_BUFFER_PER_CHAT:
                message_buffers[key].pop(0)
            if sum(len(v) for v in message_buffers.values()) >= GLOBAL_MAX_BUFFERS:
                def _head_ts(k: str) -> float:
                    head = message_buffers.get(k, [None])[0] or {}
                    return float(head.get("ts") or 0.0)
                oldest_key = min(message_buffers, key=_head_ts)
                message_buffers[oldest_key].pop(0)
            message_buffers[key].append(payload)
            logger.debug("debounce[%s]: appended; size=%d", key, len(message_buffers[key]))
            task = pending_tasks.get(key)
            if task is None or task.done():
                logger.debug("debounce[%s]: schedule task (was %s)", key,
                             "none/done" if task is None or task.done() else "running")
                pending_tasks[key] = asyncio.create_task(schedule_response(key))

    asyncio.create_task(_append_and_schedule())