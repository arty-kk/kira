# app/bot/handlers/battle.py
import logging
import uuid

from typing import Optional

from aiogram import F
from aiogram.enums import ChatType, MessageEntityType
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from aiogram.exceptions import TelegramBadRequest

from app.clients.telegram_client import get_bot
from app.bot.components.dispatcher import dp
from app.bot.components.constants import redis_client
from app.services.addons import group_battle as battle_service

logger = logging.getLogger(__name__)

bot = get_bot()

@dp.message(Command("battle"), F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]))
async def cmd_group_battle(message: Message, command: CommandObject | None = None) -> None:

    if redis_client is None:
        await message.reply("⏳ Initializing… try again in a few seconds.")
        return

    chat_id = message.chat.id
    challenger_id = str(message.from_user.id)
    opponent_id: Optional[str] = None

    if message.reply_to_message and not message.reply_to_message.from_user.is_bot:
        opponent_id = str(message.reply_to_message.from_user.id)
    else:
        for ent in message.entities or []:
            if ent.type == MessageEntityType.TEXT_MENTION and ent.user:
                opponent_id = str(ent.user.id)
                break
            if ent.type == MessageEntityType.MENTION:
                username = message.text[ent.offset + 1 : ent.offset + ent.length]
                cached = await redis_client.hget(f"user_map:{chat_id}", username)
                if cached:
                    opponent_id = str(cached)
                    break
                try:
                    chat = await bot.get_chat(username)
                    opponent_id = str(chat.id)
                    break
                except TelegramBadRequest as e:
                    if "chat not found" not in str(e).lower():
                        logger.exception("Unexpected Telegram error for @%s: %s", username, e)

        if not opponent_id:
            await message.reply(
                "❌ To start a battle, do one of:\n"
                "• Reply to a user’s message with `/battle`\n"
                "• Mention them by typing `/battle @username`",
                quote=True,
            )
            return

    if opponent_id == challenger_id:
        await message.reply("🤔 You can’t battle yourself.", quote=True)
        return

    if await redis_client.sismember("battle:opt_out", opponent_id):
        await message.reply("🚫 That user has opted out of Battles.", quote=True)
        return
    if await redis_client.sismember("battle:opt_out", challenger_id):
        await message.reply("🚫 You opted out of Battles. Use /battle_on to opt in.", quote=True)
        return

    active_key = f"active_game:{chat_id}"
    gid = str(uuid.uuid4())
    ttl = int((battle_service.T_START + battle_service.SAFETY).total_seconds())
    locked = await redis_client.set(active_key, gid, ex=ttl, nx=True)
    if not locked:
        await message.reply("⚠️ A battle is already in progress. Please wait.", quote=True)
        return

    try:
        await battle_service.launch_battle(challenger_id, opponent_id)
        try:
            await message.delete()
        except Exception:
            pass
    except Exception:
        await redis_client.delete(active_key)
        logger.exception("launch_battle failed")
        await message.reply(
            "❌ Failed to start the battle (opponent may have left or an internal error occurred).",
            quote=True,
        )
        return


@dp.message(
    F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]),
    F.text.regexp(r"(^|\s)/battle(\s|$)"),
)
async def cmd_group_battle_loose(message: Message) -> None:
    await cmd_group_battle(message, None)


@dp.message(Command("battle_off"), F.chat.type == ChatType.PRIVATE)
async def cmd_battle_off(message: Message) -> None:
    await redis_client.sadd("battle:opt_out", str(message.from_user.id))
    await message.reply("✅ You’ve opted out of Battles.")


@dp.message(Command("battle_on"), F.chat.type == ChatType.PRIVATE)
async def cmd_battle_on(message: Message) -> None:
    await redis_client.srem("battle:opt_out", str(message.from_user.id))
    await message.reply("✅ You’ve opted in to Battles.")
