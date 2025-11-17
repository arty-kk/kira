#app/tasks/moderation.py
from __future__ import annotations

import os
import asyncio
import logging

from celery import shared_task
from app.tasks.utils.bg_loop import get_bg_loop


logger = logging.getLogger(__name__)

MODERATION_TIMEOUT = int(os.getenv("MODERATION_TIMEOUT", "30"))


def _run(coro):
    loop = get_bg_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result()


@shared_task(name="moderation.passive_moderate", bind=True, acks_late=True)
def passive_moderate(self, payload: dict) -> str:

    from app.bot.handlers.moderation import handle_passive_moderation
    
    async def _do() -> str:
        return await asyncio.wait_for(
            handle_passive_moderation(
                chat_id     = payload["chat_id"],
                message     = None,
                text        = payload.get("text", ""),
                entities    = payload.get("entities") or [],
                image_b64   = payload.get("image_b64"),
                image_mime  = payload.get("image_mime"),
                source      = payload.get("source", "user"),
                user_id     = payload["user_id"],
                message_id  = payload["message_id"],
            ),
            timeout=MODERATION_TIMEOUT,
        )

    return _run(_do())