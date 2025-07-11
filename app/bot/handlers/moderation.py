# app/bot/handlers/moderation.py

import logging
from typing import List

from aiogram import types

from app.clients.telegram_client import get_bot
from app.bot.components.dispatcher import dp
from app.config import settings
from app.services.addons.passive_moderation import check_light, check_deep

logger = logging.getLogger(__name__)

bot = get_bot()

async def handle_passive_moderation(
    chat_id: int,
    message: types.Message,
    text: str,
    entities: List[dict] | None = None,
) -> None:

    try:
        light_status = await check_light(chat_id, message.from_user.id, text, entities)
        if light_status != "clean":
            reason_map = {
                "flood": "Frequent messages (flood/spam)",
                "spam_links": "Too many links in one message",
                "promo": "Link missing required keyword(s)",
                "toxic": "Toxic or abusive content",
            }
            reason_text = reason_map.get(light_status, "Unknown reason")
            snippet = text[:200] + ("…" if len(text) > 200 else "")

            alert_text = (
                f"🚨 <b>Passive Moderation Alert (chat ID: <code>{chat_id}</code>)</b>\n"
                f"User: <a href=\"tg://user?id={message.from_user.id}\">{message.from_user.full_name}</a> "
                f"(<code>{message.from_user.id}</code>)\n"
                f"Message ID: <code>{message.message_id}</code>\n"
                f"Text: {snippet}\n\n"
                f"Reason: <b>{reason_text}</b>."
            )
            if str(chat_id).startswith("-100"):
                public_chat_id = str(chat_id)[4:]
                alert_text += (
                    f"\n<a href=\"https://t.me/c/{public_chat_id}/{message.message_id}\">Link to message</a>"
                )

            targets = (
                [settings.MODERATOR_NOTIFICATION_CHAT_ID]
                if settings.MODERATOR_NOTIFICATION_CHAT_ID
                else settings.MODERATOR_IDS
            )
            for mid in targets:
                try:
                    await bot.send_message(
                        chat_id=mid,
                        text=alert_text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    logger.exception("Failed to send light moderation alert to %s", mid)
            return

        blocked = await check_deep(chat_id, message.from_user.id, text, source="user")
        if blocked:
            snippet = text[:200] + ("…" if len(text) > 200 else "")
            alert_text = (
                f"🚨 <b>Deep Moderation Alert (chat ID: <code>{chat_id}</code>)</b>\n"
                f"User: <a href=\"tg://user?id={message.from_user.id}\">{message.from_user.full_name}</a> "
                f"(<code>{message.from_user.id}</code>)\n"
                f"Message ID: <code>{message.message_id}</code>\n"
                f"Text: {snippet}\n\n"
                f"Reason: <b>Contextual violation</b>."
            )
            if str(chat_id).startswith("-100"):
                public_chat_id = str(chat_id)[4:]
                alert_text += (
                    f"\n<a href=\"https://t.me/c/{public_chat_id}/{message.message_id}\">Link to message</a>"
                )

            targets = (
                [settings.MODERATOR_NOTIFICATION_CHAT_ID]
                if settings.MODERATOR_NOTIFICATION_CHAT_ID
                else settings.MODERATOR_IDS
            )
            for mid in targets:
                try:
                    await bot.send_message(
                        chat_id=mid,
                        text=alert_text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    logger.exception("Failed to send deep moderation alert to %s", mid)

    except Exception:
        logger.exception(
            "Error in passive moderation for chat %s, message %s",
            chat_id,
            getattr(message, "message_id", "<unknown>"),
        )
