#app/bot/handlers/moderation.py
from __future__ import annotations

import logging
import contextlib
import asyncio
import time
import html
import secrets
import io
import base64
from typing import Any, List

from aiogram import types, F
from aiogram.enums import ChatType
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramForbiddenError

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
    classify_profile_nsfw_fast,
    get_last_ai_moderation_category,
    get_last_ai_moderation_flags,
    should_delete_ai_flagged_message,
    is_emoji_flood_text,
    is_symbol_noise_text,
    count_message_emojis,
)
from app.services.addons.analytics import record_moderation as analytics_record_moderation
from app.bot.handlers.moderation_context import (
    resolve_message_moderation_context,
    resolve_message_moderation_context_async,
)
from app.bot.utils.trusted_scope import (
    extract_source_scope_ids,
    is_trusted_actor,
    is_trusted_destination as trusted_destination_check,
    is_trusted_repost as trusted_repost_check,
    trusted_scope_ids as build_trusted_scope_ids,
)

logger = logging.getLogger(__name__)


class ModerationSignalPersistenceError(RuntimeError):
    """Raised when passive moderation cannot persist status signal required by queue worker."""


bot = get_bot()

def get_targets() -> list[int]:
    targets = {int(x) for x in (getattr(settings, "MODERATOR_IDS", []) or []) if str(x).strip()}
    notify_chat_id = int(getattr(settings, "MODERATOR_NOTIFICATION_CHAT_ID", 0) or 0)
    if notify_chat_id:
        targets.add(notify_chat_id)
    return sorted(targets)

async def _check_light_with_flags(
    chat_id: int,
    user_id: int,
    text: str,
    entities: list[dict[str, Any]] | None,
    *,
    source: str,
    allow_ai_for_source: bool,
    policy: dict[str, Any],
    image_b64: str | None,
    image_mime: str | None,
) -> tuple[str, tuple[str, ...], str]:
    light_status = await check_light(
        chat_id,
        user_id,
        text,
        entities,
        source=source,
        allow_ai_for_source=allow_ai_for_source,
        policy=policy,
        image_b64=image_b64,
        image_mime=image_mime,
    )
    return light_status, get_last_ai_moderation_flags(), get_last_ai_moderation_category()


def _linked_channel_cache_key(chat_id: int) -> str:
    return f"linked_channel_id:{chat_id}"


def _chat_title_cache_key(chat_id: int) -> str:
    return f"chat_title:{chat_id}"


def _extract_chat_display_name(chat_obj: Any) -> str:
    title = (getattr(chat_obj, "title", None) or "").strip()
    if title:
        return title
    username = (getattr(chat_obj, "username", None) or "").strip().lstrip("@")
    if username:
        return f"@{username}"
    full_name = (getattr(chat_obj, "full_name", None) or "").strip()
    if full_name:
        return full_name
    return ""


async def _resolve_chat_display_name(
    chat_id: int,
    message: types.Message | None,
    *,
    fallback_chat_title: str | None = None,
) -> str:
    if fallback_chat_title:
        fallback_name = str(fallback_chat_title).strip()
        if fallback_name:
            return fallback_name

    if message is not None:
        from_message = _extract_chat_display_name(getattr(message, "chat", None))
        if from_message:
            return from_message

    cache_key = _chat_title_cache_key(chat_id)
    try:
        cached = await redis_client.get(cache_key)
        if cached:
            if isinstance(cached, (bytes, bytearray)):
                cached = cached.decode("utf-8", "ignore")
            name = str(cached).strip()
            if name:
                return name
    except Exception:
        logger.debug("chat title cache read failed", exc_info=True)

    resolved = ""
    try:
        chat = await bot.get_chat(chat_id)
        resolved = _extract_chat_display_name(chat)
    except Exception:
        logger.debug("chat title lookup failed for chat_id=%s", chat_id, exc_info=True)

    if resolved:
        try:
            await redis_client.set(
                cache_key,
                resolved,
                ex=int(getattr(settings, "MODERATOR_ADMIN_CACHE_TTL_SECONDS", 86400)),
            )
        except Exception:
            logger.debug("chat title cache write failed", exc_info=True)
    return resolved

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



def _trusted_scope_ids() -> tuple[set[int], set[int], set[int]]:
    trusted_destinations, trusted_source_channel_ids, trusted_scope = build_trusted_scope_ids(settings)
    return trusted_destinations, trusted_source_channel_ids, trusted_scope


async def _is_fully_trusted_actor_or_action(
    *,
    chat_id: int,
    message: types.Message | None,
    source: str,
    user_id: int | None = None,
    from_linked: bool = False,
) -> bool:
    _, trusted_source_channel_ids, trusted_scope = _trusted_scope_ids()
    return await is_trusted_actor(
        message=message,
        user_id=user_id,
        chat_id=chat_id,
        from_linked=from_linked,
        trusted_scope_ids=trusted_scope,
        trusted_source_channel_ids=trusted_source_channel_ids,
        is_admin_cb=_is_admin,
    )


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

    results = await asyncio.gather(
        *[
            bot.send_message(
                chat_id=int(mid),
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=_ban_markup(chat_id, offender_id, msg_id),
            )
            for mid in targets
        ],
        return_exceptions=True,
    )
    for mid, result in zip(targets, results):
        if isinstance(result, TelegramForbiddenError):
            logger.info("Skip moderation alert: forbidden to initiate chat with target=%s", int(mid))
            continue
        if isinstance(result, Exception):
            logger.error(
                "Failed to send moderation alert with actions target=%s",
                int(mid),
                exc_info=(type(result), result, result.__traceback__),
            )

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
    chat_title: str | None = None,
) -> str:
    status = "clean"
    reason_text = ""
    finalize_early = False
    from_linked = False
    _uid = int(getattr(getattr(message, "from_user", None), "id", user_id or 0))
    _mid = int(getattr(message, "message_id", message_id or 0))

    async def _persist_status_to_redis(*, persisted_status: str, persisted_reason: str) -> None:
        try:
            if _mid > 0:
                await redis_client.hset(
                    f"mod:msg:{chat_id}:{_mid}",
                    mapping={
                        "status": persisted_status,
                        "reason": persisted_reason,
                        "ts": int(time.time()),
                        "user_id": int(_uid),
                    },
                )
            else:
                logger.warning(
                    "PASSIVE_MODERATION_SKIP_PERSIST_INVALID_MESSAGE_ID: chat_id=%s msg_id=%s user_id=%s status=%s",
                    chat_id,
                    _mid,
                    _uid,
                    persisted_status,
                )
        except Exception as exc:
            logger.exception(
                "Failed to persist moderation status to Redis: chat_id=%s msg_id=%s status=%s",
                chat_id,
                _mid,
                persisted_status,
            )
            raise ModerationSignalPersistenceError(
                f"mod:msg persistence failed for chat_id={chat_id} msg_id={_mid} status={persisted_status}"
            ) from exc

    try:
        if message:
            from_linked = await is_from_linked_channel(message)

        if settings.MODERATION_ADMIN_EXEMPT:
            uid_for_admin = (int(getattr(getattr(message, "from_user", None), "id", 0)) if message else (int(user_id) if user_id else 0))
            if uid_for_admin and await _is_admin(chat_id, uid_for_admin):
                finalize_early = True
    except Exception:
        pass

    deep_text_threshold = int(getattr(settings, "MOD_DEEP_TEXT_THRESHOLD", 400))

    if not ( (message and getattr(message, "from_user", None)) or user_id ):
        finalize_early = True

    if not finalize_early and message is not None and getattr(getattr(message, "chat", None), "type", None) == ChatType.PRIVATE:
        finalize_early = True
    if not finalize_early and message is None and user_id is not None and int(chat_id) == int(user_id):
        finalize_early = True

    normalized_source = (source or "user").strip().lower()
    allowed_sources = {"user", "bot", "channel"}
    if normalized_source not in allowed_sources:
        logger.warning("Unknown passive moderation source=%r; fallback to 'user'", source)
        normalized_source = "user"

    _, _, trusted_scope = _trusted_scope_ids()

    is_destination_trusted = trusted_destination_check(chat_id, getattr(message, "chat", None), settings)

    is_trusted_source = bool(normalized_source == "user" or is_destination_trusted or from_linked)
    logger.info(
        "PASSIVE_MODERATION_START: chat_id=%s msg_id=%s user_id=%s source=%s is_trusted_destination=%s is_comment_context=%s",
        chat_id,
        message_id or getattr(message, "message_id", None),
        user_id or getattr(getattr(message, "from_user", None), "id", None),
        normalized_source,
        is_destination_trusted,
        is_comment_context,
    )
    is_fully_trusted = await _is_fully_trusted_actor_or_action(
        chat_id=chat_id,
        message=message,
        source=normalized_source,
        user_id=user_id,
        from_linked=from_linked,
    )
    if is_fully_trusted:
        finalize_early = True

    light_throttle = f"mod_alert:light:{chat_id}:{_uid}"
    deep_throttle  = f"mod_alert:deep:{chat_id}:{_uid}"

    if not finalize_early and bool(getattr(settings, "MODERATION_PROFILE_NSFW_ENFORCE", True)) and _uid > 0:
        try:
            blocked_key = _profile_nsfw_blocked_chat_key(chat_id, _uid)
            if await redis_client.exists(blocked_key):
                if _mid:
                    await _flag(chat_id, _mid, action="delete", reason="profile_nsfw_blocked|context=group", user_id=_uid)
                    await _delete_message_safe(chat_id, _mid)
                await _restrict_user_write_safe(chat_id, _uid)
                logger.info(
                    "PASSIVE_MODERATION_PROFILE_NSFW_BLOCKED_KEY: chat_id=%s msg_id=%s user_id=%s",
                    chat_id,
                    _mid,
                    _uid,
                )
                status = "blocked"
                reason_text = "profile_nsfw_blocked"
                finalize_early = True

            if not finalize_early and await _is_profile_nsfw(_uid):
                if _mid:
                    await _flag(chat_id, _mid, action="restrict", reason="profile_nsfw|context=group", user_id=_uid)
                    await _delete_message_safe(chat_id, _mid)
                await redis_client.set(blocked_key, 1)
                await _cleanup_user_history_and_mute(chat_id, _uid)
                logger.info(
                    "PASSIVE_MODERATION_PROFILE_NSFW_DETECTED: chat_id=%s msg_id=%s user_id=%s",
                    chat_id,
                    _mid,
                    _uid,
                )
                status = "blocked"
                reason_text = "profile_nsfw"
                finalize_early = True
        except Exception:
            logger.debug("profile nsfw enforcement failed", exc_info=True)

    if not finalize_early and not (text and text.strip()) and not image_b64:
        finalize_early = True

    if finalize_early:
        await _persist_status_to_redis(persisted_status=status, persisted_reason=reason_text)

        logger.info(
            "PASSIVE_MODERATION_RESULT: chat_id=%s msg_id=%s user_id=%s status=%s reason=%s",
            chat_id,
            _mid,
            _uid,
            status,
            reason_text,
        )
        return status

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
        try:
            moderation_context = await resolve_message_moderation_context_async(
                message,
                from_linked=bool(from_linked),
            )
        except Exception:
            moderation_context = resolve_message_moderation_context(message, from_linked=bool(from_linked))
    else:
        moderation_context = "group"
    light_policy = resolve_moderation_policy(moderation_context, settings)

    try:
        targets = get_targets()

        all_entities = entities if entities is not None else (
            _serialize_entities(getattr(message, "entities", [])) +
            _serialize_entities(getattr(message, "caption_entities", []))
        )
        light_ai_flags: tuple[str, ...] = ()
        light_ai_category = ""
        try:
            light_result = await asyncio.wait_for(
                _check_light_with_flags(
                    chat_id,
                    _uid,
                    text,
                    all_entities,
                    source=normalized_source,
                    allow_ai_for_source=is_trusted_source,
                    policy=light_policy,
                    image_b64=image_b64,
                    image_mime=image_mime,
                ),
                timeout=getattr(settings, "MOD_LIGHT_TIMEOUT", 2.0),
            )
            if isinstance(light_result, tuple) and len(light_result) == 3:
                light_status, light_ai_flags, light_ai_category = light_result
            else:
                light_status = str(light_result or "clean")
        except asyncio.TimeoutError:
            logger.warning("check_light timed out for chat=%s user=%s", chat_id, _uid)
            light_status = "light_timeout_risk"
        chat_display_name = await _resolve_chat_display_name(
            chat_id,
            message,
            fallback_chat_title=chat_title,
        )
        safe_chat_name = html.escape(chat_display_name) if chat_display_name else ""

        status = "clean"
        reason_text = ""
        reason_code = ""
        ai_flag_signal = False
        if light_status != "clean":
            reason_map = {
                "flood": "Frequent messages (flood/spam)",
                "spam_links": "Too many links in one message",
                "spam_mentions": "Too many mentions in one message",
                "promo": "Promotional content",
                "promo_profile_cta": "Promotional CTA to profile/bio/channel",
                "link_violation": "Disallowed link (policy)",
                "toxic": "AI moderation policy violation",
                "sexual_content": "Sexual/erotic content policy violation",
                "emoji_flood": "Emoji flood / visual spam",
                "symbol_noise": "Obfuscated symbol/noise flood",
                "custom_emoji_spam": "Custom emoji flood",
                "emoji_overlimit": "Too many emojis in one message",
                "light_timeout_risk": "Light moderation timeout (risk fallback)",
            }
            reason_text = reason_map.get(light_status, "Unknown reason")
            reason_code = str(light_status or "unknown").strip().lower() or "unknown"
            status = "flagged"
            ai_flag_signal = light_status == "toxic"
            if light_status == "toxic":
                ai_category = light_ai_category or get_last_ai_moderation_category()
                if ai_category:
                    reason_text = f"AI moderation policy violation ({ai_category})"

            raw_snippet = (text or "")[:200]
            snippet = html.escape(raw_snippet) + ("…" if len(text) > 200 else "")
            body = f"Text: {snippet}" if snippet else "Text: (empty)"
            if image_b64:
                body += "\n📷 Image: attached"
            chat_scope = f"{safe_chat_name} | " if safe_chat_name else ""
            alert_text = (
                f"🚨 <b>Passive Moderation Alert ({chat_scope}chat ID: <code>{chat_id}</code>)</b>\n"
                f"User: <a href=\"tg://user?id={_uid}\">{getattr(getattr(message,'from_user',None),'full_name',str(_uid))}</a> "
                f"(<code>{_uid}</code>)\n"
                f"Message ID: <code>{_mid}</code>\n"
                f"{body}\n\n"
                f"Reason: <b>{reason_text}</b>.\n"
                f"Reason code: <code>{html.escape(reason_code)}</code>."
            )
            if str(chat_id).startswith("-100"):
                public_chat_id = str(chat_id)[4:]
                alert_text += (
                    f"\n<a href=\"https://t.me/c/{public_chat_id}/{_mid}\">Link to message</a>"
                )

            notify_ai_flags = bool(getattr(settings, "MODERATION_NOTIFY_ADMINS_ON_AI_FLAGS", False))
            if allow_alert and (notify_ai_flags or not ai_flag_signal):
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
            new_user
        )

        risk = base_risk
        if moderation_context == "comment":
            risk = bool((base_risk and light_status != "clean") or (light_status == "promo_profile_cta"))
        elif moderation_context == "group" and light_status == "promo_profile_cta":
            risk = True

        blocked = False
        if risk:
            try:
                blocked = await asyncio.wait_for(
                    check_deep(
                        chat_id,
                        _uid,
                        text,
                        source=normalized_source,
                        allow_ai_for_source=is_trusted_source,
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
            ai_flag_signal = True
            deep_ai_category = get_last_ai_moderation_category()
            if deep_ai_category:
                reason_text = f"Contextual violation ({deep_ai_category})"
                reason_code = f"deep_ai:{deep_ai_category}"
            else:
                reason_text = "Contextual violation"
                reason_code = "deep_contextual"
            raw_snippet = (text or "")[:200]
            snippet = html.escape(raw_snippet) + ("…" if len(text) > 200 else "")
            chat_scope = f"{safe_chat_name} | " if safe_chat_name else ""
            alert_text = (
                f"🚨 <b>Deep Moderation Alert ({chat_scope}chat ID: <code>{chat_id}</code>)</b>\n"
                f"User: <a href=\"tg://user?id={_uid}\">{getattr(getattr(message,'from_user',None),'full_name',str(_uid))}</a> "
                f"(<code>{_uid}</code>)\n"
                f"Message ID: <code>{_mid}</code>\n"
                f"Text: {snippet}\n\n"
                f"Reason: <b>{html.escape(reason_text or 'Contextual violation')}</b>.\n"
                f"Reason code: <code>{html.escape(reason_code or 'deep_contextual')}</code>."
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

            notify_ai_flags = bool(getattr(settings, "MODERATION_NOTIFY_ADMINS_ON_AI_FLAGS", False))
            if allow_deep_alert and (notify_ai_flags or not ai_flag_signal):
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

        if ai_flag_signal and _mid and status != "blocked":
            try:
                ai_flags = get_last_ai_moderation_flags()
                if not ai_flags:
                    ai_flags = light_ai_flags
                should_delete_ai = bool(getattr(settings, "MODERATION_DELETE_FLAGGED", False)) or should_delete_ai_flagged_message(ai_flags)
                if should_delete_ai:
                    await _delete_message_safe(chat_id, _mid)
            except Exception:
                logger.debug("ai-flagged: delete failed", exc_info=True)

        try:
            ttl = int(getattr(settings, "MOD_FLAG_TTL_SECONDS", 86_400))
            await _persist_status_to_redis(persisted_status=status, persisted_reason=reason_text)
            if status in ("flagged", "blocked"):
                uid = int(_uid)
                await redis_client.sadd(f"mod_flagged_users:{chat_id}", uid)
                await redis_client.set(f"mod_flagged_ttl:{chat_id}:{uid}", 1, ex=ttl)
                try:
                    await redis_client.zrem(f"last_ping_zset:{chat_id}", str(uid))
                except Exception:
                    pass
        except ModerationSignalPersistenceError:
            raise
        except Exception as exc:
            logger.exception(
                "Failed to persist moderation status to Redis: chat_id=%s msg_id=%s status=%s",
                chat_id,
                _mid,
                status,
            )
            raise ModerationSignalPersistenceError(
                f"mod:msg persistence failed for chat_id={chat_id} msg_id={_mid} status={status}"
            ) from exc

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

    except ModerationSignalPersistenceError:
        raise
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

    logger.info(
        "PASSIVE_MODERATION_RESULT: chat_id=%s msg_id=%s user_id=%s status=%s reason=%s",
        chat_id,
        _mid,
        _uid,
        status,
        reason_text,
    )
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


async def _unban_user_safe(chat_id: int, user_id: int) -> bool:
    try:
        await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
        return True
    except Exception:
        logger.debug("unban_chat_member failed", exc_info=True)
        return False


async def _restrict_user_write_safe(chat_id: int, user_id: int) -> bool:
    try:
        perms = types.ChatPermissions(
            can_send_messages=False,
            can_send_audios=False,
            can_send_documents=False,
            can_send_photos=False,
            can_send_videos=False,
            can_send_video_notes=False,
            can_send_voice_notes=False,
            can_send_polls=False,
            can_send_other_messages=False,
            can_add_web_page_previews=False,
            can_change_info=False,
            can_invite_users=False,
            can_pin_messages=False,
            can_manage_topics=False,
        )
        await bot.restrict_chat_member(chat_id, user_id, permissions=perms)
        return True
    except Exception:
        logger.debug("restrict_chat_member failed", exc_info=True)
        return False


def _profile_nsfw_blocked_chat_key(chat_id: int, user_id: int) -> str:
    return f"mod:profile_nsfw_blocked:{int(chat_id)}:{int(user_id)}"


async def _is_profile_nsfw(user_id: int) -> bool:
    if not bool(getattr(settings, "MODERATION_PROFILE_NSFW_ENFORCE", True)):
        return False

    flagged = False
    try:
        photos = await bot.get_user_profile_photos(user_id=int(user_id), limit=1)
        photo_set = getattr(photos, "photos", None) or []
        if photo_set and photo_set[0]:
            best_photo = photo_set[0][-1]
            tg_file = await bot.get_file(best_photo.file_id)
            raw = io.BytesIO()
            await bot.download(tg_file, raw)
            image_b64 = base64.b64encode(raw.getvalue()).decode("utf-8")
            flagged = await classify_profile_nsfw_fast(
                image_b64=image_b64,
                image_mime="image/jpeg",
            )
    except Exception:
        logger.debug("profile nsfw check failed for user_id=%s", user_id, exc_info=True)
        flagged = False

    return flagged


async def _cleanup_user_history_and_mute(chat_id: int, user_id: int) -> None:
    await _restrict_user_write_safe(chat_id, user_id)


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


@dp.message_reaction(F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]))
async def moderation_on_reaction(event: types.MessageReactionUpdated) -> None:
    if not bool(getattr(settings, "MODERATION_PROFILE_NSFW_ENFORCE", True)):
        return

    try:
        chat_id = int(event.chat.id)
    except Exception:
        return

    allowed_chat_ids = {
        int(x) for x in (getattr(settings, "ALLOWED_GROUP_IDS", []) or [])
    }
    comment_target_chat_ids = {
        int(x) for x in (getattr(settings, "COMMENT_TARGET_CHAT_IDS", []) or [])
    }
    comment_source_channel_ids = {
        int(x) for x in (getattr(settings, "COMMENT_SOURCE_CHANNEL_IDS", []) or [])
    }
    comment_enabled = bool(getattr(settings, "COMMENT_MODERATION_ENABLED", False))

    is_trusted_destination = chat_id in allowed_chat_ids
    if not is_trusted_destination and comment_enabled:
        linked_chat_id = None
        with contextlib.suppress(Exception):
            linked_chat_id = int(getattr(event.chat, "linked_chat_id", 0) or 0)
        is_trusted_destination = bool(
            chat_id in comment_target_chat_ids
            or (linked_chat_id and linked_chat_id in comment_source_channel_ids)
        )

    if not is_trusted_destination:
        return

    user = getattr(event, "user", None)
    if not user:
        return

    uid = int(getattr(user, "id", 0) or 0)
    if not uid or bool(getattr(user, "is_bot", False)):
        return

    if await _is_fully_trusted_actor_or_action(
        chat_id=chat_id,
        message=None,
        source="user",
        user_id=uid,
        from_linked=False,
    ):
        return

    blocked_key = _profile_nsfw_blocked_chat_key(chat_id, uid)
    try:
        if await redis_client.exists(blocked_key):
            return
    except Exception:
        logger.debug("reaction blocked-key check failed", exc_info=True)

    try:
        if not await _is_profile_nsfw(uid):
            return
    except Exception:
        logger.debug("reaction profile nsfw check failed", exc_info=True)
        return

    try:
        await redis_client.set(blocked_key, 1)
    except Exception:
        logger.debug("reaction blocked-key write failed", exc_info=True)

    await _flag_inline_without_message(
        chat_id,
        action="restrict",
        reason="profile_nsfw_reaction|context=group",
        user_id=uid,
    )
    await _cleanup_user_history_and_mute(chat_id, uid)

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
        # Применяется к обычным forwards (forward_from* / forward_sender_name),
        # но не к Telegram auto-forward из linked-channel.
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
    # Telegram auto-forward комментариев из linked-channel не считается
    # обычным forward для MODERATION_DELETE_BOT_CHANNEL_CHAT_FORWARDS.
    if bool(getattr(message, "is_automatic_forward", False)):
        return False

    _, _, trusted_scope = _trusted_scope_ids()

    is_destination_trusted = trusted_destination_check(chat_id, getattr(message, "chat", None), settings)

    sender_chat = getattr(message, "sender_chat", None)
    is_sender_chat_entity = bool(
        sender_chat and getattr(sender_chat, "type", None) in (ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP)
    )
    is_trusted_sender_chat = bool(sender_chat and int(getattr(sender_chat, "id", 0) or 0) in trusted_scope)

    is_trusted_repost = trusted_repost_check(message, trusted_scope, destination_trusted=is_destination_trusted)

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

    if await _is_fully_trusted_actor_or_action(
        chat_id=chat_id,
        message=message,
        source=("channel" if is_sender_chat_entity else "user"),
        user_id=int(getattr(getattr(message, "from_user", None), "id", 0) or 0),
        from_linked=from_linked,
    ):
        return False

    try:
        context = await resolve_message_moderation_context_async(
            message,
            from_linked=bool(from_linked),
        )
    except Exception:
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
        if u and is_destination_trusted:
            is_admin = await _is_admin(chat_id, int(u.id))
        elif settings.MODERATION_ADMIN_EXEMPT and u:
            is_admin = await _is_admin(chat_id, int(u.id))
    except Exception:
        is_admin = False

    if is_admin:
        return False

    if is_trusted_sender_chat:
        return False

    if context == "comment" and bool(getattr(settings, "COMMENT_MODERATION_REQUIRE_REGISTERED_ACTOR", False)):
        registered_ids = {
            int(x)
            for x in (getattr(settings, "COMMENT_MODERATION_REGISTERED_IDS", []) or [])
            if str(x).strip()
        }
        registered_usernames = {
            str(x).lstrip("@").strip().lower()
            for x in (getattr(settings, "COMMENT_MODERATION_REGISTERED_USERNAMES", []) or [])
            if str(x).strip()
        }
        allowed_comment_sources = {
            int(x)
            for x in (getattr(settings, "COMMENT_SOURCE_CHANNEL_IDS", []) or [])
            if str(x).strip()
        }

        actor_allowed = False
        actor_user = getattr(message, "from_user", None)
        if actor_user is not None:
            with contextlib.suppress(Exception):
                actor_uid = int(getattr(actor_user, "id", 0) or 0)
                if actor_uid and actor_uid in registered_ids:
                    actor_allowed = True
            if not actor_allowed:
                uname = str(getattr(actor_user, "username", "") or "").lstrip("@").strip().lower()
                if uname and uname in registered_usernames:
                    actor_allowed = True

        if not actor_allowed:
            sender_chat_local = getattr(message, "sender_chat", None)
            with contextlib.suppress(Exception):
                sender_chat_id_local = int(getattr(sender_chat_local, "id", 0) or 0)
                if sender_chat_id_local and sender_chat_id_local in allowed_comment_sources:
                    actor_allowed = True

        if not actor_allowed:
            await _flag(
                chat_id,
                message.message_id,
                action="delete",
                reason=_ctx_reason("comment_unregistered_actor"),
                user_id=int(getattr(getattr(message, "from_user", None), "id", 0) or 0),
            )
            return await _delete_and_handle("comment_unregistered_actor")

    is_automatic_forward = bool(getattr(message, "is_automatic_forward", False))
    has_forward_origin = bool(
        getattr(message, "forward_from", None)
        or getattr(message, "forward_from_chat", None)
        or getattr(message, "forward_sender_name", None)
    )
    is_regular_forward = bool(not is_automatic_forward and has_forward_origin)
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
        and not is_trusted_repost
    ):
        try:
            uid = int(getattr(getattr(message, "from_user", None), "id", 0) or 0)
            await _flag(chat_id, message.message_id, action="delete", reason=_ctx_reason("external_channel"), user_id=uid)
            return await _delete_and_handle("external_channel")
        except Exception:
            pass

    delete_forwards_on = bool(policy["delete_channel_forwards"])
    forward_disallowed = bool(
        delete_forwards_on
        and is_regular_forward
        and not from_linked
        and not is_admin
        and not is_trusted_repost
    )
    if forward_disallowed:
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

    if is_regular_forward and settings.MODERATION_NEW_DELETE_FORWARDS_24H and await _is_new_user(chat_id, u.id):
        await _flag(chat_id, message.message_id, action="delete", reason=_ctx_reason("forward_new_user"), user_id=u.id)
        return await _delete_and_handle("forward_new_user")

    if getattr(message, "external_reply", None) and policy["delete_external_replies"]:
        await _flag(chat_id, message.message_id, action="delete", reason=_ctx_reason("external_reply"), user_id=u.id)
        return await _delete_and_handle("external_reply")

    raw = (message.text or message.caption or "") or ""
    ents = (message.entities or []) + (message.caption_entities or [])

    max_emoji_per_message = int(getattr(settings, "MODERATION_MAX_EMOJI_PER_MESSAGE", 5) or 0)
    if max_emoji_per_message > 0 and count_message_emojis(raw, [
        {
            "type": str(e.type.value if hasattr(e.type, "value") else e.type),
            "offset": int(getattr(e, "offset", 0) or 0),
            "length": int(getattr(e, "length", 0) or 0),
        }
        for e in ents
    ]) > max_emoji_per_message:
        await _flag(chat_id, message.message_id, action="delete", reason=_ctx_reason("emoji_overlimit"), user_id=u.id)
        return await _delete_and_handle("emoji_overlimit")

    if is_emoji_flood_text(raw):
        await _flag(chat_id, message.message_id, action="delete", reason=_ctx_reason("emoji_flood"), user_id=u.id)
        return await _delete_and_handle("emoji_flood")

    if is_symbol_noise_text(raw):
        await _flag(chat_id, message.message_id, action="delete", reason=_ctx_reason("symbol_noise"), user_id=u.id)
        return await _delete_and_handle("symbol_noise")

    if bool(getattr(settings, "MODERATION_DELETE_NON_MEMBER_MENTIONS", False)):
        def _norm_uname(uname: str | None) -> str:
            return (uname or "").lstrip("@").strip().lower()

        async def _resolve_mention_member_id(entity) -> int | None:
            t = entity.type.value if hasattr(entity.type, "value") else entity.type
            t = str(t).lower()
            if t == "text_mention":
                with contextlib.suppress(Exception):
                    ent_user = getattr(entity, "user", None)
                    if ent_user and getattr(ent_user, "id", None):
                        return int(ent_user.id)
                return None
            if t != "mention":
                return None

            try:
                uname = _norm_uname(raw[entity.offset : entity.offset + entity.length])
            except Exception:
                return None
            if not uname:
                return None

            try:
                cached_uid = await redis_client.hget(f"user_map:{chat_id}", uname)
                if cached_uid is not None:
                    if isinstance(cached_uid, (bytes, bytearray)):
                        cached_uid = cached_uid.decode("utf-8", "ignore")
                    return int(str(cached_uid).strip())
            except Exception:
                logger.debug("mention membership: user_map lookup failed chat=%s uname=%s", chat_id, uname, exc_info=True)

            try:
                resolved_chat = await bot.get_chat(f"@{uname}")
                resolved_id = int(getattr(resolved_chat, "id", 0) or 0)
                return resolved_id if resolved_id else None
            except Exception:
                logger.debug("mention membership: get_chat failed chat=%s uname=%s", chat_id, uname, exc_info=True)
                return None

        async def _is_member_or_unknown(member_id: int | None) -> bool:
            if not member_id:
                return True
            try:
                member = await bot.get_chat_member(chat_id, int(member_id))
                status = getattr(member, "status", None)
                if hasattr(status, "value"):
                    status = status.value
                return str(status or "").strip().lower() not in {"left", "kicked"}
            except Exception as exc:
                err = str(exc).lower()
                if any(token in err for token in ("user not found", "not participant", "participant")):
                    return False
                logger.warning(
                    "mention membership: get_chat_member transient failure chat=%s user_id=%s error=%s",
                    chat_id,
                    member_id,
                    exc,
                )
                return True

        for e in ents:
            member_id = await _resolve_mention_member_id(e)
            if member_id is None:
                continue
            if not await _is_member_or_unknown(member_id):
                await _flag(
                    chat_id,
                    message.message_id,
                    action="delete",
                    reason=_ctx_reason("mention_non_member"),
                    user_id=int(getattr(u, "id", 0) or 0),
                )
                return await _delete_and_handle("mention_non_member")

    if not settings.MODERATION_ALLOW_MENTIONS:
        for e in ents:
            t = e.type.value if hasattr(e.type, "value") else e.type
            if t in ("mention", "text_mention"):
                await _flag(chat_id, message.message_id, action="delete", reason=_ctx_reason("mention"), user_id=u.id)
                return await _delete_and_handle("mention")

    custom_emoji_threshold = int(getattr(settings, "MODERATION_CUSTOM_EMOJI_SPAM_THRESHOLD", 12) or 0)
    if custom_emoji_threshold > 0:
        custom_emoji_count = sum(
            1
            for e in ents
            if str(e.type.value if hasattr(e.type, "value") else e.type).lower() == "custom_emoji"
        )
        if custom_emoji_count >= custom_emoji_threshold:
            await _flag(chat_id, message.message_id, action="delete", reason=_ctx_reason("custom_emoji_spam"), user_id=u.id)
            return await _delete_and_handle("custom_emoji_spam")

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
