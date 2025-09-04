cat > app/bot/handlers/private.py << 'EOF'
# app/bot/handlers/private.py
import logging
import tempfile
import os
import re
import io
import base64
import asyncio
import unicodedata

from pathlib import Path
from contextlib import suppress
from PIL import Image, UnidentifiedImageError
from datetime import datetime, timedelta, time

from aiogram import F
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter, TelegramNetworkError, TelegramForbiddenError
from aiogram.enums import ChatType, ContentType, ChatAction
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove,
    FSInputFile,
)

from app.bot.i18n import t
from app.clients.telegram_client import get_bot
from app.bot.components.dispatcher import dp
from app.bot.components.constants import redis_client, WELCOME_MESSAGES
from app.bot.handlers.payments import cmd_buy
from app.bot.utils.user_mode import get_user_mode, set_user_mode, UserMode
from app.bot.utils.debouncer import buffer_message_for_response
from app.config import settings
from sqlalchemy import update
from app.core.db import AsyncSessionLocal
from app.core.models import User
from app.core.memory import inc_msg_count, is_spam
from app.clients.openai_client import get_openai
from app.services.addons.personal_ping import register_private_activity
from app.services.addons.welcome_manager import generate_private_welcome
from app.services.responder.rag.relevance import is_relevant
from app.services.user.user_service import (
    get_or_create_user,
    increment_usage,
)

logger = logging.getLogger(__name__)

bot = get_bot()

async def _typing_loop_pm(chat_id: int) -> None:
    try:
        while True:
            try:
                await bot.send_chat_action(chat_id, ChatAction.TYPING)
                await asyncio.sleep(5)
            except TelegramRetryAfter as e:
                delay = max(1.0, float(getattr(e, "retry_after", 1)))
                await asyncio.sleep(delay)
            except (TelegramNetworkError, asyncio.TimeoutError, TimeoutError):
                await asyncio.sleep(2)
            except TelegramForbiddenError:
                break
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("typing loop error for pm chat_id=%s", chat_id, exc_info=True)

async def build_quick_links_kb(user_id: int) -> ReplyKeyboardMarkup | None:

    buttons = []
    if getattr(settings, "SHOW_REQUESTS_BUTTON", True):
        buttons.append(KeyboardButton(text=await t(user_id, "menu.requests")))
    if getattr(settings, "SHOW_LINK_BUTTON", True):
        buttons.append(KeyboardButton(text=await t(user_id, "menu.link")))
    if getattr(settings, "SHOW_TOKEN_BUTTON", True):
        buttons.append(KeyboardButton(text=await t(user_id, "menu.token")))
    if getattr(settings, "SHOW_MODE_BUTTON", True):
        buttons.append(KeyboardButton(text=await t(user_id, "menu.mode")))
    if getattr(settings, "SHOW_FAQ_BUTTON", True):
        buttons.append(KeyboardButton(text=await t(user_id, "menu.faq")))

    if not buttons:
        return None

    rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


@dp.message(Command("start"), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message) -> None:

    async with AsyncSessionLocal() as db:
        await get_or_create_user(db, message.from_user)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🇺🇸 EN", callback_data="lang:en"),
            InlineKeyboardButton(text="🇷🇺 RU", callback_data="lang:ru"),
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

    text = await t(message.from_user.id, "private.choose_lang")
    await message.reply(text, reply_markup=kb, parse_mode="HTML")
    await set_user_mode(message.from_user.id, UserMode.OFF_TOPIC)


@dp.callback_query(F.data.startswith("lang:"), F.message.chat.type == ChatType.PRIVATE)
async def set_language(cb: CallbackQuery) -> None:

    try:
        await cb.answer(cache_time=1)
    except TelegramBadRequest:
        pass

    try:
        if getattr(settings, "CLEAR_SETUP_MESSAGES", True):
            await cb.message.delete()
        else:
            await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    lang = cb.data.split(":", 1)[1]
    allowed_langs = {"ru","en","es","pt","de","fr","tr","ar","id","vi"}
    if lang not in allowed_langs:
        lang = settings.DEFAULT_LANG

    await redis_client.set(f"lang:{cb.from_user.id}", lang)

    prompt_text  = await t(cb.from_user.id, "gender.prompt")
    male_label   = await t(cb.from_user.id, "gender.male")
    female_label = await t(cb.from_user.id, "gender.female")

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

    try:
        await cb.answer(cache_time=1)
    except TelegramBadRequest:
        pass

    try:
        if getattr(settings, "CLEAR_SETUP_MESSAGES", True):
            await cb.message.delete()
        else:
            await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    gender = cb.data.split(":", 1)[1]
    chat_id = cb.from_user.id

    if gender not in {"male", "female"}:
        return

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

    kb = await build_quick_links_kb(cb.from_user.id)

    static_sent = False
    if getattr(settings, "ENABLE_PRIVATE_STATIC_WELCOME", True):
        video_path = Path(__file__).parent.parent / "media" / "Hi.mp4"
        try:
            await bot.send_video(
                chat_id=cb.from_user.id,
                video=FSInputFile(video_path),
                caption=text,
                parse_mode="HTML",
                reply_markup=kb,
            )
            static_sent = True
        except Exception:
            await bot.send_message(
                chat_id=cb.from_user.id,
                text=text,
                parse_mode="HTML",
                reply_markup=kb,
            )
            static_sent = True

    if getattr(settings, "ENABLE_PRIVATE_AI_WELCOME", True):
        typing_task = asyncio.create_task(_typing_loop_pm(cb.from_user.id))
        try:
            ai_text = await generate_private_welcome(chat_id=chat_id, user=cb.from_user)
            await bot.send_message(
                chat_id=cb.from_user.id,
                text=ai_text,
                parse_mode=None,
                reply_markup=(None if static_sent else kb),
            )
        except Exception:
            logger.exception("Failed to generate/send private AI welcome")
        finally:
            typing_task.cancel()
            with suppress(asyncio.CancelledError):
                await typing_task


@dp.message(F.chat.type == ChatType.PRIVATE, F.text)
async def on_private_message(message: Message) -> None:

    chat_id = message.chat.id
    user_id = message.from_user.id

    if message.from_user.is_bot:
        return

    try:
        seen = await redis_client.set(
            f"seen:{chat_id}:{message.message_id}",
            1,
            nx=True,
            ex=86_400,
        )
        if not seen:
            logger.info("Drop duplicate delivery chat=%s msg_id=%s", chat_id, message.message_id)
            return
    except Exception:
        logger.exception("failed to set seen-key, proceeding but may risk dupes")

    asyncio.create_task(register_private_activity(user_id))

    text = (message.text or "").strip()

    def _norm_btn(s: str) -> str:
        s = unicodedata.normalize("NFKC", s or "")
        s = s.replace("\uFE0F", "").replace("\uFE0E", "").replace("\u200D", "").replace("\u00A0", " ")
        s = "".join(ch for ch in s if unicodedata.category(ch) != "Cf")
        s = " ".join(s.split())
        return s.casefold()

    req_label  = await t(user_id, "menu.requests")
    game_label = await t(user_id, "menu.link")
    token_label = await t(user_id, "menu.token")
    mode_label = await t(user_id, "menu.mode")
    faq_label = await t(user_id, "menu.faq")
    text_n = _norm_btn(text)
    req_n  = _norm_btn(req_label or "")
    game_n = _norm_btn(game_label or "")
    token_n  = _norm_btn(token_label or "")
    mode_n = _norm_btn(mode_label or "")
    faq_n = _norm_btn(faq_label or "")

    if text_n in {game_n, token_n, req_n, mode_n, faq_n}:
        if text_n == game_n:
            url = await t(user_id, "private.play_url")
            if not url or "http" not in (url or ""):
                link_text = await t(user_id, "private.play_link")
                m = re.search(r'(https?://\S+)', link_text or "")
                url = m.group(1) if m else "https://t.me/galaxytap_bot?startapp"
            btn_text = game_label or "🎮 GalaxyTap"
            kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text=btn_text, url=url)]]
            )
            await bot.send_message(
                chat_id,
                await t(user_id, "private.play_link"),
                reply_markup=kb,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        elif text_n == token_n:
            token_text = await t(user_id, "private.token_text", BOT_NAME=settings.BOT_NAME)
            token_url  = await t(user_id, "private.token_url")
            btn_text   = await t(user_id, "private.token_button")
            kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text=btn_text, url=token_url)]]
            )
            await bot.send_message(
                chat_id,
                token_text,
                reply_markup=kb,
                parse_mode="HTML",
                disable_web_page_preview=False,
            )
        elif text_n == req_n:
            await cmd_buy(message)
        elif text_n == faq_n:
            about = await t(user_id, "faq.about", BOT_NAME=settings.BOT_NAME)
            await bot.send_message(chat_id, about, parse_mode="HTML")
        else:
            await cmd_mode(message)
        return
    if await is_spam(chat_id, user_id):
        return
    if text.startswith("/"):
        return

    user_mode = await get_user_mode(user_id)

    if user_mode == UserMode.OFF_TOPIC:
        topic = False
    else:
        try:
            topic, _hits = await is_relevant(
                text,
                model=settings.EMBEDDING_MODEL,
                threshold=settings.RELEVANCE_THRESHOLD,
                return_hits=False,
            )
        except Exception:
            logger.exception("relevance gate error")
            topic = False

    if user_mode == UserMode.ON_TOPIC and not topic:
        text = await t(user_id, "private.off_topic_block")
        await bot.send_message(chat_id, text, parse_mode="HTML")
        return

    async with AsyncSessionLocal() as db:
        
        user = await get_or_create_user(db, message.from_user)
        allow_web = False

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

            will_use_paid = (user.free_requests_left <= 0 and user.paid_requests > 0)
            allow_web = bool(will_use_paid)

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
            "allow_web": allow_web,
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
        inline_keyboard=[[
            InlineKeyboardButton(
                text=("✅ " if cur == m else "") + labels[m],
                callback_data=f"set_mode:{m.value}",
            )
            for m in UserMode
        ]]
    )
    header = await t(user_id, "mode.current", mode=labels[cur])
    body = "\n".join([
        await t(user_id, "mode.auto"),
        await t(user_id, "mode.on_topic"),
        await t(user_id, "mode.off_topic"),
    ])
    await message.reply(f"{header}\n\n{body}", reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("set_mode:"), F.message.chat.type == ChatType.PRIVATE)
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

    try:
        seen = await redis_client.set(
            f"seen:{chat_id}:{message.message_id}",
            1,
            nx=True,
            ex=86_400,
        )
        if not seen:
            logger.info("Drop duplicate voice delivery chat=%s msg_id=%s", chat_id, message.message_id)
            return
    except Exception:
        logger.exception("failed to set seen-key for voice")

    asyncio.create_task(register_private_activity(user_id))

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".oga", delete=False) as tmp:
            tmp_path = tmp.name
        await bot.download(message.voice, tmp_path)
    except Exception as e:
        logger.exception("Failed to download voice message", exc_info=e)
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
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

        user_mode = await get_user_mode(user_id)

        if user_mode == UserMode.OFF_TOPIC:
            topic = False
        else:
            try:
                topic, _hits = await is_relevant(
                    text,
                    model=settings.EMBEDDING_MODEL,
                    threshold=settings.RELEVANCE_THRESHOLD,
                    return_hits=False,
                )
            except Exception:
                logger.exception("relevance gate error (voice)")
                topic = False

        if user_mode == UserMode.ON_TOPIC and not topic:
            msg = await t(user_id, "private.off_topic_block")
            await message.reply(msg, parse_mode="HTML")
            return

        async with AsyncSessionLocal() as db:

            user = await get_or_create_user(db, message.from_user)
            allow_web = False

            if topic:
                today = datetime.now().date()
                key = f"on_topic_daily:{chat_id}:{today}"
                used = int(await redis_client.get(key) or 0)
                if used >= settings.ON_TOPIC_DAILY_LIMIT:
                    msg = await t(user_id, "private.on_topic_limit")
                    await message.reply(msg)
                    return
                await redis_client.incr(key)
                expire_at = datetime.combine(today + timedelta(days=1), time.min)
                await redis_client.expireat(key, int(expire_at.timestamp()))
            else:
                if user.free_requests_left + user.paid_requests <= 0:
                    msg = await t(user_id, "private.need_purchase")
                    await message.reply(msg)
                    await cmd_buy(message)
                    return

                will_use_paid = (user.free_requests_left <= 0 and user.paid_requests > 0)
                allow_web = bool(will_use_paid)

                await increment_usage(db, user.id)
                await db.refresh(user)

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
            "allow_web": allow_web,
        }
        buffer_message_for_response(payload)

    except Exception as e:
        logger.error("Whisper transcription failed: %s", e, exc_info=True)
        await message.reply("⚠️ Voice recognition failed. Please try again.")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_MAX_SIDE = 2048
_ALLOWED_FORMATS = {"JPEG","JPG","PNG","WEBP"}
_MAX_FRAMES = 1

def _is_single_media(message: Message) -> bool:
    return message.media_group_id is None

async def _download_to_tmp(message: Message, suffix: str) -> str | None:
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name
        await bot.download(message, tmp_path)
        return tmp_path
    except Exception:
        logger.exception("Failed to download image")
        return None

async def _strict_image_load(tmp_path: str) -> Image.Image:
    try:
        with Image.open(tmp_path) as im:
            fmt = (im.format or "").upper()
            if fmt == "JPG": fmt = "JPEG"
            if fmt not in _ALLOWED_FORMATS:
                raise ValueError(f"Unsupported image format: {fmt}")
            im.verify()
        im2 = Image.open(tmp_path)
        im2.load()
        return im2
    except UnidentifiedImageError:
        raise ValueError("Not an image or corrupted file")
    except Exception as e:
        raise ValueError(str(e))

def _sanitize_and_compress(img: Image.Image) -> bytes:
    try:
        n_frames = getattr(img, "n_frames", 1)
        if int(n_frames) > _MAX_FRAMES:
            raise ValueError("Animated images are not allowed")
    except Exception:
        pass
    if img.mode != "RGB":
        img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > _MAX_SIDE:
        s = _MAX_SIDE / float(max(w, h))
        img = img.resize((int(w*s), int(h*s)))
    for q in (85, 80, 75, 70, 60, 50):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=q, optimize=True)  # без EXIF
        data = buf.getvalue()
        if len(data) <= _MAX_IMAGE_BYTES:
            return data
    img = img.resize((max(1, img.size[0]//2), max(1, img.size[1]//2)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=60, optimize=True)
    data = buf.getvalue()
    if len(data) > _MAX_IMAGE_BYTES:
        raise ValueError("Image too large after compression")
    return data

def _reject_multi_or_oversize_and_reply(chat_id: int, reason: str):
    asyncio.create_task(bot.send_message(
        chat_id,
        f"⚠️ Cannot process image: {reason}\nPlease send exactly one image (≤ 5 MB) in a single message."
    ))


@dp.message(F.chat.type == ChatType.PRIVATE, F.content_type == ContentType.PHOTO)
async def on_private_photo(message: Message) -> None:
    chat_id = message.chat.id
    user_id = message.from_user.id

    if await is_spam(chat_id, user_id) or message.from_user.is_bot:
        return
    if not _is_single_media(message):
        _reject_multi_or_oversize_and_reply(chat_id, "albums are not supported")
        return
    try:
        seen = await redis_client.set(f"seen:{chat_id}:{message.message_id}", 1, nx=True, ex=86_400)
        if not seen:
            logger.info("Drop duplicate photo delivery chat=%s msg_id=%s", chat_id, message.message_id)
            return
    except Exception:
        logger.exception("failed to set seen-key for photo")

    asyncio.create_task(register_private_activity(user_id))

    biggest = message.photo[-1]
    if (biggest.file_size or 0) > _MAX_IMAGE_BYTES:
        _reject_multi_or_oversize_and_reply(chat_id, "file is larger than 5 MB")
        return

    caption = (message.caption or "").strip()
    user_mode = await get_user_mode(user_id)
    if user_mode == UserMode.OFF_TOPIC:
        topic = False
    else:
        try:
            if caption:
                topic, _ = await is_relevant(
                    caption,
                    model=settings.EMBEDDING_MODEL,
                    threshold=settings.RELEVANCE_THRESHOLD,
                    return_hits=False
                )
            else:
                topic = True
        except Exception:
            logger.exception("relevance gate error (photo)")
            topic = False
    if user_mode == UserMode.ON_TOPIC and not topic:
        text = await t(user_id, "private.off_topic_block")
        await bot.send_message(chat_id, text, parse_mode="HTML")
        return
    async with AsyncSessionLocal() as db:

        user = await get_or_create_user(db, message.from_user)
        allow_web = False

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

            will_use_paid = (user.free_requests_left <= 0 and user.paid_requests > 0)
            allow_web = bool(will_use_paid)

            await increment_usage(db, user.id)
            await db.refresh(user)

    tmp_path = None
    try:
        tmp_path = await _download_to_tmp(biggest, suffix=".jpg")
        if not tmp_path:
            _reject_multi_or_oversize_and_reply(chat_id, "download failed"); return
        img = await _strict_image_load(tmp_path)
        safe_jpeg = _sanitize_and_compress(img)
        if len(safe_jpeg) > _MAX_IMAGE_BYTES:
            _reject_multi_or_oversize_and_reply(chat_id, "file is larger than 5 MB after compression"); return

        caption = (message.caption or "").strip()

        memo = ("[Image attached]" + (f" {caption}" if caption else ""))
        await inc_msg_count(chat_id)
        await redis_client.set(f"msg:{chat_id}:{message.message_id}", memo, ex=300)

        payload = {
            "chat_id": chat_id,
            "text": caption,
            "user_id": user_id,
            "reply_to": (message.reply_to_message and message.reply_to_message.message_id),
            "is_group": False,
            "msg_id": message.message_id,
            "image_b64": base64.b64encode(safe_jpeg).decode("ascii"),
            "image_mime": "image/jpeg",
            "allow_web": allow_web,
        }
        buffer_message_for_response(payload)

    except ValueError as ve:
        logger.warning("Image validation failed: %s", ve)
        _reject_multi_or_oversize_and_reply(chat_id, str(ve))
    except Exception:
        logger.exception("Image processing failed")
        _reject_multi_or_oversize_and_reply(chat_id, "internal error")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try: os.remove(tmp_path)
            except Exception: pass


@dp.message(F.chat.type == ChatType.PRIVATE, F.content_type == ContentType.DOCUMENT)
async def on_private_document_image(message: Message) -> None:
    chat_id = message.chat.id
    user_id = message.from_user.id

    if await is_spam(chat_id, user_id) or message.from_user.is_bot:
        return
    if not _is_single_media(message):
        _reject_multi_or_oversize_and_reply(chat_id, "albums are not supported"); return
    doc = message.document
    if not doc or not (doc.mime_type or "").startswith("image/"):
        return
    if (doc.file_size or 0) > _MAX_IMAGE_BYTES:
        _reject_multi_or_oversize_and_reply(chat_id, "file is larger than 5 MB"); return

    tmp_path = None

    asyncio.create_task(register_private_activity(user_id))

    caption = (message.caption or "").strip()
    user_mode = await get_user_mode(user_id)
    if user_mode == UserMode.OFF_TOPIC:
        topic = False
    else:
        try:
            if caption:
                topic, _ = await is_relevant(
                    caption,
                    model=settings.EMBEDDING_MODEL,
                    threshold=settings.RELEVANCE_THRESHOLD,
                    return_hits=False
                )
            else:
                topic = True
        except Exception:
            logger.exception("relevance gate error (doc-image)")
            topic = False
    if user_mode == UserMode.ON_TOPIC and not topic:
        text = await t(user_id, "private.off_topic_block")
        await bot.send_message(chat_id, text, parse_mode="HTML")
        return
    async with AsyncSessionLocal() as db:

        user = await get_or_create_user(db, message.from_user)
        allow_web = False

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

            will_use_paid = (user.free_requests_left <= 0 and user.paid_requests > 0)
            allow_web = bool(will_use_paid)

            await increment_usage(db, user.id)
            await db.refresh(user)

    try:
        suffix = ".jpg" if (doc.mime_type or "").lower() in ("image/jpeg","image/jpg") else ".png"
        tmp_path = await _download_to_tmp(doc, suffix=suffix)
        if not tmp_path:
            _reject_multi_or_oversize_and_reply(chat_id, "download failed"); return
        img = await _strict_image_load(tmp_path)
        safe_jpeg = _sanitize_and_compress(img)
        if len(safe_jpeg) > _MAX_IMAGE_BYTES:
            _reject_multi_or_oversize_and_reply(chat_id, "file is larger than 5 MB after compression"); return

        caption = (message.caption or "").strip()
        memo = ("[Image attached]" + (f" {caption}" if caption else ""))
        await inc_msg_count(chat_id)
        await redis_client.set(f"msg:{chat_id}:{message.message_id}", memo, ex=300)

        payload = {
            "chat_id": chat_id,
            "text": caption,
            "user_id": user_id,
            "reply_to": (message.reply_to_message and message.reply_to_message.message_id),
            "is_group": False,
            "msg_id": message.message_id,
            "image_b64": base64.b64encode(safe_jpeg).decode("ascii"),
            "image_mime": "image/jpeg",
            "allow_web": allow_web,
        }
        buffer_message_for_response(payload)

    except ValueError as ve:
        logger.warning("Document image validation failed: %s", ve)
        _reject_multi_or_oversize_and_reply(chat_id, str(ve))
    except Exception:
        logger.exception("Document image processing failed")
        _reject_multi_or_oversize_and_reply(chat_id, "internal error")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try: os.remove(tmp_path)
            except Exception: pass
EOF