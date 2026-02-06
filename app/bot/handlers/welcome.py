#app/bot/handlers/welcome.py
import asyncio
import logging
import contextlib
import os
import time as time_module

from aiogram import F
from aiogram.enums import ChatType, ContentType
from aiogram.types import Message, ChatMemberUpdated, User
from aiogram.filters import ChatMemberUpdatedFilter, IS_NOT_MEMBER, IS_MEMBER

from app.bot.components.dispatcher import dp
from app.bot.components.constants import redis_client
from app.core.memory import MEMORY_TTL
from app.config import settings
from app.services.addons.welcome_manager import can_greet
from app.services.addons.analytics import record_new_user
from app.tasks.welcome import send_group_welcome_task

logger = logging.getLogger(__name__)


async def _clear_user_group_memory(chat_id: int, user_id: int) -> None:

    try:
        keys = [
            f"mem:stm:g:{chat_id}:u:{user_id}",
            f"mem:mtm:g:{chat_id}:u:{user_id}",
            f"mem:mtm_tokens:g:{chat_id}:u:{user_id}",
            f"mem:mtm_recent:g:{chat_id}:u:{user_id}",
            f"mem:mtm_recent_tokens:g:{chat_id}:u:{user_id}",
            f"mem:ltm:g:{chat_id}:u:{user_id}",
            f"mem:ltm_slices:g:{chat_id}:u:{user_id}",
        ]
        async with redis_client.pipeline(transaction=True) as pipe:
            for k in keys:
                pipe.delete(k)
            pipe.srem(f"all_users:{chat_id}", user_id)
            await pipe.execute()
        logger.info("Cleared layered memory for user %s in chat %s", user_id, chat_id)
    except Exception:
        logger.exception("Failed to clear layered memory for %s in chat %s", user_id, chat_id)


async def _schedule_group_welcome_once(chat_id: int, user: User) -> None:
    uid = user.id
    key = f"welcome_scheduled:{chat_id}:{uid}"

    try:
        first = await redis_client.set(
            key, 1,
            ex=getattr(settings, "NEW_USER_TTL_SECONDS", 600),
            nx=True,
        )
    except Exception:
        logger.exception("welcome: failed to set dedupe key for %s in chat %s", uid, chat_id)
        first = True

    if not first:
        logger.info("welcome: duplicate join for %s in chat %s, skipping", uid, chat_id)
        return

    logger.info("Scheduling welcome task for %s in chat %s", uid, chat_id)
    send_group_welcome_task.delay(
        chat_id,
        {
            "id": uid,
            "username": getattr(user, "username", None),
            "full_name": getattr(user, "full_name", None),
            "language_code": getattr(user, "language_code", None),
        },
    )


@dp.message(
    F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]),
    F.content_type == ContentType.NEW_CHAT_MEMBERS
)
async def on_new_members(message: Message) -> None:

    chat_id = message.chat.id
    for user in message.new_chat_members:
        uid = user.id
        try:
            async with redis_client.pipeline(transaction=True) as pipe:
                pipe.sadd(f"all_users:{chat_id}", uid)
                pipe.expire(f"all_users:{chat_id}", MEMORY_TTL)
                pipe.sadd(f"new_users:{chat_id}", uid)
                pipe.expire(f"new_users:{chat_id}", settings.NEW_USER_TTL_SECONDS)
                pipe.set(f"last_message_ts:{chat_id}", time_module.time())
                pipe.expire(f"last_message_ts:{chat_id}", MEMORY_TTL)
                try:
                    await pipe.execute()
                except asyncio.TimeoutError:
                    logger.error("Redis pipeline timeout in welcome handler")

            with contextlib.suppress(Exception):
                await record_new_user(chat_id, uid)

            try:
                if getattr(user, "language_code", None):
                    await redis_client.set(f"lang:{uid}", user.language_code.lower())
            except Exception:
                logger.debug("welcome: failed to store language for %s", uid, exc_info=True)

            logger.info("Added new member %s to chat %s", uid, chat_id)

            if not getattr(settings, "ENABLE_GROUP_AI_WELCOME", True):
                logger.info("Group welcomes disabled by flag for chat %s", chat_id)
                continue

            if not await can_greet(chat_id):
                logger.info("Rate limit reached for %s in chat %s", uid, chat_id)
                continue

            await _schedule_group_welcome_once(chat_id, user)

        except Exception:
            logger.exception("Error welcoming new member %s in chat %s", uid, chat_id)


@dp.chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER))
async def on_user_join_via_chat_member(update: ChatMemberUpdated) -> None:

    chat_id = update.chat.id
    user = update.new_chat_member.user
    uid = user.id

    logger.info(
        "WEBHOOK_JOIN handler pid=%s chat_id=%s user_id=%s",
        os.getpid(),
        chat_id,
        uid,
    )

    try:
        async with redis_client.pipeline(transaction=True) as pipe:
            pipe.sadd(f"all_users:{chat_id}", uid)
            pipe.expire(f"all_users:{chat_id}", MEMORY_TTL)
            pipe.sadd(f"new_users:{chat_id}", uid)
            pipe.expire(f"new_users:{chat_id}", settings.NEW_USER_TTL_SECONDS)
            pipe.set(f"last_message_ts:{chat_id}", time_module.time())
            pipe.expire(f"last_message_ts:{chat_id}", MEMORY_TTL)
            try:
                await pipe.execute()
            except asyncio.TimeoutError:
                logger.error("Redis pipeline timeout in welcome handler")
        
        with contextlib.suppress(Exception):
            await record_new_user(chat_id, uid)
        
        try:
            if getattr(user, "language_code", None):
                await redis_client.set(f"lang:{uid}", user.language_code.lower())
        except Exception:
            logger.debug("welcome(chat_member): failed to store language for %s", uid, exc_info=True)

        logger.info("User %s joined chat %s via ChatMemberUpdated", uid, chat_id)

        if not getattr(settings, "ENABLE_GROUP_AI_WELCOME", True):
            logger.info("Group welcomes disabled by flag for chat %s", chat_id)
            return

        if not await can_greet(chat_id):
            logger.info("Rate limit reached for %s in chat %s", uid, chat_id)
            return
        
        await _schedule_group_welcome_once(chat_id, user)

    except Exception:
        logger.exception("Error in on_user_join_via_chat_member for user %s in chat %s", uid, chat_id)


@dp.chat_member(
    ChatMemberUpdatedFilter(
        IS_MEMBER >> IS_NOT_MEMBER
    )
)
async def on_user_leave_group(update: ChatMemberUpdated) -> None:
    chat_id = update.chat.id
    user_id = update.old_chat_member.user.id
    await _clear_user_group_memory(chat_id, user_id)