#app/bot/handlers/payments.py
from __future__ import annotations

import asyncio
import logging
import time

from contextlib import suppress
from dataclasses import dataclass
from functools import wraps
from typing import Any, Dict, Optional, Tuple, Literal, Callable, Awaitable

from aiogram import F
from aiogram.enums import ChatType, ContentType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, OperationalError

from app.bot.components.constants import redis_client
from app.bot.components.dispatcher import dp
from app.bot.i18n import t
from app.bot.utils.shop_tiers import (
    find_gift,
    gift_display_name,
    gift_tiers,
    purchase_tiers,
)
from app.bot.utils.telegram_safe import (
    delete_message_safe,
    send_invoice_safe,
    send_message_safe,
)
from app.clients.telegram_client import get_bot
from app.config import settings
from app.core.db import session_scope
from app.core.memory import push_message
from app.core.models import PaymentOutbox
from app.services.user.user_service import compute_remaining, get_or_create_user
from app.tasks.celery_app import celery

logger = logging.getLogger(__name__)
bot = get_bot()


# ----------------------------
# Redis keys / runtime settings
# ----------------------------

class RedisKeys:
    @staticmethod
    def pending(user_id: int) -> str:
        return f"pending_invoice:{user_id}"

    @staticmethod
    def pending_payload(user_id: int) -> str:
        return f"pending_invoice_payload:{user_id}"

    @staticmethod
    def pending_msg(user_id: int) -> str:
        return f"pending_invoice_msg:{user_id}"

    @staticmethod
    def shop_last_tab(user_id: int) -> str:
        return f"shop:last_tab:{user_id}"

    @staticmethod
    def buy_menu_msg(user_id: int) -> str:
        return f"buy_menu_msg:{user_id}"

    @staticmethod
    def buy_info_msg(user_id: int) -> str:
        return f"buy_info_msg:{user_id}"

    @staticmethod
    def cb_rate(user_id: int) -> str:
        return f"cb_rate:{user_id}"


PENDING_TTL = int(getattr(settings, "PENDING_INVOICE_TTL", 1800))
PENDING_STALE_GRACE_SEC = int(getattr(settings, "PENDING_INVOICE_STALE_GRACE_SEC", 60))

SHOP_LAST_TAB_TTL = int(getattr(settings, "SHOP_LAST_TAB_TTL", 7 * 86400))
CB_RATE_TTL = 1

TRANSIENT_NOTICE_TTL = int(getattr(settings, "PAYMENTS_TRANSIENT_NOTICE_TTL", 6))
SUCCESS_NOTICE_TTL_BUY = int(getattr(settings, "PAYMENTS_SUCCESS_NOTICE_TTL_BUY", 60))
SUCCESS_NOTICE_TTL_GIFT = int(getattr(settings, "PAYMENTS_SUCCESS_NOTICE_TTL_GIFT", 90))

CB_DEDUPE_TTL = int(getattr(settings, "PAYMENTS_CB_DEDUPE_TTL", 86400))
ENQUEUE_ERROR_MAX_LEN = 500


# ----------------------------
# Small helpers (style like private_new)
# ----------------------------

def _to_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        if isinstance(v, (bytes, bytearray)):
            v = v.decode("utf-8", "ignore")
        s = str(v).strip()
        return int(s) if s else None
    except Exception:
        return None


async def tr(uid: int, key: str, default: str = "", **kwargs: Any) -> str:
    try:
        s = await t(uid, key, **kwargs)
        return s or default
    except Exception:
        return default


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


def dedupe_callback(ttl: int = CB_DEDUPE_TTL):
    """
    Drops duplicate callback deliveries by cb.id (like private_new.dedupe_callback()).
    """
    def deco(fn: Callable[..., Awaitable[Any]]):
        @wraps(fn)
        async def wrapper(cb: CallbackQuery, *args, **kwargs):
            try:
                key = f"seen:cbq:{cb.id}"
                seen = await redis_client.set(key, 1, nx=True, ex=ttl)
                if not seen:
                    await _cb_ack(cb, cache=1)
                    return
            except Exception:
                pass
            return await fn(cb, *args, **kwargs)
        return wrapper
    return deco


def _norm_tab(tab: Optional[str]) -> Literal["home", "gifts", "reqs"]:
    t0 = (tab or "").strip().lower()
    if t0 in {"gift", "gifts"}:
        return "gifts"
    if t0 in {"req", "reqs", "requests"}:
        return "reqs"
    return "home"


def _shop_title_key(tab: str) -> str:
    return {
        "gifts": "shop.title.gifts",
        "reqs": "shop.title.reqs",
        "home": "shop.title.home",
    }.get(_norm_tab(tab), "shop.title.home")


def _shop_subtitle_key(tab: str) -> str:
    return {
        "gifts": "shop.subtitle.gifts",
        "reqs": "shop.subtitle.reqs",
        "home": "shop.subtitle.home",
    }.get(_norm_tab(tab), "shop.subtitle.home")


# ----------------------------
# Payload parsing
# ----------------------------

PayloadKind = Literal["buy", "gift"]


@dataclass(frozen=True)
class ParsedPayload:
    kind: PayloadKind
    req: Optional[int] = None
    gift_code: Optional[str] = None

    @property
    def raw(self) -> str:
        if self.kind == "buy":
            return f"buy_{int(self.req or 0)}"
        return f"gift_{self.gift_code or ''}"


def _parse_payload(payload: str) -> Optional[ParsedPayload]:
    p = (payload or "").strip()
    if p.startswith("buy_"):
        try:
            return ParsedPayload(kind="buy", req=int(p.split("_", 1)[1]))
        except Exception:
            return None
    if p.startswith("gift_"):
        code = (p.split("_", 1)[1] or "").strip()
        if not code:
            return None
        return ParsedPayload(kind="gift", gift_code=code)
    return None


# ----------------------------
# UX helpers (delete later, notices)
# ----------------------------

async def _delete_later(chat_id: int, message_id: Optional[int], delay: int) -> None:
    if not message_id:
        return
    try:
        await asyncio.sleep(max(1, int(delay)))
        await delete_message_safe(bot, chat_id, message_id)
    except Exception:
        pass


async def send_transient_notice(
    chat_id: int,
    text: str,
    *,
    parse_mode: Optional[str] = None,
    delay: Optional[int] = None,
) -> None:
    msg = await send_message_safe(bot, chat_id, text, parse_mode=parse_mode)
    if not msg:
        return
    asyncio.create_task(_delete_later(chat_id, msg.message_id, delay or TRANSIENT_NOTICE_TTL))


# ----------------------------
# Pending state (redis)
# ----------------------------

async def _clear_pending_state(user_id: int) -> None:
    with suppress(Exception):
        await redis_client.delete(
            RedisKeys.pending(user_id),
            RedisKeys.pending_payload(user_id),
            RedisKeys.pending_msg(user_id),
            RedisKeys.buy_info_msg(user_id),
            RedisKeys.buy_menu_msg(user_id),
        )


async def _normalize_pending_state(user_id: int) -> None:
    """
    Ensures pending keys have TTL and clears stale pending state if timestamp is too old.
    """
    try:
        raw_ts = await redis_client.get(RedisKeys.pending(user_id))
        ts = _to_int(raw_ts)
        now = int(time.time())

        if ts and ts > 1_600_000_000:
            if (now - ts) > (PENDING_TTL + max(1, PENDING_STALE_GRACE_SEC)):
                await _clear_pending_state(user_id)
                return

        keys = [
            RedisKeys.pending(user_id),
            RedisKeys.pending_payload(user_id),
            RedisKeys.pending_msg(user_id),
            RedisKeys.buy_info_msg(user_id),
        ]
        for k in keys:
            with suppress(Exception):
                ttl = await redis_client.ttl(k)
                if ttl == -1:
                    await redis_client.expire(k, PENDING_TTL)
    except Exception:
        return


async def _get_last_tab(user_id: int) -> str:
    try:
        raw = await redis_client.get(RedisKeys.shop_last_tab(user_id))
        val = raw.decode("utf-8", "ignore") if isinstance(raw, (bytes, bytearray)) else (str(raw) if raw else "")
    except Exception:
        val = ""
    return _norm_tab(val)


async def _store_last_tab(user_id: int, tab: str) -> None:
    with suppress(Exception):
        await redis_client.set(
            RedisKeys.shop_last_tab(user_id),
            _norm_tab(tab),
            ex=max(60, SHOP_LAST_TAB_TTL),
        )


# ----------------------------
# Cooldown (keep existing semantics)
# ----------------------------

async def cooldown(cb: CallbackQuery) -> bool:
    try:
        ok = await redis_client.set(RedisKeys.cb_rate(cb.from_user.id), 1, ex=CB_RATE_TTL, nx=True)
        if ok:
            return False
        await _cb_ack(cb, await tr(cb.from_user.id, "payments.too_frequent", ""), alert=False)
        return True
    except Exception:
        return False


# ----------------------------
# Cleanup UI
# ----------------------------

async def _delete_prev_shop_menu(user_id: int, chat_id: int) -> None:
    with suppress(Exception):
        prev_menu_raw = await redis_client.get(RedisKeys.buy_menu_msg(user_id))
        prev_menu_id = _to_int(prev_menu_raw)
        if prev_menu_id:
            await delete_message_safe(bot, chat_id, prev_menu_id)
        await redis_client.delete(RedisKeys.buy_menu_msg(user_id))


async def clear_payment_ui(user_id: int, chat_id: int) -> None:
    try:
        inv_msg_id = _to_int(await redis_client.get(RedisKeys.pending_msg(user_id)))
        info_msg_id = _to_int(await redis_client.get(RedisKeys.buy_info_msg(user_id)))
        menu_msg_id = _to_int(await redis_client.get(RedisKeys.buy_menu_msg(user_id)))

        await delete_message_safe(bot, chat_id, inv_msg_id)
        await delete_message_safe(bot, chat_id, info_msg_id)
        await delete_message_safe(bot, chat_id, menu_msg_id)

        await redis_client.delete(
            RedisKeys.pending(user_id),
            RedisKeys.pending_payload(user_id),
            RedisKeys.pending_msg(user_id),
            RedisKeys.buy_info_msg(user_id),
            RedisKeys.buy_menu_msg(user_id),
        )
    except Exception:
        logger.exception("Failed to clear payment UI for user=%s", user_id)


async def clear_payment_runtime_keys(user_id: int) -> int:
    keys = [
        RedisKeys.pending(user_id),
        RedisKeys.pending_payload(user_id),
        RedisKeys.pending_msg(user_id),
        RedisKeys.buy_info_msg(user_id),
        RedisKeys.cb_rate(user_id),
    ]
    try:
        return int(await redis_client.unlink(*keys))
    except Exception:
        return int(await redis_client.delete(*keys))


# ----------------------------
# Pending invoice stub (message + keyboard)
# ----------------------------

async def _build_pending_stub(user_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    payload_raw = await redis_client.get(RedisKeys.pending_payload(user_id))
    payload_str = (
        payload_raw.decode("utf-8", "ignore")
        if isinstance(payload_raw, (bytes, bytearray))
        else (str(payload_raw) if payload_raw else "")
    )
    parsed = _parse_payload(payload_str)

    cancel_label = (
        (await tr(user_id, "payments.pending.cancel_button", ""))
        or (await tr(user_id, "payments.cancel_button", ""))
        or "❌ Cancel"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=cancel_label, callback_data="payments:cancel")],
        ]
    )

    if parsed and parsed.kind == "gift":
        gift = find_gift(parsed.gift_code or "")
        if gift:
            g_name = await gift_display_name(user_id, gift)
            g_emoji = (str(gift.get("emoji") or "")).strip()
            g_label = (f"{g_emoji} {g_name}").strip() if g_emoji else g_name
            text = (
                await tr(user_id, "payments.pending_wait_gift", "", gift=g_label)
                or await tr(user_id, "payments.pending_exists_gift", "", gift=g_label)
                or f"⏳ Payment pending.\n🎁 Gift: {g_label}\nPay the invoice above or cancel."
            )
            return text, kb

    if parsed and parsed.kind == "buy" and isinstance(parsed.req, int):
        req = int(parsed.req)
        text = (
            await tr(user_id, "payments.pending_wait_tier", "", req=req)
            or await tr(user_id, "payments.pending_exists_tier", "", req=req)
            or f"⏳ Payment pending for 💬 {req}. Pay the invoice above or cancel."
        )
        return text, kb

    text = (
        await tr(user_id, "payments.pending_wait", "")
        or await tr(user_id, "payments.pending_exists", "")
        or "⏳ Payment pending. Pay the invoice above or cancel."
    )
    return text, kb


async def show_pending_invoice_stub(chat_id: int, user_id: int) -> bool:
    await _normalize_pending_state(user_id)

    inv_msg_id = _to_int(await redis_client.get(RedisKeys.pending_msg(user_id)))
    if not inv_msg_id:
        await clear_payment_ui(user_id, chat_id)
        txt = (await tr(user_id, "payments.pending_expired", "⏳ Invoice expired. Please try again."))
        await send_transient_notice(chat_id, txt, parse_mode="HTML")
        return False

    text, kb = await _build_pending_stub(user_id)

    prev_info_id = _to_int(await redis_client.get(RedisKeys.buy_info_msg(user_id)))
    if prev_info_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=prev_info_id,
                text=text,
                parse_mode="HTML",
                reply_markup=kb,
            )
            await redis_client.expire(RedisKeys.buy_info_msg(user_id), PENDING_TTL)
            return True
        except TelegramBadRequest:
            with suppress(Exception):
                await redis_client.delete(RedisKeys.buy_info_msg(user_id))
        except Exception:
            with suppress(Exception):
                await redis_client.delete(RedisKeys.buy_info_msg(user_id))

    msg = await send_message_safe(
        bot,
        chat_id,
        text,
        parse_mode="HTML",
        reply_markup=kb,
        reply_to_message_id=inv_msg_id,
    ) or await send_message_safe(bot, chat_id, text, parse_mode="HTML", reply_markup=kb)

    if not msg:
        logger.warning("show_pending_invoice_stub: failed to create stub for user=%s", user_id)
        return True

    await redis_client.set(RedisKeys.buy_info_msg(user_id), msg.message_id, ex=PENDING_TTL)
    return True


async def ensure_no_pending_or_show_stub(chat_id: int, user_id: int) -> bool:
    if await redis_client.exists(RedisKeys.pending(user_id)):
        return not await show_pending_invoice_stub(chat_id, user_id)
    return True


# ----------------------------
# Payment “success” UX
# ----------------------------

async def _push_gift_event(chat_id: int, user_id: int, gift_label: str) -> None:
    gl = (gift_label or "").strip()
    if not gl:
        return
    note = f"[ContextNote] Gift received: {gl}."
    with suppress(Exception):
        await push_message(
            int(chat_id),
            "system",
            note,
            user_id=int(user_id),
            namespace="default",
        )


async def _payment_success_notice(
    user_id: int,
    *,
    kind: PayloadKind,
    duplicate: bool,
    req_amt: int = 0,
    remaining: Optional[int] = None,
) -> Tuple[str, int]:
    if kind == "buy":
        if duplicate:
            text = (
                await tr(user_id, "payments.success_duplicate", "", remaining=remaining)
                or await tr(user_id, "payments.success", "", req=req_amt, remaining=remaining)
                or f"✅ Payment already processed. Remaining: {remaining}"
            )
        else:
            text = (
                await tr(user_id, "payments.success", "", req=req_amt, remaining=remaining)
                or f"✅ Success! +{req_amt} requests. Left: 💬 <b>{remaining}</b>"
            )
        return text, SUCCESS_NOTICE_TTL_BUY

    # gift
    if duplicate:
        text = (await tr(user_id, "payments.success_duplicate_short", "✅ Payment already processed."))
    else:
        text = (await tr(user_id, "payments.gift_delivered", "✅ Gift delivered."))
    return text, SUCCESS_NOTICE_TTL_GIFT


async def _refresh_shop_after_payment(msg: Message) -> None:
    tab = await _get_last_tab(msg.from_user.id)
    await _show_shop(msg, tab=tab)


# ----------------------------
# Shop rendering
# ----------------------------

async def _render_shop(user_id: int, remaining: int, tab: str = "home") -> Tuple[str, InlineKeyboardMarkup]:
    tab0 = _norm_tab(tab)

    title = (await tr(user_id, _shop_title_key(tab0), "")) or (await tr(user_id, "shop.title", "<b>🛒 Shop</b>"))
    subtitle = (await tr(user_id, _shop_subtitle_key(tab0), "")) or (await tr(user_id, "shop.subtitle", ""))
    balance = await tr(user_id, "shop.balance", "", remaining=remaining)
    if not balance:
        balance = (await tr(user_id, "payments.you_have", "", remaining=remaining)) or f"Requests left: 💬 <b>{remaining}</b>"

    close_label = (await tr(user_id, "ui.close", "✖️ Close")) or "✖️ Close"
    back_label = (await tr(user_id, "ui.back", "◀ Back")) or "◀ Back"

    rows: list[list[InlineKeyboardButton]] = []

    if tab0 == "home":
        btn_gifts = (await tr(user_id, "shop.open.gifts", "")) or (await tr(user_id, "shop.tab.gifts", "")) or "🎁 Gifts"
        btn_reqs = (await tr(user_id, "shop.open.reqs", "")) or (await tr(user_id, "shop.tab.requests", "")) or "⚡ Buy requests"
        rows.append([InlineKeyboardButton(text=btn_gifts, callback_data="shop:tab:gifts")])
        rows.append([InlineKeyboardButton(text=btn_reqs, callback_data="shop:tab:reqs")])
        rows.append([InlineKeyboardButton(text=close_label, callback_data="ui:close")])
        return f"{title}\n{balance}\n\n{subtitle}", InlineKeyboardMarkup(inline_keyboard=rows)

    if tab0 == "gifts":
        gifts = gift_tiers()
        for i in range(0, len(gifts), 2):
            chunk = gifts[i : i + 2]
            row: list[InlineKeyboardButton] = []
            for g in chunk:
                emoji = (g.get("emoji") or "").strip()
                code = (g.get("code") or "").strip()
                req = int(g.get("requests") or 0)
                stars = int(g.get("stars") or 0)
                btn_text = (
                    await tr(user_id, "shop.gift.button", "", emoji=emoji, req=req, stars=stars)
                    or f"{emoji} • 💬 {req} • ⭐️ {stars}"
                ).strip()
                row.append(InlineKeyboardButton(text=btn_text, callback_data=f"gift_tier:{code}"))
            rows.append(row)

        req_tab = (await tr(user_id, "shop.tab.requests", "⚡️ Requests")) or "⚡️ Requests"
        rows.append(
            [
                InlineKeyboardButton(text=back_label, callback_data="shop:tab:home"),
                InlineKeyboardButton(text=req_tab, callback_data="shop:tab:reqs"),
            ]
        )
        rows.append([InlineKeyboardButton(text=close_label, callback_data="ui:close")])
        return f"{title}\n{balance}\n\n{subtitle}", InlineKeyboardMarkup(inline_keyboard=rows)

    # reqs
    tiers_map = purchase_tiers()
    tiers = sorted(list(tiers_map.items()), key=lambda kv: int(kv[0]))
    for i in range(0, len(tiers), 2):
        batch = tiers[i : i + 2]
        row: list[InlineKeyboardButton] = []
        for req, stars in batch:
            label = await tr(user_id, "payments.buy_button", "", req=req, stars=stars)
            row.append(InlineKeyboardButton(text=label or f"💬 {req} = ⭐ {stars}", callback_data=f"buy_tier:{req}"))
        rows.append(row)

    gift_tab = (await tr(user_id, "shop.tab.gifts", "🎁 Gifts")) or "🎁 Gifts"
    rows.append(
        [
            InlineKeyboardButton(text=back_label, callback_data="shop:tab:home"),
            InlineKeyboardButton(text=gift_tab, callback_data="shop:tab:gifts"),
        ]
    )
    rows.append([InlineKeyboardButton(text=close_label, callback_data="ui:close")])
    return f"{title}\n{balance}\n\n{subtitle}", InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_shop(message: Message | CallbackQuery, *, tab: Optional[str] = None) -> None:
    if isinstance(message, CallbackQuery):
        user_id = message.from_user.id
        cb = message
        msg_obj = cb.message
        chat_id = (msg_obj.chat.id if msg_obj and msg_obj.chat else user_id)
        await _cb_ack(cb)
    else:
        user_id = message.from_user.id
        chat_id = message.chat.id
        cb = None
        msg_obj = None

    if not await ensure_no_pending_or_show_stub(chat_id, user_id):
        return

    tab0 = _norm_tab(tab) if tab is not None else _norm_tab(await _get_last_tab(user_id))

    try:
        async with session_scope(stmt_timeout_ms=2000) as db:
            tg_user = (cb.from_user if cb else message.from_user)  # type: ignore[union-attr]
            user = await get_or_create_user(db, tg_user)
            remaining = compute_remaining(user)
    except Exception:
        logger.exception("_show_shop: DB error")
        await send_message_safe(bot, chat_id, (await tr(user_id, "payments.gen_error", "⚠️ Error. Please try again.")), parse_mode="HTML")
        return

    text, keyboard = await _render_shop(user_id, remaining, tab=tab0)
    await _store_last_tab(user_id, tab0)

    if cb and msg_obj:
        try:
            await msg_obj.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
            with suppress(Exception):
                await redis_client.set(RedisKeys.buy_menu_msg(user_id), msg_obj.message_id, ex=PENDING_TTL)
            return
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                return

    sent = await send_message_safe(bot, chat_id, text, reply_markup=keyboard, parse_mode="HTML")
    if sent:
        with suppress(Exception):
            await redis_client.set(RedisKeys.buy_menu_msg(user_id), sent.message_id, ex=PENDING_TTL)


# ----------------------------
# Invoice creation (shared)
# ----------------------------

async def _mark_pending(user_id: int, payload: str) -> bool:
    """
    Returns True if pending lock acquired.
    """
    if not await redis_client.set(RedisKeys.pending(user_id), int(time.time()), ex=PENDING_TTL, nx=True):
        return False
    with suppress(Exception):
        await redis_client.set(RedisKeys.pending_payload(user_id), payload, ex=PENDING_TTL)
    return True


async def _delete_shop_menu_after_invoice(user_id: int, chat_id: int) -> None:
    with suppress(Exception):
        menu_id_raw = await redis_client.get(RedisKeys.buy_menu_msg(user_id))
        menu_id = _to_int(menu_id_raw)
        if menu_id:
            await delete_message_safe(bot, chat_id, menu_id)
        await redis_client.delete(RedisKeys.buy_menu_msg(user_id))


@dataclass(frozen=True)
class InvoiceSpec:
    payload: str
    title: str
    description: str
    prices: list[LabeledPrice]


async def _build_invoice_spec_for_buy(user_id: int, req: int) -> Optional[InvoiceSpec]:
    tiers_map = purchase_tiers()
    stars = int(tiers_map.get(req, 0) or 0)
    if req not in tiers_map or stars <= 0:
        return None

    buy_label = await tr(user_id, "payments.buy_button", "", req=req, stars=stars)
    prices = [LabeledPrice(label=buy_label or f"💬 {req} = ⭐ {stars}", amount=stars)]

    title = await tr(user_id, "payments.invoice_title", "", req=req) or f"Buy 💬 {req} chat requests"
    desc = await tr(user_id, "payments.invoice_desc", "", req=req, stars=stars) or f"Get 💬 {req} chat requests for ⭐ {stars}"

    return InvoiceSpec(
        payload=f"buy_{req}",
        title=title,
        description=desc,
        prices=prices,
    )


async def _build_invoice_spec_for_gift(user_id: int, code: str) -> tuple[Optional[InvoiceSpec], Optional[Dict[str, Any]]]:
    gift = find_gift(code)
    if not gift:
        return None, None

    emoji = (gift.get("emoji") or "").strip()
    name = await gift_display_name(user_id, gift)
    req = int(gift.get("requests") or 0)
    stars = int(gift.get("stars") or 0)
    if req <= 0 or stars <= 0:
        return None, gift

    label = (
        await tr(user_id, "shop.gift.button", "", emoji=emoji, gift=name, req=req, stars=stars)
        or f"{emoji} {name} • 💬 {req} • ⭐️ {stars}"
    )
    prices = [LabeledPrice(label=label.strip(), amount=stars)]

    inv_title = await tr(user_id, "shop.gift.invoice_title", "", gift=name) or f"🎁 Gift: {name}"
    inv_desc = await tr(user_id, "shop.gift.invoice_desc", "", gift=name, req=req, stars=stars) or f"💬 {req} requests • ⭐️ {stars}"

    return InvoiceSpec(
        payload=f"gift_{gift['code']}",
        title=inv_title,
        description=inv_desc,
        prices=prices,
    ), gift


async def _send_invoice_and_stub(chat_id: int, user_id: int, spec: InvoiceSpec) -> bool:
    inv_msg = await send_invoice_safe(
        bot,
        chat_id=user_id,  # keep: invoice goes to user chat
        provider_token=settings.PAYMENT_PROVIDER_TOKEN,
        title=spec.title,
        description=spec.description,
        payload=spec.payload,
        currency=settings.PAYMENT_CURRENCY,
        prices=spec.prices,
    )

    if not inv_msg:
        logger.warning("Failed to send invoice payload=%s", spec.payload)
        with suppress(Exception):
            await redis_client.delete(RedisKeys.pending(user_id), RedisKeys.pending_payload(user_id), RedisKeys.pending_msg(user_id))
        await send_message_safe(bot, chat_id, (await tr(user_id, "payments.gen_error", "Couldn't create the invoice. Please try again later.")), parse_mode="HTML")
        return False

    await redis_client.set(RedisKeys.pending_msg(user_id), inv_msg.message_id, ex=PENDING_TTL)
    await show_pending_invoice_stub(chat_id, user_id)
    await _delete_shop_menu_after_invoice(user_id, chat_id)
    return True


# ----------------------------
# Handlers
# ----------------------------

@dp.message(Command("buy"), F.chat.type == ChatType.PRIVATE)
async def cmd_buy(message: Message) -> None:
    user_id = message.from_user.id
    chat_id = message.chat.id

    await _delete_prev_shop_menu(user_id, chat_id)

    if not await ensure_no_pending_or_show_stub(chat_id, user_id):
        return

    await _show_shop(message, tab="home")


async def cmd_buy_reqs(message: Message) -> None:
    user_id = message.from_user.id
    chat_id = message.chat.id

    await _delete_prev_shop_menu(user_id, chat_id)

    if not await ensure_no_pending_or_show_stub(chat_id, user_id):
        return

    await _show_shop(message, tab="reqs")


@dp.message(Command("shop"), F.chat.type == ChatType.PRIVATE)
async def cmd_shop(message: Message) -> None:
    await cmd_buy(message)


@dp.callback_query(F.data.startswith("shop:tab:"), F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def shop_switch_tab(cb: CallbackQuery) -> None:
    if await cooldown(cb):
        return
    tab = (cb.data.split(":", 2)[-1] or "gifts").strip().lower()
    await _show_shop(cb, tab=_norm_tab(tab))


@dp.callback_query(F.data.startswith("buy_tier:"), F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def on_buy_tier(cb: CallbackQuery) -> None:
    if await cooldown(cb):
        return
    await _cb_ack(cb)

    user_id = cb.from_user.id
    chat_id = cb.message.chat.id if cb.message else user_id

    try:
        req = int((cb.data or "").split(":", 1)[1])
    except Exception:
        await send_message_safe(bot, chat_id, (await tr(user_id, "payments.error", "Payment error, please try again.")), parse_mode="HTML")
        return

    spec = await _build_invoice_spec_for_buy(user_id, req)
    if not spec:
        logger.warning("on_buy_tier: invalid tier req=%r", req)
        await send_message_safe(bot, chat_id, (await tr(user_id, "payments.error", "Payment error, please try again.")), parse_mode="HTML")
        return

    if not await _mark_pending(user_id, spec.payload):
        await show_pending_invoice_stub(chat_id, user_id)
        return

    await _send_invoice_and_stub(chat_id, user_id, spec)


@dp.callback_query(F.data == "payments:cancel", F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def payments_cancel(cb: CallbackQuery) -> None:
    if await cooldown(cb):
        return

    user_id = cb.from_user.id
    chat_id = cb.message.chat.id if cb.message else user_id
    tab = await _get_last_tab(user_id)

    had_pending = False
    with suppress(Exception):
        had_pending = bool(await redis_client.exists(RedisKeys.pending(user_id)))

    await clear_payment_ui(user_id, chat_id)

    if had_pending:
        await send_transient_notice(chat_id, (await tr(user_id, "payments.cancelled", "Canceled.")), parse_mode="HTML")

    await _show_shop(cb, tab=tab)


@dp.callback_query(F.data.startswith("gift_tier:"), F.message.chat.type == ChatType.PRIVATE)
@dedupe_callback()
async def on_gift_tier(cb: CallbackQuery) -> None:
    if await cooldown(cb):
        return
    await _cb_ack(cb)

    user_id = cb.from_user.id
    chat_id = cb.message.chat.id if cb.message else user_id

    code = (cb.data.split(":", 1)[1] or "").strip()
    spec, gift = await _build_invoice_spec_for_gift(user_id, code)
    if not spec or not gift:
        await send_message_safe(bot, chat_id, (await tr(user_id, "payments.error", "Payment error, please try again.")), parse_mode="HTML")
        return

    if not await _mark_pending(user_id, spec.payload):
        await show_pending_invoice_stub(chat_id, user_id)
        return

    await _send_invoice_and_stub(chat_id, user_id, spec)


@dp.pre_checkout_query()
async def on_pre_checkout(pre: PreCheckoutQuery) -> None:
    logger.info("Pre-checkout query received: %s", pre.invoice_payload)

    parsed = _parse_payload(pre.invoice_payload or "")
    ok = False
    try:
        if parsed and parsed.kind == "buy":
            ok = isinstance(parsed.req, int) and parsed.req in purchase_tiers()
        elif parsed and parsed.kind == "gift":
            ok = bool(find_gift(str(parsed.gift_code or "")))
    except Exception:
        ok = False

    if not ok:
        user_id = pre.from_user.id
        chat_id = pre.from_user.id
        had_pending = False
        with suppress(Exception):
            had_pending = bool(await redis_client.exists(RedisKeys.pending(user_id)))

        await clear_payment_ui(user_id, chat_id)
        await clear_payment_runtime_keys(user_id)

        notice_key = "payments.pending_expired" if had_pending else "payments.error"
        notice_text = await tr(
            user_id,
            notice_key,
            "⏳ Invoice expired. Please try again." if had_pending else "Payment error, please try again.",
        )
        await send_transient_notice(chat_id, notice_text, parse_mode="HTML")

    error_msg = (await tr(pre.from_user.id, "payments.error", "Payment error, please try again."))
    try:
        await bot.answer_pre_checkout_query(pre.id, ok=ok, error_message=None if ok else error_msg)
    except Exception:
        logger.exception("on_pre_checkout: error sending answer_pre_checkout")


@dp.message(F.content_type == ContentType.SUCCESSFUL_PAYMENT, F.chat.type == ChatType.PRIVATE)
async def on_payment_success(message: Message) -> None:
    payload = message.successful_payment.invoice_payload

    try:
        sp = message.successful_payment
        charge_id = getattr(sp, "telegram_payment_charge_id", None)
        provider_charge_id = getattr(sp, "provider_payment_charge_id", None)

        logger.info("Successful payment: payload=%s, charge_id=%s", payload, charge_id)
        if not charge_id:
            raise ValueError("Missing telegram_payment_charge_id (cannot dedupe safely)")

        parsed = _parse_payload(payload)
        if not parsed:
            raise ValueError("Invalid payload format")

        kind: PayloadKind = parsed.kind
        gift: Optional[Dict[str, Any]] = None

        if kind == "buy":
            req_amt = int(parsed.req or 0)
            tiers_map = purchase_tiers()
            if req_amt not in tiers_map:
                raise ValueError("Unknown purchase tier")
            stars_amt = int(tiers_map.get(req_amt, 0) or 0)
            if stars_amt <= 0:
                raise ValueError("Invalid purchase tier price")
        else:
            gift = find_gift(str(parsed.gift_code or ""))
            if not gift:
                raise ValueError("Unknown gift tier")
            req_amt = int(gift.get("requests") or 0)
            stars_amt = int(gift.get("stars") or 0)
            if req_amt <= 0 or stars_amt <= 0:
                raise ValueError("Invalid gift tier params")

        if (getattr(sp, "currency", None) or "") != settings.PAYMENT_CURRENCY:
            raise ValueError("Currency mismatch")
        if int(getattr(sp, "total_amount", 0) or 0) != int(stars_amt):
            raise ValueError("Amount mismatch")

        outbox_status = "pending"
        for attempt in range(3):
            try:
                async with session_scope(stmt_timeout_ms=5000) as db:
                    user = await get_or_create_user(db, message.from_user)

                    stmt = (
                        pg_insert(PaymentOutbox)
                        .values(
                            user_id=user.id,
                            kind=kind,
                            status="pending",
                            requests_amount=req_amt,
                            stars_amount=stars_amt,
                            invoice_payload=payload,
                            telegram_payment_charge_id=str(charge_id),
                            provider_payment_charge_id=str(provider_charge_id) if provider_charge_id else None,
                            gift_code=gift.get("code") if gift else None,
                            gift_title=gift.get("title") if gift else None,
                            gift_emoji=gift.get("emoji") if gift else None,
                        )
                        .on_conflict_do_nothing(index_elements=["telegram_payment_charge_id"])
                        .returning(PaymentOutbox.status)
                    )
                    row = (await db.execute(stmt)).scalar_one_or_none()
                    if row is None:
                        status_row = await db.execute(
                            select(PaymentOutbox.status)
                            .where(PaymentOutbox.telegram_payment_charge_id == str(charge_id))
                        )
                        outbox_status = status_row.scalar_one_or_none() or "pending"
                    else:
                        outbox_status = row
                break
            except (OperationalError, DBAPIError):
                if attempt == 2:
                    raise
                await asyncio.sleep(0.05 * (2 ** attempt))

        await clear_payment_ui(message.from_user.id, message.chat.id)

        if outbox_status == "applied":
            dup_txt = await tr(message.from_user.id, "payments.success_duplicate_short", "✅ Payment already processed.")
            await send_transient_notice(message.chat.id, dup_txt, parse_mode="HTML", delay=SUCCESS_NOTICE_TTL_BUY)
            return

        try:
            celery.send_task("payments.process_outbox", args=[str(charge_id)])
            processing_txt = await tr(
                message.from_user.id,
                "payments.processing",
                "✅ Payment received. We are processing it now.",
            )
            await send_transient_notice(message.chat.id, processing_txt, parse_mode="HTML", delay=SUCCESS_NOTICE_TTL_BUY)
        except Exception as e:
            logger.exception("on_payment_success: failed to enqueue outbox processing for charge_id=%s", charge_id)
            enqueue_error = str(e).strip() or e.__class__.__name__
            safe_error = enqueue_error[:ENQUEUE_ERROR_MAX_LEN]

            async with session_scope(stmt_timeout_ms=5000) as db:
                row = (
                    await db.execute(
                        select(PaymentOutbox)
                        .where(PaymentOutbox.telegram_payment_charge_id == str(charge_id))
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if row:
                    row.status = "failed"
                    row.last_error = safe_error

            error_txt = await tr(
                message.from_user.id,
                "payments.error",
                "Payment error, please try again.",
            )
            await send_transient_notice(message.chat.id, error_txt, parse_mode="HTML", delay=SUCCESS_NOTICE_TTL_BUY)
        return

    except ValueError as ve:
        logger.warning("on_payment_success: invalid payload '%s': %s", payload, ve)
        with suppress(Exception):
            await clear_payment_ui(message.from_user.id, message.chat.id)
        txt = (await tr(message.from_user.id, "payments.invalid_details", "❌ Invalid payment details, please contact support."))
        await send_message_safe(bot, message.chat.id, txt, parse_mode="HTML")
    except Exception:
        logger.exception("on_payment_success: error finalizing payment")
        with suppress(Exception):
            await clear_payment_ui(message.from_user.id, message.chat.id)
        await send_message_safe(
            bot,
            message.chat.id,
            (await tr(message.from_user.id, "payments.error", "Payment error, please try again.")),
            parse_mode="HTML",
        )
    finally:
        with suppress(Exception):
            await clear_payment_runtime_keys(message.from_user.id)
