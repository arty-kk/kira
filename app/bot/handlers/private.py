cat > app/bot/handlers/private.py << EOF
# app/bot/handlers/private.py
import asyncio
import re
import logging

from pathlib import Path
from datetime import datetime, timedelta, time

from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    User, Message, CallbackQuery,
    InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup,
    FSInputFile,
)
from aiogram.utils.markdown import hlink
from app.clients.telegram_client import get_bot
from app.bot.components.dispatcher import dp
from app.bot.components.constants import redis_client, WELCOME_MESSAGES
from app.bot.i18n.menu_translation import GENDER_LABELS, GENDER_PROMPT
from app.bot.handlers.payments import cmd_buy
from app.bot.utils.keep_typing import typing_indicator
from app.bot.utils.user_mode import get_user_mode, set_user_mode, UserMode
from app.config import settings
from app.tasks.message import process_message
from sqlalchemy import update
from app.core.db import AsyncSessionLocal
from app.core.models import User
from app.core.memory import inc_msg_count, is_spam
from app.services.addons.welcome_manager import generate_welcome, can_greet
from app.services.addons.personal_ping import register_private_activity
from app.services.responder.rag.topic_detector import is_on_topic
from app.services.user.user_service import (
    get_or_create_user,
    compute_remaining,
    increment_usage,
)

logger = logging.getLogger(__name__)

bot = get_bot()


@dp.message(Command("start"), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message) -> None:

    async with AsyncSessionLocal() as db:
        await get_or_create_user(db, message.from_user)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🇷🇺 RUS", callback_data="lang:ru"),
            InlineKeyboardButton(text="🇺🇸 ENG", callback_data="lang:en"),
        ],
        [
            InlineKeyboardButton(text="🇵🇹 PT", callback_data="lang:pt"),
            InlineKeyboardButton(text="🇪🇸 ES", callback_data="lang:es"),
        ],
        [
            InlineKeyboardButton(text="🇸🇦 AR", callback_data="lang:ar"),
            InlineKeyboardButton(text="🇹🇷 TR", callback_data="lang:tr"),
        ],
        [
            InlineKeyboardButton(text="🇩🇪 DE", callback_data="lang:de"),
            InlineKeyboardButton(text="🇫🇷 FR", callback_data="lang:fr"),
        ],
        [
            InlineKeyboardButton(text="🇮🇩 ID", callback_data="lang:id"),
            InlineKeyboardButton(text="🇻🇳 VI", callback_data="lang:vi"),
        ],
    ])

    await message.reply(
        "<b>🔎 Choose your language</b>",
        reply_markup=kb,
    )


@dp.callback_query(F.data.startswith("lang:"), F.message.chat.type == ChatType.PRIVATE)
async def set_language(cb: CallbackQuery) -> None:

    try:
        await cb.answer(cache_time=1)
    except TelegramBadRequest:
        pass
    lang = cb.data.split(":", 1)[1]
    await redis_client.set(f"lang:{cb.from_user.id}", lang)

    male_label, female_label = GENDER_LABELS.get(
        lang,
        GENDER_LABELS[settings.DEFAULT_LANG]
    )
    prompt_text = GENDER_PROMPT.get(
        lang,
        GENDER_PROMPT[settings.DEFAULT_LANG]
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=male_label,   callback_data="gender:male"),
            InlineKeyboardButton(text=female_label, callback_data="gender:female"),
        ]
    ])
    await bot.send_message(
        chat_id=cb.from_user.id,
        text=prompt_text,
        parse_mode="HTML",
        reply_markup=kb,
    )


@dp.callback_query(F.data.startswith("gender:"), F.message.chat.type == ChatType.PRIVATE)
async def set_gender(cb: CallbackQuery) -> None:

    await cb.answer(cache_time=1)
    gender = cb.data.split(":", 1)[1]
    chat_id = cb.from_user.id
    user = cb.from_user

    async with AsyncSessionLocal() as db:
        await db.execute(
            update(User)
            .where(User.id == cb.from_user.id)
            .values(gender=gender)
        )
        await db.commit()

    raw_lang = await redis_client.get(f"lang:{chat_id}")
    lang = raw_lang.decode() if isinstance(raw_lang, (bytes, bytearray)) else raw_lang or settings.DEFAULT_LANG
    template = WELCOME_MESSAGES.get(lang, WELCOME_MESSAGES[settings.DEFAULT_LANG])
    text = template.format(full_name=cb.from_user.full_name, BOT_NAME=settings.BOT_NAME)

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🛒 Requests"),
                KeyboardButton(text="🎮 GalaxyTap"),
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
        if text in {"🎮 GalaxyTap", "🛒 Requests", "⚙️ Mode"}:
            if text == "🎮 GalaxyTap":
                await bot.send_message(chat_id, "🔗 Click to Play 👉 https://t.me/galaxytap_bot?startapp")
            elif text == "🛒 Requests":
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
            topic, _ = await is_on_topic(text)
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
                    (UserMode.ON_TOPIC, "GalaxyTap"),
                    (UserMode.OFF_TOPIC, "Off-topic"),
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
    try:
        await cb.answer(cache_time=1)
    except TelegramBadRequest:
        pass
    mode = cb.data.split(":", 1)[1]
    try:
        await set_user_mode(cb.from_user.id, UserMode(mode))
        await cb.message.edit_text(f"✅ Mode set to <b>{mode}</b>.")
    except ValueError:
        await cb.message.edit_text("❌ Unknown mode.")
EOF
