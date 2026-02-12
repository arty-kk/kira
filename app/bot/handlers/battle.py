#app/bot/handlers/battle.py
import logging
import re
from html import escape

from typing import Optional

from aiogram import F
from aiogram.enums import ChatType, MessageEntityType
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, CallbackQuery
from aiogram.exceptions import TelegramBadRequest

from app.clients.telegram_client import get_bot
from app.bot.components.dispatcher import dp
from app.bot.components.constants import redis_client, BOT_ID as SELF_BOT_ID, BOT_USERNAME as SELF_BOT_USERNAME
from app.config import settings
from app.services.addons import group_battle as battle_service
from app.tasks.battle import battle_launch_task

logger = logging.getLogger(__name__)

bot = get_bot()

BATTLE_ENQUEUE_DEDUP_TTL_SECONDS = 20


def _is_chat_allowed(chat) -> bool:
    try:
        cid = int(chat.id)
    except Exception:
        return False
    allowed_ids = set(getattr(settings, "ALLOWED_GROUP_IDS", []) or [])
    return cid in allowed_ids


async def _resolve_stats_target_user_id(message: Message) -> Optional[str]:

    chat_id = message.chat.id
    if message.reply_to_message and message.reply_to_message.from_user and not message.reply_to_message.from_user.is_bot:
        return str(message.reply_to_message.from_user.id)
    raw = message.text or message.caption or ""
    entities = (message.entities or []) + (message.caption_entities or [])
    for ent in entities:
        if ent.type == MessageEntityType.TEXT_MENTION and ent.user and not ent.user.is_bot:
            return str(ent.user.id)
    for ent in entities:
        if ent.type == MessageEntityType.MENTION:
            uname = raw[ent.offset + 1 : ent.offset + ent.length]  # без '@'
            cached = await redis_client.hget(f"user_map:{chat_id}", uname)
            if cached:
                return str(cached)
            try:
                chat = await bot.get_chat(f"@{uname}")
                if chat and chat.id and chat.type != ChatType.CHANNEL:
                    return str(chat.id)
            except Exception:
                pass
    return None


@dp.message(Command("battle"), F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]))
async def cmd_group_battle(message: Message, command: CommandObject | None = None) -> None:

    chat_id = message.chat.id
    if not _is_chat_allowed(message.chat):
        logger.info(
            "Ignore unauthorized group chat=%s uname=%s",
            chat_id,
            getattr(message.chat, "username", None),
        )
        return

    if redis_client is None:
        await message.reply("⏳ Initializing… try again in a few seconds.")
        return

    raw = message.text or ""
    all_entities = (message.entities or []) + (message.caption_entities or [])
    bot_un = (SELF_BOT_USERNAME or "").lower()
    if bot_un:
        pattern = rf"(^|\s)/battle@{re.escape(bot_un)}(\s|$)"
        if re.search(pattern, raw.lower()):
            return
        for ent in all_entities:
            if ent.type == MessageEntityType.TEXT_MENTION and ent.user and ent.user.id == SELF_BOT_ID:
                return
            if ent.type == MessageEntityType.MENTION:
                uname = raw[ent.offset + 1 : ent.offset + ent.length]
                if uname.lower() == bot_un:
                    return

    challenger_id = str(message.from_user.id)
    opponent_id: Optional[str] = None

    if message.reply_to_message and not message.reply_to_message.from_user.is_bot:
        opponent_id = str(message.reply_to_message.from_user.id)
    else:
        for ent in all_entities:
            if ent.type == MessageEntityType.TEXT_MENTION and ent.user:
                opponent_id = str(ent.user.id)
                break
            if ent.type == MessageEntityType.MENTION:
                raw = message.text or message.caption or ""
                username = raw[ent.offset + 1 : ent.offset + ent.length]
                cached = await redis_client.hget(f"user_map:{chat_id}", username)
                if cached:
                    opponent_id = str(cached)
                    break
                try:
                    chat = await bot.get_chat(f"@{username}")
                    opponent_id = str(chat.id)
                    break
                except TelegramBadRequest as e:
                    if "chat not found" not in str(e).lower():
                        logger.exception("Unexpected Telegram error for @%s: %s", username, e)

        if not opponent_id:
            opponent_id = str(SELF_BOT_ID)

    if opponent_id == challenger_id:
        await message.reply("🤔 You can’t battle yourself.", quote=True)
        return

    if opponent_id != str(SELF_BOT_ID) and await redis_client.sismember("battle:opt_out", opponent_id):
        await message.reply("🚫 That user has opted out of Battles.", quote=True)
        return
    if await redis_client.sismember("battle:opt_out", challenger_id):
        await message.reply("🚫 You opted out of Battles. Use /battle_on to opt in.", quote=True)
        return

    dedup_key = f"battle:req:{chat_id}:{challenger_id}:{opponent_id}"
    try:
        queued = await redis_client.set(dedup_key, 1, nx=True, ex=BATTLE_ENQUEUE_DEDUP_TTL_SECONDS)
        if not queued:
            return
        battle_launch_task.delay(challenger_id, opponent_id, chat_id)
        try:
            await message.delete()
        except Exception:
            pass
    except Exception:
        logger.exception("battle enqueue failed")
        await message.reply(
            "❌ Failed to start the battle (opponent may have left or an internal error occurred).",
            quote=True,
        )
        return


@dp.message(Command("battle_stats"), F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]))
async def cmd_battle_stats(message: Message) -> None:
    chat_id = message.chat.id
    if not _is_chat_allowed(message.chat):
        logger.info(
            "Ignore unauthorized group chat=%s uname=%s",
            chat_id,
            getattr(message.chat, "username", None),
        )
        return

    def _to_int(x) -> int:
        try:
            return int(x or 0)
        except Exception:
            return 0

    overall_key = f"battle:bot_stats:{chat_id}"
    try:
        wins, losses, ties = await redis_client.hmget(overall_key, "win", "loss", "tie")
    except Exception:
        logger.exception("Failed to read overall bot stats")
        wins = losses = ties = 0
    wins, losses, ties = _to_int(wins), _to_int(losses), _to_int(ties)
    total = wins + losses + ties

    target_id = await _resolve_stats_target_user_id(message)
    if target_id and target_id == str(SELF_BOT_ID):
        target_id = None
    per_user_text = ""
    if target_id:
        per_key = f"battle:bot_vs:{chat_id}:{target_id}"
        try:
            pw, pl, pt = await redis_client.hmget(per_key, "win", "loss", "tie")
        except Exception:
            logger.exception("Failed to read vs-user bot stats")
            pw = pl = pt = 0
        per_wins, per_losses, per_ties = _to_int(pw), _to_int(pl), _to_int(pt)
        ptotal = per_wins + per_losses + per_ties
        try:
            m = await bot.get_chat_member(chat_id, int(target_id))
            uname = f"@{m.user.username}" if m.user.username else (m.user.full_name or target_id)
        except Exception:
            uname = target_id
        per_user_text = (
            f"\n\n<b>Vs {escape(uname)}</b>\n"
            f"• Games: <b>{ptotal}</b>\n"
            f"• W/L/T: <b>{per_wins}</b>/<b>{per_losses}</b>/<b>{per_ties}</b>"
        )

    if total == 0 and not per_user_text:
        await message.reply("No battles recorded in this chat yet.", quote=True)
        return

    try:
        me_cm = await bot.get_chat_member(chat_id, int(SELF_BOT_ID))
        bot_disp = f"@{me_cm.user.username}" if me_cm.user.username else (me_cm.user.full_name or "Bot")
    except Exception:
        try:
            me = await bot.get_me()
            bot_disp = f"@{me.username}" if getattr(me, "username", None) else (getattr(me, "full_name", None) or getattr(me, "first_name", None) or "Bot")
        except Exception:
            bot_disp = "Bot"

    text = (
        f"<b>Battle Stats with {escape(bot_disp)}</b>\n"
        f"• Games: <b>{total}</b>\n"
        f"• W/L/T: <b>{wins}</b>/<b>{losses}</b>/<b>{ties}</b>"
        f"{per_user_text}"
    )
    await message.reply(text, parse_mode="HTML", quote=True)


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

@dp.callback_query(F.data.startswith("battle_start:"))
async def cb_battle_start(query: CallbackQuery) -> None:
    await battle_service.on_battle_start(query)

@dp.callback_query(F.data.startswith("battle_move:"))
async def cb_battle_move(query: CallbackQuery) -> None:
    await battle_service.on_battle_move(query)
