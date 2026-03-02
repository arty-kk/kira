#app/tasks/welcome.py
from __future__ import annotations

import asyncio
import logging
import html
import re
import os

from types import SimpleNamespace
from contextlib import suppress
from celery import current_task

from aiogram.enums import ChatAction
from aiogram.exceptions import (
    TelegramRetryAfter, TelegramForbiddenError,
    TelegramBadRequest, TelegramNetworkError,
)

from app.config import settings
from app.tasks.celery_app import celery, run_coro_sync
from app.clients.telegram_client import get_bot
from app.services.addons.welcome_manager import (
    generate_welcome,
    generate_private_welcome,
)

logger = logging.getLogger(__name__)


WELCOME_TTL = getattr(settings, "WELCOME_TTL_SECONDS", 180)
WELCOME_GROUP_TIME_LIMIT_SEC = 90
WELCOME_GROUP_RUN_TIMEOUT_SEC = 85
WELCOME_PRIVATE_TIME_LIMIT_SEC = 90
WELCOME_PRIVATE_RUN_TIMEOUT_SEC = 85


async def _delete_later(bot, chat_id: int, msg_id: int, delay: int) -> None:
    await asyncio.sleep(delay)
    with suppress(Exception):
        await bot.delete_message(chat_id, msg_id)

        
async def typing_loop(bot, chat_id: int, action: ChatAction = ChatAction.TYPING, period: float = 5.0) -> None:
    try:
        while True:
            try:
                await bot.send_chat_action(chat_id, action)
            except TelegramRetryAfter as e:
                delay = max(1.0, float(getattr(e, "retry_after", period)))
                await asyncio.sleep(delay)
                continue
            except (TelegramForbiddenError, TelegramBadRequest):
                break
            except (TelegramNetworkError, asyncio.TimeoutError, TimeoutError):
                await asyncio.sleep(period)
                continue

            await asyncio.sleep(period)
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.debug("typing_loop error for chat_id=%s", chat_id, exc_info=True)


@celery.task(name="welcome.group", time_limit=WELCOME_GROUP_TIME_LIMIT_SEC)
def send_group_welcome_task(chat_id: int, user: dict) -> None:

    try:
        task_id = getattr(getattr(current_task, "request", None), "id", None)
    except Exception:
        task_id = None

    logger.info(
        "WELCOME_TASK start pid=%s task_id=%s chat_id=%s user_id=%s",
        os.getpid(),
        task_id,
        chat_id,
        user.get("id"),
    )
    
    async def _inner():
        bot = get_bot()

        u = SimpleNamespace(
            id=user["id"],
            username=user.get("username"),
            full_name=user.get("full_name"),
            language_code=user.get("language_code"),
        )

        text = ""
        typing_task = asyncio.create_task(typing_loop(bot, chat_id, ChatAction.TYPING))
        try:
            text = await generate_welcome(chat_id, u, "")
            msg = await bot.send_message(chat_id, text, parse_mode="HTML")
            asyncio.create_task(_delete_later(bot, chat_id, msg.message_id, WELCOME_TTL))
        except Exception as e:
            logger.error("Welcome send failed, trying minimal HTML fallback", exc_info=e)
            logger.debug("Broken welcome HTML: %s", text or "<empty>")

            safe_plain = re.sub(r"<[^>]+>", "", text or "").strip() or "Welcome!"
            mention_html = (
                f'<a href="tg://user?id={u.id}">'
                f'{html.escape((u.full_name or str(u.id))[:64])}'
                f'</a>'
            )
            fallback_html = f"{mention_html} {html.escape(safe_plain)}".strip()

            try:
                await bot.send_message(chat_id, fallback_html, parse_mode="HTML")
            except Exception:
                logger.error("Minimal HTML still failed, sending plain text", exc_info=True)

                username = user.get("username")
                if username:
                    mention_pt = f"@{username}"
                else:
                    mention_pt = f"tg://user?id={u.id}"

                if mention_pt not in safe_plain:
                    safe_plain = f"{mention_pt} {safe_plain}".strip()

                await bot.send_message(chat_id, safe_plain, parse_mode=None)
        finally:
            typing_task.cancel()
            with suppress(asyncio.CancelledError):
                await typing_task

    run_coro_sync(_inner(), timeout=WELCOME_GROUP_RUN_TIMEOUT_SEC)


@celery.task(name="welcome.private_ai", time_limit=WELCOME_PRIVATE_TIME_LIMIT_SEC)
def send_private_ai_welcome_task(uid: int) -> None:

    async def _inner():
        bot = get_bot()

        typing_task = asyncio.create_task(typing_loop(bot, uid, ChatAction.TYPING))
        try:
            ai_text = await generate_private_welcome(chat_id=uid, user=None)
            ai_text = (ai_text or "").strip()
            if not ai_text:
                return
            await bot.send_message(uid, ai_text, parse_mode=None)
        except Exception:
            logger.exception("AI welcome send failed")
        finally:
            typing_task.cancel()
            with suppress(asyncio.CancelledError):
                await typing_task

    run_coro_sync(_inner(), timeout=WELCOME_PRIVATE_RUN_TIMEOUT_SEC)
