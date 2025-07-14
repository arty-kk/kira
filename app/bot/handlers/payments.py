cat >app/bot/handlers/payments.py<< EOF
# app/bot/handlers/payments.py
import logging

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

from app.clients.telegram_client import get_bot
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

    tiers = list(settings.PURCHASE_TIERS.items())
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(tiers), 2):
        batch = tiers[i : i + 2]
        rows.append([
            InlineKeyboardButton(
                text=f"{req} Requests = {stars} ⭐",
                callback_data=f"buy_tier:{req}",
            )
            for req, stars in batch
        ])
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
    await message.reply("Choose a package:", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("buy_tier:"))
async def on_buy_tier(cb: CallbackQuery) -> None:

    await cb.answer()
    req = int(cb.data.split(":", 1)[1])
    stars = settings.PURCHASE_TIERS.get(req, 0)
    prices = [LabeledPrice(label=f"{req} requests", amount=stars)]
    await bot.send_invoice(
        chat_id=cb.from_user.id,
        provider_token=settings.PAYMENT_PROVIDER_TOKEN,
        title=f"Buy {req} requests",
        description=f"Get {req} requests for {stars} ⭐",
        payload=f"buy_{req}",
        currency=settings.PAYMENT_CURRENCY,
        prices=prices,
    )


@dp.pre_checkout_query()
async def on_pre_checkout(pre: PreCheckoutQuery) -> None:

    logger.info("Pre-checkout query received: %s", pre.invoice_payload)
    ok = pre.invoice_payload.startswith("buy_")
    await bot.answer_pre_checkout_query(
        pre.id, ok=ok, error_message="Payment error, please try again."
    )


@dp.message(F.content_type == "successful_payment", F.chat.type == ChatType.PRIVATE)
async def on_payment_success(message: Message) -> None:

    payload = message.successful_payment.invoice_payload
    logger.info("Successful payment: %s", payload)
    req = int(payload.split("_", 1)[1])
    async with AsyncSessionLocal() as db:
        user = await get_or_create_user(db, message.from_user)
        await add_paid_requests(db, user.id, req)
        await db.refresh(user)
        remaining = compute_remaining(user)
    await message.reply(
        f"✅ You purchased {req} requests.\n"
        f"📊 You have <b>{remaining}</b> requests left."
    )
EOF