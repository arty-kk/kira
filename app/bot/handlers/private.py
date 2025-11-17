#app/bot/handlers/private.py
import logging
import tempfile
import os
import re
import io
import html
import base64
import json
import asyncio
import unicodedata

from pathlib import Path
from functools import wraps
from typing import Any
from sqlalchemy.sql import func
from sqlalchemy import update, select, delete
from contextlib import suppress
from PIL import Image, ImageOps, UnidentifiedImageError

try:
    RESAMPLING = Image.Resampling.LANCZOS
except AttributeError:
    RESAMPLING = Image.LANCZOS

from aiogram import F
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter, TelegramNetworkError, TelegramForbiddenError
from aiogram.enums import ChatType, ContentType, ChatAction
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, FSInputFile,
)

from app.bot.i18n import t
from app.bot.i18n.menu_translation import LANG_BUTTONS
from app.clients.telegram_client import get_bot
from app.bot.components.dispatcher import dp
from app.bot.components.constants import redis_client, WELCOME_MESSAGES
from app.bot.handlers.payments import cmd_buy
from app.bot.utils.debouncer import buffer_message_for_response
from app.bot.utils.spam_guard import pm_block_guard
from app.bot.utils.telegram_safe import send_message_safe, send_video_safe
from app.config import settings
from app.core.db import session_scope
from app.core.models import User, ApiKey, ApiKeyStats
from app.core.memory import inc_msg_count, is_spam, cache_gender
from app.services.addons.personal_ping import register_private_activity
from app.tasks.welcome import send_private_ai_welcome_task
from app.services.user.user_service import get_or_create_user, increment_usage
from app.services.addons.analytics import record_user_message
from app.emo_engine import get_persona
from app.emo_engine.persona.constants.user_prefs import (
    ZODIAC, ZODIAC_SET, TEMP_PRESETS, SOCIALITY_SET, ARCHETYPES, MAX_ARCH,
    normalize_prefs, merge_prefs,
)
from app.api.api_keys import (
    create_key,
    deactivate_key,
    list_keys_for_user,
)

logger = logging.getLogger(__name__)
bot = get_bot()

ALLOWED_LANGS = set(LANG_BUTTONS.keys())
SAFE_URL_RX = re.compile(r'^(?:https?://|tg://)[^\s<>"\']{1,2048}$', flags=re.IGNORECASE)
MIN_JPEG_QUALITY = int(getattr(settings, "MIN_JPEG_QUALITY", 35))
MIN_SIDE = int(getattr(settings, "MIN_IMAGE_SIDE", 720))

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_SIDE = 2048
ALLOWED_FORMATS = {"JPEG", "JPG", "PNG", "WEBP"}
MAX_FRAMES = 1
Image.MAX_IMAGE_PIXELS = int(getattr(settings, "MAX_IMAGE_PIXELS", 36_000_000))

MAX_VOICE_BYTES = int(getattr(settings, "MAX_VOICE_BYTES", 25 * 1024 * 1024))
MAX_VOICE_DURATION = int(getattr(settings, "MAX_VOICE_DURATION_SEC", 300))
ALLOWED_VOICE_MIMES = {
    "audio/ogg", "audio/opus", "audio/mpeg", "audio/mp3",
    "audio/wav", "audio/x-wav", "audio/webm",
    "audio/mp4", "audio/m4a", "audio/aac",
}

WZ_KEY = "persona:wizard:{uid}"
WZ_TTL = getattr(settings, "PERSONA_WIZARD_TTL_SEC", 604800)

ZODIAC_BADGES = {
    "Aries": "♈︎", "Taurus": "♉︎", "Gemini": "♊︎", "Cancer": "♋︎",
    "Leo": "♌︎", "Virgo": "♍︎", "Libra": "♎︎", "Scorpio": "♏︎",
    "Sagittarius": "♐︎", "Capricorn": "♑︎", "Aquarius": "♒︎", "Pisces": "♓︎",
}

def key_fingerprint(key_hash: str | None) -> str:
    if not key_hash:
        return "unknown"
    h = key_hash.strip()
    if len(h) <= 8:
        return h
    return f"{h[:4]}…{h[-4:]}"


def safe_url(u: str | None) -> str | None:
    u = (u or "").strip()
    return u if SAFE_URL_RX.match(u) else None


def _pct01(x: float) -> int:
    try:
        return int(round(max(0.0, min(1.0, float(x))) * 100))
    except Exception:
        return 0


def _bar(p: float, width: int = 10) -> str:
    try:
        p = max(0.0, min(1.0, float(p)))
    except Exception:
        p = 0.0
    filled = int(round(p * width))
    return ("█" * filled) + ("░" * (width - filled))


def dedupe_callback(ttl: int = 2):
    def deco(fn):
        @wraps(fn)
        async def wrapper(cb: CallbackQuery, *args, **kwargs):
            try:
                key = f"seen:cb:{cb.id}"
                seen = await redis_client.set(key, 1, nx=True, ex=ttl)
                if not seen:
                    with suppress(TelegramBadRequest):
                        await cb.answer(cache_time=1)
                    return
            except Exception:
                pass
            return await fn(cb, *args, **kwargs)
        return wrapper
    return deco


async def _cb_ack(cb: CallbackQuery, text: str | None = None, alert: bool = False, cache: int = 1) -> None:
    with suppress(TelegramBadRequest):
        await cb.answer(text=text, show_alert=alert, cache_time=cache)


async def _delete_or_hide(msg) -> None:
    try:
        await msg.delete()
    except TelegramBadRequest:
        with suppress(TelegramBadRequest):
            await msg.edit_reply_markup(reply_markup=None)


async def _replace_panel(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    await _cb_ack(cb)
    await _delete_or_hide(cb.message)
    await send_message_safe(bot, cb.from_user.id, text, parse_mode="HTML", reply_markup=kb)


async def build_quick_links_kb(user_id: int) -> ReplyKeyboardMarkup | None:
    buttons = []
    if getattr(settings, "SHOW_REQUESTS_BUTTON", True):
        buttons.append(KeyboardButton(text=await t(user_id, "menu.requests")))
    if getattr(settings, "SHOW_CHANNEL_BUTTON", True):
        buttons.append(KeyboardButton(text=await t(user_id, "menu.link")))
    if getattr(settings, "SHOW_TOKEN_BUTTON", True):
        buttons.append(KeyboardButton(text=await t(user_id, "menu.token")))
    if getattr(settings, "SHOW_FAQ_BUTTON", True):
        buttons.append(KeyboardButton(text=await t(user_id, "menu.faq")))
    if getattr(settings, "SHOW_PERSONA_BUTTON", True):
        buttons.append(KeyboardButton(text=await t(user_id, "menu.persona")))
    if getattr(settings, "SHOW_API_BUTTON", True):
        buttons.append(KeyboardButton(text=await t(user_id, "menu.api")))
    if not buttons:
        return None
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


async def _show_main_panel(uid: int, text: str) -> None:
    kb = await build_quick_links_kb(uid)
    await send_message_safe(bot, uid, text, parse_mode="HTML", reply_markup=kb)


async def _send_private_welcome(uid: int, *, full_name: str | None = None) -> None:
    
    async with session_scope(stmt_timeout_ms=2000) as db:
        res = await db.execute(
            update(User)
            .where(User.id == uid, User.pm_welcome_sent.is_(None))
            .values(pm_welcome_sent=func.now())
            .returning(User.id)
        )
        claimed = res.scalar() is not None

    if not claimed:
        return

    raw_lang = await redis_client.get(f"lang_ui:{uid}") or await redis_client.get(f"lang:{uid}")
    lang = (raw_lang.decode() if isinstance(raw_lang, (bytes, bytearray)) else raw_lang) or getattr(settings, "DEFAULT_LANG", "en")
    template = (WELCOME_MESSAGES.get(lang)
                or WELCOME_MESSAGES.get(getattr(settings, "DEFAULT_LANG", ""), "")
                or next(iter(WELCOME_MESSAGES.values()), ""))
    safe_name = html.escape(full_name or "", quote=True)
    text = template.format(full_name=safe_name, BOT_NAME=settings.BOT_NAME)
    kb = await build_quick_links_kb(uid)
    
    sent_any = False

    if getattr(settings, "ENABLE_PRIVATE_STATIC_WELCOME", True):
        video_enabled = getattr(settings, "ENABLE_PRIVATE_WELCOME_VIDEO", False)

        if video_enabled:
            try:
                video_path = Path(__file__).parent.parent / "media" / "video.mp4"
                msg_v = await send_video_safe(
                    bot, chat_id=uid, video=FSInputFile(video_path),
                    caption=text, parse_mode="HTML", reply_markup=kb,
                )
                sent_any = bool(msg_v)
            except Exception:
                logger.debug("static welcome video failed", exc_info=True)

        if (not video_enabled) or (not sent_any):
            await send_message_safe(bot, uid, text, parse_mode="HTML", reply_markup=kb)
            sent_any = True

    if getattr(settings, "ENABLE_PRIVATE_AI_WELCOME", True):
        try:
            send_private_ai_welcome_task.delay(uid)
        except Exception:
            logger.exception("Failed to schedule private AI welcome for %s", uid)


async def _first_delivery(chat_id: int, msg_id: int, kind: str, ttl: int = 86_400) -> bool:
    try:
        seen = await redis_client.set(f"seen:{chat_id}:{msg_id}", 1, nx=True, ex=ttl)
        if not seen:
            logger.info("Drop duplicate %s delivery chat=%s msg_id=%s", kind, chat_id, msg_id)
            return False
    except Exception:
        logger.exception("failed to set seen-key for %s", kind)
    return True


async def _ensure_access_and_increment(message: Message, text_for_guard: str | None) -> tuple[User, bool] | None:
    chat_id, user_id = message.chat.id, message.from_user.id
    if await pm_block_guard(bot, t, user_id=user_id, chat_id=chat_id, text=text_for_guard):
        return None
    asyncio.create_task(register_private_activity(user_id))
    async with session_scope(stmt_timeout_ms=2000) as db:
        user = await get_or_create_user(db, message.from_user)
    if user.free_requests + user.paid_requests <= 0:
        msg = await t(user_id, "private.need_purchase")
        await send_message_safe(bot, chat_id, msg, parse_mode="HTML")
        await cmd_buy(message)
        return None
    allow_web = bool(user.free_requests <= 0 and user.paid_requests > 0)
    async with session_scope(stmt_timeout_ms=2000) as db:
        await increment_usage(db, user.id)
    return user, allow_web


async def _store_context(chat_id: int, msg_id: int, text: str) -> None:
    await inc_msg_count(chat_id)
    await redis_client.set(
        f"msg:{chat_id}:{msg_id}",
        text.strip(),
        ex=getattr(settings, "REPLY_CONTEXT_TTL_SEC", 86400),
    )


def _norm_btn(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("\uFE0F", "").replace("\uFE0E", "").replace("\u200D", "").replace("\u00A0", " ")
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Cf")
    s = " ".join(s.split())
    return s.casefold()


@dp.message(Command("start"), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message) -> None:
    async with session_scope(stmt_timeout_ms=2000) as db:
        user = await get_or_create_user(db, message.from_user)
        try:
            if not getattr(user, "persona_prefs", None):
                try:
                    base_temp = json.loads(settings.PERSONA_TEMPERAMENT)
                    if not isinstance(base_temp, dict):
                        raise TypeError()
                except Exception:
                    base_temp = {"sanguine": 0.4, "choleric": 0.25, "phlegmatic": 0.20, "melancholic": 0.15}
                defaults = normalize_prefs({
                    "zodiac": settings.PERSONA_ZODIAC,
                    "temperament": base_temp,
                    "sociality": "ambivert",
                    "archetypes": [],
                })
                await db.execute(
                    update(User)
                    .where(
                        User.id == user.id,
                        User.persona_prefs.in_([None, {}]),
                    )
                    .values(persona_prefs=defaults)
                )
        except Exception:
            logger.debug("init default persona_prefs failed", exc_info=True)

    ordered_langs = ["en", "ru"] + sorted(ALLOWED_LANGS - {"en", "ru"})
    rows, row = [], []
    for code in ordered_langs:
        label = LANG_BUTTONS.get(code, code.upper())
        row.append(InlineKeyboardButton(text=label, callback_data=f"lang:{code}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    text = await t(message.from_user.id, "private.choose_lang")
    await send_message_safe(bot, chat_id=message.chat.id, text=text, parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data.startswith("lang:"), F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def set_language(cb: CallbackQuery) -> None:
    await _cb_ack(cb)
    await _delete_or_hide(cb.message)
    lang = cb.data.split(":", 1)[1]
    if lang not in ALLOWED_LANGS:
        default_lang = getattr(settings, "DEFAULT_LANG", "en")
        lang = default_lang if default_lang in ALLOWED_LANGS else "en"
    await redis_client.set(f"lang:{cb.from_user.id}", lang)
    await redis_client.set(f"lang_ui:{cb.from_user.id}", lang)

    prompt_text = await t(cb.from_user.id, "gender.prompt")
    male_label = await t(cb.from_user.id, "gender.male")
    female_label = await t(cb.from_user.id, "gender.female")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=male_label, callback_data="gender:male"),
        InlineKeyboardButton(text=female_label, callback_data="gender:female"),
    ]])
    await send_message_safe(bot, chat_id=cb.from_user.id, text=prompt_text, parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data.startswith("gender:"), F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def set_gender(cb: CallbackQuery) -> None:
    await _cb_ack(cb)
    await _delete_or_hide(cb.message)
    gender = cb.data.split(":", 1)[1]
    if gender not in {"male", "female"}:
        return
    async with session_scope(stmt_timeout_ms=2000) as db:
        await db.execute(update(User).where(User.id == cb.from_user.id).values(gender=gender))
    await cache_gender(cb.from_user.id, gender)
    await start_persona_wizard(cb.message)


@dp.message(F.chat.type == ChatType.PRIVATE, F.text)
async def on_private_message(message: Message) -> None:
    chat_id, user_id = message.chat.id, message.from_user.id
    if message.from_user.is_bot:
        return
    if not await _first_delivery(chat_id, message.message_id, "chat"):
        return

    text = (message.text or "").strip()
    try:
        has_link = bool(re.search(r'(?:https?://|tg://)', text, re.I))
        asyncio.create_task(
            record_user_message(
                chat_id, user_id,
                display_name=message.from_user.full_name,
                content_type="text",
                addressed_to_bot=True,
                has_link=has_link,
            )
        )
    except Exception:
        pass
    if await pm_block_guard(bot, t, user_id=user_id, chat_id=chat_id, text=text):
        return
    asyncio.create_task(register_private_activity(user_id))

    req_label = await t(user_id, "menu.requests")
    channel_label = await t(user_id, "menu.link")
    token_label = await t(user_id, "menu.token")
    faq_label = await t(user_id, "menu.faq")
    persona_label = await t(user_id, "menu.persona")
    api_label = await t(user_id, "menu.api")
    mapping = {
        _norm_btn(channel_label or ""): "channel",
        _norm_btn(token_label or ""): "token",
        _norm_btn(req_label or ""): "req",
        _norm_btn(faq_label or ""): "faq",
        _norm_btn(persona_label or ""): "persona",
        _norm_btn(api_label or ""): "api",
    }
    text_n = _norm_btn(text)
    if text_n in mapping:
        kind = mapping[text_n]
        if kind == "channel":
            raw_url = await t(user_id, "private.channel_url")
            url = safe_url(raw_url)
            if not url:
                link_text = await t(user_id, "private.channel")
                m = re.search(r'((?:https?://|tg://)\S+)', link_text or "")
                url = safe_url(m.group(1) if m else None)
            btn_text = channel_label
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=btn_text, url=url)]]) if url else None
            await send_message_safe(
                bot, chat_id, await t(user_id, "private.channel"),
                reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True,
            )
        elif kind == "token":
            token_text = await t(user_id, "private.token_text", BOT_NAME=settings.BOT_NAME)
            token_url = safe_url(await t(user_id, "private.token_url"))
            btn_text = await t(user_id, "private.token_button")
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=btn_text, url=token_url)]]) if token_url else None
            await send_message_safe(bot, chat_id, token_text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=False)
        elif kind == "req":
            await cmd_buy(message)
        elif kind == "faq":
            about = await t(user_id, "faq.about", BOT_NAME=settings.BOT_NAME)
            await send_message_safe(bot, chat_id, about, parse_mode="HTML")
        elif kind == "persona":
            await start_persona_wizard(message)
        elif kind == "api":
            await show_api_menu(message)
        return

    if await is_spam(chat_id, user_id) or text.startswith("/"):
        return

    res = await _ensure_access_and_increment(message, text)
    if not res:
        return
    _, allow_web = res

    await _store_context(chat_id, message.message_id, text)
    payload = {
        "chat_id": chat_id,
        "text": text,
        "user_id": user_id,
        "reply_to": (message.reply_to_message and message.reply_to_message.message_id),
        "is_group": False,
        "msg_id": message.message_id,
        "trigger": "pm",
        "allow_web": allow_web,
    }
    buffer_message_for_response(payload)


def is_single_media(message: Message) -> bool:
    return message.media_group_id is None


async def download_to_tmp(tg_obj: Any, suffix: str) -> str | None:
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name
        await bot.download(tg_obj, tmp_path)
        return tmp_path
    except Exception:
        logger.exception("Failed to download image")
        if tmp_path and os.path.exists(tmp_path):
            with suppress(Exception):
                os.remove(tmp_path)
        return None


async def strict_image_load(tmp_path: str) -> Image.Image:
    try:
        with Image.open(tmp_path) as im:
            fmt = (im.format or "").upper()
            if fmt == "JPG":
                fmt = "JPEG"
            if fmt not in ALLOWED_FORMATS:
                raise ValueError(f"Unsupported image format: {fmt}")
            im.verify()
        with Image.open(tmp_path) as im2:
            im2.load()
            try:
                im2 = ImageOps.exif_transpose(im2)
            except Exception:
                pass
            return im2.copy()
    except UnidentifiedImageError:
        raise ValueError("Not an image or corrupted file")
    except Image.DecompressionBombError:
        raise ValueError("Image too large (decompression bomb)")
    except Exception as e:
        raise ValueError(str(e))


def sanitize_and_compress(img: Image.Image) -> bytes:

    n_frames = int(getattr(img, "n_frames", 1) or 1)
    if n_frames > MAX_FRAMES:
        raise ValueError("Animated or multi-frame images are not allowed")

    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")

    w, h = img.size
    if max(w, h) > MAX_SIDE:
        s = MAX_SIDE / float(max(w, h))
        img = img.resize((int(w*s), int(h*s)), resample=RESAMPLING)

    def _save_as_jpeg(jimg: Image.Image, q: int) -> bytes:
        buf = io.BytesIO()
        try:
            jimg.save(
                buf, format="JPEG",
                quality=q, optimize=True,progressive=True,
                subsampling=2, exif=b""
            )
        except OSError:
            try:
                buf.seek(0); buf.truncate(0)
                jimg.save(
                    buf, format="JPEG",
                    quality=q, optimize=True, progressive=False,
                    subsampling=2, exif=b""
                )
            except OSError:
                buf.seek(0); buf.truncate(0)
                jimg.save(
                    buf, format="JPEG",
                    quality=q, progressive=False,
                    subsampling=2, exif=b""
                )
        return buf.getvalue()

    quality_steps = [85, 80, 75, 70, 65, 60, 55, 50, 45, 40, MIN_JPEG_QUALITY]
    for _ in range(6):
        for q in quality_steps:
            data = _save_as_jpeg(img, q)
            if len(data) <= MAX_IMAGE_BYTES:
                return data
        cur_max = max(img.size)
        if cur_max <= MIN_SIDE:
            break
        new_max = max(MIN_SIDE, int(cur_max * 0.85))
        s = new_max / float(cur_max)
        img = img.resize((max(1, int(img.size[0]*s)), max(1, int(img.size[1]*s))), resample=RESAMPLING)

    img = img.resize((max(1, img.size[0]//2), max(1, img.size[1]//2)), resample=RESAMPLING)
    data = _save_as_jpeg(img, max(60, MIN_JPEG_QUALITY))
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Image too large after compression")
    return data


async def localized_image_error(user_id: int | None, reason: str) -> str:
    if user_id:
        try:
            msg = await t(user_id, "errors.image_generic", reason=reason)
            if msg:
                return msg
        except Exception:
            pass
    return f"⚠️ Cannot process image: {reason}\nPlease send exactly one image (≤ 5 MB) in a single message."


def reject_multi_or_oversize_and_reply(chat_id: int, reason: str, user_id: int | None = None):
    async def _send():
        msg = await localized_image_error(user_id, reason)
        await send_message_safe(bot, chat_id, msg, parse_mode="HTML")
    asyncio.create_task(_send())


async def localized_voice_error(user_id: int | None, reason: str) -> str:
    if user_id:
        try:
            msg = await t(user_id, "errors.voice_generic", reason=reason)
            if msg:
                return msg
        except Exception:
            pass
    return f"⚠️ Cannot process voice message: {reason}"


def reject_voice_and_reply(chat_id: int, reason: str, user_id: int | None = None):
    async def _send():
        msg = await localized_voice_error(user_id, reason)
        await send_message_safe(bot, chat_id, msg, parse_mode="HTML")
    asyncio.create_task(_send())


async def _handle_image_payload(message: Message, caption: str, jpeg_bytes: bytes, allow_web: bool) -> None:
    chat_id, user_id = message.chat.id, message.from_user.id
    memo = "[Image attached]" + (f" {caption}" if caption else "")
    await _store_context(chat_id, message.message_id, memo)
    payload = {
        "chat_id": chat_id,
        "text": caption,
        "user_id": user_id,
        "reply_to": (message.reply_to_message and message.reply_to_message.message_id),
        "is_group": False,
        "msg_id": message.message_id,
        "image_b64": base64.b64encode(jpeg_bytes).decode("ascii"),
        "image_mime": "image/jpeg",
        "trigger": "pm",
        "allow_web": allow_web,
    }
    buffer_message_for_response(payload)


@dp.message(F.chat.type == ChatType.PRIVATE, F.content_type == ContentType.PHOTO)
async def on_private_photo(message: Message) -> None:
    chat_id, user_id = message.chat.id, message.from_user.id
    if await is_spam(chat_id, user_id) or message.from_user.is_bot:
        return
    if not is_single_media(message):
        reject_multi_or_oversize_and_reply(chat_id, "albums are not supported", user_id)
        return
    if not await _first_delivery(chat_id, message.message_id, "photo"):
        return

    try:
        has_link = bool(re.search(r'(?:https?://|tg://)', (message.caption or ""), re.I))
        asyncio.create_task(
            record_user_message(
                chat_id, user_id,
                display_name=message.from_user.full_name,
                content_type="photo",
                addressed_to_bot=True,
                has_link=has_link,
            )
        )
    except Exception:
        pass
    res = await _ensure_access_and_increment(message, (message.caption or "").strip() or None)
    if not res:
        return
    _, allow_web = res

    tmp_path = None
    try:
        biggest = message.photo[-1]
        caption = (message.caption or "").strip()
        tmp_path = await download_to_tmp(biggest, suffix=".jpg")
        if not tmp_path:
            reject_multi_or_oversize_and_reply(chat_id, "download failed", user_id)
            return
        img = await strict_image_load(tmp_path)
        safe_jpeg = sanitize_and_compress(img)
        if len(safe_jpeg) > MAX_IMAGE_BYTES:
            reject_multi_or_oversize_and_reply(chat_id, "file is larger than 5 MB after compression", user_id)
            return
        await _handle_image_payload(message, caption, safe_jpeg, allow_web)
    except ValueError as ve:
        logger.warning("Image validation failed: %s", ve)
        reject_multi_or_oversize_and_reply(chat_id, str(ve), user_id)
    except Exception:
        logger.exception("Image processing failed")
        reject_multi_or_oversize_and_reply(chat_id, "internal error", user_id)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            with suppress(Exception):
                os.remove(tmp_path)


@dp.message(F.chat.type == ChatType.PRIVATE, F.content_type == ContentType.DOCUMENT)
async def on_private_document_image(message: Message) -> None:
    chat_id, user_id = message.chat.id, message.from_user.id
    if await is_spam(chat_id, user_id) or message.from_user.is_bot:
        return
    if not await _first_delivery(chat_id, message.message_id, "document"):
        return
    if not is_single_media(message):
        reject_multi_or_oversize_and_reply(chat_id, "albums are not supported", user_id)
        return

    doc = message.document
    if not doc or not (doc.mime_type or "").startswith("image/"):
        return

    MAX_DOC_IMAGE_BYTES = int(getattr(settings, "MAX_DOC_IMAGE_BYTES", 30 * 1024 * 1024))
    try:
        if getattr(doc, "file_size", 0) and int(doc.file_size) > MAX_DOC_IMAGE_BYTES:
            reject_multi_or_oversize_and_reply(chat_id, "file is too large", user_id)
            return
    except Exception:
        pass

    allowed_mimes = {"image/jpeg", "image/jpg", "image/pjpeg", "image/png", "image/x-png", "image/webp"}
    if (doc.mime_type or "").lower() not in allowed_mimes:
        reject_multi_or_oversize_and_reply(chat_id, "unsupported image format", user_id)
        return

    try:
        has_link = bool(re.search(r'(?:https?://|tg://)', (message.caption or ""), re.I))
        asyncio.create_task(
            record_user_message(
                chat_id, user_id,
                display_name=message.from_user.full_name,
                content_type="document",
                addressed_to_bot=True,
                has_link=has_link,
            )
        )
    except Exception:
        pass
    res = await _ensure_access_and_increment(message, (message.caption or "").strip() or None)
    if not res:
        return
    _, allow_web = res

    tmp_path = None

    try:
        caption = (message.caption or "").strip()
        mime_lower = (doc.mime_type or "").lower()
        if mime_lower in ("image/jpeg", "image/jpg", "image/pjpeg"):
            suffix = ".jpg"
        elif mime_lower == "image/webp":
            suffix = ".webp"
        else:
            suffix = ".png"
        tmp_path = await download_to_tmp(doc, suffix=suffix)
        if not tmp_path:
            reject_multi_or_oversize_and_reply(chat_id, "download failed", user_id)
            return
        img = await strict_image_load(tmp_path)
        safe_jpeg = sanitize_and_compress(img)
        if len(safe_jpeg) > MAX_IMAGE_BYTES:
            reject_multi_or_oversize_and_reply(chat_id, "file is larger than 5 MB after compression", user_id)
            return
        await _handle_image_payload(message, caption, safe_jpeg, allow_web)
    except ValueError as ve:
        logger.warning("Document image validation failed: %s", ve)
        reject_multi_or_oversize_and_reply(chat_id, str(ve), user_id)
    except Exception:
        logger.exception("Document image processing failed")
        reject_multi_or_oversize_and_reply(chat_id, "internal error", user_id)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            with suppress(Exception):
                os.remove(tmp_path)


@dp.message(F.chat.type == ChatType.PRIVATE, F.content_type == ContentType.VOICE)
async def on_private_voice(message: Message) -> None:
    chat_id, user_id = message.chat.id, message.from_user.id
    if await is_spam(chat_id, user_id) or message.from_user.is_bot:
        return
    if not await _first_delivery(chat_id, message.message_id, "voice"):
        return

    try:
        asyncio.create_task(
            record_user_message(
                chat_id, user_id,
                display_name=message.from_user.full_name,
                content_type="voice",
                addressed_to_bot=True,
                has_link=False,
            )
        )
    except Exception:
        pass

    voice = message.voice
    
    size = int(getattr(voice, "file_size", 0) or 0) if getattr(voice, "file_size", None) is not None else 0
    if size <= 0:
        reject_voice_and_reply(chat_id, "empty file", user_id)
        return
    if size > MAX_VOICE_BYTES:
        reject_voice_and_reply(chat_id, "file is too large", user_id)
        return

    duration = int(getattr(voice, "duration", 0) or 0) if getattr(voice, "duration", None) is not None else 0
    if duration and duration > MAX_VOICE_DURATION:
        reject_voice_and_reply(chat_id, "voice message is too long", user_id)
        return

    mime = (getattr(voice, "mime_type", None) or "").lower().strip()
    if mime and mime not in ALLOWED_VOICE_MIMES:
        reject_voice_and_reply(chat_id, "unsupported audio format", user_id)
        return

    voice_file_id = getattr(voice, "file_id", None)
    if not voice_file_id:
        reject_voice_and_reply(chat_id, "internal error (no file id)", user_id)
        return

    res = await _ensure_access_and_increment(message, text_for_guard=None)
    if not res:
        return
    _, allow_web = res

    payload = {
        "chat_id": chat_id,
        "text": None,
        "user_id": user_id,
        "reply_to": (message.reply_to_message and message.reply_to_message.message_id),
        "is_group": False,
        "voice_in": True,
        "voice_file_id": voice_file_id,
        "msg_id": message.message_id,
        "trigger": "pm",
        "allow_web": allow_web,
        "entities": [],
    }
    buffer_message_for_response(payload)


async def _wiz_get(uid: int) -> dict:
    key = WZ_KEY.format(uid=uid)
    raw = await redis_client.get(key)
    try:
        data = json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else (raw or "{}")) if raw else {}
        if raw:
            with suppress(Exception):
                await redis_client.expire(key, WZ_TTL)
        return data
    except Exception:
        return {}


async def _wiz_require(uid: int) -> dict | None:
    st = await _wiz_get(uid)
    try:
        exists = await redis_client.exists(WZ_KEY.format(uid=uid))
    except Exception:
        exists = False
    if exists:
        return st or {}
    with suppress(Exception):
        await send_message_safe(bot, uid, await t(uid, "persona.expired") or "Session expired. Starting over.", parse_mode="HTML")
    class _FakeMessage:
        chat = type("C", (), {"id": uid})
        from_user = type("U", (), {"id": uid})
    await start_persona_wizard(_FakeMessage())
    return None


async def _wiz_set(uid: int, data: dict) -> None:
    await redis_client.set(WZ_KEY.format(uid=uid), json.dumps(data, ensure_ascii=False), ex=WZ_TTL)


async def _wiz_clear(uid: int) -> None:
    await redis_client.delete(WZ_KEY.format(uid=uid))


async def _wiz_hydrate_from_db(uid: int, st: dict | None = None) -> dict:
    st = dict(st or {})
    async with session_scope(read_only=True, stmt_timeout_ms=2000) as db:
        user = await db.get(User, uid)
        prefs = (getattr(user, "persona_prefs", None) or {})
    if isinstance(prefs, dict):
        if isinstance(prefs.get("zodiac"), str):
            st.setdefault("zodiac", prefs["zodiac"])
        tmap = prefs.get("temperament")
        if isinstance(tmap, dict) and tmap:
            st.setdefault("temperament", tmap)
            with suppress(Exception):
                st.setdefault("temperament_key", max(tmap, key=tmap.get))
        if isinstance(prefs.get("sociality"), str):
            st.setdefault("sociality", prefs["sociality"])
        if isinstance(prefs.get("archetypes"), list):
            st.setdefault("archetypes", list(prefs["archetypes"]))
    return st


def _rows_kv(items: list[tuple[str, str]], n: int, kind: str) -> list[list[InlineKeyboardButton]]:
    rows, row = [], []
    for label, key in items:
        row.append(InlineKeyboardButton(text=label, callback_data=f"persona:pick:{kind}:{key}"))
        if len(row) == n:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


async def start_persona_wizard(message: Message) -> None:
    
    user_id = message.chat.id

    if not getattr(settings, "SHOW_PERSONA_BUTTON", True):

        try:
            await _wiz_clear(user_id)
        except Exception:
            logger.debug("start_persona_wizard: _wiz_clear failed", exc_info=True)

        await _show_main_panel(
            user_id,
            await t(user_id, "menu.main") or "Main Menu",
        )

        try:
            async with session_scope(read_only=True, stmt_timeout_ms=2000) as db:
                u = await db.get(User, user_id)
                already = bool(u and u.pm_welcome_sent)
            if not already:
                full_name = getattr(getattr(message, "from_user", None), "full_name", None)
                await _send_private_welcome(user_id, full_name=full_name)
        except Exception:
            logger.debug("start_persona_wizard: welcome send failed", exc_info=True)

        return

    await _wiz_clear(user_id)
    st = await _wiz_hydrate_from_db(user_id, {})
    st.setdefault("step", "start")
    await _wiz_set(user_id, st)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=await t(user_id, "persona.next"), callback_data="persona:step:zodiac")],
        [InlineKeyboardButton(text=await t(user_id, "persona.reset"), callback_data="persona:reset")],
        [InlineKeyboardButton(text=await t(user_id, "persona.cancel"), callback_data="persona:cancel")],
    ])
    await send_message_safe(
        bot,
        message.chat.id,
        await t(user_id, "persona.start"),
        parse_mode="HTML",
        reply_markup=kb,
    )



@dp.callback_query(F.data == "persona:cancel", F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def persona_cancel(cb: CallbackQuery) -> None:
    await _cb_ack(cb)
    await _delete_or_hide(cb.message)
    uid = cb.from_user.id
    try:
        await _wiz_clear(uid)
    except Exception:
        logger.debug("persona_cancel: _wiz_clear failed", exc_info=True)
    await _show_main_panel(uid, await t(uid, "persona.cancel.ok") or "Canceled.")
    try:
        async with session_scope(read_only=True, stmt_timeout_ms=2000) as db:
            u = await db.get(User, uid)
            already = bool(u and u.pm_welcome_sent)
        if not already:
            await _send_private_welcome(uid, full_name=cb.from_user.full_name)
    except Exception:
        logger.debug("persona_cancel: welcome send failed", exc_info=True)


@dp.callback_query(F.data == "persona:reset", F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def persona_reset(cb: CallbackQuery) -> None:
    uid = cb.from_user.id
    await _cb_ack(cb)
    await _wiz_clear(uid)
    try:
        try:
            base_temp = json.loads(settings.PERSONA_TEMPERAMENT)
            if not isinstance(base_temp, dict):
                raise TypeError()
        except Exception:
            base_temp = {"sanguine": 0.4, "choleric": 0.25, "phlegmatic": 0.20, "melancholic": 0.15}
        defaults = normalize_prefs({
            "zodiac": settings.PERSONA_ZODIAC,
            "temperament": base_temp,
            "sociality": "ambivert",
            "archetypes": [],
        })
        async with session_scope(stmt_timeout_ms=2000) as db:
            await db.execute(update(User).where(User.id == uid).values(persona_prefs=defaults))
        p = await get_persona(chat_id=uid)
        p.apply_overrides(defaults)
    except Exception:
        logger.debug("persona_reset: write defaults failed; falling back to runtime reset", exc_info=True)
        p = await get_persona(chat_id=uid)
        p.apply_overrides(None, reset=True)

    await _delete_or_hide(cb.message)
    await _show_main_panel(uid, await t(uid, "persona.reset.ok") or "Persona reset to defaults. Main menu is below.")


@dp.callback_query(F.data == "persona:step:zodiac", F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def step_zodiac(cb: CallbackQuery) -> None:
    uid = cb.from_user.id
    st = await _wiz_require(uid)
    if st is None:
        await _cb_ack(cb)
        return
    st["step"] = "zodiac"
    await _wiz_set(uid, st)
    picked = st.get("zodiac")
    items = []
    for z in ZODIAC:
        badge = ZODIAC_BADGES.get(z, "")
        label = f"{badge} {z}".strip()
        if z == picked:
            label = f"✅ {label}"
        items.append((label, z))
    kb = InlineKeyboardMarkup(inline_keyboard=_rows_kv(items, 4, "zodiac") + [
        [InlineKeyboardButton(text=await t(uid, "persona.skip"), callback_data="persona:step:temperament")]
    ])
    await _cb_ack(cb)
    await _delete_or_hide(cb.message)
    await send_message_safe(bot, uid, await t(uid, "persona.zodiac.title"), parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data.startswith("persona:pick:"), F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def pick_generic(cb: CallbackQuery) -> None:
    uid = cb.from_user.id
    _, _, rest = cb.data.partition("persona:pick:")
    kind, _, value = rest.partition(":")
    st = await _wiz_get(uid)
    cur = st.get("step")

    if not cur:
        st = await _wiz_hydrate_from_db(uid, st)
        st["step"] = {"zodiac": "zodiac", "temp": "temperament", "sociality": "sociality"}.get(kind, "zodiac")
        await _wiz_set(uid, st)
        cur = st["step"]

    if kind == "zodiac" and cur == "zodiac" and value in ZODIAC_SET:
        st["zodiac"] = value
        st["step"] = "temperament"
    elif kind == "temp" and cur == "temperament" and value in TEMP_PRESETS:
        st["temperament"] = TEMP_PRESETS[value]
        st["temperament_key"] = value
        st["step"] = "sociality"
    elif kind == "sociality" and cur == "sociality" and value in SOCIALITY_SET:
        st["sociality"] = value
        st["step"] = "archetypes"
    await _wiz_set(uid, st)

    nxt = st.get("step")
    if nxt == "temperament":
        await show_temperament(cb)
    elif nxt == "sociality":
        await show_sociality(cb)
    elif nxt == "archetypes":
        await show_archetypes(cb)
    else:
        await _cb_ack(cb)
        await step_zodiac(cb)


async def show_temperament(cb: CallbackQuery) -> None:
    uid = cb.from_user.id
    st = await _wiz_require(uid)
    if st is None:
        await _cb_ack(cb)
        return
    st["step"] = "temperament"
    await _wiz_set(uid, st)
    sel = st.get("temperament_key")
    items = [
        ((("✅ " if sel == "sanguine" else "") + (await t(uid, "persona.temp.sanguine"))), "sanguine"),
        ((("✅ " if sel == "choleric" else "") + (await t(uid, "persona.temp.choleric"))), "choleric"),
        ((("✅ " if sel == "phlegmatic" else "") + (await t(uid, "persona.temp.phlegmatic"))), "phlegmatic"),
        ((("✅ " if sel == "melancholic" else "") + (await t(uid, "persona.temp.melancholic"))), "melancholic"),
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=_rows_kv(items, 2, "temp") + [
        [InlineKeyboardButton(text=await t(uid, "persona.skip"), callback_data="persona:step:sociality")],
        [InlineKeyboardButton(text=await t(uid, "persona.back"), callback_data="persona:step:zodiac")],
    ])
    await _replace_panel(cb, await t(uid, "persona.temperament.title"), kb)


@dp.callback_query(F.data == "persona:step:temperament", F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def step_temperament(cb: CallbackQuery) -> None:
    await show_temperament(cb)


@dp.callback_query(F.data == "persona:step:sociality", F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def step_sociality(cb: CallbackQuery) -> None:
    await show_sociality(cb)


async def show_sociality(cb: CallbackQuery) -> None:
    uid = cb.from_user.id
    st = await _wiz_require(uid)
    if st is None:
        await _cb_ack(cb)
        return
    st["step"] = "sociality"
    await _wiz_set(uid, st)
    sel = st.get("sociality")
    items = [
        ((("✅ " if sel == "introvert" else "") + (await t(uid, "persona.social.introvert"))), "introvert"),
        ((("✅ " if sel == "ambivert" else "") + (await t(uid, "persona.social.ambivert"))), "ambivert"),
        ((("✅ " if sel == "extrovert" else "") + (await t(uid, "persona.social.extrovert"))), "extrovert"),
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=_rows_kv(items, 3, "sociality") + [
        [InlineKeyboardButton(text=await t(uid, "persona.skip"), callback_data="persona:step:archetypes")],
        [InlineKeyboardButton(text=await t(uid, "persona.back"), callback_data="persona:step:temperament")],
    ])
    await _replace_panel(cb, await t(uid, "persona.sociality.title"), kb)


@dp.callback_query(F.data == "persona:step:archetypes", F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def step_archetypes(cb: CallbackQuery) -> None:
    await show_archetypes(cb)


async def show_archetypes(cb: CallbackQuery) -> None:
    uid = cb.from_user.id
    st = await _wiz_require(uid)
    if st is None:
        await _cb_ack(cb)
        return
    st["step"] = "archetypes"
    await _wiz_set(uid, st)
    selected = set(st.get("archetypes", []))

    rows, row = [], []
    for name in ARCHETYPES:
        mark = "✅ " if name in selected else ""
        row.append(InlineKeyboardButton(text=f"{mark}{name}", callback_data=f"persona:arch:toggle:{name}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    preview_label = await t(uid, "persona.preview") or "Preview 👀"
    rows += [
        [InlineKeyboardButton(text=preview_label, callback_data="persona:preview")],
        [InlineKeyboardButton(text=await t(uid, "persona.done"), callback_data="persona:finish")],
        [InlineKeyboardButton(text=await t(uid, "persona.back"), callback_data="persona:step:sociality")],
        [InlineKeyboardButton(text=await t(uid, "persona.reset"), callback_data="persona:reset")],
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    text = await t(uid, "persona.archetypes.title")

    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await _cb_ack(cb)
        return
    except TelegramBadRequest:
        pass
    await _replace_panel(cb, text, kb)


@dp.callback_query(F.data.startswith("persona:arch:toggle:"), F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def arch_toggle(cb: CallbackQuery) -> None:
    uid = cb.from_user.id
    name = cb.data.split(":", 3)[-1]
    if name not in ARCHETYPES:
        await _cb_ack(cb, text=await t(uid, "persona.invalid_archetype") or "Invalid archetype")
        return
    st = await _wiz_get(uid)
    if not st:
        with suppress(Exception):
            await send_message_safe(bot, uid, await t(uid, "persona.expired") or "Session expired. Starting over.", parse_mode="HTML")
        await start_persona_wizard(type("M", (), {"chat": type("C", (), {"id": uid}), "from_user": type("U", (), {"id": uid})})())
        return
    cur = set(st.get("archetypes", []))
    if name in cur:
        cur.remove(name)
    else:
        if len(cur) < MAX_ARCH:
            cur.add(name)
        else:
            await _cb_ack(cb, text=await t(uid, "persona.pick.limit", MAX=MAX_ARCH) or f"You can pick up to {MAX_ARCH}.")
    st["archetypes"] = list(cur)
    st["step"] = "archetypes"
    await _wiz_set(uid, st)
    await show_archetypes(cb)


@dp.callback_query(F.data == "persona:preview", F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def persona_preview(cb: CallbackQuery) -> None:
    uid = cb.from_user.id
    try:
        p = await get_persona(chat_id=uid)
        st = await _wiz_get(uid)
        prefs = {}
        if isinstance(st.get("temperament"), dict):
            prefs["temperament"] = st["temperament"]
        if isinstance(st.get("zodiac"), str):
            prefs["zodiac"] = st["zodiac"]
        if isinstance(st.get("sociality"), str):
            prefs["sociality"] = st["sociality"]
        if isinstance(st.get("archetypes"), list):
            prefs["archetypes"] = st["archetypes"]

        z = prefs.get("zodiac", p.zodiac)
        tmap = prefs.get("temperament", p.temperament) or {}
        soc = prefs.get("sociality", p.sociality)
        arch = prefs.get("archetypes", p.archetypes) or []

        title = await t(uid, "persona.preview.title") or "Preview"
        l_z = await t(uid, "persona.preview.zodiac") or "Zodiac"
        l_t = await t(uid, "persona.preview.temperament") or "Temperament"
        l_s = await t(uid, "persona.preview.sociality") or "Sociality"
        l_a = await t(uid, "persona.preview.archetypes") or "Archetypes"

        lab_san = await t(uid, "persona.temp.sanguine") or "Sanguine"
        lab_ch = await t(uid, "persona.temp.choleric") or "Choleric"
        lab_ph = await t(uid, "persona.temp.phlegmatic") or "Phlegmatic"
        lab_me = await t(uid, "persona.temp.melancholic") or "Melancholic"
        temp_labels = {"sanguine": lab_san, "choleric": lab_ch, "phlegmatic": lab_ph, "melancholic": lab_me}

        temp_lines = []
        for k, v in sorted(tmap.items(), key=lambda kv: float(kv[1]), reverse=True):
            name = html.escape(temp_labels.get(k, k.title()))
            pct = _pct01(v)
            bar = _bar(v, width=10)
            temp_lines.append(f"{name} — {pct}% <code>{bar}</code>")

        arch_str = ", ".join(arch) if arch else "—"
        badge = ZODIAC_BADGES.get(z, "")
        z_line = f"{badge} {z}".strip()
        txt = (
            f"<b>{html.escape(title)}</b>\n\n"
            f"<b>{html.escape(l_z)}:</b> {html.escape(z_line)}\n"
            f"<b>{html.escape(l_s)}:</b> {html.escape(soc)}\n"
            f"<b>{html.escape(l_a)}:</b> {html.escape(arch_str)}\n\n"
            f"<b>{html.escape(l_t)}:</b>\n" + ("\n".join(temp_lines) if temp_lines else "—")
        )

        st["step"] = "preview"
        await _wiz_set(uid, st)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=await t(uid, "persona.back"), callback_data="persona:step:archetypes")],
            [InlineKeyboardButton(text=await t(uid, "persona.done"), callback_data="persona:finish")],
            [InlineKeyboardButton(text=await t(uid, "persona.reset"), callback_data="persona:reset")],
        ])
        await _cb_ack(cb)
        try:
            await cb.message.edit_text(txt, parse_mode="HTML", reply_markup=kb)
        except TelegramBadRequest:
            await cb.message.reply(txt, parse_mode="HTML", reply_markup=kb)
    except Exception:
        logger.debug("persona_preview failed", exc_info=True)
        msg = await t(uid, "persona.preview.failed")
        await _cb_ack(cb, text=msg or "Failed to render preview")


@dp.callback_query(F.data == "persona:finish", F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def persona_finish(cb: CallbackQuery) -> None:
    uid = cb.from_user.id
    await _cb_ack(cb)
    st = await _wiz_get(uid) or {}

    raw = {}
    if isinstance(st.get("zodiac"), str):
        raw["zodiac"] = st["zodiac"]
    if isinstance(st.get("temperament"), dict):
        raw["temperament"] = {k: float(st["temperament"].get(k, 0.0)) for k in st["temperament"].keys()}
    if isinstance(st.get("sociality"), str):
        raw["sociality"] = st["sociality"]
    if isinstance(st.get("archetypes"), list):
        raw["archetypes"] = list(st["archetypes"])
    prefs = normalize_prefs(raw)

    async with session_scope(stmt_timeout_ms=2000) as db:
        user = await db.get(User, uid)
        current = (getattr(user, "persona_prefs", None) or {})
        if not prefs:
            merged_or_none = (current or None)
        else:
            merged = merge_prefs(current, prefs)
            if merged != current:
                await db.execute(update(User).where(User.id == uid).values(persona_prefs=merged))
            merged_or_none = (merged or None)

    p = await get_persona(chat_id=uid)
    if merged_or_none:
        p.apply_overrides(merged_or_none)
    else:
        p.apply_overrides(None, reset=True)

    await _wiz_clear(uid)
    await _delete_or_hide(cb.message)
    await _show_main_panel(uid, await t(uid, "persona.saved"))

    try:
        async with session_scope(read_only=True, stmt_timeout_ms=2000) as db:
            u = await db.get(User, uid)
            already = bool(u and u.pm_welcome_sent)
        if not already:
            await _send_private_welcome(uid, full_name=cb.from_user.full_name)
    except Exception:
        logger.debug("persona_finish: welcome send failed", exc_info=True)


async def show_api_menu(message: Message | CallbackQuery) -> None:
    if isinstance(message, CallbackQuery):
        uid = message.from_user.id
        chat_id = message.from_user.id
        cb: CallbackQuery | None = message
    else:
        uid = message.from_user.id
        chat_id = message.chat.id
        cb = None

    async with session_scope(read_only=True, stmt_timeout_ms=2000) as db:
        keys = await list_keys_for_user(db, uid)
        stats_map: dict[int, ApiKeyStats] = {}
        if keys:
            res = await db.execute(
                select(ApiKeyStats).where(ApiKeyStats.api_key_id.in_([k.id for k in keys]))
            )
            for st in res.scalars():
                stats_map[st.api_key_id] = st

    title = await t(uid, "api.title") or "<b>Conversation API</b>"
    text_lines: list[str] = [title]

    base_url = (getattr(settings, "PUBLIC_API_BASE_URL", "") or "").strip()
    if base_url:
        base_line = await t(uid, "api.base_url", url=base_url) or f"{base_url}"
        text_lines.append(base_line)

    actions: list[list[InlineKeyboardButton]] = []

    if keys:
        keys_title = await t(uid, "api.keys.title") or "API keys:"
        text_lines.append(keys_title)

        for idx, k in enumerate(keys, start=1):
            status_emoji = "🟢" if k.active else "🔴"
            suffix = (getattr(k, "key_hash", "") or "")[-6:] or str(k.id)

            line = await t(
                uid,
                "api.key.item",
                id=idx,
                status=status_emoji,
                suffix=suffix,
            )
            if not line:
                line = f"{status_emoji} #{idx} • …{suffix}"
            text_lines.append(line)

            row_btns: list[InlineKeyboardButton] = []

            if k.active:
                txt_disable = await t(uid, "api.key.button.disable") or "⏸"
                row_btns.append(
                    InlineKeyboardButton(
                        text=txt_disable,
                        callback_data=f"api:k:{k.id}:off",
                    )
                )
            else:
                txt_enable = await t(uid, "api.key.button.enable") or "▶️"
                row_btns.append(
                    InlineKeyboardButton(
                        text=txt_enable,
                        callback_data=f"api:k:{k.id}:on",
                    )
                )

            txt_show = await t(uid, "api.key.button.show") or "👁"
            row_btns.append(
                InlineKeyboardButton(
                    text=txt_show,
                    callback_data=f"api:k:{k.id}:show",
                )
            )

            txt_drop = await t(uid, "api.key.button.drop") or "🗑"
            row_btns.append(
                InlineKeyboardButton(
                    text=txt_drop,
                    callback_data=f"api:k:{k.id}:drop",
                )
            )

            actions.append(row_btns)
    else:
        no_key = await t(uid, "api.no_key") or "You don't have an API key yet."
        text_lines.append(no_key)

    btn_new = await t(uid, "api.button.new") or "🔑 New key"
    actions.append([InlineKeyboardButton(text=btn_new, callback_data="api:rotate")])

    howto = await t(uid, "api.button.howto") or "📘 How to Use?"
    actions.append(
        [InlineKeyboardButton(
            text=howto,
            url="https://synchatica.com/conversation-api",
        )]
    )

    back_label = await t(uid, "api.button.back") or "⬅️ Back"
    actions.append([InlineKeyboardButton(text=back_label, callback_data="api:back")])

    text = "\n".join(text_lines)
    kb = InlineKeyboardMarkup(inline_keyboard=actions)

    if cb:
        await _replace_panel(cb, text, kb)
    else:
        await send_message_safe(bot, chat_id, text, parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data == "api:panel", F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def api_panel(cb: CallbackQuery) -> None:
    await show_api_menu(cb)


@dp.callback_query(F.data == "api:rotate", F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def api_rotate(cb: CallbackQuery) -> None:
    uid = cb.from_user.id

    async with session_scope(stmt_timeout_ms=5000) as db:
        api_key, secret = await create_key(db, uid)

    try:
        ttl = int(getattr(settings, "API_KEY_SECRET_TTL_SEC", 365 * 24 * 3600))
    except Exception:
        ttl = 365 * 24 * 3600

    try:
        await redis_client.set(
            f"api:secret:{uid}:{api_key.id}",
            secret,
            ex=max(60, ttl),
        )
    except Exception:
        pass

    title = await t(uid, "api.rotate.title") or "<b>New API key</b>"
    save_hint = await t(uid, "api.rotate.save") or "Save this key."
    again_hint = await t(uid, "api.rotate.again") or "You can view it again in the API menu."

    text = (
        f"{title}\n"
        f"{save_hint}\n\n"
        f"<code>{secret}</code>\n\n"
        f"{again_hint}"
    )

    back_label = await t(uid, "api.button.back") or "⬅️ Back"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=back_label, callback_data="api:panel")]
        ]
    )
    await _replace_panel(cb, text, kb)



@dp.callback_query(F.data == "api:delete", F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def api_delete(cb: CallbackQuery) -> None:
    uid = cb.from_user.id

    async with session_scope(stmt_timeout_ms=5000) as db:
        keys = await list_keys_for_user(db, uid)
        for k in keys:
            if k.active:
                await deactivate_key(db, user_id=uid, api_key_id=k.id)

    text = await t(uid, "api.delete.done") or (
        "API key disabled.\n"
        "All requests with your key(s) are now rejected.\n"
        "You can create a new key at any time."
    )

    btn_new = await t(uid, "api.button.new") or "🔑 New key"
    back_label = await t(uid, "api.button.back") or "⬅️ Back"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=btn_new, callback_data="api:rotate")],
            [InlineKeyboardButton(text=back_label, callback_data="api:back")],
        ]
    )
    await _replace_panel(cb, text, kb)


@dp.callback_query(F.data == "api:back", F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def api_back(cb: CallbackQuery) -> None:
    await _cb_ack(cb)
    await _delete_or_hide(cb.message)
    await _show_main_panel(
        cb.from_user.id,
        await t(cb.from_user.id, "menu.main") or "Main Menu",
    )


@dp.callback_query(F.data.startswith("api:k:"), F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def api_key_action(cb: CallbackQuery) -> None:
    uid = cb.from_user.id
    data = cb.data or ""
    prefix = "api:k:"
    if not data.startswith(prefix):
        await _cb_ack(cb)
        return

    rest = data[len(prefix):]
    try:
        key_id_str, action = rest.rsplit(":", 1)
        key_id = int(key_id_str)
    except ValueError:
        await _cb_ack(cb)
        return

    if action == "show":
        redis_key = f"api:secret:{uid}:{key_id}"
        try:
            raw = await redis_client.get(redis_key)
        except Exception:
            raw = None

        if not raw:
            msg = await t(uid, "api.key.show.unavailable") or "Key value is not available."
            await _cb_ack(cb, text=msg, alert=True)
            return

        secret = raw.decode("utf-8", "ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)
        title = await t(uid, "api.key.show.title") or "API key"
        txt = f"{title}\n<code>{secret}</code>"
        await _cb_ack(cb)
        await send_message_safe(bot, uid, txt, parse_mode="HTML")
        return

    async with session_scope(stmt_timeout_ms=5000) as db:
        key = await db.get(ApiKey, key_id)
        if not key or key.user_id != uid:
            msg = await t(uid, "api.key.not_found") or "Key not found"
            await _cb_ack(cb, text=msg, alert=True)
            return

        if action == "off":
            if key.active:
                await deactivate_key(db, user_id=uid, api_key_id=key.id)

        elif action == "on":
            if not key.active:
                key.active = True
                await db.flush()
                try:
                    if key.key_hash:
                        ck = f"api:key:{key.key_hash}"
                        ttl = int(getattr(settings, "API_KEY_CACHE_TTL_SEC", 3600))
                        await redis_client.hset(
                            ck,
                            mapping={
                                "id": key.id,
                                "user_id": uid,
                                "active": 1,
                            },
                        )
                        await redis_client.expire(ck, max(10, ttl))
                except Exception:
                    pass

        elif action == "drop":
            if key.active:
                await deactivate_key(db, user_id=uid, api_key_id=key.id)

            try:
                await redis_client.delete(f"api:secret:{uid}:{key.id}")
            except Exception:
                pass

            try:
                if key.key_hash:
                    await redis_client.delete(f"api:key:{key.key_hash}")
            except Exception:
                pass

            if getattr(settings, "API_PERSONA_PER_KEY", True):
                try:
                    from app.tasks.celery_app import celery
                    celery.send_task(
                        "api.cleanup_memory_for_key",
                        args=[key.id],
                    )
                except Exception:
                    logger.exception(
                        "api_key_action: failed to schedule cleanup_memory_for_key for key_id=%s",
                        key.id,
                    )

            try:
                await db.execute(delete(ApiKeyStats).where(ApiKeyStats.api_key_id == key.id))
            except Exception:
                logger.exception("api_key_action: delete ApiKeyStats failed for key_id=%s", key.id)
            
            try:
                await db.delete(key)
            except Exception:
                logger.exception("api_key_action: delete ApiKey failed for key_id=%s", key.id)
            await db.flush()

    await _cb_ack(cb)
    await show_api_menu(cb)