# app/bot/handlers/private.py

import logging

from pathlib import Path
from datetime import datetime, timedelta, time

from aiogram import F, types
from aiogram.enums import ChatType, ContentType
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    FSInputFile,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.clients.telegram_client import get_bot
from app.bot.components.dispatcher import dp
from app.bot.components.constants import redis_client, WELCOME_MESSAGES
from app.bot.handlers.payments import cmd_buy
from app.bot.utils.keep_typing import typing_indicator
from app.bot.utils.user_mode import get_user_mode, set_user_mode, UserMode
from app.config import settings
from app.tasks import process_message
from app.core import AsyncSessionLocal, inc_msg_count, is_spam
from app.services.addons.personal_ping import register_private_activity
from app.services.responder.rag.topic_detector import is_on_topic
from app.services.user import (
    get_or_create_user,
    compute_remaining,
    increment_usage,
)

logger = logging.getLogger(__name__)

bot = get_bot()

@dp.message(Command("start"), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message) -> None:

    cid = message.chat.id
    async with AsyncSessionLocal() as db:
        await get_or_create_user(db, message.from_user)

    languages = [
        ("🇺🇸 English", "en"),
        ("🇷🇺 Русский", "ru"),
        ("🇪🇸 Español", "es"),
        ("🇸🇦 العربية", "ar"),
        ("🇵🇹 Português", "pt"),
        ("🇮🇳 हिन्दी", "hi"),
        ("🇮🇩 Indonesia", "id"),
        ("🇹🇷 Türkçe", "tr"),
    ]
    kb = InlineKeyboardBuilder()
    for label, code in languages:
        kb.button(text=label, callback_data=f"lang:{code}")
    kb.adjust(1)

    await message.reply(
        "<b>🔎 Choose your language</b>",
        reply_markup=kb.as_markup(),
    )


@dp.callback_query(F.data.startswith("lang:"), F.message.chat.type == ChatType.PRIVATE)
async def set_language(cb: CallbackQuery) -> None:

    await cb.answer()
    lang = cb.data.split(":", 1)[1]
    await redis_client.set(f"lang:{cb.from_user.id}", lang)

    template = WELCOME_MESSAGES.get(lang, WELCOME_MESSAGES[settings.DEFAULT_LANG])
    text = template.format(full_name=cb.from_user.full_name, BOT_NAME=settings.BOT_NAME)

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🛒 Buy Requests"),
                KeyboardButton(text="🚀 Play GalaxyTap"),
                KeyboardButton(text="⚙️ Mode"),
            ]
        ],
        resize_keyboard=True,
    )
    video_path = Path(__file__).parent.parent / "media" / "Hi.mp4"
    await bot.send_video(
        chat_id=cb.from_user.id,
        video=FSInputFile(video_path),
        caption=text,
        parse_mode="HTML",
        reply_markup=kb,
    )


@dp.message(F.chat.type == ChatType.PRIVATE, F.text)
async def on_private_message(message: Message) -> None:

    chat_id = message.chat.id

    if await is_spam(chat_id, message.from_user.id) or message.from_user.is_bot or message.text.startswith("/"):
        return

    await register_private_activity(message.from_user.id)

    async with typing_indicator(chat_id):

        text = message.text.strip()
        if text in {"🚀 Play GalaxyTap", "🛒 Buy Requests", "⚙️ Mode"}:
            if text == "🚀 Play GalaxyTap":
                await bot.send_message(chat_id, "🔗 Click to Play 👉 https://t.me/galaxytap_bot?startapp")
            elif text == "🛒 Buy Requests":
                await bot.send_message(chat_id, "⌛ Opening purchase menu…")
                await cmd_buy(message)
            else:
                await bot.send_message(chat_id, "⌛ Opening mode menu…")
                await cmd_mode(message)
            return

        user_id = message.from_user.id
        user_mode = await get_user_mode(user_id)

    if user_mode == UserMode.OFF_TOPIC:
        topic = False
    else:
        try:
            topic = await is_on_topic(text)
        except Exception:
            logger.exception("is_on_topic error")
            topic = False

    if user_mode == UserMode.ON_TOPIC and not topic:
        await bot.send_message(
            chat_id, "🚫 Only on-topic messages are allowed. Switch with /mode."
        )
        return

    async with AsyncSessionLocal() as db:
        user = await get_or_create_user(db, message.from_user)

        if topic:
            today = datetime.now().date()
            key = f"on_topic_daily:{chat_id}:{today}"
            used = int(await redis_client.get(key) or 0)
            if used >= settings.ON_TOPIC_DAILY_LIMIT:
                await bot.send_message(chat_id, "⚠️ On-topic daily limit reached. Try again tomorrow.")
                return
            await redis_client.incr(key)
            expire_at = datetime.combine(today + timedelta(days=1), time.min)
            await redis_client.expireat(key, int(expire_at.timestamp()))
        else:
            if user.free_requests_left + user.paid_requests <= 0:
                await bot.send_message(chat_id, "⚠️ To continue, buy more Requests.")
                await cmd_buy(message)
                return
            await increment_usage(db, user.id)
            await db.refresh(user)

        remaining = compute_remaining(user)

        await inc_msg_count(chat_id)

        process_message.delay(
            chat_id=chat_id,
            text=text,
            placeholder_id=None,
            reply_to_message_id=None,
            user_id=user_id,
            remaining=remaining,
        )


@dp.message(Command("mode"), F.chat.type == ChatType.PRIVATE)
async def cmd_mode(message: Message, command: CommandObject | None = None) -> None:

    uid = message.from_user.id
    arg = (command.args or "").strip().lower() if command else ""
    if arg in {m.value for m in UserMode}:
        await set_user_mode(uid, UserMode(arg))
        await message.reply(f"✅ Mode set to <b>{arg}</b>.")
        return

    cur = await get_user_mode(uid)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=("✅ " if cur == m else "") + label,
                    callback_data=f"set_mode:{m.value}",
                )
                for m, label in [
                    (UserMode.AUTO, "Auto"),
                    (UserMode.ON_TOPIC, "On-topic only"),
                    (UserMode.OFF_TOPIC, "Off-topic only"),
                ]
            ]
        ]
    )
    await message.reply(
        f"⚙️ Current mode: <b>{cur.value}</b>\nChoose new one:",
        reply_markup=kb,
    )


@dp.callback_query(F.data.startswith("set_mode:"))
async def cb_set_mode(cb: CallbackQuery) -> None:
    await cb.answer()
    mode = cb.data.split(":", 1)[1]
    try:
        await set_user_mode(cb.from_user.id, UserMode(mode))
        await cb.message.edit_text(f"✅ Mode set to <b>{mode}</b>.")
    except ValueError:
        await cb.message.edit_text("❌ Unknown mode.")
