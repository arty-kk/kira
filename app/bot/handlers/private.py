cat > app/bot/handlers/private.py << 'EOF'
# app/bot/handlers/private.py
import logging
import tempfile
import os
import re
import asyncio
import unicodedata

from pathlib import Path
from datetime import datetime, timedelta, time

from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.enums import ChatType
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
from typing import Tuple, Optional
from app.clients.telegram_client import get_bot
from app.bot.components.dispatcher import dp
from app.bot.components.constants import redis_client, WELCOME_MESSAGES
from app.bot.i18n.menu_translation import GENDER_LABELS, GENDER_PROMPT
from app.bot.handlers.payments import cmd_buy
from app.bot.utils.user_mode import get_user_mode, set_user_mode, UserMode
from app.bot.utils.debouncer import buffer_message_for_response
from app.config import settings
from app.tasks.message import process_message
from app.core.db import AsyncSessionLocal
from app.core.models import User
from app.core.memory import inc_msg_count, is_spam
from app.services.addons.personal_ping import register_private_activity
from app.services.responder.rag.topic_detector import is_on_topic
from app.services.user.user_service import (
    get_or_create_user,
    increment_usage,
)

logger = logging.getLogger(__name__)

bot = get_bot()


@dp.message(Command("start"), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message) -> None:

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

    try:
        await cb.answer(cache_time=1)
    except TelegramBadRequest:
        pass
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
    user_id = message.from_user.id

    if message.from_user.is_bot:
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
        text = await t(user_id, "private.off_topic_block")
        await bot.send_message(chat_id, text, parse_mode="HTML")
        return

    async with AsyncSessionLocal() as db:
        
        user = await get_or_create_user(db, message.from_user)

        if topic:
            today = datetime.now().date()
            key = f"on_topic_daily:{chat_id}:{today}"
            used = int(await redis_client.get(key) or 0)
            if used >= settings.ON_TOPIC_DAILY_LIMIT:
                msg = await t(user_id, "private.on_topic_limit")
                await bot.send_message(chat_id, msg)
                return
            await redis_client.incr(key)
            expire_at = datetime.combine(today + timedelta(days=1), time.min)
            await redis_client.expireat(key, int(expire_at.timestamp()))
        else:
            if user.free_requests_left + user.paid_requests <= 0:
                msg = await t(user_id, "private.need_purchase")
                await bot.send_message(chat_id, msg)
                await cmd_buy(message)
                return
            await increment_usage(db, user.id)
            await db.refresh(user)

        await inc_msg_count(chat_id)
        await redis_client.set(
            f"msg:{chat_id}:{message.message_id}",
            text,
            ex=300
        )
        payload = {
            "chat_id": chat_id,   
            "text": text,
            "user_id": user_id,
            "reply_to": message.reply_to_message and message.reply_to_message.message_id,
            "is_group": False,
            "msg_id": message.message_id,
        }
        buffer_message_for_response(payload)


@dp.message(Command("mode"), F.chat.type == ChatType.PRIVATE)
async def cmd_mode(message: Message, command: CommandObject | None = None) -> None:

    user_id = message.from_user.id
    arg = (command.args or "").strip().lower() if command else ""
    if arg in {m.value for m in UserMode}:
        await set_user_mode(user_id, UserMode(arg))
        text = await t(user_id, "mode.set", mode=arg)
        await message.reply(text, parse_mode="HTML")
        return

    cur = await get_user_mode(user_id)
    labels = {
        UserMode.AUTO: "Auto",
        UserMode.ON_TOPIC: "On-topic",
        UserMode.OFF_TOPIC: "Off-topic",
    }
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
    
    try:
        await cb.answer()
    except TelegramBadRequest:
        pass
    mode = cb.data.split(":", 1)[1]
    try:
        await set_user_mode(cb.from_user.id, UserMode(mode))
        text = await t(cb.from_user.id, "mode.set", mode=mode)
        await cb.message.edit_text(text, parse_mode="HTML")
    except ValueError:
        error = await t(cb.from_user.id, "mode.unknown")
        await cb.message.edit_text(error, parse_mode="HTML")


@dp.message(F.chat.type == ChatType.PRIVATE, F.content_type == ContentType.VOICE)
async def on_private_voice(message: Message) -> None:
    
    chat_id = message.chat.id
    user_id = message.from_user.id

    if await is_spam(chat_id, user_id) or message.from_user.is_bot:
        return

    asyncio.create_task(register_private_activity(user_id))

    try:
        seen = await redis_client.set(
            f"seen:{chat_id}:{message.message_id}",
            1,
            nx=True,
            ex=3 * 86_400,
        )
        if not seen:
            logger.info("Drop duplicate voice delivery chat=%s msg_id=%s", chat_id, message.message_id)
            return
    except Exception:
        logger.exception("failed to set seen-key for voice")

    with tempfile.NamedTemporaryFile(suffix=".oga", delete=False) as tmp:
        try:
            await bot.download(message.voice, tmp.name)
            tmp_path = tmp.name
        except Exception as e:
            logger.exception("Failed to download voice message", exc_info=e)
            return

    text: str | None = None
    try:
        client = get_openai()
        with open(tmp_path, "rb") as audio:
            resp = await client.audio.transcriptions.create(
                model="whisper-1",
                file=audio,
                response_format="text"
            )
        text = resp.strip()

        if not text:
            await message.reply("⚠️ Voice recognition failed. Please try again.")
            return

        try:
            await inc_msg_count(chat_id)
            await redis_client.set(
                f"msg:{chat_id}:{message.message_id}", text, ex=300
            )
        except Exception:
            logger.exception("failed to store transcribed voice message")

        payload = {
            "chat_id": chat_id,
            "text": text,
            "user_id": user_id,
            "reply_to": None,
            "is_group": False,
            "voice_in": True,
            "msg_id": message.message_id,
        }
        buffer_message_for_response(payload)

    except Exception as e:
        logger.error("Whisper transcription failed: %s", e, exc_info=True)
        await message.reply("⚠️ Voice recognition failed. Please try again.")
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
EOF
