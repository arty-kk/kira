# app/bot/handlers/payments.py
import logging
import asyncio

from typing import Optional

from aiogram import F
from aiogram.enums import ChatType, ContentType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, PreCheckoutQuery,
    LabeledPrice, InlineKeyboardButton, InlineKeyboardMarkup,
)

from app.bot.i18n import t
from app.clients.telegram_client import get_bot
from app.bot.components.constants import redis_client
from app.bot.components.dispatcher import dp
from app.config import settings
from app.core.db import AsyncSessionLocal
from app.services.user.user_service import (
    get_or_create_user, add_paid_requests,
    compute_remaining,
)

logger = logging.getLogger(__name__)
bot = get_bot()

def _k_pending(user_id: int) -> str: return f"pending_invoice:{user_id}"
def _k_pending_tier(user_id: int) -> str: return f"pending_invoice_tier:{user_id}"
def _k_pending_msg(user_id: int) -> str: return f"pending_invoice_msg:{user_id}"
def _k_buy_menu_msg(user_id: int) -> str: return f"buy_menu_msg:{user_id}"
def _k_buy_info_msg(user_id: int) -> str: return f"buy_info_msg:{user_id}"
def _k_cb_rate(user_id: int) -> str: return f"cb_rate:{user_id}"

PENDING_TTL = int(getattr(settings, "PENDING_INVOICE_TTL", 1800))
CB_RATE_TTL = 1
TRANSIENT_NOTICE_TTL = int(getattr(settings, "PAYMENTS_TRANSIENT_NOTICE_TTL", 6))

async def _cooldown(cb: CallbackQuery) -> bool:

    try:
        ok = await redis_client.set(_k_cb_rate(cb.from_user.id), 1, ex=CB_RATE_TTL, nx=True)
        if not ok:
            try:
                await cb.answer(await t(cb.from_user.id, "payments.too_frequent"), show_alert=False)
            except Exception:
                pass
            return True
        return False
    except Exception:
        return False

async def _delete_message_safe(chat_id: int, message_id: Optional[int]) -> None:
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramBadRequest:
        pass
    except Exception:
        logger.exception("Failed to delete message chat=%s msg_id=%s", chat_id, message_id)

async def _clear_payment_ui(user_id: int, chat_id: int) -> None:

    try:
        inv_msg_id = await redis_client.get(_k_pending_msg(user_id))
        info_msg_id = await redis_client.get(_k_buy_info_msg(user_id))
        menu_msg_id = await redis_client.get(_k_buy_menu_msg(user_id))

        await _delete_message_safe(chat_id, int(inv_msg_id) if inv_msg_id else None)
        await _delete_message_safe(chat_id, int(info_msg_id) if info_msg_id else None)
        await _delete_message_safe(chat_id, int(menu_msg_id) if menu_msg_id else None)

        await redis_client.delete(
            _k_pending(user_id),
            _k_pending_tier(user_id),
            _k_pending_msg(user_id),
            _k_buy_info_msg(user_id),
            _k_buy_menu_msg(user_id),
        )
    except Exception:
        logger.exception("Failed to clear payment UI for user=%s", user_id)

async def _delete_later(chat_id: int, message_id: Optional[int], delay: int) -> None:
    if not message_id:
        return
    try:
        await asyncio.sleep(max(1, int(delay)))
        await _delete_message_safe(chat_id, message_id)
    except Exception:
        pass

async def _send_transient_notice(chat_id: int, text: str, *, parse_mode: Optional[str] = None, delay: Optional[int] = None) -> None:
    try:
        msg = await bot.send_message(chat_id, text, parse_mode=parse_mode)
    except TelegramBadRequest:
        msg = await bot.send_message(chat_id, text)
    except Exception:
        logger.exception("Failed to send transient notice")
        return
    asyncio.create_task(_delete_later(chat_id, msg.message_id, delay or TRANSIENT_NOTICE_TTL))

async def _show_pending_invoice_stub(chat_id: int, user_id: int) -> None:

    prev_info_id = await redis_client.get(_k_buy_info_msg(user_id))
    if prev_info_id:
        try:
            await _delete_message_safe(chat_id, int(prev_info_id))
        except Exception:
            pass

    inv_msg_raw = await redis_client.get(_k_pending_msg(user_id))
    tier_raw = await redis_client.get(_k_pending_tier(user_id))
    inv_msg_id = int(inv_msg_raw) if inv_msg_raw else None
    req = int(tier_raw) if tier_raw else None

    cancel_label = await t(user_id, "payments.cancel_button")
    btns = [[InlineKeyboardButton(text=cancel_label, callback_data="buy_cancel")]]
    kb = InlineKeyboardMarkup(inline_keyboard=btns)

    text = (
        await t(user_id, "payments.pending_exists_tier", req=req)
        if req
        else await t(user_id, "payments.pending_exists")
    )
    try:
        msg = await bot.send_message(
            chat_id,
            text,
            reply_markup=kb,
            reply_to_message_id=inv_msg_id if inv_msg_id else None,
        )
        await redis_client.set(_k_buy_info_msg(user_id), msg.message_id, ex=PENDING_TTL)
    except TelegramBadRequest:
        msg = await bot.send_message(chat_id, text, reply_markup=kb)
        await redis_client.set(_k_buy_info_msg(user_id), msg.message_id, ex=PENDING_TTL)


@dp.message(Command("buy"), F.chat.type == ChatType.PRIVATE)
async def cmd_buy(message: Message) -> None:

    user_id = message.from_user.id
    chat_id = message.chat.id

    if await redis_client.exists(_k_pending(user_id)):
        try:
            menu_id_raw = await redis_client.get(_k_buy_menu_msg(user_id))
            if menu_id_raw:
                await _delete_message_safe(chat_id, int(menu_id_raw))
                await redis_client.delete(_k_buy_menu_msg(user_id))
        except Exception:
            logger.debug("cleanup buy menu on pending failed", exc_info=True)
        await _show_pending_invoice_stub(chat_id, user_id)
        return

    try:
        async with AsyncSessionLocal() as db:
            user = await get_or_create_user(db, message.from_user)
            remaining = compute_remaining(user)
    except Exception:
        logger.exception("cmd_buy: DB error")
        await bot.send_message(chat_id, await t(user_id, "payments.gen_error"))
        return

    tiers = list(settings.PURCHASE_TIERS.items())
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(tiers), 2):
        batch = tiers[i : i + 2]
        row: list[InlineKeyboardButton] = []
        for req, stars in batch:
            label = await t(user_id, "payments.buy_button", req=req, stars=stars)
            row.append(InlineKeyboardButton(text=label, callback_data=f"buy_tier:{req}"))
        rows.append(row)

    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
    text = await t(user_id, "payments.you_have", remaining=remaining)
    sent = await bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode="HTML")
    try:
        await redis_client.set(_k_buy_menu_msg(user_id), sent.message_id, ex=PENDING_TTL)
    except Exception:
        logger.exception("Failed to store buy menu msg id")

@dp.callback_query(F.data.startswith("buy_tier:"))
async def on_buy_tier(cb: CallbackQuery) -> None:
    if await _cooldown(cb):
        return

    await cb.answer()
    req = int(cb.data.split(":", 1)[1])
    stars = settings.PURCHASE_TIERS.get(req, 0)
    user_id = cb.from_user.id
    chat_id = cb.message.chat.id if cb.message else user_id

    if req not in settings.PURCHASE_TIERS or stars <= 0:
        logger.warning("on_buy_tier: invalid or zero tier %r", req)
        await bot.send_message(chat_id, await t(user_id, "payments.error"))
        return

    if not await redis_client.set(_k_pending(user_id), 1, ex=PENDING_TTL, nx=True):
        await _show_pending_invoice_stub(chat_id, user_id)
        return

    buy_label = await t(user_id, "payments.buy_button", req=req, stars=stars)
    prices = [LabeledPrice(label=buy_label, amount=stars)]
    title = await t(user_id, "payments.invoice_title", req=req)
    desc  = await t(user_id, "payments.invoice_desc", req=req, stars=stars)

    try:
        inv_msg: Message = await bot.send_invoice(
            chat_id=user_id,
            provider_token=settings.PAYMENT_PROVIDER_TOKEN,
            title=title,
            description=desc,
            payload=f"buy_{req}",
            currency=settings.PAYMENT_CURRENCY,
            prices=prices,
        )
        await redis_client.set(_k_pending_tier(user_id), req, ex=PENDING_TTL)
        await redis_client.set(_k_pending_msg(user_id), inv_msg.message_id, ex=PENDING_TTL)

        await _show_pending_invoice_stub(chat_id, user_id)

        try:
            menu_id_raw = await redis_client.get(_k_buy_menu_msg(user_id))
            if menu_id_raw:
                await _delete_message_safe(chat_id, int(menu_id_raw))
                await redis_client.delete(_k_buy_menu_msg(user_id))
        except Exception:
            logger.debug("Failed to delete buy menu after tier selection", exc_info=True)

    except Exception:
        logger.exception("Failed to send invoice for tier %s", req)
        await redis_client.delete(_k_pending(user_id), _k_pending_tier(user_id), _k_pending_msg(user_id))
        await bot.send_message(chat_id, await t(user_id, "payments.gen_error"))

@dp.callback_query(F.data == "buy_cancel")
async def on_buy_cancel(cb: CallbackQuery) -> None:
    if await _cooldown(cb):
        return

    await cb.answer()
    user_id = cb.from_user.id
    chat_id = cb.message.chat.id if cb.message else user_id

    await _clear_payment_ui(user_id, chat_id)

    await _send_transient_notice(
        chat_id,
        await t(user_id, "payments.cancelled"),
        parse_mode="HTML",
    )

@dp.pre_checkout_query()
async def on_pre_checkout(pre: PreCheckoutQuery) -> None:
    logger.info("Pre-checkout query received: %s", pre.invoice_payload)
    payload = pre.invoice_payload or ""

    try:
        if not payload.startswith("buy_"):
            raise ValueError
        req = int(payload.split("_", 1)[1])
        ok = req in settings.PURCHASE_TIERS
    except Exception:
        ok = False

    error_msg = await t(pre.from_user.id, "payments.error")
    try:
        await bot.answer_pre_checkout_query(pre.id, ok=ok, error_message=error_msg)
    except Exception:
        logger.exception("on_pre_checkout: error sending answer_pre_checkout")

@dp.message(F.content_type == ContentType.SUCCESSFUL_PAYMENT, F.chat.type == ChatType.PRIVATE)
async def on_payment_success(message: Message) -> None:
    payload = message.successful_payment.invoice_payload

    try:
        charge_id = getattr(message.successful_payment, "telegram_payment_charge_id", None)
        logger.info("Successful payment: payload=%s, charge_id=%s", payload, charge_id)

        parts = (payload or "").split("_", 1)
        if len(parts) != 2 or parts[0] != "buy":
            raise ValueError("Invalid payload format")
        req = int(parts[1])
        if req not in settings.PURCHASE_TIERS:
            raise ValueError("Unknown purchase tier")

        async with AsyncSessionLocal() as db:
            user = await get_or_create_user(db, message.from_user)
            await add_paid_requests(db, user.id, req)
            await db.commit()
            await db.refresh(user)
            remaining = compute_remaining(user)

        await _clear_payment_ui(message.from_user.id, message.chat.id)

        text = await t(message.from_user.id, "payments.success", req=req, remaining=remaining)
        await _send_transient_notice(message.chat.id, text, parse_mode="HTML")

    except ValueError as ve:
        logger.warning("on_payment_success: invalid payload '%s': %s", payload, ve)
        await bot.send_message(message.chat.id, "❌ Invalid payment details, please contact support.", parse_mode="HTML")
    except Exception:
        logger.exception("on_payment_success: error finalizing payment")
        await bot.send_message(message.chat.id, await t(message.from_user.id, "payments.error"), parse_mode="HTML")
    finally:
        await redis_client.delete(
            _k_pending(message.from_user.id),
            _k_pending_tier(message.from_user.id),
            _k_pending_msg(message.from_user.id),
            _k_buy_info_msg(message.from_user.id),
            _k_buy_menu_msg(message.from_user.id),
        )
