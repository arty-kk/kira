#app/bot/handlers/moderation.py
from __future__ import annotations

import logging
import asyncio
import time
import html
import secrets
from typing import Any, List

from aiogram import types, F
from aiogram.enums import ChatType
from aiogram.dispatcher.event.bases import SkipHandler

from app.clients.telegram_client import get_bot
from app.bot.components.dispatcher import dp
from app.bot.components.constants import redis_client
import app.bot.components.constants as consts
from app.config import settings
from app.services.addons.passive_moderation import (
    extract_urls,
    is_telegram_link,
    contains_telegram_obfuscated,
    contains_any_link_obfuscated,
    check_light,
    check_deep,
)
from app.services.addons.analytics import record_moderation as analytics_record_moderation
from app.bot.handlers.moderation_context import resolve_message_moderation_context

logger = logging.getLogger(__name__)

bot = get_bot()

def get_targets() -> list[int]:
    targets = {int(x) for x in (getattr(settings, "MODERATOR_IDS", []) or []) if str(x).strip()}
    notify_chat_id = int(getattr(settings, "MODERATOR_NOTIFICATION_CHAT_ID", 0) or 0)
    if notify_chat_id:
        targets.add(notify_chat_id)
    return sorted(targets)

def _linked_channel_cache_key(chat_id: int) -> str:
    return f"linked_channel_id:{chat_id}"

async def _get_linked_channel_id(chat_id: int) -> int | None:

    key = _linked_channel_cache_key(chat_id)
    try:
        cached = await redis_client.get(key)
        if cached:
            try:
                return int(cached)
            except Exception:
                pass
        try:
            ch = await bot.get_chat(chat_id)
            linked_id = getattr(ch, "linked_chat_id", None)
        except Exception:
            linked_id = None
        if linked_id:
            try:
                await redis_client.set(key, int(linked_id), ex=int(getattr(settings, "MODERATOR_ADMIN_CACHE_TTL_SECONDS", 86400)))
            except Exception:
                pass
            return int(linked_id)
    except Exception:
        logger.debug("linked channel lookup failed", exc_info=True)
    return None

async def is_from_linked_channel(message: types.Message) -> bool:

    try:
        chat_id = int(message.chat.id)
        linked_id = getattr(message.chat, "linked_chat_id", None)
        if not linked_id:
            linked_id = await _get_linked_channel_id(chat_id)
        if not linked_id:
            return False
        sc = getattr(message, "sender_chat", None)
        if sc and getattr(sc, "type", None) == ChatType.CHANNEL and int(sc.id) == int(linked_id):
            return True
        if getattr(message, "is_automatic_forward", False):
            fc = getattr(message, "forward_from_chat", None)
            if fc and getattr(fc, "type", None) == ChatType.CHANNEL and int(fc.id) == int(linked_id):
                return True
        fc = getattr(message, "forward_from_chat", None)
        if fc and getattr(fc, "type", None) == ChatType.CHANNEL and int(fc.id) == int(linked_id):
            return True
    except Exception:
        logger.debug("is_from_linked_channel check failed", exc_info=True)
    return False

def _admins_key(chat_id: int) -> str:
    return f"chat_admins:{chat_id}"

async def _refresh_admin_cache(chat_id: int) -> list[int]:
    admins = await bot.get_chat_administrators(chat_id)
    ids = []
    for m in admins:
        try:
            if getattr(m, "status", "") in ("creator", "administrator") and getattr(m, "user", None):
                ids.append(int(m.user.id))
        except Exception:
            continue

    key = _admins_key(chat_id)
    try:
        async with redis_client.pipeline(transaction=True) as pipe:
            pipe.delete(key)
            if ids:
                pipe.sadd(key, *ids)
            pipe.expire(key, int(getattr(settings, "MODERATOR_ADMIN_CACHE_TTL_SECONDS", 86400)))
            await pipe.execute()
    except Exception:
        logger.debug("admin cache refresh failed", exc_info=True)

    return ids

async def _is_admin(chat_id: int, user_id: int) -> bool:
    key = _admins_key(chat_id)
    try:
        if await redis_client.exists(key):
            return bool(await redis_client.sismember(key, int(user_id)))

        await _refresh_admin_cache(chat_id)
        return bool(await redis_client.sismember(key, int(user_id)))
    except Exception:
        try:
            m = await bot.get_chat_member(chat_id, user_id)
            return m.status in ("administrator", "creator")
        except Exception:
            return False

def _ban_markup(target_chat_id: int, offender_id: int, msg_id: int | None = None) -> types.InlineKeyboardMarkup:
    payload = f"mod:ban:{int(target_chat_id)}:{int(offender_id)}"
    if msg_id is not None:
        payload += f":{int(msg_id)}"
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text="🚫 Ban user", callback_data=payload)]
        ]
    )

async def _send_alert_with_actions(targets: list[int], *, text: str, chat_id: int, offender_id: int, msg_id: int | None) -> None:
    if not targets:
        logger.warning("No moderator targets configured; skipping alert with actions")
        return

    tasks = [
        bot.send_message(
            chat_id=int(mid),
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_ban_markup(chat_id, offender_id, msg_id),
        )
        for mid in targets
    ]
    for fut in asyncio.as_completed(tasks):
        try:
            await fut
        except Exception:
            logger.exception("Failed to send moderation alert with actions")

def _serialize_entities(ents: List[types.MessageEntity]) -> List[dict]:
    out = []
    for e in ents or []:
        etype = getattr(e, "type", None)
        if hasattr(etype, "value"):
            etype = etype.value
        item = {"offset": e.offset, "length": e.length, "type": etype}
        try:
            if str(etype).lower() == "text_link" and getattr(e, "url", None):
                item["url"] = e.url
        except Exception:
            pass
        try:
            if str(etype).lower() == "text_mention" and getattr(e, "user", None):
                user = e.user
                item["user"] = {
                    "id": getattr(user, "id", None),
                    "is_bot": getattr(user, "is_bot", None),
                }
        except Exception:
            pass
        out.append(item)
    return out

async def handle_passive_moderation(
    chat_id: int,
    message: types.Message | None,
    text: str,
    entities: List[dict] | None = None,
    *,
    image_b64: str | None = None,
    image_mime: str | None = None,
    source: str = "user",
    user_id: int | None = None,
    message_id: int | None = None,
    is_comment_context: bool | None = None,
) -> str:

    try:
        if message and await is_from_linked_channel(message):
            return "clean"

        if settings.MODERATION_ADMIN_EXEMPT:
            uid_for_admin = (int(getattr(getattr(message, "from_user", None), "id", 0)) if message else (int(user_id) if user_id else 0))
            if uid_for_admin and await _is_admin(chat_id, uid_for_admin):
                return "clean"
    except Exception:
        pass

    deep_text_threshold = int(getattr(settings, "MOD_DEEP_TEXT_THRESHOLD", 400))

    if not ( (message and getattr(message, "from_user", None)) or user_id ):
        return "clean"

    if message is not None and getattr(getattr(message, "chat", None), "type", None) == ChatType.PRIVATE:
        return "clean"
    if message is None and user_id is not None and int(chat_id) == int(user_id):
        return "clean"

    normalized_source = (source or "user").strip().lower()
    allowed_sources = {"user", "bot", "channel"}
    if normalized_source not in allowed_sources:
        logger.warning("Unknown passive moderation source=%r; fallback to 'user'", source)
        normalized_source = "user"

    _uid = int(getattr(getattr(message, "from_user", None), "id", user_id or 0))
    _mid = int(getattr(message, "message_id", message_id or 0))
    light_throttle = f"mod_alert:light:{chat_id}:{_uid}"
    deep_throttle  = f"mod_alert:deep:{chat_id}:{_uid}"

    if not (text and text.strip()) and not image_b64:
        return "clean"

    try:
        async with redis_client.pipeline(transaction=True) as pipe:
            throttle_sec = getattr(settings, "MOD_ALERT_THROTTLE_SECONDS", 60)
            pipe.set(light_throttle, 1, ex=throttle_sec, nx=True)
            allow_alert, = await pipe.execute()
    except Exception:
        logger.exception("Failed to set light-throttle key; allowing alert by default")
        allow_alert = True

    if is_comment_context is not None:
        moderation_context = "comment" if bool(is_comment_context) else "group"
    elif message is not None:
        moderation_context = resolve_message_moderation_context(message, from_linked=False)
    else:
        moderation_context = "group"
    light_policy = resolve_moderation_policy(moderation_context, settings)

    try:
        targets = get_targets()

        all_entities = entities if entities is not None else (
            _serialize_entities(getattr(message, "entities", [])) +
            _serialize_entities(getattr(message, "caption_entities", []))
        )
        try:
            light_status = await asyncio.wait_for(
                check_light(
                    chat_id,
                    _uid,
                    text,
                    all_entities,
                    source=normalized_source,
                    policy=light_policy,
                    image_b64=image_b64,
                    image_mime=image_mime,
                ),
                timeout=getattr(settings, "MOD_LIGHT_TIMEOUT", 2.0),
            )
        except asyncio.TimeoutError:
            logger.warning("check_light timed out for chat=%s user=%s", chat_id, _uid)
            light_status = "light_timeout_risk"
        status = "clean"
        reason_text = ""
        if light_status != "clean":
            reason_map = {
                "flood": "Frequent messages (flood/spam)",
                "spam_links": "Too many links in one message",
                "spam_mentions": "Too many mentions in one message",
                "promo": "Promotional content",
                "link_violation": "Disallowed link (policy)",
                "toxic": "Toxic or abusive content",
                "light_timeout_risk": "Light moderation timeout (risk fallback)",
            }
            reason_text = reason_map.get(light_status, "Unknown reason")
            status = "flagged"

            raw_snippet = (text or "")[:200]
            snippet = html.escape(raw_snippet) + ("…" if len(text) > 200 else "")
            body = f"Text: {snippet}" if snippet else "Text: (empty)"
            if image_b64:
                body += "\n📷 Image: attached"
            alert_text = (
                f"🚨 <b>Passive Moderation Alert (chat ID: <code>{chat_id}</code>)</b>\n"
                f"User: <a href=\"tg://user?id={_uid}\">{getattr(getattr(message,'from_user',None),'full_name',str(_uid))}</a> "
                f"(<code>{_uid}</code>)\n"
                f"Message ID: <code>{_mid}</code>\n"
                f"{body}\n\n"
                f"Reason: <b>{reason_text}</b>."
            )
            if str(chat_id).startswith("-100"):
                public_chat_id = str(chat_id)[4:]
                alert_text += (
                    f"\n<a href=\"https://t.me/c/{public_chat_id}/{_mid}\">Link to message</a>"
                )

            if allow_alert:
                asyncio.create_task(_send_alert_with_actions(
                    targets,
                    text=alert_text,
                    chat_id=chat_id,
                    offender_id=int(_uid),
                    msg_id=int(_mid),
                ))

        urls_for_risk = extract_urls(text or "", all_entities)
        try:
            new_user = await _is_new_user(chat_id, _uid)
        except Exception:
            new_user = False

        base_risk = (
            (image_b64 is not None) or
            bool(urls_for_risk) or
            contains_telegram_obfuscated(text or "") or
            contains_any_link_obfuscated(text or "") or
            len(text or "") > deep_text_threshold or
            new_user or
            (light_status == "toxic")
        )

        risk = base_risk
        if moderation_context == "comment":
            risk = bool(base_risk and light_status != "clean")

        blocked = False
        if risk:
            try:
                blocked = await asyncio.wait_for(
                    check_deep(
                        chat_id,
                        _uid,
                        text,
                        source=normalized_source,
                        image_b64=image_b64,
                        image_mime=image_mime,
                    ),
                    timeout=getattr(settings, "MOD_DEEP_TIMEOUT", 5.0),
                )
            except asyncio.TimeoutError:
                logger.warning("check_deep timed out for chat=%s user=%s", chat_id, _uid)
                blocked = False

        if blocked:
            status = "blocked"
            raw_snippet = (text or "")[:200]
            snippet = html.escape(raw_snippet) + ("…" if len(text) > 200 else "")
            alert_text = (
                f"🚨 <b>Deep Moderation Alert (chat ID: <code>{chat_id}</code>)</b>\n"
                f"User: <a href=\"tg://user?id={_uid}\">{getattr(getattr(message,'from_user',None),'full_name',str(_uid))}</a> "
                f"(<code>{_uid}</code>)\n"
                f"Message ID: <code>{_mid}</code>\n"
                f"Text: {snippet}\n\n"
                f"Reason: <b>Contextual violation</b>."
            )
            if str(chat_id).startswith("-100"):
                public_chat_id = str(chat_id)[4:]
                alert_text += (
                    f"\n<a href=\"https://t.me/c/{public_chat_id}/{_mid}\">Link to message</a>"
                )

            allow_deep_alert = allow_alert
            try:
                async with redis_client.pipeline(transaction=True) as pipe:
                    throttle_sec = max(5, int(getattr(settings, "MOD_ALERT_THROTTLE_SECONDS", 60)) // 2)
                    pipe.set(deep_throttle, 1, ex=throttle_sec, nx=True)
                    allow_deep_alert, = await pipe.execute()
            except Exception:
                logger.exception("Failed to set deep-throttle key; allowing deep alert by default")
                allow_deep_alert = True

            if allow_deep_alert:
                asyncio.create_task(_send_alert_with_actions(
                    targets,
                    text=alert_text,
                    chat_id=chat_id,
                    offender_id=int(_uid),
                    msg_id=int(_mid),
                ))

            try:
                if getattr(settings, "MODERATION_DELETE_BLOCKED", True):
                    if _mid:
                        await _delete_message_safe(chat_id, _mid)
            except Exception:
                logger.debug("blocked: delete failed", exc_info=True)

        try:
            ttl = int(getattr(settings, "MOD_FLAG_TTL_SECONDS", 86_400))
            await redis_client.hset(
                f"mod:msg:{chat_id}:{_mid}",
                mapping={
                    "status": status,
                    "reason": reason_text,
                    "ts": int(time.time()),
                    "user_id": int(_uid),
                },
            )
            if status in ("flagged", "blocked"):
                uid = int(_uid)
                await redis_client.sadd(f"mod_flagged_users:{chat_id}", uid)
                await redis_client.set(f"mod_flagged_ttl:{chat_id}:{uid}", 1, ex=ttl)
                try:
                    await redis_client.zrem(f"last_ping_zset:{chat_id}", str(uid))
                except Exception:
                    pass
        except Exception:
            logger.exception("Failed to persist moderation status to Redis")

        if status == "clean" and not targets:
            uid_log = getattr(getattr(message, "from_user", None), "id", user_id)
            logger.warning(
                "Passive moderation triggered (deep check done) for chat=%s user=%s but no moderator targets configured",
                chat_id, uid_log
            )

        try:
            await analytics_record_moderation(chat_id, status, reason_text or "")
        except Exception:
            logger.debug("analytics(record_moderation) failed", exc_info=True)

    except Exception:
        error_reason = "internal_error"
        fallback_mid = int(getattr(message, "message_id", message_id or 0))
        fallback_uid = int(getattr(getattr(message, "from_user", None), "id", user_id or 0))

        try:
            await redis_client.hset(
                f"mod:msg:{chat_id}:{fallback_mid}",
                mapping={
                    "status": "error",
                    "reason": error_reason,
                    "ts": int(time.time()),
                    "user_id": fallback_uid,
                },
            )
        except Exception:
            logger.exception("Failed to persist error moderation status to Redis")

        try:
            await analytics_record_moderation(chat_id, "error", error_reason)
        except Exception:
            logger.debug("analytics(record_moderation) failed for error status", exc_info=True)

        logger.exception(
            "Error in passive moderation for chat %s, message %s",
            chat_id,
            getattr(message, "message_id", "<unknown>"),
        )
        return "error"

    return status

async def _delete_message_safe(chat_id: int, message_id: int) -> bool:
    try:
        await bot.delete_message(chat_id, message_id)
        return True
    except Exception:
        logger.debug("delete_message failed", exc_info=True)
        return False

async def _ban_user_safe(chat_id: int, user_id: int, revoke: bool = True) -> bool:
    try:
        await bot.ban_chat_member(chat_id, user_id, revoke_messages=revoke)
        return True
    except Exception:
        logger.debug("ban_chat_member failed", exc_info=True)
        return False

def _now() -> int:
    return int(time.time())

async def _is_new_user(chat_id: int, user_id: int) -> bool:
    try:
        return bool(await redis_client.exists(f"new_user_until:{chat_id}:{user_id}"))
    except Exception:
        return False

async def _mark_new_user(chat_id: int, user_id: int, ttl: int | None = None) -> None:
    ttl = int(ttl or settings.NEW_USER_TTL_SECONDS)
    try:
        await redis_client.set(f"new_user_until:{chat_id}:{user_id}", _now() + ttl, ex=ttl + 600)
    except Exception:
        logger.debug("mark_new_user failed", exc_info=True)

async def _inc_join_msg_counter(chat_id: int, user_id: int) -> int:
    key = f"new_user_msg_count:{chat_id}:{user_id}"
    try:
        n = await redis_client.incr(key)
        await redis_client.expire(key, max(3600, settings.NEW_USER_TTL_SECONDS))
        return int(n or 0)
    except Exception:
        return 0

async def _flag(chat_id: int, msg_id: int, *, action: str, reason: str, user_id: int | None = None) -> None:
    try:
        await redis_client.hset(
            f"mod:combot:{chat_id}:{msg_id}",
            mapping={
                "action": action,
                "reason": reason,
                "user_id": int(user_id) if user_id is not None else 0,
                "ts": _now(),
            },
        )
    except Exception:
        logger.debug("store combot flag failed", exc_info=True)


async def _flag_inline_without_message(chat_id: int, *, action: str, reason: str, user_id: int | None = None) -> None:
    ts = _now()
    unique_suffix = secrets.token_hex(4)
    key = f"mod:combot:inline:{chat_id}:{ts}:{unique_suffix}"
    ttl = int(max(1, int(getattr(settings, "NEW_USER_TTL_SECONDS", 86400))))
    try:
        await redis_client.hset(
            key,
            mapping={
                "action": action,
                "reason": reason,
                "user_id": int(user_id) if user_id is not None else 0,
                "ts": ts,
            },
        )
        await redis_client.expire(key, ttl)
    except Exception:
        logger.debug("store inline combot flag without message failed", exc_info=True)

@dp.message(F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]), F.new_chat_members)
async def moderation_on_join(message: types.Message) -> None:
    chat_id = message.chat.id
    for m in (message.new_chat_members or []):
        if not m.is_bot:
            await _mark_new_user(chat_id, m.id, settings.NEW_USER_TTL_SECONDS)
    if settings.MODERATION_DELETE_SERVICE_JOINS:
        await _delete_message_safe(chat_id, message.message_id)

@dp.message(F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]), F.left_chat_member)
async def moderation_on_left(message: types.Message) -> None:
    if settings.MODERATION_DELETE_SERVICE_LEAVES:
        await _delete_message_safe(message.chat.id, message.message_id)

@dp.message(F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]), F.pinned_message)
async def moderation_on_pinned(message: types.Message) -> None:
    if settings.MODERATION_DELETE_SERVICE_PINNED:
        await _delete_message_safe(message.chat.id, message.message_id)

@dp.edited_message(F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]))
async def moderation_on_edited(message: types.Message) -> None:
    if not (message.from_user and not message.from_user.is_bot):
        return
    if settings.MODERATION_ADMIN_EXEMPT and await _is_admin(message.chat.id, int(message.from_user.id)):
        return
    if settings.MODERATION_EDITED_DELETE:
        await _delete_message_safe(message.chat.id, message.message_id)


def resolve_moderation_policy(context: str, cfg: Any) -> dict[str, Any]:
    policy = {
        "delete_external_channel_msgs": bool(getattr(cfg, "MODERATION_DELETE_EXTERNAL_CHANNEL_MSGS", True)),
        "delete_channel_forwards": bool(getattr(cfg, "MODERATION_DELETE_BOT_CHANNEL_CHAT_FORWARDS", True)),
        "delete_external_replies": bool(getattr(cfg, "MODERATION_EXTERNAL_REPLIES_DELETE", True)),
        "link_policy": "group_default",
    }
    if context != "comment":
        return policy

    comment_link_policy = str(getattr(cfg, "COMMENT_MODERATION_LINK_POLICY", "group_default") or "group_default").strip().lower()
    policy["link_policy"] = comment_link_policy
    policy["delete_external_replies"] = bool(getattr(cfg, "COMMENT_MODERATION_DELETE_EXTERNAL_REPLIES", False))
    if comment_link_policy == "relaxed":
        policy["delete_external_channel_msgs"] = False
        policy["delete_channel_forwards"] = False
    return policy


async def apply_moderation_filters(chat_id: int, message: types.Message) -> bool:
    
    try:
        sc = getattr(message, "sender_chat", None)
        if sc and int(sc.id) == int(message.chat.id) and getattr(sc, "type", None) in (ChatType.GROUP, ChatType.SUPERGROUP):
            return False
    except Exception:
        pass

    try:
        from_linked = await is_from_linked_channel(message)
    except Exception:
        from_linked = False

    context = resolve_message_moderation_context(message, from_linked=bool(from_linked))
    policy = resolve_moderation_policy(context, settings)

    def _ctx_reason(reason: str) -> str:
        return f"{reason}|context={context}"

    async def _delete_and_handle(reason: str) -> bool:
        ok = await _delete_message_safe(chat_id, message.message_id)
        if not ok:
            logger.warning("Moderation: failed to delete (%s) chat=%s msg=%s", reason, chat_id, message.message_id)
        return True

    logger.debug(
        "apply_moderation_filters: context=%s link_policy=%s chat=%s msg=%s",
        context,
        policy.get("link_policy"),
        chat_id,
        getattr(message, "message_id", None),
    )

    u = getattr(message, "from_user", None)
    is_admin = False
    try:
        if settings.MODERATION_ADMIN_EXEMPT and u:
            is_admin = await _is_admin(chat_id, int(u.id))
    except Exception:
        is_admin = False

    is_forward = bool(
        getattr(message, "is_automatic_forward", False)
        or getattr(message, "forward_from", None)
        or getattr(message, "forward_from_chat", None)
        or getattr(message, "forward_sender_name", None)
    )
    fchat = getattr(message, "forward_from_chat", None)
    is_channel_forward = bool(fchat and getattr(fchat, "type", None) == ChatType.CHANNEL)
    is_external_channel_msg = bool(
        (message.sender_chat and message.sender_chat.type == ChatType.CHANNEL) or is_channel_forward
    )

    if (
        is_external_channel_msg
        and policy["delete_external_channel_msgs"]
        and not from_linked
        and not is_admin
    ):
        try:
            uid = int(getattr(getattr(message, "from_user", None), "id", 0) or 0)
            await _flag(chat_id, message.message_id, action="delete", reason=_ctx_reason("external_channel"), user_id=uid)
            return await _delete_and_handle("external_channel")
        except Exception:
            pass

    delete_forwards_on = bool(policy["delete_channel_forwards"])
    if delete_forwards_on and is_forward and not from_linked and not is_admin:
        src_is_bot = bool(getattr(message, "forward_from", None) and getattr(message.forward_from, "is_bot", False))
        fchat = getattr(message, "forward_from_chat", None)
        try:
            src_is_chat = bool(fchat and getattr(fchat, "type", None) in (ChatType.CHANNEL, ChatType.SUPERGROUP, ChatType.GROUP))
        except Exception:
            src_is_chat = bool(fchat)
        src_hidden = bool(getattr(message, "forward_sender_name", None))
        if src_is_bot or src_is_chat or src_hidden:
            try:
                uid = int(getattr(getattr(message, "from_user", None), "id", 0) or 0)
                await _flag(chat_id, message.message_id, action="delete", reason=_ctx_reason("forward_disallowed"), user_id=uid)
            except Exception:
                pass
            return await _delete_and_handle("forward_disallowed")

    try:
        delete_buttons_on = bool(getattr(settings, "MODERATION_DELETE_BUTTON_MESSAGES", True))
        rm = getattr(message, "reply_markup", None)
        has_buttons = bool(
            rm and (
                isinstance(rm, types.InlineKeyboardMarkup)
                or isinstance(rm, types.ReplyKeyboardMarkup)
                or getattr(rm, "inline_keyboard", None)
                or getattr(rm, "keyboard", None)
            )
        )
    except Exception:
        delete_buttons_on = False
        has_buttons = False

    if delete_buttons_on and has_buttons and not from_linked and not is_admin:
        try:
            is_our_bot = bool(
                getattr(message.from_user, "is_bot", False) and
                int(message.from_user.id) == int(consts.BOT_ID)
            )
        except Exception:
            is_our_bot = False
        if is_our_bot:
            return False

        try:
            uid = int(getattr(getattr(message, "from_user", None), "id", 0) or 0)
            await _flag(chat_id, message.message_id, action="delete", reason=_ctx_reason("button"), user_id=uid)
        except Exception:
            pass
        return await _delete_and_handle("button")

    if not u or is_admin:
        return False

    if getattr(message, "sticker", None):
        if not settings.MODERATION_ALLOW_STICKERS:
            await _flag(chat_id, message.message_id, action="delete", reason=_ctx_reason("sticker"), user_id=u.id)
            return await _delete_and_handle("sticker")
        return False

    if getattr(message, "game", None):
        if not settings.MODERATION_ALLOW_GAMES:
            await _flag(chat_id, message.message_id, action="delete", reason=_ctx_reason("game"), user_id=u.id)
            return await _delete_and_handle("game")
        return False

    if getattr(message, "dice", None):
        if not settings.MODERATION_ALLOW_DICE:
            await _flag(chat_id, message.message_id, action="delete", reason=_ctx_reason("dice"), user_id=u.id)
            return await _delete_and_handle("dice")
        return False

    if getattr(message, "via_bot", None) and settings.MODERATION_INLINE_BOT_MSGS_DELETE:
        await _flag(chat_id, message.message_id, action="delete", reason=_ctx_reason("via_bot"), user_id=u.id)
        return await _delete_and_handle("via_bot")

    if getattr(message, "story", None) and settings.MODERATION_STORIES_DELETE:
        await _flag(chat_id, message.message_id, action="delete", reason=_ctx_reason("story"), user_id=u.id)
        return await _delete_and_handle("story")

    if getattr(message, "voice", None) and settings.MODERATION_VOICE_DELETE:
        await _flag(chat_id, message.message_id, action="delete", reason=_ctx_reason("voice"), user_id=u.id)
        return await _delete_and_handle("voice")

    if getattr(message, "video_note", None) and settings.MODERATION_VIDEO_NOTE_DELETE:
        await _flag(chat_id, message.message_id, action="delete", reason=_ctx_reason("video_note"), user_id=u.id)
        return await _delete_and_handle("video_note")

    if getattr(message, "audio", None) and settings.MODERATION_AUDIO_DELETE:
        await _flag(chat_id, message.message_id, action="delete", reason=_ctx_reason("audio"), user_id=u.id)
        return await _delete_and_handle("audio")

    if getattr(message, "photo", None) and settings.MODERATION_IMAGES_DELETE:
        await _flag(chat_id, message.message_id, action="delete", reason=_ctx_reason("image"), user_id=u.id)
        return await _delete_and_handle("image")

    if getattr(message, "video", None) and settings.MODERATION_VIDEOS_DELETE:
        await _flag(chat_id, message.message_id, action="delete", reason=_ctx_reason("video"), user_id=u.id)
        return await _delete_and_handle("video")

    if getattr(message, "animation", None) and settings.MODERATION_GIFS_DELETE:
        await _flag(chat_id, message.message_id, action="delete", reason=_ctx_reason("gif"), user_id=u.id)
        return await _delete_and_handle("gif")

    if getattr(message, "document", None) and settings.MODERATION_FILES_DELETE_ALL:
        mime = getattr(getattr(message, "document", None), "mime_type", "") or ""
        if (not mime.startswith("image/")) or settings.MODERATION_IMAGES_DELETE:
            await _flag(chat_id, message.message_id, action="delete", reason=_ctx_reason("file"), user_id=u.id)
            return await _delete_and_handle("file")

    if is_forward and settings.MODERATION_NEW_DELETE_FORWARDS_24H and await _is_new_user(chat_id, u.id):
        await _flag(chat_id, message.message_id, action="delete", reason=_ctx_reason("forward_new_user"), user_id=u.id)
        return await _delete_and_handle("forward_new_user")

    if getattr(message, "external_reply", None) and policy["delete_external_replies"]:
        await _flag(chat_id, message.message_id, action="delete", reason=_ctx_reason("external_reply"), user_id=u.id)
        return await _delete_and_handle("external_reply")

    raw = (message.text or message.caption or "") or ""
    ents = (message.entities or []) + (message.caption_entities or [])

    if not settings.MODERATION_ALLOW_MENTIONS:
        for e in ents:
            t = e.type.value if hasattr(e.type, "value") else e.type
            if t in ("mention", "text_mention"):
                await _flag(chat_id, message.message_id, action="delete", reason=_ctx_reason("mention"), user_id=u.id)
                return await _delete_and_handle("mention")

    if not settings.MODERATION_ALLOW_CUSTOM_EMOJI:
        for e in ents:
            t = e.type.value if hasattr(e.type, "value") else e.type
            if t == "custom_emoji":
                await _flag(chat_id, message.message_id, action="delete", reason=_ctx_reason("custom_emoji"), user_id=u.id)
                return await _delete_and_handle("custom_emoji")

    if settings.MODERATION_COMMANDS_DELETE_ALL:
        for e in ents:
            t = e.type.value if hasattr(e.type, "value") else e.type
            if t == "bot_command":
                token = raw[e.offset : e.offset + e.length].split()[0]  # "/battle@Bot"
                token = token.lstrip("/")
                parts = token.split("@", 1)
                cmd_name = (parts[0] if parts else "").lower()
                target_uname = (parts[1] if len(parts) > 1 else "").lower()

                to_our_bot = False
                try:
                    if consts.BOT_USERNAME and target_uname and target_uname == consts.BOT_USERNAME.lower():
                        to_our_bot = True
                except Exception:
                    pass
                try:
                    if (message.reply_to_message
                        and message.reply_to_message.from_user
                        and int(message.reply_to_message.from_user.id) == int(consts.BOT_ID)):
                        to_our_bot = True
                except Exception:
                    pass

                if to_our_bot:
                    continue

                whitelist = [c.lower() for c in getattr(settings, "MODERATION_COMMAND_WHITELIST", [])]
                if cmd_name not in whitelist:
                    await _flag(
                        chat_id, message.message_id,
                        action="delete", reason=_ctx_reason(f"command:{cmd_name}"), user_id=u.id
                    )
                    return await _delete_and_handle(f"command:{cmd_name}")

    ents_payload = []
    for e in ents:
        t = (e.type.value if hasattr(e.type, "value") else e.type)
        d = {"offset": e.offset, "length": e.length, "type": t}
        try:
            if str(t).lower() == "text_link" and getattr(e, "url", None):
                d["url"] = e.url
        except Exception:
            pass
        ents_payload.append(d)
    urls = extract_urls(raw, ents_payload)

    if urls:
        if settings.MODERATION_SPAM_BAN_FIRST_LINK_AFTER_JOIN and await _is_new_user(chat_id, u.id):
            n = await _inc_join_msg_counter(chat_id, u.id)
            if n == 1:
                banned = await _ban_user_safe(chat_id, u.id, revoke=settings.MODERATION_BAN_REVOKE_MESSAGES)
                await _flag(chat_id, message.message_id, action=("ban" if banned else "delete"), reason=_ctx_reason("first_link_after_join"), user_id=u.id)
                if not banned:
                    ok = await _delete_message_safe(chat_id, message.message_id)
                    if not ok:
                        logger.warning("Moderation: failed to delete first_link_after_join chat=%s msg=%s", chat_id, message.message_id)
                return True

        if getattr(settings, "MODERATION_DELETE_TELEGRAM_LINKS", True) and (
            any(is_telegram_link(u) for u in urls) or contains_telegram_obfuscated(raw)
        ):
            await _flag(chat_id, message.message_id, action="delete", reason=_ctx_reason("telegram_link"), user_id=u.id)
            ok = await _delete_message_safe(chat_id, message.message_id)
            if not ok:
                logger.warning("Moderation: failed to delete (telegram_link) chat=%s msg=%s", chat_id, message.message_id)
            return True

        if settings.MODERATION_LINKS_DELETE_ALL or (settings.MODERATION_NEW_DELETE_LINKS_24H and await _is_new_user(chat_id, u.id)):
            await _flag(chat_id, message.message_id, action="delete", reason=_ctx_reason("link"), user_id=u.id)
            ok = await _delete_message_safe(chat_id, message.message_id)
            if not ok:
                logger.warning("Moderation: failed to delete (telegram_link) chat=%s msg=%s", chat_id, message.message_id)
            return True

    else:
        if settings.MODERATION_LINKS_DELETE_ALL and contains_any_link_obfuscated(raw):
            await _flag(chat_id, message.message_id, action="delete", reason=_ctx_reason("link_obfuscated"), user_id=u.id)
            ok = await _delete_message_safe(chat_id, message.message_id)
            if not ok:
                logger.warning("Moderation: failed to delete (link_obfuscated) chat=%s msg=%s", chat_id, message.message_id)
            return True
        if getattr(settings, "MODERATION_DELETE_TELEGRAM_LINKS", True) and contains_telegram_obfuscated(raw):
            await _flag(chat_id, message.message_id, action="delete", reason=_ctx_reason("telegram_link"), user_id=u.id)
            ok = await _delete_message_safe(chat_id, message.message_id)
            if not ok:
                logger.warning("Moderation: failed to delete (telegram_link) chat=%s msg=%s", chat_id, message.message_id)
            return True

    return False

@dp.message(
    F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]),
    F.reply_markup,
)
async def moderation_guard_reply_markup(message: types.Message) -> None:
    try:
        if await apply_moderation_filters(message.chat.id, message):
            return
    except Exception:
        logger.exception("moderation_guard_reply_markup failed")
    raise SkipHandler

@dp.callback_query(F.data.startswith("mod:ban:"))
async def moderation_inline_ban(cb: types.CallbackQuery) -> None:
    try:
        parts = (cb.data or "").split(":")
        if len(parts) < 4:
            await cb.answer("Malformed action.", show_alert=True)
            return
        _, action, chat_id_str, offender_id_str, *rest = parts
        if action != "ban":
            await cb.answer("Unsupported action.", show_alert=True)
            return

        chat_id = int(chat_id_str)
        offender_id = int(offender_id_str)
        trigger_msg_id = None
        if rest:
            try:
                trigger_msg_id = int(rest[0])
            except Exception:
                trigger_msg_id = None

        admin_id = cb.from_user.id if cb.from_user else 0
        if not await _is_admin(chat_id, int(admin_id)):
            await cb.answer("You must be an admin of that chat to perform this action.", show_alert=True)
            return

        banned = await _ban_user_safe(chat_id, offender_id, revoke=getattr(settings, "MODERATION_BAN_REVOKE_MESSAGES", True))
        if banned:
            try:
                if isinstance(trigger_msg_id, int) and trigger_msg_id > 0:
                    await _flag(chat_id, trigger_msg_id, action="ban", reason="inline_button", user_id=offender_id)
                else:
                    await _flag_inline_without_message(chat_id, action="ban", reason="inline_button", user_id=offender_id)
            except Exception:
                logger.debug("inline ban: flag failed", exc_info=True)

            try:
                if cb.message:
                    base = (getattr(cb.message, "html_text", None)
                            or cb.message.text
                            or cb.message.caption
                            or "Moderation alert")
                    new_text = base + f"\n\n<b>Action:</b> User <code>{offender_id}</code> has been <b>banned</b> by <a href=\"tg://user?id={admin_id}\">admin</a>."
                    await cb.message.edit_text(new_text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=None)
            except Exception:
                try:
                    if cb.message:
                        await cb.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass

            await cb.answer("User banned and messages revoked.", show_alert=False)
        else:
            await cb.answer("Failed to ban the user. I may lack the necessary rights.", show_alert=True)

    except Exception:
        logger.exception("moderation_inline_ban: error")
        try:
            await cb.answer("Unexpected error while processing the action.", show_alert=True)
        except Exception:
            pass
