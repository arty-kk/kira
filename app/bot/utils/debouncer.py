#app/bot/utils/debouncer.py
import asyncio
import json
import time
import logging

from collections import defaultdict

import app.bot.components.constants as consts
from app.tasks.queue_schema import validate_bot_job
from app.services.user.user_service import refund_reservation_by_id
from app.config import settings

logger = logging.getLogger(__name__)

message_buffers: dict[str, list[dict]] = defaultdict(list)
pending_tasks: dict[str, asyncio.Task] = {}
_locks: dict[str, asyncio.Lock] = {}
_global_lock = asyncio.Lock()
total_buffered: int = 0

MAX_BUFFER_PER_CHAT = int(getattr(settings, "DEBOUNCE_BUFFER_PER_CHAT", 30))
GLOBAL_MAX_BUFFERS   = int(getattr(settings, "DEBOUNCE_GLOBAL_MAX", 10000))

DEBOUNCE_MODE = getattr(settings, "DEBOUNCE_MODE", "human").lower()  # human | merge | single
MAX_BATCH_CHARS = int(getattr(settings, "DEBOUNCE_MAX_BATCH_CHARS", 1800))
MIN_DELAY = float(getattr(settings, "DEBOUNCE_MIN_DELAY", 2.0))
MAX_DELAY = float(getattr(settings, "DEBOUNCE_MAX_DELAY", 120.0))
BUSY_HOLD_TIMEOUT = float(getattr(settings, "DEBOUNCE_BUSY_HOLD_TIMEOUT", 3.0))
BUSY_POLL_INTERVAL = float(getattr(settings, "DEBOUNCE_BUSY_POLL_INTERVAL", 0.25))
IDLE_GRACE = float(getattr(settings, "DEBOUNCE_IDLE_GRACE", 0.2))
CHAT_BUSY_PREFIX = "chatbusy:"
BOT_QUEUE_MAX_PAYLOAD_BYTES = int(getattr(settings, "BOT_QUEUE_MAX_PAYLOAD_BYTES", 64 * 1024))

def _get_lock(key: str) -> asyncio.Lock:
    return _locks.setdefault(key, asyncio.Lock())

TYPING_BASE_DELAY = float(getattr(settings, "TYPING_BASE_DELAY", 2.0))
TYPING_PER_CHAR   = float(getattr(settings, "TYPING_PER_CHAR", 0.075))

def compute_typing_delay(text: str) -> float:
    s = (text or "").strip()
    n = len(s)

    if n <= 0:
        return MIN_DELAY

    d = TYPING_BASE_DELAY + TYPING_PER_CHAR * n
    return max(MIN_DELAY, min(MAX_DELAY, d))


def _collect_payload_reservation_ids(payload: dict) -> list[int]:
    reservation_ids: list[int] = []
    seen_reservation_ids: set[int] = set()

    raw_reservation_ids = payload.get("reservation_ids")
    if isinstance(raw_reservation_ids, list):
        for raw_reservation_id in raw_reservation_ids:
            try:
                reservation_id_item = int(raw_reservation_id)
            except Exception:
                continue
            if reservation_id_item <= 0 or reservation_id_item in seen_reservation_ids:
                continue
            seen_reservation_ids.add(reservation_id_item)
            reservation_ids.append(reservation_id_item)

    try:
        reservation_id = int(payload.get("reservation_id") or 0)
    except Exception:
        reservation_id = 0
    if reservation_id > 0 and reservation_id not in seen_reservation_ids:
        reservation_ids.append(reservation_id)

    return reservation_ids


async def _refund_payload_reservations(payload: dict) -> bool:
    reservation_ids = _collect_payload_reservation_ids(payload)
    for reservation_id_item in reservation_ids:
        try:
            await refund_reservation_by_id(reservation_id_item)
        except Exception:
            logger.exception("debouncer: failed to refund reservation_id=%s", reservation_id_item)
    return bool(reservation_ids)


async def _log_and_refund_dropped_payload(*, chat_key: str, drop_reason: str, dropped_payload: dict) -> None:
    reservation_count = len(_collect_payload_reservation_ids(dropped_payload))
    has_reservation = await _refund_payload_reservations(dropped_payload)
    if has_reservation:
        logger.info(
            "dropped_with_refund",
            extra={
                "chat_key": chat_key,
                "drop_reason": drop_reason,
                "reservation_count": reservation_count,
            },
        )
    else:
        logger.info(
            "dropped_without_reservation",
            extra={
                "chat_key": chat_key,
                "drop_reason": drop_reason,
            },
        )


async def _enqueue(payload: dict):

    try:
        chat_id = int(payload.get("chat_id") or 0)
    except Exception:
        chat_id = 0
    try:
        user_id = int(payload.get("user_id") or 0)
    except Exception:
        user_id = 0
    try:
        msg_id = int(payload.get("msg_id") or 0)
    except Exception:
        msg_id = 0

    err = validate_bot_job(payload)
    if err:
        logger.warning(
            "debouncer.enqueue_reject",
            extra={
                "reason": err,
                "chat_id": chat_id,
                "user_id": user_id,
                "msg_id": msg_id,
            },
        )
        await _refund_payload_reservations(payload)
        return
    try:
        data = json.dumps(payload, ensure_ascii=False)
    except Exception:
        logger.exception("debouncer: failed to encode queue payload")
        await _refund_payload_reservations(payload)
        return
    payload_bytes = data.encode("utf-8")
    if BOT_QUEUE_MAX_PAYLOAD_BYTES > 0 and len(payload_bytes) > BOT_QUEUE_MAX_PAYLOAD_BYTES:
        logger.warning(
            "debouncer: payload too large (%d bytes), dropping",
            len(payload_bytes),
        )
        await _refund_payload_reservations(payload)
        return
    try:
        await consts.redis_queue.lpush(settings.QUEUE_KEY, data)
    except Exception:
        logger.exception("debouncer: enqueue failed")
        await _refund_payload_reservations(payload)

async def schedule_response(key: str):
    global total_buffered
    lock = _get_lock(key)
    try:
        while True:
            async with lock:
                if not message_buffers.get(key):
                    break

            if DEBOUNCE_MODE == "single":
                async with _global_lock:
                    async with lock:
                        msgs = message_buffers.pop(key, [])
                        if msgs:
                            total_buffered -= len(msgs)
                for m in msgs:
                    p = m.copy()
                    p.pop("merged_msg_ids", None)
                    await _enqueue(p)
                continue

            if DEBOUNCE_MODE == "merge":
                async with _global_lock:
                    async with lock:
                        msgs = message_buffers.pop(key, [])
                        if msgs:
                            total_buffered -= len(msgs)
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

            async with _global_lock:
                async with lock:
                    msgs = message_buffers.pop(key, [])
                    if msgs:
                        total_buffered -= len(msgs)
            if msgs:
                await _merge_and_send(msgs)
    except asyncio.CancelledError:
        return
    finally:
        current = asyncio.current_task()
        async with _global_lock:
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
    cur_reply_to = None

    def _pick_first_nonzero(items: list[dict], key: str):
        for it in items:
            v = it.get(key)
            try:
                iv = int(v) if v is not None else 0
            except Exception:
                iv = 0
            if iv > 0:
                return iv
        return None

    def _pick_last_nonzero(items: list[dict], key: str):
        for it in reversed(items):
            v = it.get(key)
            try:
                iv = int(v) if v is not None else 0
            except Exception:
                iv = 0
            if iv > 0:
                return iv
        return None

    def _flush():
        nonlocal batch, cur_len, cur_reply_to
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
            trigger_priority = ("mention", "channel_post", "check_on_topic")
            merged_trigger = None
            for trigger_value in trigger_priority:
                if any((b.get("trigger") == trigger_value) for b in batch):
                    merged_trigger = trigger_value
                    break
            payload["trigger"] = merged_trigger
            reservation_ids: list[int] = []
            seen_reservation_ids: set[int] = set()
            for b in batch:
                try:
                    reservation_id = int(b.get("reservation_id") or 0)
                except Exception:
                    reservation_id = 0
                if reservation_id > 0 and reservation_id not in seen_reservation_ids:
                    seen_reservation_ids.add(reservation_id)
                    reservation_ids.append(reservation_id)
            payload["reservation_ids"] = reservation_ids
            if any(bool(b.get("allow_web")) for b in batch):
                payload["allow_web"] = True
            picked_reply_to = _pick_first_nonzero(batch, "reply_to")
            if picked_reply_to and not payload.get("reply_to"):
                payload["reply_to"] = picked_reply_to
            picked_tg_reply_to = _pick_last_nonzero(batch, "tg_reply_to")
            if picked_tg_reply_to and not payload.get("tg_reply_to"):
                payload["tg_reply_to"] = picked_tg_reply_to
            enforce_any = any(bool(b.get("enforce_on_topic")) for b in batch)
            payload["enforce_on_topic"] = (False if merged_trigger == "mention" else enforce_any)
            out_payloads.append(payload)
        batch = []
        cur_len = 0
        cur_reply_to = None

    for m in msgs:
        t = (m.get("text") or "").strip()
        if m.get("image_b64") or m.get("voice_in"):
            _flush()
            out_payloads.append(m.copy())
            continue

        try:
            m_r = int(m.get("reply_to") or 0)
        except Exception:
            m_r = 0
        if m_r > 0:
            if cur_reply_to is None:
                cur_reply_to = m_r
            elif cur_reply_to != m_r:
                _flush()
                cur_reply_to = m_r

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
        async with _global_lock:
            async with lock:
                global total_buffered

                def _drop_and_refund(*, drop_key: str, drop_reason: str) -> None:
                    global total_buffered
                    dropped_payload = message_buffers[drop_key].pop(0)
                    total_buffered -= 1
                    asyncio.create_task(
                        _log_and_refund_dropped_payload(
                            chat_key=drop_key,
                            drop_reason=drop_reason,
                            dropped_payload=dropped_payload,
                        )
                    )

                payload.setdefault("ts", time.time())
                if len(message_buffers[key]) >= MAX_BUFFER_PER_CHAT:
                    _drop_and_refund(drop_key=key, drop_reason="per_chat_limit")
                if GLOBAL_MAX_BUFFERS > 0 and total_buffered >= GLOBAL_MAX_BUFFERS:
                    def _head_ts(k: str) -> float:
                        items = message_buffers.get(k) or []
                        head = items[0] if items else {}
                        return float(head.get("ts") or 0.0)
                    if message_buffers:
                        oldest_key = min(message_buffers, key=_head_ts)
                        if oldest_key == key:
                            if message_buffers[key]:
                                _drop_and_refund(drop_key=key, drop_reason="global_limit")
                        else:
                            oldest_lock = _get_lock(oldest_key)
                            async with oldest_lock:
                                if message_buffers.get(oldest_key):
                                    _drop_and_refund(drop_key=oldest_key, drop_reason="global_limit")
                message_buffers[key].append(payload)
                total_buffered += 1
                logger.debug("debounce[%s]: appended; size=%d", key, len(message_buffers[key]))
                task = pending_tasks.get(key)
                if task is None or task.done():
                    logger.debug("debounce[%s]: schedule task (was %s)", key,
                                 "none/done" if task is None or task.done() else "running")
                    pending_tasks[key] = asyncio.create_task(schedule_response(key))

    asyncio.create_task(_append_and_schedule())
