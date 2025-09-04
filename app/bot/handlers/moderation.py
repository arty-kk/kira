# app/bot/handlers/moderation.py
import logging
import asyncio

from typing import List
from aiogram import types

from app.clients.telegram_client import get_bot
from app.bot.components.dispatcher import dp
from app.bot.components.constants import redis_client
from app.config import settings
from app.services.addons.passive_moderation import check_light, check_deep

logger = logging.getLogger(__name__)

bot = get_bot()

get_targets = lambda: ([settings.MODERATOR_NOTIFICATION_CHAT_ID]
                        if settings.MODERATOR_NOTIFICATION_CHAT_ID
                        else settings.MODERATOR_IDS) or []


async def _broadcast_alert(targets: list[int], text: str) -> None:

    if not targets:
        logger.warning("No moderator targets configured; skipping alert")
        return

    tasks = [
        bot.send_message(
            chat_id=int(mid),
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        for mid in targets
    ]
    for fut in asyncio.as_completed(tasks):
        try:
            await fut
        except Exception:
            logger.exception("Failed to send moderation alert")

def _serialize_entities(ents: List[types.MessageEntity]) -> List[dict]:
    out = []
    for e in ents or []:
        out.append({"offset": e.offset, "length": e.length, "type": e.type.value})
    return out

async def handle_passive_moderation(
    chat_id: int,
    message: types.Message,
    text: str,
    entities: List[dict] | None = None,
) -> None:

    light_throttle = f"mod_alert:light:{chat_id}:{message.from_user.id}"
    try:
        async with redis_client.pipeline(transaction=True) as pipe:
            throttle_sec = getattr(settings, "MOD_ALERT_THROTTLE_SECONDS", 60)
            pipe.set(light_throttle, 1, ex=throttle_sec, nx=True)
            allowed, = await pipe.execute()
    except Exception:
        logger.exception("Failed to set light-throttle key; allowing alert by default")
        allowed = True
    if not allowed:
        return

    if not text:
        return

    try:
        targets = get_targets()

        all_entities = entities if entities is not None else (
            _serialize_entities(message.entities) +
            _serialize_entities(message.caption_entities)
        )
        try:
            light_status = await asyncio.wait_for(
                check_light(chat_id, message.from_user.id, text, all_entities),
                timeout=getattr(settings, "MOD_LIGHT_TIMEOUT", 2.0),
            )
        except asyncio.TimeoutError:
            logger.warning("check_light timed out for chat=%s user=%s", chat_id, message.from_user.id)
            light_status = "clean"
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

            await _broadcast_alert(targets, alert_text)
            return

        try:
            blocked = await asyncio.wait_for(
                check_deep(chat_id, message.from_user.id, text, source="user"),
                timeout=getattr(settings, "MOD_DEEP_TIMEOUT", 5.0),
            )
        except asyncio.TimeoutError:
            logger.warning("check_deep timed out for chat=%s user=%s", chat_id, message.from_user.id)
            blocked = False
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

            await _broadcast_alert(targets, alert_text)
            return

        if not targets:
            logger.warning(
                "Passive moderation triggered (deep) for chat=%s user=%s but no moderator targets configured",
                chat_id, message.from_user.id
            )

    except Exception:
        logger.exception(
            "Error in passive moderation for chat %s, message %s",
            chat_id,
            getattr(message, "message_id", "<unknown>"),
        )
