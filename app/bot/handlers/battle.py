#app/bot/handlers/battle.py
import logging
from html import escape
from typing import Optional

from aiogram import F
from aiogram.enums import ChatType, MessageEntityType
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

import app.bot.components.constants as consts
from app.bot.components.constants import redis_client
from app.bot.components.dispatcher import dp
from app.clients.telegram_client import get_bot
from app.config import settings
from app.services.addons import group_battle as battle_service

logger = logging.getLogger(__name__)

bot = get_bot()


def _is_chat_allowed(chat) -> bool:
    try:
        cid = int(chat.id)
    except Exception:
        return False
    allowed_ids = set(getattr(settings, "ALLOWED_GROUP_IDS", []) or [])
    return cid in allowed_ids


def _normalize_cached_user_id(cached_value) -> Optional[str]:
    if cached_value is None:
        return None
    try:
        if isinstance(cached_value, (bytes, bytearray)):
            cached_value = cached_value.decode("utf-8")
        normalized = str(cached_value).strip()
        if not normalized:
            return None
        return str(int(normalized))
    except Exception:
        return None


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
            normalized_cached = _normalize_cached_user_id(cached)
            if normalized_cached is not None:
                return normalized_cached
            try:
                chat = await bot.get_chat(f"@{uname}")
                if chat and chat.id and chat.type != ChatType.CHANNEL:
                    return str(chat.id)
            except Exception:
                pass
    return None


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
    if target_id and target_id == str(consts.BOT_ID):
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
        me_cm = await bot.get_chat_member(chat_id, int(consts.BOT_ID))
        bot_disp = f"@{me_cm.user.username}" if me_cm.user.username else (me_cm.user.full_name or "Bot")
    except Exception:
        try:
            me = await bot.get_me()
            bot_disp = f"@{me.username}" if getattr(me, "username", None) else (
                getattr(me, "full_name", None) or getattr(me, "first_name", None) or "Bot"
            )
        except Exception:
            bot_disp = "Bot"

    text = (
        f"<b>Battle Stats with {escape(bot_disp)}</b>\n"
        f"• Games: <b>{total}</b>\n"
        f"• W/L/T: <b>{wins}</b>/<b>{losses}</b>/<b>{ties}</b>"
        f"{per_user_text}"
    )
    await message.reply(text, parse_mode="HTML", quote=True)


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
