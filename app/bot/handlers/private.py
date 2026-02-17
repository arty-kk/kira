#app/bot/handlers/private.py
from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import os
import re
import unicodedata

from contextlib import suppress
from functools import wraps
from pathlib import Path
from typing import Any, Optional, Awaitable

from aiogram import F
from aiogram.enums import ChatType, ContentType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup,
)
from sqlalchemy import delete, or_, select, update
from sqlalchemy.sql import func

from app.api.api_keys import cache_active_key, create_key, deactivate_key, list_keys_for_user
from app.bot.components.constants import WELCOME_MESSAGES, redis_client
from app.bot.components.dispatcher import dp
from app.core.media_utils import (
    MAX_IMAGE_BYTES,
    download_to_tmp as media_download_to_tmp,
    sanitize_and_compress as media_sanitize_and_compress,
    strict_image_load as media_strict_image_load,
)
from app.bot.handlers.payments import (
    RedisKeys,
    clear_payment_runtime_keys,
    clear_payment_ui,
    cmd_buy,
    cmd_buy_reqs,
    send_transient_notice,
    show_pending_invoice_stub,
)
from app.bot.i18n import t
from app.bot.i18n.menu_translation import LANG_BUTTONS
from app.bot.utils.debouncer import buffer_message_for_response
from app.bot.utils.spam_guard import pm_block_guard
from app.bot.utils.telegram_safe import delete_message_safe, send_message_safe, send_video_safe
from app.clients.telegram_client import get_bot
from app.config import settings
from app.core.db import session_scope
from app.core.memory import cache_gender, delete_user_redis_data, inc_msg_count, is_spam
from app.core.models import ApiKey, ApiKeyKnowledge, ApiKeyStats, User
from app.emo_engine import get_persona
from app.emo_engine.persona.constants.user_prefs import (
    ARCHETYPES, MAX_ARCH, SOCIALITY_SET, TEMP_PRESETS,
    ZODIAC, ZODIAC_SET, merge_prefs, normalize_prefs,
)
from app.services.addons.analytics import record_user_message
from app.services.addons.personal_ping import register_private_activity
from app.services.user.user_service import compute_remaining, get_or_create_user, reserve_request
from app.tasks.welcome import send_private_ai_welcome_task

logger = logging.getLogger(__name__)
bot = get_bot()

# ---------------------------
# Settings / constants
# ---------------------------
ONB_UI_TTL = int(getattr(settings, "ONB_UI_TTL_SEC", 24 * 3600))
SOFT_PENDING_INVOICE = bool(getattr(settings, "SOFT_PENDING_INVOICE", True))
ALLOWED_LANGS = set(LANG_BUTTONS.keys())

_BLANK_TEXT = "\u200b"

SAFE_URL_RX = re.compile(r'^(?:https?://|tg://)[^\s<>"\']{1,2048}$', flags=re.IGNORECASE)

UI_SECRET_MSG_TTL = int(getattr(settings, "UI_SECRET_MSG_TTL_SEC", 60))

MAIN_MENU_HINT_TTL = int(getattr(settings, "MAIN_MENU_HINT_TTL_SEC", 5))

MAX_KB_JSON_BYTES = int(getattr(settings, "MAX_KB_JSON_BYTES", 5 * 1024 * 1024))

MAX_VOICE_BYTES = int(getattr(settings, "MAX_VOICE_BYTES", 25 * 1024 * 1024))
MAX_VOICE_DURATION = int(getattr(settings, "MAX_VOICE_DURATION_SEC", 300))
ALLOWED_VOICE_MIMES = {
    "audio/ogg", "audio/opus", "audio/mpeg", "audio/mp3",
    "audio/wav", "audio/x-wav", "audio/webm",
    "audio/mp4", "audio/m4a", "audio/aac",
}

# Persona wizard
WZ_KEY = "persona:wizard:{uid}"
WZ_TTL = getattr(settings, "PERSONA_WIZARD_TTL_SEC", 604800)

ZODIAC_BADGES = {
    "Aries": "♈︎", "Taurus": "♉︎", "Gemini": "♊︎", "Cancer": "♋︎",
    "Leo": "♌︎", "Virgo": "♍︎", "Libra": "♎︎", "Scorpio": "♏︎",
    "Sagittarius": "♐︎", "Capricorn": "♑︎", "Aquarius": "♒︎", "Pisces": "♓︎",
}


# ---------------------------
# Redis keys
# ---------------------------
def _k_onb_lang_msg(uid: int) -> str:
    return f"onb:lang_msg:{uid}"


def _k_onb_gender_msg(uid: int) -> str:
    return f"onb:gender_msg:{uid}"


def _kb_upload_slot_key(uid: int) -> str:
    return f"kb:upload_for:{uid}"


# ---------------------------
# Small utilities
# ---------------------------
def _to_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        if isinstance(v, (bytes, bytearray)):
            v = v.decode("utf-8", "ignore")
        s = str(v).strip()
        return int(s) if s else None
    except Exception:
        return None


def safe_url(u: str | None) -> str | None:
    u = (u or "").strip()
    return u if SAFE_URL_RX.match(u) else None


def _extract_urls_from_entities(text: str, entities: list[Any] | None) -> list[str]:

    out: list[str] = []
    t = text or ""
    for e in (entities or []):
        try:
            et = getattr(e, "type", None)
            et_val = getattr(et, "value", et)
            if et_val == "url":
                part = ""
                if hasattr(e, "extract_from"):
                    part = (e.extract_from(t) or "").strip()
                else:
                    off = int(getattr(e, "offset", 0) or 0)
                    ln = int(getattr(e, "length", 0) or 0)
                    part = (t[off:off + ln] if ln > 0 else "").strip()
                u = safe_url(part)
                if u:
                    out.append(u)
            elif et_val == "text_link":
                u = safe_url(getattr(e, "url", None))
                if u:
                    out.append(u)
        except Exception:
            continue
    seen = set()
    uniq: list[str] = []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq

def _augment_text_with_urls(text: str, urls: list[str]) -> str:
    t = (text or "").strip()
    if not urls:
        return t
    missing = [u for u in urls if u and (u not in t)]
    if not t:
        return "\n".join(missing) if missing else ""
    if missing:
        return (t + "\n" + "\n".join(missing)).strip()
    return t

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


def _norm_btn(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("\uFE0F", "").replace("\uFE0E", "").replace("\u200D", "").replace("\u00A0", " ")
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Cf")
    s = " ".join(s.split())
    return s.casefold()


async def tr(uid: int, key: str, default: str = "", **kwargs: Any) -> str:

    try:
        s = await t(uid, key, **kwargs)
        return s or default
    except Exception:
        return default


def _persona_key(name: str) -> str:
    return (name or "").strip().lower()


async def _tr_zodiac(uid: int, zodiac: str) -> str:
    key = _persona_key(zodiac)
    return await tr(uid, f"persona.zodiac.{key}", zodiac)


async def _tr_archetype(uid: int, archetype: str) -> str:
    key = _persona_key(archetype)
    return await tr(uid, f"persona.archetype.{key}", archetype)


async def _tr_sociality(uid: int, sociality: str) -> str:
    key = _persona_key(sociality)
    return await tr(uid, f"persona.social.{key}", sociality)


# ---------------------------
# Telegram UI helpers
# ---------------------------
async def _delete_later(chat_id: int, message_id: Optional[int], delay: int) -> None:
    if not message_id:
        return
    try:
        await asyncio.sleep(max(1, int(delay)))
        await delete_message_safe(bot, chat_id, int(message_id))
    except Exception:
        pass


async def _edit_text_later(chat_id: int, message_id: Optional[int], delay: int, new_text: str) -> None:
    if not message_id:
        return
    try:
        await asyncio.sleep(max(1, int(delay)))
        with suppress(TelegramBadRequest):
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=int(message_id),
                text=new_text,
            )
    except Exception:
        pass


async def _cb_ack(cb: CallbackQuery, text: str | None = None, alert: bool = False, cache: int = 1) -> None:
    with suppress(TelegramBadRequest):
        await cb.answer(text=text, show_alert=alert, cache_time=cache)


async def _delete_or_hide(msg: Message) -> None:
    try:
        await msg.delete()
    except TelegramBadRequest:
        with suppress(TelegramBadRequest):
            await msg.edit_reply_markup(reply_markup=None)


async def _replace_panel(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup | None) -> None:
    await _cb_ack(cb)
    if cb.message:
        await _delete_or_hide(cb.message)
    await send_message_safe(bot, cb.from_user.id, text, parse_mode="HTML", reply_markup=kb)


def dedupe_callback(ttl: int = 86_400):
    def deco(fn):
        @wraps(fn)
        async def wrapper(cb: CallbackQuery, *args, **kwargs):
            try:
                key = f"seen:cbq:{cb.id}"
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


async def build_quick_links_kb(user_id: int) -> ReplyKeyboardMarkup | None:
    buttons: list[KeyboardButton] = []

    def add(flag: str, key: str, default: str) -> None:
        if getattr(settings, flag, True):
            # translation is async, so we cannot call here directly
            # we’ll fill later in the loop below
            buttons.append(KeyboardButton(text=f"__i18n__:{key}::{default}"))

    add("SHOW_SHOP_BUTTON", "menu.shop", "🛒 Shop")
    add("SHOW_REQUESTS_BUTTON", "menu.requests", "⚡ Requests")
    add("SHOW_CHANNEL_BUTTON", "menu.link", "📢 Channel")
    add("SHOW_PERSONA_BUTTON", "menu.persona", "🧬 Persona")
    add("SHOW_MEMORY_CLEAR_BUTTON", "menu.memory_clear", "🧹 Clear memory")
    add("SHOW_API_BUTTON", "menu.api", "🔑 API")

    if not buttons:
        return None

    # Resolve i18n placeholders
    resolved: list[KeyboardButton] = []
    for b in buttons:
        raw = b.text or ""
        if raw.startswith("__i18n__:"):
            _, rest = raw.split("__i18n__:", 1)
            k, d = rest.split("::", 1)
            resolved.append(KeyboardButton(text=(await tr(user_id, k, d))))
        else:
            resolved.append(b)

    rows = [resolved[i:i + 2] for i in range(0, len(resolved), 2)]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


async def _show_main_panel(uid: int, text: str, *, hide_after: int | None = None) -> None:
    kb = await build_quick_links_kb(uid)
    msg = await send_message_safe(bot, uid, text, parse_mode="HTML", reply_markup=kb)
    ttl = int(hide_after or 0)
    if msg and ttl > 0:
        asyncio.create_task(_edit_text_later(uid, msg.message_id, ttl, _BLANK_TEXT))


# ---------------------------
# Delivery dedupe
# ---------------------------
async def _first_delivery(chat_id: int, msg_id: int, kind: str, ttl: int = 86_400) -> bool:
    try:
        seen = await redis_client.set(f"seen:{chat_id}:{msg_id}", 1, nx=True, ex=ttl)
        if not seen:
            logger.info("Drop duplicate %s delivery chat=%s msg_id=%s", kind, chat_id, msg_id)
            return False
    except Exception:
        logger.exception("failed to set seen-key for %s", kind)
    return True


# ---------------------------
# Welcome / onboarding
# ---------------------------
async def _clear_onboarding_ui(uid: int, chat_id: int) -> None:
    try:
        lang_mid = _to_int(await redis_client.get(_k_onb_lang_msg(uid)))
        gender_mid = _to_int(await redis_client.get(_k_onb_gender_msg(uid)))
        await delete_message_safe(bot, chat_id, lang_mid)
        await delete_message_safe(bot, chat_id, gender_mid)
    finally:
        with suppress(Exception):
            await redis_client.delete(_k_onb_lang_msg(uid), _k_onb_gender_msg(uid))


async def _send_private_welcome(uid: int, *, full_name: str | None = None) -> bool:
    # claim welcome only once
    async with session_scope(stmt_timeout_ms=2000) as db:
        res = await db.execute(
            update(User)
            .where(User.id == uid, User.pm_welcome_sent.is_(None))
            .values(pm_welcome_sent=func.now())
            .returning(User.id)
        )
        claimed = res.scalar() is not None

    if not claimed:
        return False

    raw_lang = await redis_client.get(f"lang_ui:{uid}") or await redis_client.get(f"lang:{uid}")
    lang = (raw_lang.decode() if isinstance(raw_lang, (bytes, bytearray)) else raw_lang) or getattr(settings, "DEFAULT_LANG", "en")

    template = (
        WELCOME_MESSAGES.get(lang)
        or WELCOME_MESSAGES.get(getattr(settings, "DEFAULT_LANG", ""), "")
        or next(iter(WELCOME_MESSAGES.values()), "")
    )

    safe_name = html.escape(full_name or "", quote=True)
    try:
        text = template.format(full_name=safe_name, BOT_NAME=settings.BOT_NAME)
    except Exception:
        text = f"Hi {safe_name}!"

    kb = await build_quick_links_kb(uid)

    sent_any = False
    if getattr(settings, "ENABLE_PRIVATE_STATIC_WELCOME", True):
        video_enabled = getattr(settings, "ENABLE_PRIVATE_WELCOME_VIDEO", False)
        if video_enabled:
            try:
                video_path = Path(__file__).parent.parent / "media" / "video.mp4"
                msg_v = await send_video_safe(
                    bot,
                    chat_id=uid,
                    video=FSInputFile(video_path),
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=kb,
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

    return sent_any


# ---------------------------
# Access / billing guard
# ---------------------------
async def _ensure_access_and_increment(message: Message, text_for_guard: str | None) -> tuple[User, bool, str, int] | None:
    chat_id, user_id = message.chat.id, message.from_user.id

    if await pm_block_guard(bot, t, user_id=user_id, chat_id=chat_id, text=text_for_guard):
        return None

    asyncio.create_task(register_private_activity(user_id))

    if not SOFT_PENDING_INVOICE:
        with suppress(Exception):
            if await redis_client.exists(f"pending_invoice:{user_id}"):
                still_pending = await show_pending_invoice_stub(chat_id, user_id)
                if still_pending:
                    return None

    async with session_scope(stmt_timeout_ms=2000) as db:
        user = await get_or_create_user(db, message.from_user)
        remaining = compute_remaining(user)
        cr = (
            await reserve_request(
                db,
                user.id,
                prefer_paid=(user.paid_requests > 0),
                chat_id=chat_id,
                message_id=message.message_id,
            )
            if remaining > 0
            else None
        )

    if remaining <= 0 or not (cr and cr.reserved):
        msg = await tr(user_id, "private.need_gift_soft", "You're out of requests. Open the shop to buy more.")
        gifts_btn = await tr(user_id, "shop.open.gifts", "🎁 Gifts")
        reqs_btn = await tr(user_id, "shop.open.reqs", "⚡ Buy requests")

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=gifts_btn, callback_data="shop:tab:gifts")],
            [InlineKeyboardButton(text=reqs_btn, callback_data="shop:tab:reqs")],
        ])
        await send_message_safe(bot, chat_id, msg, parse_mode="HTML", reply_markup=kb)
        return None

    used_paid = bool(cr.used_paid)
    allow_web = used_paid
    billing_tier = "paid" if used_paid else "free"
    return user, allow_web, billing_tier, int(cr.reservation_id or 0)


def _mk_ctx_payload(role: str, text: str, *, speaker_id: int | None = None) -> str:
    r = (role or "").strip().lower()
    if r not in ("user", "assistant", "system"):
        r = "user"
    t = (text or "").strip()
    payload: dict[str, Any] = {"role": r, "text": t}
    if speaker_id is not None:
        try:
            payload["speaker_id"] = int(speaker_id)
        except Exception:
            pass
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


async def _store_context(chat_id: int, msg_id: int, text: str, *, speaker_id: int | None = None) -> None:
    await inc_msg_count(chat_id)
    await redis_client.set(
        f"msg:{chat_id}:{msg_id}",
        _mk_ctx_payload("user", text, speaker_id=speaker_id),
        ex=getattr(settings, "REPLY_CONTEXT_TTL_SEC", 86400),
    )


async def _store_quote_context(
    chat_id: int,
    msg_id: int,
    text: str,
    *,
    role: str = "assistant",
    speaker_id: int | None = None,
) -> None:

    if not msg_id:
        return
    txt = (text or "").strip()
    if not txt:
        return
    try:
        await redis_client.set(
            f"msg:{chat_id}:{int(msg_id)}",
            _mk_ctx_payload(role, txt, speaker_id=speaker_id),
            ex=getattr(settings, "REPLY_CONTEXT_TTL_SEC", 86400),
        )
    except Exception:
        pass


async def _store_reply_target_best_effort(chat_id: int, msg: Message) -> None:

    try:
        r = msg.reply_to_message
        if not r:
            return
        mid = int(getattr(r, "message_id", 0) or 0)
        if mid <= 0:
            return
        txt = (getattr(r, "text", None) or getattr(r, "caption", None) or "").strip()
        if not txt:
            return
        is_bot = bool(getattr(getattr(r, "from_user", None), "is_bot", False))
        role = "assistant" if is_bot else "user"
        sid = getattr(getattr(r, "from_user", None), "id", None)
        await _store_quote_context(chat_id, mid, txt, role=role, speaker_id=sid)
    except Exception:
        return


# ---------------------------
# Common “close UI”
# ---------------------------
async def _ui_close_impl(cb: CallbackQuery) -> None:
    uid = cb.from_user.id
    chat_id = cb.message.chat.id if cb.message else uid
    await _cb_ack(cb)
    if cb.message:
        msg_id = cb.message.message_id
        pending_msg_id = _to_int(await redis_client.get(RedisKeys.pending_msg(uid)))
        buy_info_msg_id = _to_int(await redis_client.get(RedisKeys.buy_info_msg(uid)))
        if msg_id and msg_id in {pending_msg_id, buy_info_msg_id}:
            had_pending = False
            with suppress(Exception):
                had_pending = bool(await redis_client.exists(RedisKeys.pending(uid)))
            await clear_payment_ui(uid, chat_id)
            await clear_payment_runtime_keys(uid)
            if had_pending:
                await send_transient_notice(
                    chat_id,
                    (await tr(uid, "payments.cancelled", "Canceled.")),
                    parse_mode="HTML",
                )
    if cb.message:
        await _delete_or_hide(cb.message)

    await _show_main_panel(uid, await tr(uid, "menu.main", "Main menu"), hide_after=MAIN_MENU_HINT_TTL)


@dp.callback_query(F.data == "ui:close", F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def ui_close(cb: CallbackQuery) -> None:
    await _ui_close_impl(cb)


# ---------------------------
# /start onboarding
# ---------------------------
@dp.message(Command("start"), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message) -> None:
    if not await _first_delivery(message.chat.id, message.message_id, "start"):
        return

    # Ensure default persona prefs exist (best-effort)
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
                    .where(User.id == user.id, or_(User.persona_prefs.is_(None), User.persona_prefs == {}))
                    .values(persona_prefs=defaults)
                )
        except Exception:
            logger.debug("init default persona_prefs failed", exc_info=True)

    uid = message.from_user.id
    with suppress(Exception):
        await _wiz_clear(uid)
    await _clear_onboarding_ui(uid, message.chat.id)

    ordered_langs = ["en", "ru"] + sorted(ALLOWED_LANGS - {"en", "ru"})
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    for code in ordered_langs:
        label = LANG_BUTTONS.get(code, code.upper())
        row.append(InlineKeyboardButton(text=label, callback_data=f"lang:{code}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    text = await tr(uid, "private.choose_lang", "🔎 Choose your language")

    msg = await send_message_safe(bot, chat_id=message.chat.id, text=text, parse_mode="HTML", reply_markup=kb)
    if msg:
        with suppress(Exception):
            await redis_client.set(_k_onb_lang_msg(uid), msg.message_id, ex=ONB_UI_TTL)


# ---------------------------
# Memory clear confirm
# ---------------------------
async def ask_clear_memory_confirm(message: Message) -> None:
    uid = message.from_user.id
    chat_id = message.chat.id

    text = await tr(uid, "memory.clear.confirm", "⚠️ Clear memory? This cannot be undone. Continue?")
    yes = await tr(uid, "memory.clear.confirm_yes", "✅ Yes")
    back = await tr(uid, "ui.back", "◀ Back")
    close = await tr(uid, "ui.close", "✖️ Close")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=yes, callback_data="mem:clear:yes")],
            [InlineKeyboardButton(text=back, callback_data="mem:clear:no")],
            [InlineKeyboardButton(text=close, callback_data="ui:close")],
        ]
    )
    await send_message_safe(bot, chat_id, text, parse_mode="HTML", reply_markup=kb)


# ---------------------------
# Language / gender onboarding
# ---------------------------
@dp.callback_query(F.data.startswith("lang:"), F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def set_language(cb: CallbackQuery) -> None:
    await _cb_ack(cb)
    if cb.message:
        await _delete_or_hide(cb.message)

    lang = cb.data.split(":", 1)[1]
    if lang not in ALLOWED_LANGS:
        default_lang = getattr(settings, "DEFAULT_LANG", "en")
        lang = default_lang if default_lang in ALLOWED_LANGS else "en"

    await redis_client.set(f"lang:{cb.from_user.id}", lang)
    await redis_client.set(f"lang_ui:{cb.from_user.id}", lang)

    uid = cb.from_user.id
    prompt_text = await tr(uid, "gender.prompt", "<b>Please select your gender:</b>")
    male_label = await tr(uid, "gender.male", "👨 Male")
    female_label = await tr(uid, "gender.female", "👩 Female")

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=male_label, callback_data="gender:male"),
        InlineKeyboardButton(text=female_label, callback_data="gender:female"),
    ]])

    msg = await send_message_safe(bot, chat_id=uid, text=prompt_text, parse_mode="HTML", reply_markup=kb)
    if msg:
        with suppress(Exception):
            await redis_client.set(_k_onb_gender_msg(uid), msg.message_id, ex=ONB_UI_TTL)


@dp.callback_query(F.data.startswith("gender:"), F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def set_gender(cb: CallbackQuery) -> None:
    await _cb_ack(cb)
    if cb.message:
        await _delete_or_hide(cb.message)

    gender = cb.data.split(":", 1)[1]
    if gender not in {"male", "female"}:
        return

    async with session_scope(stmt_timeout_ms=2000) as db:
        await db.execute(update(User).where(User.id == cb.from_user.id).values(gender=gender))
    await cache_gender(cb.from_user.id, gender)

    uid = cb.from_user.id
    with suppress(Exception):
        await _wiz_clear(uid)
    await _clear_onboarding_ui(uid, uid)

    sent = await _send_private_welcome(uid, full_name=cb.from_user.full_name)
    if not sent:
        ready_txt = await tr(uid, "menu.main", "Main menu")
        await _show_main_panel(uid, ready_txt)


# ---------------------------
# Menu routing in private chat
# ---------------------------
async def _build_menu_mapping(uid: int) -> dict[str, str]:
    shop_label = await tr(uid, "menu.shop", "🛒 Shop")
    reqs_label = await tr(uid, "menu.requests", "")
    channel_label = await tr(uid, "menu.link", "📢 Channel")
    persona_label = await tr(uid, "menu.persona", "🧬 Persona")
    api_label = await tr(uid, "menu.api", "🔑 API")
    mem_clear_label = await tr(uid, "menu.memory_clear", "🧹 Clear memory")

    mapping = {
        _norm_btn(channel_label): "channel",
        _norm_btn(shop_label): "shop_home",
        _norm_btn(persona_label): "persona",
        _norm_btn(api_label): "api",
        _norm_btn(mem_clear_label): "mem_clear",
    }
    reqs_norm = _norm_btn(reqs_label)
    shop_norm = _norm_btn(shop_label)
    if reqs_norm and reqs_norm != shop_norm:
        mapping[reqs_norm] = "reqs"
    else:
        # Fallback for legacy keyboards where requests label matches shop label.
        mapping[shop_norm] = "shop_home"
    return mapping


async def _send_channel_link(uid: int, chat_id: int) -> None:
    channel_label = await tr(uid, "menu.link", "📢 Channel")

    raw_url = await tr(uid, "private.channel_url", "")
    url = safe_url(raw_url)

    if not url:
        link_text = await tr(uid, "private.channel", "")
        m = re.search(r'((?:https?://|tg://)\S+)', link_text or "")
        url = safe_url(m.group(1) if m else None)

    close_label = await tr(uid, "ui.close", "✖️ Close")

    kb = (
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=channel_label, url=url)],
            [InlineKeyboardButton(text=close_label, callback_data="ui:close")],
        ]) if url else None
    )

    channel_text = await tr(uid, "private.channel", "📢 Channel link:")
    await send_message_safe(
        bot,
        chat_id,
        channel_text,
        reply_markup=kb,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@dp.message(F.chat.type == ChatType.PRIVATE, F.text)
async def on_private_message(message: Message) -> None:
    chat_id, user_id = message.chat.id, message.from_user.id
    if message.from_user.is_bot:
        return
    if not await _first_delivery(chat_id, message.message_id, "chat"):
        return

    raw_text = (message.text or "")
    urls = _extract_urls_from_entities(raw_text, message.entities)
    text = _augment_text_with_urls(raw_text, urls)

    # analytics (best-effort)
    try:
        has_link = bool(urls) or bool(re.search(r'(?:https?://|tg://)', text, re.I))
        asyncio.create_task(record_user_message(
            chat_id, user_id,
            display_name=message.from_user.full_name,
            content_type="text",
            addressed_to_bot=True,
            has_link=has_link,
        ))
    except Exception:
        pass

    # Quick menu routing
    mapping = await _build_menu_mapping(user_id)
    text_n = _norm_btn(text)
    if text_n in mapping:
        kind = mapping[text_n]
        if kind == "channel":
            await _send_channel_link(user_id, chat_id)
        elif kind == "shop_home":
            await cmd_buy(message)
        elif kind == "reqs":
            await cmd_buy_reqs(message)
        elif kind == "persona":
            await start_persona_wizard(message)
        elif kind == "api":
            await show_api_menu(message)
        elif kind == "mem_clear":
            await ask_clear_memory_confirm(message)
        return

    if await is_spam(chat_id, user_id) or text.startswith("/"):
        return

    res = await _ensure_access_and_increment(message, text)
    if not res:
        return

    _, allow_web, billing_tier, reservation_id = res
    await _store_context(chat_id, message.message_id, text, speaker_id=user_id)
    await _store_reply_target_best_effort(chat_id, message)
    
    reply_to_mid = (message.reply_to_message and message.reply_to_message.message_id)
    tg_reply_to = message.message_id if reply_to_mid else 0

    payload = {
        "chat_id": chat_id,
        "text": text,
        "user_id": user_id,
        "reply_to": reply_to_mid,
        "tg_reply_to": tg_reply_to,
        "reservation_id": reservation_id,
        "is_group": False,
        "msg_id": message.message_id,
        "trigger": "pm",
        "allow_web": allow_web,
        "billing_tier": billing_tier,
        "soft_reply_context": True,
    }
    buffer_message_for_response(payload)


# ---------------------------
# Media helpers (image/json/voice)
# ---------------------------
def is_single_media(message: Message) -> bool:
    return message.media_group_id is None


async def download_to_tmp(tg_obj: Any, suffix: str) -> str | None:
    return await media_download_to_tmp(tg_obj, suffix)


async def strict_image_load(tmp_path: str) -> Any:
    return await media_strict_image_load(tmp_path)


def sanitize_and_compress(img: Any) -> bytes:
    return media_sanitize_and_compress(img)


async def localized_image_error(user_id: int | None, reason: str) -> str:
    safe_reason = html.escape(reason or "", quote=True)
    if user_id:
        msg = await tr(
            user_id,
            "errors.image_generic",
            "",
            reason=safe_reason,
        )
        if msg:
            return msg
    return f"⚠️ Cannot process image: {safe_reason}\nPlease send exactly one image (≤ 5 MB) in a single message."


async def localized_voice_error(user_id: int | None, reason: str) -> str:
    safe_reason = html.escape(reason or "", quote=True)
    if user_id:
        msg = await tr(user_id, "errors.voice_generic", "", reason=safe_reason)
        if msg:
            return msg
    return f"⚠️ Cannot process voice message: {safe_reason}"


def _fire_and_forget(coro: Awaitable[None]) -> None:
    asyncio.create_task(coro)


def reject_multi_or_oversize_and_reply(chat_id: int, reason: str, user_id: int | None = None) -> None:
    async def _send() -> None:
        msg = await localized_image_error(user_id, reason)
        await send_message_safe(bot, chat_id, msg, parse_mode="HTML")
    _fire_and_forget(_send())


def reject_voice_and_reply(chat_id: int, reason: str, user_id: int | None = None) -> None:
    async def _send() -> None:
        msg = await localized_voice_error(user_id, reason)
        await send_message_safe(bot, chat_id, msg, parse_mode="HTML")
    _fire_and_forget(_send())


async def _handle_image_payload(
    message: Message,
    caption: str,
    jpeg_bytes: bytes,
    allow_web: bool,
    billing_tier: str,
    reservation_id: int,
) -> None:
    chat_id, user_id = message.chat.id, message.from_user.id
    cap_raw = (caption or "")
    urls = _extract_urls_from_entities(cap_raw, getattr(message, "caption_entities", None))
    cap = _augment_text_with_urls(cap_raw, urls)
    memo = "[Image]" + (f" {cap}" if cap else "")

    await _store_context(chat_id, message.message_id, memo, speaker_id=user_id)
    await _store_reply_target_best_effort(chat_id, message)
    
    reply_to_mid = (message.reply_to_message and message.reply_to_message.message_id)
    tg_reply_to = message.message_id if reply_to_mid else 0

    payload = {
        "chat_id": chat_id,
        "text": cap,
        "user_id": user_id,
        "reply_to": reply_to_mid,
        "tg_reply_to": tg_reply_to,
        "reservation_id": reservation_id,
        "is_group": False,
        "msg_id": message.message_id,
        "image_b64": base64.b64encode(jpeg_bytes).decode("ascii"),
        "image_mime": "image/jpeg",
        "trigger": "pm",
        "allow_web": allow_web,
        "billing_tier": billing_tier,
        "soft_reply_context": True,
    }
    buffer_message_for_response(payload)


def _analytics_best_effort(message: Message, content_type: str, has_link: bool) -> None:
    try:
        asyncio.create_task(record_user_message(
            message.chat.id,
            message.from_user.id,
            display_name=message.from_user.full_name,
            content_type=content_type,
            addressed_to_bot=True,
            has_link=has_link,
        ))
    except Exception:
        pass


# ---------------------------
# Photo handler
# ---------------------------
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

    cap_raw = (message.caption or "")
    cap_urls = _extract_urls_from_entities(cap_raw, getattr(message, "caption_entities", None))
    has_link = bool(cap_urls) or bool(re.search(r'(?:https?://|tg://)', cap_raw, re.I))
    _analytics_best_effort(message, "photo", has_link)

    res = await _ensure_access_and_increment(message, (message.caption or "").strip() or None)
    if not res:
        return
    _, allow_web, billing_tier, reservation_id = res

    tmp_path: str | None = None
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

        await _handle_image_payload(message, caption, safe_jpeg, allow_web, billing_tier, reservation_id)

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


# ---------------------------
# Document handler (JSON KB upload + images)
# ---------------------------
async def _handle_kb_json_upload(message: Message, doc) -> None:
    chat_id, user_id = message.chat.id, message.from_user.id
    slot_key = _kb_upload_slot_key(user_id)

    raw_slot = None
    with suppress(Exception):
        raw_slot = await redis_client.get(slot_key)

    if not raw_slot:
        txt = await tr(user_id, "kb.upload.no_slot", "No upload slot. Open API → KB → Upload, then send the JSON here.")
        await send_message_safe(bot, chat_id, txt, parse_mode="HTML")
        return

    api_key_id: int | None = None
    with suppress(Exception):
        api_key_id = int(raw_slot.decode() if isinstance(raw_slot, (bytes, bytearray)) else str(raw_slot))

    if not api_key_id:
        txt = await tr(user_id, "kb.upload.no_slot", "No upload slot. Open API → KB → Upload, then send the JSON here.")
        await send_message_safe(bot, chat_id, txt, parse_mode="HTML")
        return

    tmp_path = await download_to_tmp(doc, suffix=".json")
    if not tmp_path:
        txt = await tr(user_id, "kb.upload.download_failed", "Failed to download the file. Please try again.")
        await send_message_safe(bot, chat_id, txt, parse_mode="HTML")
        return

    raw_text = ""
    try:
        raw_text = Path(tmp_path).read_text(encoding="utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        txt = await tr(user_id, "kb.upload.bad_encoding", "Invalid encoding. Please send a valid UTF-8 JSON file.")
        await send_message_safe(bot, chat_id, txt, parse_mode="HTML")
        return
    except Exception:
        txt = await tr(user_id, "kb.upload.read_failed", "Failed to read the file. Please try again.")
        await send_message_safe(bot, chat_id, txt, parse_mode="HTML")
        return
    finally:
        with suppress(Exception):
            os.remove(tmp_path)

    if not raw_text.strip():
        txt = await tr(user_id, "kb.upload.empty", "The file is empty or contains only whitespace.")
        await send_message_safe(bot, chat_id, txt, parse_mode="HTML")
        return

    try:
        data = json.loads(raw_text)
    except Exception:
        txt = await tr(user_id, "kb.upload.bad_json", "Invalid JSON. Please send a valid UTF-8 JSON file.")
        await send_message_safe(bot, chat_id, txt, parse_mode="HTML")
        return

    if not isinstance(data, list) or not data:
        txt = await tr(user_id, "kb.upload.expect_array", "Expected a JSON array of objects.")
        await send_message_safe(bot, chat_id, txt, parse_mode="HTML")
        return

    items: list[dict] = []
    for idx, it in enumerate(data):
        if not isinstance(it, dict):
            continue
        text_val = (it.get("text") or "").strip()
        if not text_val:
            continue
        eid = str(it.get("id") or (idx + 1))
        category = (it.get("category") or "default").strip() or "default"
        tags_val = it.get("tags") or []
        if not isinstance(tags_val, list):
            tags_val = []
        tags = [str(x).strip() for x in tags_val if isinstance(x, str) and x.strip()]
        items.append({"id": eid, "text": text_val, "category": category, "tags": tags})

    if not items:
        txt = await tr(user_id, "kb.upload.no_items", "No valid items were found in the JSON file.")
        await send_message_safe(bot, chat_id, txt, parse_mode="HTML")
        return

    max_items = 0
    with suppress(Exception):
        max_items = int(getattr(settings, "MAX_KB_ITEMS_PER_UPLOAD", 2000) or 0)

    truncated = False
    original_count = len(items)
    if max_items > 0 and len(items) > max_items:
        items = items[:max_items]
        truncated = True

    async with session_scope() as db:
        res_key = await db.execute(select(ApiKey).where(ApiKey.id == api_key_id).with_for_update())
        key = res_key.scalar_one_or_none()

        if not key or key.user_id != user_id:
            txt = await tr(user_id, "api.key.not_found", "Key not found")
            await send_message_safe(bot, chat_id, txt, parse_mode="HTML")
            with suppress(Exception):
                await redis_client.delete(slot_key)
            return

        res = await db.execute(
            select(ApiKeyKnowledge.version)
            .where(ApiKeyKnowledge.api_key_id == api_key_id)
            .order_by(ApiKeyKnowledge.version.desc())
            .limit(1)
        )
        last_ver = res.scalar_one_or_none() or 0
        new_ver = last_ver + 1

        emb_model = getattr(settings, "EMBEDDING_MODEL", "text-embedding-3-large")
        kb = ApiKeyKnowledge(
            api_key_id=api_key_id,
            version=new_ver,
            label=None,
            items=items,
            embedding_model=emb_model,
            status="pending",
            chunks_count=0,
        )
        db.add(kb)
        await db.flush()
        kb_id = kb.id

    try:
        from app.tasks.celery_app import celery
        celery.send_task("kb.rebuild_for_api_key", args=[api_key_id, kb_id])
    except Exception:
        logger.exception("Failed to schedule kb.rebuild_for_api_key for key_id=%s kb_id=%s", api_key_id, kb_id)

    with suppress(Exception):
        await redis_client.delete(slot_key)

    txt_ok = await tr(user_id, "kb.upload.accepted", "✅ Knowledge base file accepted.\nThe index will be rebuilt shortly.")
    if truncated:
        note = await tr(
            user_id,
            "kb.upload.truncated",
            f"ℹ️ Imported {len(items)} of {original_count} items (limit).",
            kept=len(items),
            total=original_count,
        )
        txt_ok = f"{txt_ok}\n{note}"

    await send_message_safe(bot, chat_id, txt_ok, parse_mode="HTML")


@dp.message(F.chat.type == ChatType.PRIVATE, F.content_type == ContentType.DOCUMENT)
async def on_private_document(message: Message) -> None:
    chat_id, user_id = message.chat.id, message.from_user.id
    if await is_spam(chat_id, user_id) or message.from_user.is_bot:
        return
    if not await _first_delivery(chat_id, message.message_id, "document"):
        return
    if not is_single_media(message):
        reject_multi_or_oversize_and_reply(chat_id, "albums are not supported", user_id)
        return

    doc = message.document
    if not doc:
        return

    mime_lower = (doc.mime_type or "").lower().strip()
    file_name = (doc.file_name or "").lower().strip()

    # KB JSON upload path
    if mime_lower in {"application/json", "text/json"} or file_name.endswith(".json"):
        size = 0
        with suppress(Exception):
            size = int(getattr(doc, "file_size", 0) or 0)
        if size and size > MAX_KB_JSON_BYTES:
            txt = await tr(user_id, "kb.upload.too_large", "⚠️ The JSON file is too large for upload.")
            await send_message_safe(bot, chat_id, txt, parse_mode="HTML")
            return
        await _handle_kb_json_upload(message, doc)
        return

    # Only images otherwise
    if not mime_lower.startswith("image/"):
        txt = await tr(user_id, "errors.doc_unsupported", "⚠️ Unsupported file. Please send an image or a JSON knowledge base file.")
        await send_message_safe(bot, chat_id, txt, parse_mode="HTML")
        return

    max_doc_image_bytes = int(getattr(settings, "MAX_DOC_IMAGE_BYTES", 30 * 1024 * 1024))
    with suppress(Exception):
        if getattr(doc, "file_size", 0) and int(doc.file_size) > max_doc_image_bytes:
            reject_multi_or_oversize_and_reply(chat_id, "file is too large", user_id)
            return

    allowed_mimes = {"image/jpeg", "image/jpg", "image/pjpeg", "image/png", "image/x-png", "image/webp"}
    if (doc.mime_type or "").lower() not in allowed_mimes:
        reject_multi_or_oversize_and_reply(chat_id, "unsupported image format", user_id)
        return

    cap_raw = (message.caption or "")
    cap_urls = _extract_urls_from_entities(cap_raw, getattr(message, "caption_entities", None))
    has_link = bool(cap_urls) or bool(re.search(r'(?:https?://|tg://)', cap_raw, re.I))
    _analytics_best_effort(message, "document", has_link)

    res = await _ensure_access_and_increment(message, (message.caption or "").strip() or None)
    if not res:
        return
    _, allow_web, billing_tier, reservation_id = res

    tmp_path: str | None = None
    try:
        caption = (message.caption or "").strip()
        mime_lower = (doc.mime_type or "").lower()
        suffix = ".jpg" if mime_lower in ("image/jpeg", "image/jpg", "image/pjpeg") else (".webp" if mime_lower == "image/webp" else ".png")

        tmp_path = await download_to_tmp(doc, suffix=suffix)
        if not tmp_path:
            reject_multi_or_oversize_and_reply(chat_id, "download failed", user_id)
            return

        img = await strict_image_load(tmp_path)
        safe_jpeg = sanitize_and_compress(img)
        if len(safe_jpeg) > MAX_IMAGE_BYTES:
            reject_multi_or_oversize_and_reply(chat_id, "file is larger than 5 MB after compression", user_id)
            return

        await _handle_image_payload(message, caption, safe_jpeg, allow_web, billing_tier, reservation_id)

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


# ---------------------------
# Voice handler
# ---------------------------
@dp.message(F.chat.type == ChatType.PRIVATE, F.content_type == ContentType.VOICE)
async def on_private_voice(message: Message) -> None:
    chat_id, user_id = message.chat.id, message.from_user.id
    if await is_spam(chat_id, user_id) or message.from_user.is_bot:
        return
    if not await _first_delivery(chat_id, message.message_id, "voice"):
        return

    _analytics_best_effort(message, "voice", False)

    voice = message.voice
    if not voice:
        return

    size = int(getattr(voice, "file_size", 0) or 0)
    if size <= 0:
        reject_voice_and_reply(chat_id, "empty file", user_id)
        return
    if size > MAX_VOICE_BYTES:
        reject_voice_and_reply(chat_id, "file is too large", user_id)
        return

    duration = int(getattr(voice, "duration", 0) or 0)
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
    _, allow_web, billing_tier, reservation_id = res

    await _store_reply_target_best_effort(chat_id, message)
    
    reply_to_mid = (message.reply_to_message and message.reply_to_message.message_id)
    tg_reply_to = message.message_id if reply_to_mid else 0

    payload = {
        "chat_id": chat_id,
        "text": None,
        "user_id": user_id,
        "reply_to": reply_to_mid,
        "tg_reply_to": tg_reply_to,
        "reservation_id": reservation_id,
        "is_group": False,
        "voice_in": True,
        "voice_file_id": voice_file_id,
        "msg_id": message.message_id,
        "trigger": "pm",
        "allow_web": allow_web,
        "billing_tier": billing_tier,
        "entities": [],
        "soft_reply_context": True,
    }
    buffer_message_for_response(payload)


# ---------------------------
# Persona wizard state (Redis)
# ---------------------------
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


async def _wiz_require(uid: int) -> dict | None:
    st = await _wiz_get(uid)
    exists = False
    with suppress(Exception):
        exists = bool(await redis_client.exists(WZ_KEY.format(uid=uid)))
    if exists:
        return st or {}

    await send_message_safe(bot, uid, await tr(uid, "persona.expired", "Session expired. Starting over."), parse_mode="HTML")

    class _FakeMessage:
        chat = type("C", (), {"id": uid})
        from_user = type("U", (), {"id": uid})

    await start_persona_wizard(_FakeMessage())
    return None


def _rows_kv(items: list[tuple[str, str]], n: int, kind: str) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for label, key in items:
        row.append(InlineKeyboardButton(text=label, callback_data=f"persona:pick:{kind}:{key}"))
        if len(row) == n:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


# ---------------------------
# Persona wizard UI
# ---------------------------
async def start_persona_wizard(message: Message) -> None:
    user_id = message.chat.id

    if not getattr(settings, "SHOW_PERSONA_BUTTON", True):
        with suppress(Exception):
            await _wiz_clear(user_id)
        await _show_main_panel(user_id, await tr(user_id, "menu.main", "Main menu"))

        # Send welcome if not yet sent
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
        [InlineKeyboardButton(text=await tr(user_id, "persona.next", "▶️ Next"), callback_data="persona:step:zodiac")],
        [InlineKeyboardButton(text=await tr(user_id, "persona.reset", "♻️ Reset"), callback_data="persona:reset")],
        [InlineKeyboardButton(text=await tr(user_id, "ui.close", "✖️ Close"), callback_data="ui:close")],
    ])

    await send_message_safe(
        bot,
        message.chat.id,
        await tr(user_id, "persona.start", "Tune how I behave for you. Four quick steps."),
        parse_mode="HTML",
        reply_markup=kb,
    )


async def show_persona_start(cb: CallbackQuery) -> None:
    uid = cb.from_user.id
    st = await _wiz_require(uid)
    if st is None:
        await _cb_ack(cb)
        return

    st["step"] = "start"
    await _wiz_set(uid, st)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=await tr(uid, "persona.next", "▶️ Next"), callback_data="persona:step:zodiac")],
        [InlineKeyboardButton(text=await tr(uid, "persona.reset", "♻️ Reset"), callback_data="persona:reset")],
        [InlineKeyboardButton(text=await tr(uid, "ui.close", "✖️ Close"), callback_data="ui:close")],
    ])
    await _replace_panel(cb, await tr(uid, "persona.start", "Tune how I behave for you. Four quick steps."), kb)


@dp.callback_query(F.data == "persona:step:start", F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def persona_step_start(cb: CallbackQuery) -> None:
    await show_persona_start(cb)


@dp.callback_query(F.data.startswith("mem:clear:"), F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def mem_clear_cb(cb: CallbackQuery) -> None:
    uid = cb.from_user.id
    action = cb.data.split(":", 2)[-1]

    if action == "no":
        await _cb_ack(cb, text=await tr(uid, "memory.clear.cancelled", "Cancelled."))
        return await _ui_close_impl(cb)

    if action == "yes":
        await _cb_ack(cb, text=await tr(uid, "memory.clear.done", "✅ Done."))
        try:
            deleted = await delete_user_redis_data(uid)
            logger.info("mem_clear_cb: uid=%s deleted_keys=%s", uid, deleted)
        except Exception:
            logger.exception("mem_clear_cb: delete_user_redis_data failed uid=%s", uid)
        return await _ui_close_impl(cb)

    await _cb_ack(cb)


# ---------------------------
# Persona steps (zodiac/temp/sociality/archetypes/preview/finish/reset/cancel)
# ---------------------------
@dp.callback_query(F.data == "persona:cancel", F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def persona_cancel(cb: CallbackQuery) -> None:
    uid = cb.from_user.id
    await _cb_ack(cb, text=await tr(uid, "persona.cancel.ok", "Cancelled."))
    with suppress(Exception):
        await _wiz_clear(uid)
    await _ui_close_impl(cb)

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
    await _cb_ack(cb, text=await tr(uid, "persona.reset.ok", "Persona reset."))

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
        p = await get_persona(chat_id=uid, user_id=uid)
        p.apply_overrides(defaults)
    except Exception:
        logger.debug("persona_reset: write defaults failed; falling back to runtime reset", exc_info=True)
        p = await get_persona(chat_id=uid, user_id=uid)
        p.apply_overrides(None, reset=True)

    await _ui_close_impl(cb)


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
    items: list[tuple[str, str]] = []
    for z in ZODIAC:
        badge = ZODIAC_BADGES.get(z, "")
        z_localized = await _tr_zodiac(uid, z)
        label = f"{badge} {z_localized}".strip()
        if z == picked:
            label = f"✅ {label}"
        items.append((label, z))

    kb = InlineKeyboardMarkup(inline_keyboard=_rows_kv(items, 4, "zodiac") + [
        [InlineKeyboardButton(text=await tr(uid, "persona.skip", "⏭ Skip"), callback_data="persona:step:temperament")],
        [
            InlineKeyboardButton(text=await tr(uid, "ui.back", "◀ Back"), callback_data="persona:step:start"),
            InlineKeyboardButton(text=await tr(uid, "ui.close", "✖️ Close"), callback_data="ui:close"),
        ],
    ])

    await _cb_ack(cb)
    if cb.message:
        await _delete_or_hide(cb.message)
    await send_message_safe(bot, uid, await tr(uid, "persona.zodiac.title", "<b>Step 1 · Zodiac</b>"), parse_mode="HTML", reply_markup=kb)


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
    lab_san = await tr(uid, "persona.temp.sanguine", "Sanguine")
    lab_ch = await tr(uid, "persona.temp.choleric", "Choleric")
    lab_ph = await tr(uid, "persona.temp.phlegmatic", "Phlegmatic")
    lab_me = await tr(uid, "persona.temp.melancholic", "Melancholic")

    items = [
        ((("✅ " if sel == "sanguine" else "") + lab_san), "sanguine"),
        ((("✅ " if sel == "choleric" else "") + lab_ch), "choleric"),
        ((("✅ " if sel == "phlegmatic" else "") + lab_ph), "phlegmatic"),
        ((("✅ " if sel == "melancholic" else "") + lab_me), "melancholic"),
    ]

    kb = InlineKeyboardMarkup(inline_keyboard=_rows_kv(items, 2, "temp") + [
        [InlineKeyboardButton(text=await tr(uid, "persona.skip", "⏭ Skip"), callback_data="persona:step:sociality")],
        [InlineKeyboardButton(text=await tr(uid, "persona.back", "◀ Back"), callback_data="persona:step:zodiac")],
        [InlineKeyboardButton(text=await tr(uid, "ui.close", "✖️ Close"), callback_data="ui:close")],
    ])

    await _replace_panel(cb, await tr(uid, "persona.temperament.title", "<b>Step 2 · Temperament</b>"), kb)


@dp.callback_query(F.data == "persona:step:temperament", F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def step_temperament(cb: CallbackQuery) -> None:
    await show_temperament(cb)


async def show_sociality(cb: CallbackQuery) -> None:
    uid = cb.from_user.id
    st = await _wiz_require(uid)
    if st is None:
        await _cb_ack(cb)
        return

    st["step"] = "sociality"
    await _wiz_set(uid, st)

    sel = st.get("sociality")
    lab_i = await tr(uid, "persona.social.introvert", "Introvert")
    lab_a = await tr(uid, "persona.social.ambivert", "Ambivert")
    lab_e = await tr(uid, "persona.social.extrovert", "Extrovert")

    items = [
        ((("✅ " if sel == "introvert" else "") + lab_i), "introvert"),
        ((("✅ " if sel == "ambivert" else "") + lab_a), "ambivert"),
        ((("✅ " if sel == "extrovert" else "") + lab_e), "extrovert"),
    ]

    kb = InlineKeyboardMarkup(inline_keyboard=_rows_kv(items, 3, "sociality") + [
        [InlineKeyboardButton(text=await tr(uid, "persona.skip", "⏭ Skip"), callback_data="persona:step:archetypes")],
        [InlineKeyboardButton(text=await tr(uid, "persona.back", "◀ Back"), callback_data="persona:step:temperament")],
        [InlineKeyboardButton(text=await tr(uid, "ui.close", "✖️ Close"), callback_data="ui:close")],
    ])

    await _replace_panel(cb, await tr(uid, "persona.sociality.title", "<b>Step 3 · Sociality</b>"), kb)


@dp.callback_query(F.data == "persona:step:sociality", F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def step_sociality(cb: CallbackQuery) -> None:
    await show_sociality(cb)


async def show_archetypes(cb: CallbackQuery) -> None:
    uid = cb.from_user.id
    st = await _wiz_require(uid)
    if st is None:
        await _cb_ack(cb)
        return

    st["step"] = "archetypes"
    await _wiz_set(uid, st)

    selected = set(st.get("archetypes", []))
    title = await tr(uid, "persona.archetypes.title", "<b>Step 4 · Archetypes</b>")

    preview_label = await tr(uid, "persona.preview", "👀 Preview")
    done = await tr(uid, "persona.done", "✅ Done")
    back = await tr(uid, "persona.back", "◀ Back")
    reset = await tr(uid, "persona.reset", "♻️ Reset")
    close = await tr(uid, "ui.close", "✖️ Close")

    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for i, name in enumerate(ARCHETYPES):
        mark = "✅ " if name in selected else ""
        label = await _tr_archetype(uid, name)
        row.append(InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"persona:arch:toggle:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows += [
        [InlineKeyboardButton(text=preview_label, callback_data="persona:preview")],
        [InlineKeyboardButton(text=done, callback_data="persona:finish")],
        [InlineKeyboardButton(text=back, callback_data="persona:step:sociality")],
        [InlineKeyboardButton(text=reset, callback_data="persona:reset")],
        [InlineKeyboardButton(text=close, callback_data="ui:close")],
    ]

    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    # Try edit in-place first
    try:
        if cb.message:
            await cb.message.edit_text(title, parse_mode="HTML", reply_markup=kb)
            await _cb_ack(cb)
            return
    except TelegramBadRequest:
        pass

    await _replace_panel(cb, title, kb)


@dp.callback_query(F.data == "persona:step:archetypes", F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def step_archetypes(cb: CallbackQuery) -> None:
    await show_archetypes(cb)


@dp.callback_query(F.data.startswith("persona:arch:toggle:"), F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def arch_toggle(cb: CallbackQuery) -> None:
    uid = cb.from_user.id
    raw_idx = cb.data.split(":", 3)[-1]
    try:
        idx = int(raw_idx)
    except Exception:
        await _cb_ack(cb, text=await tr(uid, "persona.invalid_archetype", "Invalid archetype"))
        return
    if idx < 0 or idx >= len(ARCHETYPES):
        await _cb_ack(cb, text=await tr(uid, "persona.invalid_archetype", "Invalid archetype"))
        return
    name = ARCHETYPES[idx]

    st = await _wiz_get(uid)
    if not st:
        await send_message_safe(bot, uid, await tr(uid, "persona.expired", "Session expired. Starting over."), parse_mode="HTML")
        await start_persona_wizard(type("M", (), {"chat": type("C", (), {"id": uid}), "from_user": type("U", (), {"id": uid})})())
        return

    cur = set(st.get("archetypes", []))
    if name in cur:
        cur.remove(name)
    else:
        if len(cur) < MAX_ARCH:
            cur.add(name)
        else:
            await _cb_ack(cb, text=await tr(uid, "persona.pick.limit", f"You can pick up to {MAX_ARCH}.", MAX=MAX_ARCH))

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

        prefs: dict[str, Any] = {}
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

        title = await tr(uid, "persona.preview.title", "Preview")
        l_z = await tr(uid, "persona.preview.zodiac", "Zodiac")
        l_t = await tr(uid, "persona.preview.temperament", "Temperament")
        l_s = await tr(uid, "persona.preview.sociality", "Sociality")
        l_a = await tr(uid, "persona.preview.archetypes", "Archetypes")

        temp_labels = {
            "sanguine": await tr(uid, "persona.temp.sanguine", "Sanguine"),
            "choleric": await tr(uid, "persona.temp.choleric", "Choleric"),
            "phlegmatic": await tr(uid, "persona.temp.phlegmatic", "Phlegmatic"),
            "melancholic": await tr(uid, "persona.temp.melancholic", "Melancholic"),
        }

        temp_lines: list[str] = []
        for k, v in sorted(tmap.items(), key=lambda kv: float(kv[1]), reverse=True):
            name = html.escape(temp_labels.get(k, k.title()))
            pct = _pct01(v)
            bar = _bar(v, width=10)
            temp_lines.append(f"{name} — {pct}% <code>{bar}</code>")

        arch_labels = [await _tr_archetype(uid, str(a)) for a in arch]
        arch_str = ", ".join(arch_labels) if arch_labels else "—"
        badge = ZODIAC_BADGES.get(z, "")
        z_localized = await _tr_zodiac(uid, str(z))
        z_line = f"{badge} {z_localized}".strip()
        soc_line = await _tr_sociality(uid, str(soc))

        txt = (
            f"<b>{html.escape(title)}</b>\n\n"
            f"<b>{html.escape(l_z)}:</b> {html.escape(z_line)}\n"
            f"<b>{html.escape(l_s)}:</b> {html.escape(str(soc_line))}\n"
            f"<b>{html.escape(l_a)}:</b> {html.escape(arch_str)}\n\n"
            f"<b>{html.escape(l_t)}:</b>\n" + ("\n".join(temp_lines) if temp_lines else "—")
        )

        st["step"] = "preview"
        await _wiz_set(uid, st)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=await tr(uid, "ui.back", "◀ Back"), callback_data="persona:step:archetypes")],
            [InlineKeyboardButton(text=await tr(uid, "persona.done", "✅ Done"), callback_data="persona:finish")],
            [InlineKeyboardButton(text=await tr(uid, "persona.reset", "♻️ Reset"), callback_data="persona:reset")],
            [InlineKeyboardButton(text=await tr(uid, "ui.close", "✖️ Close"), callback_data="ui:close")],
        ])

        await _cb_ack(cb)
        try:
            if cb.message:
                await cb.message.edit_text(txt, parse_mode="HTML", reply_markup=kb)
            else:
                await send_message_safe(bot, uid, txt, parse_mode="HTML", reply_markup=kb)
        except TelegramBadRequest:
            await send_message_safe(bot, uid, txt, parse_mode="HTML", reply_markup=kb)

    except Exception:
        logger.debug("persona_preview failed", exc_info=True)
        await _cb_ack(cb, text=await tr(uid, "persona.preview.failed", "Failed to render preview"))


@dp.callback_query(F.data == "persona:finish", F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def persona_finish(cb: CallbackQuery) -> None:
    uid = cb.from_user.id
    await _cb_ack(cb)

    st = await _wiz_get(uid) or {}
    raw: dict[str, Any] = {}
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

    p = await get_persona(chat_id=uid, user_id=uid)
    if merged_or_none:
        p.apply_overrides(merged_or_none)
    else:
        p.apply_overrides(None, reset=True)

    with suppress(Exception):
        await _wiz_clear(uid)
    await _cb_ack(cb, text=await tr(uid, "persona.saved", "✅ Saved."))
    await _ui_close_impl(cb)

    # Welcome after finishing (if not sent)
    try:
        async with session_scope(read_only=True, stmt_timeout_ms=2000) as db:
            u = await db.get(User, uid)
            already = bool(u and u.pm_welcome_sent)
        if not already:
            await _send_private_welcome(uid, full_name=cb.from_user.full_name)
    except Exception:
        logger.debug("persona_finish: welcome send failed", exc_info=True)


# ---------------------------
# API menu + actions + KB panels
# ---------------------------
async def show_api_menu(message: Message | CallbackQuery) -> None:
    if isinstance(message, CallbackQuery):
        uid = message.from_user.id
        chat_id = uid
        cb: CallbackQuery | None = message
    else:
        uid = message.from_user.id
        chat_id = message.chat.id
        cb = None

    async with session_scope(read_only=True, stmt_timeout_ms=2000) as db:
        keys = await list_keys_for_user(db, uid)
        stats_map: dict[int, ApiKeyStats] = {}
        if keys:
            res = await db.execute(select(ApiKeyStats).where(ApiKeyStats.api_key_id.in_([k.id for k in keys])))
            for st in res.scalars():
                stats_map[st.api_key_id] = st

    title = await tr(uid, "api.title", "<b>Conversation API</b>")
    text_lines: list[str] = [title]

    base_url = (getattr(settings, "PUBLIC_API_BASE_URL", "") or "").strip()
    if base_url:
        text_lines.append(await tr(uid, "api.base_url", f"API URL: {base_url}", url=base_url))

    actions: list[list[InlineKeyboardButton]] = []

    if keys:
        text_lines.append(await tr(uid, "api.keys.title", "Your API keys:"))

        for idx, k in enumerate(keys, start=1):
            status_emoji = "🟢" if k.active else "🔴"
            suffix = (getattr(k, "key_hash", "") or "")[-6:] or str(k.id)

            line = await tr(uid, "api.key.item", f"{status_emoji} #{idx} • …{suffix}", id=idx, status=status_emoji, suffix=suffix)
            text_lines.append(line)

            row_btns: list[InlineKeyboardButton] = []

            if k.active:
                row_btns.append(InlineKeyboardButton(text=await tr(uid, "api.key.button.disable", "⏸ Disable"), callback_data=f"api:k:{k.id}:off"))
            else:
                row_btns.append(InlineKeyboardButton(text=await tr(uid, "api.key.button.enable", "▶️ Enable"), callback_data=f"api:k:{k.id}:on"))

            row_btns.append(InlineKeyboardButton(text=await tr(uid, "api.key.button.show", "👁 Show"), callback_data=f"api:k:{k.id}:show"))
            row_btns.append(InlineKeyboardButton(text=await tr(uid, "api.key.button.drop", "🗑 Delete"), callback_data=f"api:k:{k.id}:drop"))
            actions.append(row_btns)

            actions.append([InlineKeyboardButton(text=await tr(uid, "api.key.button.kb", "📚 KB"), callback_data=f"api:kb:{k.id}:panel")])

    else:
        text_lines.append(await tr(uid, "api.no_key", "You don't have an API key yet."))

    actions.append([InlineKeyboardButton(text=await tr(uid, "api.button.new", "🔑 New key"), callback_data="api:rotate")])

    howto = await tr(uid, "api.button.howto", "📘 How to use?")
    actions.append([InlineKeyboardButton(text=howto, url="https://synchatica.com/conversation-api")])

    close_label = await tr(uid, "ui.close", "✖️ Close")
    actions.append([InlineKeyboardButton(text=close_label, callback_data="ui:close")])

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
    await cache_active_key(api_key)

    ttl = 365 * 24 * 3600
    with suppress(Exception):
        ttl = int(getattr(settings, "API_KEY_SECRET_TTL_SEC", ttl))

    with suppress(Exception):
        await redis_client.set(f"api:secret:{uid}:{api_key.id}", secret, ex=max(60, ttl))

    title = await tr(uid, "api.rotate.title", "<b>New API key</b>")
    save_hint = await tr(uid, "api.rotate.save", "Save the API key. You can view it again for a limited time in the API menu.")
    again_hint = await tr(uid, "api.rotate.again", "You can view it again in the API menu.")

    safe_secret = html.escape(secret or "", quote=True)
    text = f"{title}\n{save_hint}\n\n<code>{safe_secret}</code>\n\n{again_hint}"

    back_label = await tr(uid, "ui.back", "◀ Back")
    close_label = await tr(uid, "ui.close", "✖️ Close")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=back_label, callback_data="api:panel"),
         InlineKeyboardButton(text=close_label, callback_data="ui:close")]
    ])
    await _replace_panel(cb, text, kb)


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
        raw = None
        with suppress(Exception):
            raw = await redis_client.get(redis_key)

        if not raw:
            msg = await tr(uid, "api.key.show.unavailable", "Key value is not available.")
            await _cb_ack(cb, text=msg, alert=True)
            return

        secret = raw.decode("utf-8", "ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)
        title = await tr(uid, "api.key.show.title", "API key")
        safe_secret = html.escape(secret or "", quote=True)
        txt = f"{title}\n<code>{safe_secret}</code>"

        await _cb_ack(cb)
        msg = await send_message_safe(bot, uid, txt, parse_mode="HTML")
        if msg:
            asyncio.create_task(_delete_later(uid, msg.message_id, UI_SECRET_MSG_TTL))
        return

    async with session_scope(stmt_timeout_ms=5000) as db:
        key = await db.get(ApiKey, key_id)
        if not key or key.user_id != uid:
            msg = await tr(uid, "api.key.not_found", "Key not found")
            await _cb_ack(cb, text=msg, alert=True)
            return

        if action == "off":
            if key.active:
                await deactivate_key(db, user_id=uid, api_key_id=key.id)

        elif action == "on":
            if not key.active:
                key.active = True
                await db.flush()
                with suppress(Exception):
                    if key.key_hash:
                        ck = f"api:key:{key.key_hash}"
                        ttl = int(getattr(settings, "API_KEY_CACHE_TTL_SEC", 3600))
                        await redis_client.hset(ck, mapping={"id": key.id, "user_id": uid, "active": 1})
                        await redis_client.expire(ck, max(10, ttl))

        elif action == "drop":
            if key.active:
                await deactivate_key(db, user_id=uid, api_key_id=key.id)

            with suppress(Exception):
                await redis_client.delete(f"api:secret:{uid}:{key.id}")
            with suppress(Exception):
                if key.key_hash:
                    await redis_client.delete(f"api:key:{key.key_hash}")

            if getattr(settings, "API_PERSONA_PER_KEY", True):
                with suppress(Exception):
                    from app.tasks.celery_app import celery
                    celery.send_task("api.cleanup_memory_for_key", args=[key.id])

            with suppress(Exception):
                from app.tasks.celery_app import celery
                celery.send_task("kb.clear_for_api_key", args=[key.id])

            with suppress(Exception):
                await db.execute(delete(ApiKeyStats).where(ApiKeyStats.api_key_id == key.id))
            with suppress(Exception):
                await db.delete(key)
            await db.flush()

    await _cb_ack(cb)
    await show_api_menu(cb)


@dp.callback_query(F.data.startswith("api:kb:"), F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def api_kb_panel(cb: CallbackQuery) -> None:
    uid = cb.from_user.id
    data = cb.data or ""
    prefix = "api:kb:"
    if not data.startswith(prefix):
        await _cb_ack(cb)
        return

    rest = data[len(prefix):]
    try:
        key_id_str, action = rest.split(":", 1)
        key_id = int(key_id_str)
    except ValueError:
        await _cb_ack(cb)
        return

    async with session_scope(read_only=True, stmt_timeout_ms=5000) as db:
        key = await db.get(ApiKey, key_id)
        if not key or key.user_id != uid:
            msg = await tr(uid, "api.key.not_found", "Key not found")
            await _cb_ack(cb, text=msg, alert=True)
            return

        res = await db.execute(
            select(ApiKeyKnowledge)
            .where(ApiKeyKnowledge.api_key_id == key_id)
            .order_by(ApiKeyKnowledge.version.desc())
            .limit(1)
        )
        kb = res.scalar_one_or_none()

    if action == "panel":
        await _show_kb_main_panel(cb, key, kb)
        return

    if action == "upload":
        with suppress(Exception):
            await redis_client.set(_kb_upload_slot_key(uid), str(key_id), ex=3600)

        title = await tr(uid, "kb.title", "<b>Knowledge base</b>")
        hint = await tr(uid, "kb.upload.hint", "📚 Send a JSON file in this chat to upload it for this key.")
        back_label = await tr(uid, "ui.back", "◀ Back")
        close_label = await tr(uid, "ui.close", "✖️ Close")

        kb2 = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=back_label, callback_data=f"api:kb:{key_id}:panel")],
            [InlineKeyboardButton(text=close_label, callback_data="ui:close")],
        ])
        await _replace_panel(cb, f"{title}\n\n{hint}", kb2)
        return

    if action == "clear":
        async with session_scope(stmt_timeout_ms=5000) as db:
            await db.execute(delete(ApiKeyKnowledge).where(ApiKeyKnowledge.api_key_id == key_id))
            await db.flush()

        with suppress(Exception):
            from app.services.responder.rag.api_kb_proc import invalidate_api_kb_cache
            invalidate_api_kb_cache(key_id)
        with suppress(Exception):
            from app.services.responder.rag.keyword_filter import invalidate_tags_index
            invalidate_tags_index(key_id)
        with suppress(Exception):
            from app.tasks.celery_app import celery
            celery.send_task("kb.clear_for_api_key", args=[key_id])

        msg = await tr(uid, "kb.cleared", "✅ Knowledge base for this key has been cleared.")
        await _cb_ack(cb, text=msg, alert=False)
        await show_api_menu(cb)
        return

    await _cb_ack(cb)


async def _show_kb_main_panel(cb: CallbackQuery, key: ApiKey, kb: ApiKeyKnowledge | None) -> None:
    uid = cb.from_user.id

    title = await tr(uid, "kb.title", "<b>Knowledge base for this key</b>")

    l_id = await tr(uid, "kb.field.id", "ID")
    l_status = await tr(uid, "kb.field.status", "Status")
    l_version = await tr(uid, "kb.field.version", "Version")
    l_items = await tr(uid, "kb.field.items", "Items")
    l_chunks = await tr(uid, "kb.field.chunks", "Chunks")

    if kb:
        status = (kb.status or "unknown").lower()
        status_emoji = {"pending": "⏳", "building": "⚙️", "ready": "🟢", "failed": "🔴"}.get(status, "")
        status_txt = await tr(uid, f"kb.status.{status}", status)
        status_label = f"{status_emoji} {status_txt}".strip()
        items_count = len(kb.items or [])
        chunks_count = getattr(kb, "chunks_count", 0) or 0
        version = kb.version
    else:
        status_label = await tr(uid, "kb.status.none", "— no KB —")
        items_count = 0
        chunks_count = 0
        version = 0

    lines = [
        title,
        "",
        f"{l_id}: <code>{key.id}</code>",
        f"{l_status}: {status_label}",
        f"{l_version}: {version}",
        f"{l_items}: {items_count}",
        f"{l_chunks}: {chunks_count}",
    ]

    kb_rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=await tr(uid, "kb.button.upload", "📤 Upload JSON"), callback_data=f"api:kb:{key.id}:upload")]
    ]

    if kb:
        kb_rows.append([InlineKeyboardButton(text=await tr(uid, "kb.button.clear", "🗑 Clear KB"), callback_data=f"api:kb:{key.id}:clear")])

    back_label = await tr(uid, "ui.back", "◀ Back")
    close_label = await tr(uid, "ui.close", "✖️ Close")
    kb_rows.append([InlineKeyboardButton(text=back_label, callback_data="api:panel"),
                    InlineKeyboardButton(text=close_label, callback_data="ui:close")])

    await _replace_panel(cb, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows))
