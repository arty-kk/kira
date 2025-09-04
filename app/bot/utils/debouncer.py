#app/bot/utils/debouncer.py
import asyncio
import json
import logging

from collections import defaultdict

import app.bot.components.constants as consts
from app.config import settings

logger = logging.getLogger(__name__)


message_buffers: dict[str, list[dict]] = defaultdict(list)
pending_tasks: dict[str, asyncio.Task] = {}


def compute_typing_delay(text: str) -> float:
    words = [w for w in text.split() if w]
    n = len(words)
    if n <= 1:
        return 3
    extra = n - 1
    base_extra = 0.1 * extra
    progressive = 0.01 * extra * (extra - 1) / 2
    return 3 + base_extra + progressive


async def schedule_response(key: str):
    try:
        lst = message_buffers.get(key)
        if not lst:
            return
        last_text = lst[-1]["text"]
        delay = compute_typing_delay(last_text)
        await asyncio.sleep(delay)

        msgs = message_buffers.pop(key, [])
        if not msgs:
            return

        last = msgs[-1]
        payload = last.copy()
        payload["text"] = "\n".join(m["text"] for m in msgs)


        payload["merged_msg_ids"] = [m.get("msg_id") for m in msgs if m.get("msg_id") is not None]
        first_reply_to = next((m.get("reply_to") for m in msgs if m.get("reply_to")), None)
        if first_reply_to and not payload.get("reply_to"):
            payload["reply_to"] = first_reply_to
        try:
            await consts.redis_queue.lpush(settings.QUEUE_KEY, json.dumps(payload))
        except Exception as e:
            logger.warning("lpush failed for %s: %s", key, e)
            return

    except asyncio.CancelledError:
        return

    finally:
        pending_tasks.pop(key, None)


def buffer_message_for_response(payload: dict):
    key = f"{payload['chat_id']}:{payload['user_id']}"

    message_buffers[key].append(payload)

    task = pending_tasks.get(key)
    if task is None or task.done():
        pending_tasks[key] = asyncio.create_task(schedule_response(key))
