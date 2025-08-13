cat > app/bot/handlers/welcome.py << 'EOF'
#app/bot/handlers/welcome.py
import asyncio
import logging
import re
import html
import time as time_module

from aiogram import F
from aiogram.enums import ChatType, ContentType
from aiogram.types import Message, ChatMemberUpdated, User
from aiogram.filters import ChatMemberUpdatedFilter, IS_NOT_MEMBER, IS_MEMBER

from app.clients.telegram_client import get_bot
from app.bot.components.dispatcher import dp
from app.bot.components.constants import redis_client
from app.core.memory import _k_g_sum_u
from app.config import settings
from app.services.addons.welcome_manager import generate_welcome, can_greet

logger = logging.getLogger(__name__)

bot = get_bot()

MEMORY_TTL = settings.MEMORY_TTL_DAYS * 86_400

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

            logger.info("Added new member %s to chat %s", uid, chat_id)

            if not await can_greet(chat_id):
                logger.info("Rate limit reached for %s in chat %s", uid, chat_id)
                continue

            logger.info("Scheduling welcome for %s in chat %s", uid, chat_id)
            asyncio.create_task(_handle_welcome(chat_id, user))

        except Exception:
            logger.exception("Error welcoming new member %s in chat %s", uid, chat_id)


@dp.chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER))
async def on_user_join_via_chat_member(update: ChatMemberUpdated) -> None:

    chat_id = update.chat.id
    user = update.new_chat_member.user
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

        logger.info("User %s joined chat %s via ChatMemberUpdated", uid, chat_id)

        if not await can_greet(chat_id):
            logger.info("Rate limit reached for %s in chat %s", uid, chat_id)
            return

        asyncio.create_task(_handle_welcome(chat_id, user))

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
    try:
        await redis_client.delete(_k_g_sum_u(chat_id, user_id))
        logger.info("Cleared personal summary for user %s in chat %s", user_id, chat_id)
    except Exception:
        logger.exception("Failed to clear personal summary for %s in chat %s", user_id, chat_id)

async def _handle_welcome(chat_id: int, user: User) -> None:
    
    text = ""

    try:
        text = await generate_welcome(chat_id, user, "")
        await bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception as e:
        logger.error("Welcome send failed, trying minimal HTML fallback", exc_info=e)
        logger.debug("Broken welcome HTML: %s", text or "<empty>")

        safe_plain = re.sub(r"<[^>]+>", "", text or "").strip() or "Welcome!"
        mention_html = (
            f'<a href="tg://user?id={user.id}">'
            f'{html.escape((user.full_name or str(user.id))[:64])}'
            f'</a>'
        )
        fallback_html = f"{mention_html} {html.escape(safe_plain)}".strip()

        try:
            await bot.send_message(chat_id, fallback_html, parse_mode="HTML")
        except Exception:
            logger.error("Minimal HTML still failed, sending plain text", exc_info=True)
            if user.username:
                mention_pt = f"@{user.username}"
            else:
                mention_pt = f"tg://user?id={user.id}"

            if mention_pt not in safe_plain:
                safe_plain = f"{mention_pt} {safe_plain}".strip()

            await bot.send_message(chat_id, safe_plain, parse_mode=None)
EOF