#app/bot/handlers/group.py
from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import random
import re
import time as time_module

from datetime import datetime, timedelta, time, timezone
from typing import Any, List
from zoneinfo import ZoneInfo

from redis.exceptions import RedisError

from aiogram import F, types
from aiogram.enums import ChatType, ContentType, MessageEntityType
from aiogram.types import Message

from app.bot.components.dispatcher import dp
import app.bot.components.constants as consts
from app.bot.components.constants import redis_client
from app.bot.handlers.moderation import apply_moderation_filters, is_from_linked_channel
from app.bot.handlers.moderation_context import resolve_message_moderation_context
from app.bot.i18n import t
from app.bot.utils.debouncer import buffer_message_for_response
from app.bot.utils.telegram_safe import delete_message_safe, send_message_safe
from app.clients.telegram_client import get_bot
from app.config import settings
from app.core.memory import MEMORY_TTL, append_group_recent, inc_msg_count, push_group_stm, record_activity
from app.services.addons.analytics import record_user_message
from app.services.addons.passive_moderation import split_context_text
from app.tasks.battle import battle_launch_task
from app.tasks.media import preprocess_group_image
from app.tasks.moderation import passive_moderate, prepare_moderation_payload

logger = logging.getLogger(__name__)

_TRUSTED_DISCUSSION_CHAT_IDS: set[int] = set()
bot = get_bot()

if not getattr(consts, "BOT_USERNAME", None):
    logger.warning("BOT_USERNAME is not set; commands with @target may be treated as addressed to other bots.")

# ---------------------------
# Settings / constants
# ---------------------------
MAX_DOCUMENT_BYTES = int(getattr(settings, "MAX_DOC_IMAGE_BYTES", 30 * 1024 * 1024))

BATTLE_CMD_RE = re.compile(r"(^|\s)/battle(?:@[A-Za-z0-9_]{3,})?(?=\s|$|[.,!?])", re.IGNORECASE)
IS_MENTION_RE = re.compile(r"(?<!\S)@\w+\b")

BATTLE_ENQUEUE_DEDUP_TTL_SECONDS = 20


# ---------------------------
# i18n wrapper (group uses chat_id)
# ---------------------------
async def tr(chat_id: int, key: str, default: str = "", **kwargs: Any) -> str:
    try:
        s = await t(chat_id, key, **kwargs)
        return s or default
    except Exception:
        return default


# ---------------------------
# Context payload (unified with PM/worker)
# ---------------------------
def _mk_ctx_payload(
    role: str,
    text: str,
    *,
    speaker_id: int | None = None,
    source: str | None = None,   # "user" | "channel" (optional; safe extra field)
) -> str:
    r = (role or "").strip().lower()
    if r not in ("user", "assistant", "system"):
        r = "user"
    payload: dict[str, Any] = {"role": r, "text": (text or "").strip()}
    if speaker_id is not None:
        try:
            payload["speaker_id"] = int(speaker_id)
        except Exception:
            pass
    if source:
        payload["source"] = str(source)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


# ---------------------------
# Small utilities / guards
# ---------------------------
def is_single_media(message: Message) -> bool:
    return message.media_group_id is None


def _is_chat_allowed(chat: types.Chat) -> bool:
    try:
        cid = int(chat.id)
    except Exception:
        return False
    allowed_ids = set(getattr(settings, "ALLOWED_GROUP_IDS", []) or [])
    return cid in allowed_ids


async def _is_message_allowed_for_group_handlers(message: Message) -> bool:
    if _is_chat_allowed(message.chat):
        return True

    if not bool(getattr(settings, "COMMENT_MODERATION_ENABLED", False)):
        return False

    target_ids = set(getattr(settings, "COMMENT_TARGET_CHAT_IDS", []) or [])
    with contextlib.suppress(Exception):
        if int(message.chat.id) in target_ids:
            return True

    with contextlib.suppress(Exception):
        if int(message.chat.id) in _TRUSTED_DISCUSSION_CHAT_IDS:
            return True

    source_ids = set(getattr(settings, "COMMENT_SOURCE_CHANNEL_IDS", []) or [])
    if not source_ids:
        return False

    linked_chat_id = None
    with contextlib.suppress(Exception):
        linked_chat_id = getattr(message.chat, "linked_chat_id", None)

    candidate_source_ids: set[int] = set()
    sender_chat = getattr(message, "sender_chat", None)
    if sender_chat and getattr(sender_chat, "type", None) == ChatType.CHANNEL:
        with contextlib.suppress(Exception):
            candidate_source_ids.add(int(sender_chat.id))

    forward_from_chat = getattr(message, "forward_from_chat", None)
    if forward_from_chat and getattr(forward_from_chat, "type", None) == ChatType.CHANNEL:
        with contextlib.suppress(Exception):
            candidate_source_ids.add(int(forward_from_chat.id))

    with contextlib.suppress(Exception):
        if linked_chat_id is not None:
            candidate_source_ids.add(int(linked_chat_id))

    if candidate_source_ids & source_ids:
        with contextlib.suppress(Exception):
            _TRUSTED_DISCUSSION_CHAT_IDS.add(int(message.chat.id))
        return True

    return False


async def _first_delivery(chat_id: int, msg_id: int, kind: str, ttl: int = 43_200) -> bool:
    try:
        seen = await redis_client.set(f"seen:{chat_id}:{msg_id}", 1, nx=True, ex=ttl)
        if not seen:
            logger.info("Drop duplicate group delivery kind=%s chat=%s msg_id=%s", kind, chat_id, msg_id)
            return False
    except Exception:
        logger.exception("failed to set seen-key in group kind=%s", kind)
    return True


def _get_default_tz():
    try:
        return ZoneInfo(settings.DEFAULT_TZ or "UTC")
    except Exception:
        return timezone.utc


def _tz_name() -> str:
    try:
        return settings.DEFAULT_TZ or "UTC"
    except Exception:
        return "UTC"


def _now_local_str() -> str:
    try:
        return datetime.now(timezone.utc).astimezone(_get_default_tz()).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d %H:%M")


def _is_effectively_empty(s: str) -> bool:
    txt = IS_MENTION_RE.sub(" ", (s or ""))
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt == ""


async def _chat_has_active_generation(chat_id: int) -> bool:
    try:
        busy = await consts.redis_queue.get(f"chatbusy:{int(chat_id)}")
    except Exception:
        return False
    return bool(busy)


def _extract_entities(message: types.Message) -> List[dict]:
    raw_ents = (message.entities or []) + (message.caption_entities or [])
    out: List[dict] = []
    for e in raw_ents:
        try:
            etype = e.type.value if hasattr(e.type, "value") else e.type
            item = {"offset": e.offset, "length": e.length, "type": etype}
            if str(etype).lower() == "text_link" and getattr(e, "url", None):
                item["url"] = e.url
            out.append(item)
        except Exception:
            continue
    return out


def _is_reply_to_our_bot(message: Message) -> bool:
    reply_from = getattr(getattr(message, "reply_to_message", None), "from_user", None)
    if not reply_from:
        return False

    bot_id = getattr(consts, "BOT_ID", None)
    if bot_id:
        with contextlib.suppress(Exception):
            if int(getattr(reply_from, "id", 0) or 0) == int(bot_id):
                return True

    expected = (getattr(consts, "BOT_USERNAME", "") or "").lstrip("@").lower()
    reply_username = (getattr(reply_from, "username", "") or "").lstrip("@").lower()
    if expected and reply_username and reply_username == expected:
        return bool(getattr(reply_from, "is_bot", False))

    return False


def _is_mention(message: types.Message) -> bool:
    expected = (consts.BOT_USERNAME or "").lstrip("@").lower()
    raw = (message.text or message.caption or "")
    entities = (message.entities or []) + (message.caption_entities or [])

    for ent in entities:
        try:
            if ent.type == MessageEntityType.MENTION:
                mention = raw[ent.offset : ent.offset + ent.length]
                if mention.lstrip("@").lower() == expected:
                    return True
            if ent.type == MessageEntityType.TEXT_MENTION and ent.user:
                bot_id = getattr(consts, "BOT_ID", None)
                if bot_id and int(ent.user.id) == int(bot_id):
                    return True
        except Exception:
            continue

    if expected:
        raw_low = raw.lower()
        raw_low = re.sub(r"https?://\S+|\S+@\S+", " ", raw_low)
        if re.search(rf"(^|[^/\w])@{re.escape(expected)}(?!\w)", raw_low):
            return True

    if _is_reply_to_our_bot(message):
        return True

    return False


def _is_bot_command_to_us(message: types.Message, name: str) -> bool:
    raw = (message.text or message.caption or "") or ""
    if not raw:
        return False
    entities = (message.entities or []) + (message.caption_entities or [])
    for ent in entities:
        try:
            if ent.type == MessageEntityType.BOT_COMMAND:
                token = raw[ent.offset : ent.offset + ent.length]
                parts = token.split("@", 1)
                cmd = parts[0].lstrip("/").lower()
                target = (parts[1] if len(parts) > 1 else "").lower()
                my_username = (consts.BOT_USERNAME or "").lstrip("@").lower()
                if cmd == name and (not target or (my_username and target == my_username)):
                    return True
        except Exception:
            continue
    return False


def _is_cmd_addressed_to_other_bot(message: types.Message, name: str) -> bool:
    raw = (message.text or message.caption or "") or ""
    if not raw:
        return False
    entities = (message.entities or []) + (message.caption_entities or [])
    for ent in entities:
        try:
            if ent.type == MessageEntityType.BOT_COMMAND:
                token = raw[ent.offset : ent.offset + ent.length]
                parts = token.split("@", 1)
                cmd = parts[0].lstrip("/").lower()
                target = (parts[1] if len(parts) > 1 else "").lower()
                if cmd == name and target:
                    my_username = (consts.BOT_USERNAME or "").lstrip("@").lower()
                    if not my_username:
                        return True
                    return target != my_username
        except Exception:
            continue
    return False


def _is_actionable_battle_command(message: types.Message) -> bool:
    if _is_bot_command_to_us(message, "battle"):
        return True
    raw = (message.text or message.caption or "") or ""
    if not raw:
        return False
    if not BATTLE_CMD_RE.search(raw):
        return False
    return not _is_cmd_addressed_to_other_bot(message, "battle")


def _mentions_other_user(message: types.Message) -> bool:
    raw = message.text or message.caption or ""
    if not raw:
        return False

    entities = (message.entities or []) + (message.caption_entities or [])
    my_id = None
    try:
        my_id = int(consts.BOT_ID)
    except Exception:
        pass
    my_un = (consts.BOT_USERNAME or "").lstrip("@").lower()

    for ent in entities:
        try:
            if ent.type == MessageEntityType.TEXT_MENTION and ent.user:
                if not ent.user.is_bot and (my_id is None or int(ent.user.id) != my_id):
                    return True
            if ent.type == MessageEntityType.MENTION:
                uname = raw[ent.offset + 1 : ent.offset + ent.length].lstrip("@").lower()
                if my_un and uname == my_un:
                    continue
                return True
        except Exception:
            continue
    return False


def _resolve_autoreply_trigger(
    *,
    is_channel: bool,
    mentioned: bool,
    mentions_other: bool,
    has_content_signal: bool,
    is_battle_cmd_to_us: bool,
    autoreply_on_topic: bool,
) -> str | None:
    if is_channel:
        return "channel_post"
    if is_battle_cmd_to_us:
        return "mention"
    if mentioned:
        return "mention"
    if mentions_other:
        return None
    if autoreply_on_topic and has_content_signal:
        return "check_on_topic"
    return None


def _is_channel_post(message: Message) -> bool:
    return bool(
        (message.sender_chat and message.sender_chat.type == ChatType.CHANNEL)
        or (message.forward_from_chat and message.forward_from_chat.type == ChatType.CHANNEL)
    )


def _channel_obj(message: Message):
    return message.sender_chat or message.forward_from_chat


def _user_id_val(message: Message, is_channel: bool) -> int:
    ch = _channel_obj(message)
    if is_channel and ch and getattr(ch, "id", None):
        return int(ch.id)
    if message.from_user and getattr(message.from_user, "id", None):
        return int(message.from_user.id)
    return int(message.chat.id)

def _replied_to_our_bot(message: Message) -> bool:
    return _is_reply_to_our_bot(message)

async def _update_presence(cid: int, message: Message) -> None:
    username = None
    if message.from_user:
        username = (message.from_user.username or str(message.from_user.id)).lower()

    try:
        async with redis_client.pipeline(transaction=True) as pipe:
            if message.from_user and message.from_user.username:
                pipe.hset(f"user_map:{cid}", mapping={message.from_user.username.lower(): message.from_user.id})
            pipe.expire(f"user_map:{cid}", MEMORY_TTL)
            pipe.set(f"last_message_ts:{cid}", time_module.time())
            if username:
                pipe.sadd(f"chat:{cid}:active_users", username)
            pipe.expire(f"chat:{cid}:active_users", MEMORY_TTL)
            pipe.expire(f"last_message_ts:{cid}", settings.GROUP_PING_ACTIVE_TTL_SECONDS)
            await pipe.execute()
    except asyncio.TimeoutError:
        logger.error("Redis pipeline timeout in group handler")
    except Exception:
        logger.debug("Redis pipeline failed in group handler", exc_info=True)


def _reply_gate_requires_mention(message: Message) -> bool:
    if message.reply_to_message and message.reply_to_message.from_user:
        if _is_reply_to_our_bot(message):
            return False
        return not _is_mention(message)
    return False


def _is_clean_message_for_on_topic(message: Message, *, mentioned: bool, mentions_other: bool) -> bool:
    if message.reply_to_message is not None:
        return False
    if mentioned or mentions_other:
        return False
    return True


async def _ensure_daily_limit(cid: int, reply_to: int | None) -> bool:
    today = datetime.now(tz=_get_default_tz()).date()
    key = f"daily:{cid}:{today}"
    reset_dt = datetime.combine(today + timedelta(days=1), time.min, tzinfo=_get_default_tz())

    used = await redis_client.incr(key)
    if used == 1:
        await redis_client.expireat(key, int(reset_dt.timestamp()))

    if used > settings.GROUP_DAILY_LIMIT:
        reset_date = (today + timedelta(days=1)).strftime("%d.%m.%Y")
        reset_at = f"{reset_date} 00:00 {_tz_name()}"
        phrase = (
            random.choice(settings.LIMIT_EXHAUSTED_PHRASES)
            if getattr(settings, "LIMIT_EXHAUSTED_PHRASES", None)
            else "Daily limit reached."
        )
        await send_message_safe(
            bot,
            cid,
            f"{phrase} (resets at {reset_at})",
            reply_to_message_id=reply_to,
            parse_mode="HTML",
        )
        return False

    return True


async def _store_context(
    cid: int,
    msg_id: int,
    text: str,
    *,
    role: str = "user",
    speaker_id: int | None = None,
    source: str | None = None,
) -> None:
    asyncio.create_task(inc_msg_count(cid))
    await redis_client.set(
        f"msg:{cid}:{msg_id}",
        _mk_ctx_payload(role, text, speaker_id=speaker_id, source=source),
        ex=getattr(settings, "REPLY_CONTEXT_TTL_SEC", 86400),
    )

async def _store_quote_context(
    cid: int,
    msg_id: int,
    text: str,
    *,
    role: str = "assistant",
    speaker_id: int | None = None,
    source: str | None = None,
) -> None:
    if not msg_id:
        return
    txt = (text or "").strip()
    if not txt:
        return
    try:
        await redis_client.set(
            f"msg:{cid}:{int(msg_id)}",
            _mk_ctx_payload(role, txt, speaker_id=speaker_id, source=source),
            ex=getattr(settings, "REPLY_CONTEXT_TTL_SEC", 86400),
        )
    except Exception:
        pass

def _analytics_best_effort(
    cid: int,
    message: Message,
    *,
    content_type: str,
    addressed_to_bot: bool,
    has_link: bool,
    is_channel: bool,
) -> None:
    try:
        ch = _channel_obj(message)
        actor_name = (
            ch.title
            if (is_channel and ch)
            else (
                (message.from_user.full_name or message.from_user.username or str(getattr(message.from_user, "id", "")))
                if message.from_user
                else None
            )
        )
        actor_id = int(ch.id) if (is_channel and ch) else int(getattr(message.from_user, "id", cid) if message.from_user else cid)

        asyncio.create_task(
            record_user_message(
                cid,
                actor_id,
                display_name=(actor_name or "")[:128],
                content_type=content_type,
                addressed_to_bot=bool(addressed_to_bot),
                has_link=bool(has_link),
            )
        )
    except Exception:
        logger.debug("analytics(record_user_message) failed", exc_info=True)


async def _maybe_log_channel_post(
    cid: int,
    message: Message,
    raw_text: str,
    ents: List[dict],
) -> bool:
    # returns False if must stop (not linked), True otherwise
    try:
        if not await is_from_linked_channel(message):
            return False
    except Exception:
        logger.exception("linked-channel check failed")
        return False

    try:
        _, log_text = split_context_text(raw_text, ents, allow_web=False)
        channel_log = {
            "text": log_text,
            "message_id": message.message_id,
            "timestamp": time_module.time(),
            "local": f"{_now_local_str()} {_tz_name()}",
        }
        await redis_client.lpush(f"mem:g:{cid}:channel_posts", json.dumps(channel_log, ensure_ascii=False))
        await redis_client.expire(f"mem:g:{cid}:channel_posts", MEMORY_TTL)
    except Exception:
        logger.debug("channel post log failed", exc_info=True)

    return True


async def _push_group_stm_and_recent(
    cid: int,
    *,
    trigger: str,
    is_channel: bool,
    user_id_val: int,
    text_for_stm: str,
    text_for_recent: str,
) -> None:
    try:
        role = "channel" if is_channel else "user"
        if (text_for_stm or "").strip():
            asyncio.create_task(push_group_stm(cid, role, text_for_stm, user_id=user_id_val))
    except Exception:
        logger.debug("push_group_stm failed", exc_info=True)

    try:
        if trigger in ("mention", "check_on_topic", "channel_post"):
            line = f"[{int(time_module.time())}] [u:{user_id_val}] {text_for_recent}"
            asyncio.create_task(append_group_recent(cid, [line]))
    except Exception:
        logger.debug("append_group_recent failed", exc_info=True)


async def _resolve_group_comment_context(message: Message) -> bool:
    from_linked = False
    with contextlib.suppress(Exception):
        from_linked = await is_from_linked_channel(message)
    return resolve_message_moderation_context(message, from_linked=from_linked) == "comment"


def _is_trusted_scope_repost(message: Message) -> bool:
    """True when a repost is routed between trusted chats/channels scopes."""
    trusted_chat_ids = {
        int(x)
        for x in (
            *(getattr(settings, "ALLOWED_GROUP_IDS", []) or []),
            *(getattr(settings, "COMMENT_TARGET_CHAT_IDS", []) or []),
        )
    }
    trusted_source_channel_ids = {
        int(x) for x in (getattr(settings, "COMMENT_SOURCE_CHANNEL_IDS", []) or [])
    }
    trusted_scope_ids = trusted_chat_ids | trusted_source_channel_ids

    with contextlib.suppress(Exception):
        chat_id = int(message.chat.id)
        if chat_id not in trusted_scope_ids:
            return False

    try:
        sc = getattr(message, "sender_chat", None)
        if sc and int(sc.id) == int(message.chat.id) and getattr(sc, "type", None) in (ChatType.GROUP, ChatType.SUPERGROUP):
            return False
    except Exception:
        pass

    source_scope_ids: set[int] = set()
    for field in ("sender_chat", "forward_from_chat"):
        src = getattr(message, field, None)
        if src and getattr(src, "type", None) in (ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP):
            with contextlib.suppress(Exception):
                source_scope_ids.add(int(src.id))

    if not source_scope_ids:
        return False

    return bool(source_scope_ids & trusted_scope_ids)


async def _log_ignored_repost_to_stm(
    message: Message,
    *,
    content_type: str,
    text: str,
    ents: List[dict],
    is_channel: bool,
) -> None:
    cid = int(message.chat.id)
    model_text, log_text = split_context_text(text, ents, allow_web=False)
    if not (log_text or "").strip():
        log_text = f"[ignored trusted repost: {content_type}]"
        model_text = log_text

    user_id_val = _user_id_val(message, is_channel)
    await _store_context(
        cid,
        message.message_id,
        log_text,
        role=("channel" if is_channel else "user"),
        speaker_id=user_id_val,
        source=("channel" if is_channel else "user"),
    )
    await _push_group_stm_and_recent(
        cid,
        trigger="channel_post",
        is_channel=is_channel,
        user_id_val=user_id_val,
        text_for_stm=(model_text or "").strip(),
        text_for_recent=(log_text or "").strip(),
    )


def _dispatch_passive_moderation(
    message: Message,
    payload: dict,
    *,
    text: str,
    ents: List[dict],
    is_channel: bool,
    user_id_val: int,
    is_comment_context: bool,
    trusted_repost: bool = False,
) -> None:

    moderation_payload = prepare_moderation_payload(
        {
            "chat_id": message.chat.id,
            "user_id": user_id_val,
            "message_id": message.message_id,
            "text": text,
            "entities": ents,
            "image_b64": payload.get("image_b64"),
            "image_mime": payload.get("image_mime"),
            "source": "channel" if is_channel else "user",
            "is_comment_context": is_comment_context,
            "trusted_repost": bool(trusted_repost),
            "chat_title": getattr(message.chat, "title", None),
        },
        context="group.dispatch",
    )
    passive_moderate.delay(moderation_payload)


# ---------------------------
# Image helpers
# ---------------------------
async def localized_group_image_error(chat_id: int, reason: str, reply_to: int | None) -> None:
    safe_reason = html.escape(reason or "", quote=True)
    msg = await tr(
        chat_id,
        "errors.image_generic_group",
        f"⚠️ Cannot process image: {safe_reason}",
        reason=safe_reason,
    )
    await send_message_safe(bot, chat_id, msg, parse_mode="HTML", reply_to_message_id=reply_to)


def reject_image_and_reply(chat_id: int, reason: str, reply_to: int | None = None) -> None:
    asyncio.create_task(localized_group_image_error(chat_id, reason, reply_to))


def _doc_suffix(mime_lower: str) -> str:
    if mime_lower in ("image/jpeg", "image/jpg", "image/pjpeg"):
        return ".jpg"
    if mime_lower == "image/webp":
        return ".webp"
    if mime_lower in ("image/png", "image/x-png"):
        return ".png"
    return ".png"


# ---------------------------
# Battle helpers
# ---------------------------
async def _resolve_battle_opponent_id(message: types.Message) -> int | None:
    def _norm_uname(u: str | None) -> str:
        return (u or "").lstrip("@").strip().lower()

    if message.reply_to_message and message.reply_to_message.from_user and not message.reply_to_message.from_user.is_bot:
        return int(message.reply_to_message.from_user.id)

    raw = message.text or message.caption or ""
    entities = (message.entities or []) + (message.caption_entities or [])
    entities_sorted = sorted(entities, key=lambda e: e.offset)

    cmd_m = BATTLE_CMD_RE.search(raw or "")
    if cmd_m:
        cmd_end = cmd_m.end()
        i = cmd_end
        while i < len(raw) and raw[i].isspace():
            i += 1
        for ent in entities_sorted:
            if ent.offset < i:
                continue
            if ent.type == MessageEntityType.TEXT_MENTION and ent.user and not ent.user.is_bot:
                return int(ent.user.id)
            if ent.type == MessageEntityType.MENTION:
                uname = _norm_uname(raw[ent.offset + 1 : ent.offset + ent.length])
                my_username = (consts.BOT_USERNAME or "").lstrip("@").lower()
                if my_username and uname == my_username:
                    continue
                cached = await redis_client.hget(f"user_map:{message.chat.id}", uname)
                if cached:
                    try:
                        if isinstance(cached, (bytes, bytearray)):
                            cached = cached.decode()
                        return int(cached)
                    except Exception:
                        pass
                try:
                    chat = await bot.get_chat(f"@{uname}")
                    if chat and chat.id and chat.type != ChatType.CHANNEL:
                        return int(chat.id)
                except Exception:
                    pass

    for ent in entities_sorted:
        if ent.type == MessageEntityType.TEXT_MENTION and ent.user and not ent.user.is_bot:
            return int(ent.user.id)

    for ent in entities_sorted:
        if ent.type == MessageEntityType.MENTION:
            uname = _norm_uname(raw[ent.offset + 1 : ent.offset + ent.length])
            my_username = (consts.BOT_USERNAME or "").lstrip("@").lower()
            if my_username and uname == my_username:
                continue
            cached = await redis_client.hget(f"user_map:{message.chat.id}", uname)
            if cached:
                try:
                    if isinstance(cached, (bytes, bytearray)):
                        cached = cached.decode()
                    return int(cached)
                except Exception:
                    pass
            try:
                chat = await bot.get_chat(f"@{uname}")
                if chat and chat.id and not chat.type == ChatType.CHANNEL:
                    return int(chat.id)
            except Exception:
                pass

    return None


async def _maybe_handle_battle(message: Message, *, trigger: str) -> bool:
    # returns True if handled (and handler should return)
    if trigger != "mention":
        return False

    if not _is_actionable_battle_command(message):
        return False

    cid = message.chat.id
    challenger_id = message.from_user.id if message.from_user else None
    if not challenger_id:
        return True

    opponent_id = await _resolve_battle_opponent_id(message)
    if not opponent_id:
        opponent_id = consts.BOT_ID

    if int(opponent_id) == int(challenger_id):
        await send_message_safe(
            bot,
            cid,
            await tr(cid, "group.battle.self", "🤔 You can't challenge yourself."),
            reply_to_message_id=message.message_id,
        )
        return True

    if int(opponent_id) != consts.BOT_ID and await redis_client.sismember("battle:opt_out", str(opponent_id)):
        await send_message_safe(
            bot,
            cid,
            await tr(cid, "group.battle.opponent_opted_out", "🚫 That user has opted out of Battles."),
            reply_to_message_id=message.message_id,
        )
        return True

    if await redis_client.sismember("battle:opt_out", str(challenger_id)):
        await send_message_safe(
            bot,
            cid,
            await tr(cid, "group.battle.you_opted_out", "🚫 You opted out of Battles. DM /battle_on to opt in."),
            reply_to_message_id=message.message_id,
        )
        return True

    dedup_key = f"battle:req:{cid}:{challenger_id}:{opponent_id}"
    try:
        queued = await redis_client.set(dedup_key, 1, nx=True, ex=BATTLE_ENQUEUE_DEDUP_TTL_SECONDS)
        if not queued:
            return True
        battle_launch_task.delay(str(challenger_id), str(opponent_id), cid)
        with contextlib.suppress(Exception):
            await delete_message_safe(bot, cid, message.message_id)
    except Exception:
        await send_message_safe(
            bot,
            cid,
            await tr(
                cid,
                "group.battle.start_failed",
                "❌ Couldn't start the battle (opponent might be unavailable or an internal error occurred).",
            ),
            reply_to_message_id=message.message_id,
        )
    return True


# ---------------------------
# Handlers
# ---------------------------
@dp.message(F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]), F.content_type == ContentType.TEXT)
async def on_group_message(message: Message) -> None:
    try:
        cid = message.chat.id

        try:
            if not await _is_message_allowed_for_group_handlers(message):
                logger.info("Ignore unauthorized group chat=%s title=%r uname=%s", cid, getattr(message.chat, "title", None), getattr(message.chat, "username", None))
                return
        except Exception:
            logger.exception("Group access check failed")
            return

        if not await _first_delivery(cid, message.message_id, "text"):
            return

        # presence/user-map updates (best-effort)
        await _update_presence(cid, message)

        if message.from_user:
            asyncio.create_task(record_activity(cid, message.from_user.id))

        # moderation pre-guard
        try:
            if await apply_moderation_filters(cid, message):
                return
        except Exception:
            logger.exception("guard filters failed (continuing)")

        is_channel = _is_channel_post(message)

        # channel post path
        trusted_repost = _is_trusted_scope_repost(message)
        trusted_repost_logged = False
        if is_channel:
            raw_text = (message.text or message.caption or "").strip()
            ents = _extract_entities(message)
            if trusted_repost:
                await _log_ignored_repost_to_stm(
                    message,
                    content_type="text",
                    text=raw_text,
                    ents=ents,
                    is_channel=is_channel,
                )
                trusted_repost_logged = True
            else:
                # must be from linked channel; also log
                ok = await _maybe_log_channel_post(cid, message, raw_text, ents)
                if not ok:
                    return
                return

        # ignore bot users unless channel post
        if message.from_user and message.from_user.is_bot and not is_channel:
            return

        if _reply_gate_requires_mention(message):
            return

        raw_text = (message.text or message.caption or "").strip()
        ents = _extract_entities(message)

        if trusted_repost:
            if not trusted_repost_logged:
                await _log_ignored_repost_to_stm(
                    message,
                    content_type="text",
                    text=raw_text,
                    ents=ents,
                    is_channel=is_channel,
                )
            return

        model_text, log_text = split_context_text(raw_text, ents, allow_web=False)

        AUTOREPLY_ON_TOPIC = bool(getattr(settings, "GROUP_AUTOREPLY_ON_TOPIC", True))

        has_battle_cmd = bool(BATTLE_CMD_RE.search(raw_text or ""))
        is_battle_cmd_to_us = _is_bot_command_to_us(message, "battle")

        if has_battle_cmd and _is_cmd_addressed_to_other_bot(message, "battle"):
            return

        mentioned = _is_mention(message)
        mentions_other = _mentions_other_user(message)

        clean_for_on_topic = _is_clean_message_for_on_topic(
            message,
            mentioned=mentioned,
            mentions_other=mentions_other,
        )

        trigger = _resolve_autoreply_trigger(
            is_channel=is_channel,
            mentioned=mentioned,
            mentions_other=mentions_other,
            has_content_signal=bool(raw_text) and clean_for_on_topic,
            is_battle_cmd_to_us=is_battle_cmd_to_us,
            autoreply_on_topic=AUTOREPLY_ON_TOPIC,
        )
        should_moderate_passive = True

        if trigger == "check_on_topic":
            logger.info(
                "group check_on_topic: chat=%s user=%s msg_id=%s",
                cid,
                (message.from_user.id if message.from_user else None),
                message.message_id,
            )

        if not trigger:
            if not should_moderate_passive:
                return

            user_id_val = _user_id_val(message, is_channel)
            is_comment_context = await _resolve_group_comment_context(message)
            payload = {
                "chat_id": cid,
                "text": model_text,
                "user_id": user_id_val,
                "reply_to": (message.reply_to_message.message_id if message.reply_to_message else None),
                "is_group": True,
                "msg_id": message.message_id,
                "is_channel_post": is_channel,
                "is_comment_context": is_comment_context,
                "trigger": trigger,
                "enforce_on_topic": False,
                "entities": ents,
            }
            _dispatch_passive_moderation(
                message,
                payload,
                text=log_text,
                ents=ents,
                is_channel=is_channel,
                user_id_val=user_id_val,
                is_comment_context=is_comment_context,
                trusted_repost=False,
            )
            return

        if trigger == "check_on_topic" and await _chat_has_active_generation(cid):
            logger.debug(
                "group check_on_topic skipped while chat is busy: chat=%s msg_id=%s",
                cid,
                message.message_id,
            )
            return

        if trigger in ("mention", "check_on_topic") and not is_channel:
            if _is_effectively_empty(model_text):
                return

        # battle shortcut (must run before daily limit/context writes)
        if await _maybe_handle_battle(message, trigger=trigger):
            return

        if not await _ensure_daily_limit(cid, message.message_id):
            return

        reply_to_id = (message.reply_to_message.message_id if message.reply_to_message else None)
        user_id_val = _user_id_val(message, is_channel)

        if _replied_to_our_bot(message) and reply_to_id:
            quoted = (message.reply_to_message.text or message.reply_to_message.caption or "").strip()
            if quoted:
                bot_sid = None
                with contextlib.suppress(Exception):
                    bot_sid = int(getattr(consts, "BOT_ID", None) or 0) or None
                await _store_quote_context(cid, reply_to_id, quoted, role="assistant", speaker_id=bot_sid)

        await _store_context(
            cid,
            message.message_id,
            log_text,
            role="user",
            speaker_id=user_id_val,
            source=("channel" if is_channel else "user"),
        )

        channel = _channel_obj(message)
        is_comment_context = await _resolve_group_comment_context(message)

        payload = {
            "chat_id": cid,
            "text": model_text,
            "user_id": user_id_val,
            "reply_to": reply_to_id,
            "is_group": True,
            "msg_id": message.message_id,
            "is_channel_post": is_channel,
            "is_comment_context": is_comment_context,
            "channel_id": channel.id if channel else None,
            "channel_title": getattr(channel, "title", None) if channel else None,
            "trigger": trigger,
            "enforce_on_topic": (trigger == "check_on_topic"),
            "entities": ents,
        }

        if message.from_user:
            with contextlib.suppress(Exception):
                await redis_client.sadd(f"all_users:{cid}", message.from_user.id)

        await _push_group_stm_and_recent(
            cid,
            trigger=trigger,
            is_channel=is_channel,
            user_id_val=user_id_val,
            text_for_stm=(log_text or "").strip(),
            text_for_recent=(log_text or "").strip(),
        )

        has_link = any((e.get("type", "").lower() in ("url", "text_link")) for e in ents)
        _analytics_best_effort(
            cid,
            message,
            content_type="text",
            addressed_to_bot=(trigger == "mention"),
            has_link=bool(has_link),
            is_channel=is_channel,
        )

        buffer_message_for_response(payload)
        _dispatch_passive_moderation(
            message,
            payload,
            text=log_text,
            ents=ents,
            is_channel=is_channel,
            user_id_val=user_id_val,
            is_comment_context=is_comment_context,
            trusted_repost=False,
        )

    except RedisError as e:
        logger.warning("Redis error in on_group_message, skipping noncritical ops: %s", e)
    except Exception:
        logger.exception("Error in on_group_message handler")


@dp.message(F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]), F.content_type == ContentType.VOICE)
async def on_group_voice(message: Message) -> None:
    try:
        cid = message.chat.id

        try:
            if not await _is_message_allowed_for_group_handlers(message):
                logger.info("Ignore unauthorized group chat=%s title=%r uname=%s", cid, getattr(message.chat, "title", None), getattr(message.chat, "username", None))
                return
        except Exception:
            logger.exception("Group access check failed")
            return

        if not await _first_delivery(cid, message.message_id, "voice"):
            return

        # moderation filters (as in old code: pre-transcribe)
        try:
            if await apply_moderation_filters(cid, message):
                return
        except Exception:
            logger.exception("guard filters failed (voice, pre-transcribe)")

        # presence updates (best-effort)
        await _update_presence(cid, message)

        if message.from_user:
            asyncio.create_task(record_activity(cid, message.from_user.id))

        if _reply_gate_requires_mention(message):
            return

        voice_file_id = getattr(getattr(message, "voice", None), "file_id", None)
        if not voice_file_id:
            return

        is_channel = _is_channel_post(message)
        if message.from_user and message.from_user.is_bot and not is_channel:
            return
        mentioned = _is_mention(message)
        mentions_other = _mentions_other_user(message)
        AUTOREPLY_ON_TOPIC = bool(getattr(settings, "GROUP_AUTOREPLY_ON_TOPIC", True))

        trusted_repost = _is_trusted_scope_repost(message)
        if trusted_repost:
            await _log_ignored_repost_to_stm(
                message,
                content_type="voice",
                text="",
                ents=[],
                is_channel=is_channel,
            )
            return

        if is_channel:
            try:
                if not await is_from_linked_channel(message):
                    return
            except Exception:
                logger.exception("linked-channel check failed (voice)")
                return

        trigger = _resolve_autoreply_trigger(
            is_channel=is_channel,
            mentioned=mentioned,
            mentions_other=mentions_other,
            has_content_signal=False,
            is_battle_cmd_to_us=False,
            autoreply_on_topic=AUTOREPLY_ON_TOPIC,
        )

        # Voice in groups requires explicit addressing signal.
        # In the current model this is `_is_mention`, which already includes
        # replies to bot messages.

        if not trigger:
            return

        if not await _ensure_daily_limit(cid, message.message_id):
            return

        asyncio.create_task(inc_msg_count(cid))

        channel = _channel_obj(message)
        user_id_val = _user_id_val(message, is_channel)
        reply_to_id = (message.reply_to_message.message_id if message.reply_to_message else None)

        if _replied_to_our_bot(message) and reply_to_id:
            quoted = (message.reply_to_message.text or message.reply_to_message.caption or "").strip()
            if quoted:
                bot_sid = None
                with contextlib.suppress(Exception):
                    bot_sid = int(getattr(consts, "BOT_ID", None) or 0) or None
                await _store_quote_context(cid, reply_to_id, quoted, role="assistant", speaker_id=bot_sid)

        ents = _extract_entities(message)
        is_comment_context = await _resolve_group_comment_context(message)

        payload = {
            "chat_id": cid,
            "text": None,
            "user_id": user_id_val,
            "reply_to": reply_to_id,
            "is_group": True,
            "msg_id": message.message_id,
            "is_channel_post": is_channel,
            "channel_id": channel.id if channel else None,
            "channel_title": getattr(channel, "title", None) if channel else None,
            "voice_in": True,
            "voice_file_id": voice_file_id,
            "trigger": trigger,
            "enforce_on_topic": (trigger == "check_on_topic"),
            "entities": [],
        }

        if message.from_user:
            with contextlib.suppress(Exception):
                await redis_client.sadd(f"all_users:{cid}", message.from_user.id)

        has_link = any((e.get("type", "").lower() in ("url", "text_link")) for e in ents)
        _analytics_best_effort(
            cid,
            message,
            content_type="voice",
            addressed_to_bot=(trigger == "mention"),
            has_link=bool(has_link),
            is_channel=is_channel,
        )

        buffer_message_for_response(payload)
        _dispatch_passive_moderation(
            message,
            payload,
            text="",
            ents=ents,
            is_channel=is_channel,
            user_id_val=user_id_val,
            is_comment_context=is_comment_context,
            trusted_repost=False,
        )

    except RedisError as e:
        logger.warning("Redis error in on_group_voice, skipping noncritical ops: %s", e)
    except Exception:
        logger.exception("Error in on_group_voice handler")


async def _handle_group_image_message_common(
    message: Message,
    *,
    file_id: str | None,
    document_id: str | None,
    mime_type: str | None,
    suffix: str | None,
    content_type_for_analytics: str,
) -> None:
    cid = message.chat.id

    # moderation guard
    try:
        if await apply_moderation_filters(cid, message):
            return
    except Exception:
        logger.exception("guard filters failed (image)")

    if _reply_gate_requires_mention(message):
        return

    is_channel = _is_channel_post(message)
    if message.from_user and message.from_user.is_bot and not is_channel:
        return

    # channel post check
    if is_channel:
        try:
            if not await is_from_linked_channel(message):
                return
        except Exception:
            logger.exception("linked-channel check failed (image)")
            return

    caption = (message.caption or "").strip()
    ents = _extract_entities(message)

    trusted_repost = _is_trusted_scope_repost(message)
    if trusted_repost:
        await _log_ignored_repost_to_stm(
            message,
            content_type=content_type_for_analytics,
            text=caption,
            ents=ents,
            is_channel=is_channel,
        )
        return

    model_caption, log_caption = split_context_text(caption, ents, allow_web=False)

    mentioned = _is_mention(message)
    mentions_other = _mentions_other_user(message)
    AUTOREPLY_ON_TOPIC = bool(getattr(settings, "GROUP_AUTOREPLY_ON_TOPIC", True))

    trigger = _resolve_autoreply_trigger(
        is_channel=is_channel,
        mentioned=mentioned,
        mentions_other=mentions_other,
        has_content_signal=False,
        is_battle_cmd_to_us=False,
        autoreply_on_topic=AUTOREPLY_ON_TOPIC,
    )

    if not trigger:
        return

    if not is_single_media(message):
        reject_image_and_reply(cid, "albums are not supported", reply_to=message.message_id)
        return

    if not await _ensure_daily_limit(cid, message.message_id):
        return

    reply_to_id = (message.reply_to_message.message_id if message.reply_to_message else None)
    user_id_val = _user_id_val(message, is_channel)

    if _replied_to_our_bot(message) and reply_to_id:
        quoted = (message.reply_to_message.text or message.reply_to_message.caption or "").strip()
        if quoted:
            bot_sid = None
            with contextlib.suppress(Exception):
                bot_sid = int(getattr(consts, "BOT_ID", None) or 0) or None
            await _store_quote_context(cid, reply_to_id, quoted, role="assistant", speaker_id=bot_sid)

    channel = _channel_obj(message)

    is_comment_context = await _resolve_group_comment_context(message)

    preprocess_payload = {
        "chat_id": cid,
        "message_id": message.message_id,
        "user_id": user_id_val,
        "trigger": trigger,
        "reply_to": reply_to_id,
        "is_channel_post": is_channel,
        "is_comment_context": is_comment_context,
        "channel_id": channel.id if channel else None,
        "channel_title": getattr(channel, "title", None) if channel else None,
        "chat_title": getattr(message.chat, "title", None),
        "entities": ents,
        "caption": model_caption,
        "caption_log": log_caption,
        "file_id": file_id,
        "document_id": document_id,
        "mime_type": mime_type,
        "suffix": suffix,
        "enforce_on_topic": (trigger == "check_on_topic"),
        "allow_web": False,
        "content_type_for_analytics": content_type_for_analytics,
    }

    if message.from_user:
        with contextlib.suppress(Exception):
            await redis_client.sadd(f"all_users:{cid}", message.from_user.id)

    has_link = any((e.get("type", "").lower() in ("url", "text_link")) for e in ents)
    _analytics_best_effort(
        cid,
        message,
        content_type=content_type_for_analytics,
        addressed_to_bot=(trigger == "mention"),
        has_link=bool(has_link),
        is_channel=is_channel,
    )
    preprocess_group_image.delay(preprocess_payload)


@dp.message(F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]), F.content_type == ContentType.PHOTO)
async def on_group_photo(message: Message) -> None:
    try:
        cid = message.chat.id

        try:
            if not await _is_message_allowed_for_group_handlers(message):
                logger.info("Ignore unauthorized group chat=%s title=%r uname=%s", cid, getattr(message.chat, "title", None), getattr(message.chat, "username", None))
                return
        except Exception:
            logger.exception("Group access check failed")
            return

        if not await _first_delivery(cid, message.message_id, "photo"):
            return

        await _update_presence(cid, message)

        if message.from_user:
            asyncio.create_task(record_activity(cid, message.from_user.id))

        if not message.photo:
            return
        biggest = message.photo[-1]

        await _handle_group_image_message_common(
            message,
            file_id=getattr(biggest, "file_id", None),
            document_id=None,
            mime_type="image/jpeg",
            suffix=".jpg",
            content_type_for_analytics="photo",
        )

    except RedisError as e:
        logger.warning("Redis error in on_group_photo, skipping noncritical ops: %s", e)
    except Exception:
        logger.exception("Error in on_group_photo handler")


@dp.message(F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]), F.content_type == ContentType.DOCUMENT)
async def on_group_document_image(message: Message) -> None:
    try:
        cid = message.chat.id
        doc = message.document
        if not doc or not (doc.mime_type or "").startswith("image/"):
            return

        try:
            if not await _is_message_allowed_for_group_handlers(message):
                logger.info("Ignore unauthorized group chat=%s title=%r uname=%s", cid, getattr(message.chat, "title", None), getattr(message.chat, "username", None))
                return
        except Exception:
            logger.exception("Group access check failed")
            return

        if not await _first_delivery(cid, message.message_id, "doc-image"):
            return

        await _update_presence(cid, message)

        if message.from_user:
            asyncio.create_task(record_activity(cid, message.from_user.id))

        mime_lower = (doc.mime_type or "").lower()
        allowed_mimes = {"image/jpeg", "image/jpg", "image/pjpeg", "image/png", "image/x-png", "image/webp"}
        if mime_lower not in allowed_mimes:
            reject_image_and_reply(cid, "unsupported image format", reply_to=message.message_id)
            return

        try:
            if getattr(doc, "file_size", None) and int(doc.file_size) > MAX_DOCUMENT_BYTES:
                reject_image_and_reply(cid, "входной файл слишком большой для обработки", reply_to=message.message_id)
                return
        except Exception:
            logger.debug("doc.file_size check failed", exc_info=True)

        await _handle_group_image_message_common(
            message,
            file_id=None,
            document_id=getattr(doc, "file_id", None),
            mime_type=doc.mime_type,
            suffix=_doc_suffix(mime_lower),
            content_type_for_analytics="document",
        )

    except RedisError as e:
        logger.warning("Redis error in on_group_document_image, skipping noncritical ops: %s", e)
    except Exception:
        logger.exception("Error in on_group_document_image handler")
