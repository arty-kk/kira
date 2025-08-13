cat >app/bot/handlers/payments.py<< 'EOF'
# app/bot/handlers/payments.py
import logging

from typing import Optional

from aiogram import F
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    PreCheckoutQuery,
    LabeledPrice,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from app.bot.i18n import t
from app.clients.telegram_client import get_bot
from app.bot.components.constants import redis_client
from app.bot.components.dispatcher import dp
from app.config import settings
from app.core.db import AsyncSessionLocal
from app.services.user.user_service import (
    get_or_create_user, 
    add_paid_requests,
    compute_remaining,
)

logger = logging.getLogger(__name__)

bot = get_bot()

@dp.message(Command("buy"), F.chat.type == ChatType.PRIVATE)
async def cmd_buy(message: Message) -> None:

    try:
        async with AsyncSessionLocal() as db:
            user = await get_or_create_user(db, message.from_user)
            remaining = compute_remaining(user)
    except Exception:
        logger.exception("cmd_buy: DB error")
        await message.reply("❌ There was an error generating the invoice, please try again later.")
        return

    tiers = list(settings.PURCHASE_TIERS.items())
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(tiers), 2):
        batch = tiers[i : i + 2]
        rows.append([
            InlineKeyboardButton(
                text=await t(message.from_user.id, "payments.buy_button", req=req, stars=stars),
                callback_data=f"buy_tier:{req}",
            )
            for req, stars in batch
        ])
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
    text = await t(message.from_user.id, "payments.you_have", remaining=remaining)
    await message.reply(text, reply_markup=keyboard, parse_mode="HTML")


@dp.callback_query(F.data.startswith("buy_tier:"))
async def on_buy_tier(cb: CallbackQuery) -> None:

    await cb.answer()
    req = int(cb.data.split(":", 1)[1])
    stars = settings.PURCHASE_TIERS.get(req, 0)
    pending_key = f"pending_invoice:{cb.from_user.id}"
    if not await redis_client.set(pending_key, 1, ex=300, nx=True):
        await cb.message.reply(
            "⚠️ You already have an outstanding invoice. Please complete the current payment before creating a new one.",
            quote=True
        )
        return

    if req not in settings.PURCHASE_TIERS:
        logger.warning("on_buy_tier: invalid tier %r", req)
        await cb.message.reply("❌ Invalid tariff, try again later.", quote=True)
        return

    buy_label = await t(cb.from_user.id, "payments.buy_button", req=req, stars=stars)
    prices = [LabeledPrice(label=buy_label, amount=stars)]
    if stars <= 0:
        logger.warning("on_buy_tier: tier %r has zero price", req)
        await cb.message.reply("❌ Tariff error, try again later.", quote=True)
        return

    title = await t(cb.from_user.id, "payments.invoice_title", req=req)
    desc  = await t(cb.from_user.id, "payments.invoice_desc", req=req, stars=stars)
    try:
        await bot.send_invoice(
            chat_id=cb.from_user.id,
            provider_token=settings.PAYMENT_PROVIDER_TOKEN,
            title=title,
            description=desc,
            payload=f"buy_{req}",
            currency=settings.PAYMENT_CURRENCY,
            prices=prices,
        )
    except Exception:
        logger.exception("Failed to send invoice for tier %s", req)
        await cb.message.reply(
            "❌ There was an error generating the invoice, please try again later.",
            quote=True
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



@dp.message(F.content_type == "successful_payment", F.chat.type == ChatType.PRIVATE)
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

        text = await t(message.from_user.id, "payments.success", req=req, remaining=remaining)
        await message.reply(text, parse_mode="HTML")

    except ValueError as ve:
        logger.warning("on_payment_success: invalid payload '%s': %s", payload, ve)
        await message.reply("❌ Invalid payment details, please contact support.", parse_mode="HTML")
    except Exception:
        logger.exception("on_payment_success: error finalizing payment")
        await message.reply(
            "❌ Your payment could not be completed, please try again later.",
            parse_mode="HTML"
        )
    finally:
        await redis_client.delete(f"pending_invoice:{message.from_user.id}")
EOF