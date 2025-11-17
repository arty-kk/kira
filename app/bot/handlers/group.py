#app/bot/handlers/group.py
import asyncio
import logging
import random
import time as time_module
import json
import re
import tempfile
import contextlib
import os
import io
import base64
from PIL import Image, ImageOps, UnidentifiedImageError

try:
    RESAMPLING = Image.Resampling.LANCZOS
except AttributeError:
    RESAMPLING = Image.LANCZOS

from datetime import datetime, timedelta, time, timezone
from zoneinfo import ZoneInfo
from typing import List, Any
from redis.exceptions import RedisError

from aiogram import F, types
from aiogram.enums import ChatType, MessageEntityType, ContentType 
from aiogram.types import Message

from app.clients.telegram_client import get_bot
from app.bot.i18n import t
from app.bot.components.dispatcher import dp
import app.bot.components.constants as consts
from app.bot.components.constants import redis_client
from app.bot.handlers.moderation import apply_moderation_filters, is_from_linked_channel
from app.bot.utils.debouncer import buffer_message_for_response
from app.bot.utils.telegram_safe import send_message_safe, delete_message_safe
from app.core.memory import record_activity, inc_msg_count, MEMORY_TTL, append_group_recent, push_group_stm
from app.services.addons import group_battle as battle_service
from app.services.addons.passive_moderation import sanitize_for_context
from app.services.addons.analytics import record_user_message
from app.tasks.moderation import passive_moderate
from app.config import settings


logger = logging.getLogger(__name__)

bot = get_bot()

if not getattr(consts, "BOT_USERNAME", None):
    logger.warning("BOT_USERNAME is not set; commands with @target may be treated as addressed to other bots.")

_SANITIZE_CONTEXT = bool(getattr(settings, "MODERATION_SANITIZE_CONTEXT_ALWAYS", True))

_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_MAX_SIDE = 2048
_ALLOWED_FORMATS = {"JPEG","JPG","PNG","WEBP"}
_MAX_FRAMES = 1
_MAX_DOCUMENT_BYTES = int(getattr(settings, "MAX_DOC_IMAGE_BYTES", 30 * 1024 * 1024))
_MIN_JPEG_QUALITY = int(getattr(settings, "MIN_JPEG_QUALITY", 35))
_MIN_SIDE = int(getattr(settings, "MIN_IMAGE_SIDE", 720))

Image.MAX_IMAGE_PIXELS = int(getattr(settings, "MAX_IMAGE_PIXELS", 36_000_000))
_BATTLE_CMD_RE = re.compile(r"(^|\s)/battle(?:@[A-Za-z0-9_]{3,})?(?=\s|$|[.,!?])", re.IGNORECASE)
_IS_MENTION_RE = re.compile(r'(?<!\S)@\w+\b')

def _is_single_media(message: Message) -> bool:
    return message.media_group_id is None

async def _download_to_tmp(file_like: Any, suffix: str) -> str | None:
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name
        await bot.download(file_like, tmp_path)
        return tmp_path
    except Exception:
        logger.exception("Failed to download image")
        if tmp_path and os.path.exists(tmp_path):
            with contextlib.suppress(Exception):
                os.remove(tmp_path)
        return None

async def _strict_image_load(tmp_path: str) -> Image.Image:
    try:
        with Image.open(tmp_path) as im:
            fmt = (im.format or "").upper()
            if fmt == "JPG": fmt = "JPEG"
            if fmt not in _ALLOWED_FORMATS:
                raise ValueError(f"Unsupported image format: {fmt}")
            im.verify()
        with Image.open(tmp_path) as im2:
            im2.load()
            try:
                im2 = ImageOps.exif_transpose(im2)
            except Exception:
                pass
            return im2.copy()
    except UnidentifiedImageError:
        raise ValueError("Not an image or corrupted file")
    except Image.DecompressionBombError:
        raise ValueError("Image too large (decompression bomb)")
    except Exception as e:
        raise ValueError(str(e))

def _sanitize_and_compress(img: Image.Image) -> bytes:
    n_frames = int(getattr(img, "n_frames", 1) or 1)
    if n_frames > _MAX_FRAMES:
        raise ValueError("Animated or multi-frame images are not allowed")

    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        alpha = img.split()[-1]
        bg.paste(img, mask=alpha)
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")

    w, h = img.size
    if max(w, h) > _MAX_SIDE:
        s = _MAX_SIDE / float(max(w, h))
        img = img.resize((int(w*s), int(h*s)), resample=RESAMPLING)

    def _save_as_jpeg(jimg: Image.Image, q: int) -> bytes:
        buf = io.BytesIO()
        try:
            jimg.save(
                buf,
                format="JPEG",
                quality=q, optimize=True,
                progressive=True, subsampling=2,
                exif=b""
            )
        except OSError:
            try:
                buf.seek(0); buf.truncate(0)
                jimg.save(
                    buf,
                    format="JPEG",
                    quality=q, optimize=True,
                    progressive=False, subsampling=2,
                    exif=b""
                )
            except OSError:
                buf.seek(0); buf.truncate(0)
                jimg.save(
                    buf,
                    format="JPEG", quality=q,
                    progressive=False, subsampling=2,
                    exif=b""
                )
        return buf.getvalue()

    quality_steps = [85, 80, 75, 70, 65, 60, 55, 50, 45, 40, _MIN_JPEG_QUALITY]
    for _ in range(6):
        for q in quality_steps:
            data = _save_as_jpeg(img, q)
            if len(data) <= _MAX_IMAGE_BYTES:
                return data
        cur_max = max(img.size)
        if cur_max <= _MIN_SIDE:
            break
        new_max = max(_MIN_SIDE, int(cur_max * 0.85))
        s = new_max / float(cur_max)
        img = img.resize((max(1, int(img.size[0]*s)), max(1, int(img.size[1]*s))), resample=RESAMPLING)

    img = img.resize((max(1, img.size[0]//2), max(1, img.size[1]//2)), resample=RESAMPLING)
    data = _save_as_jpeg(img, max(60, _MIN_JPEG_QUALITY))
    if len(data) > _MAX_IMAGE_BYTES:
        raise ValueError("Image too large after compression")
    return data

async def _localized_group_image_error(chat_id: int, reason: str, reply_to: int | None):
    try:
        msg = await t(chat_id, "errors.image_generic", reason=reason)
    except Exception:
        msg = f"⚠️ Cannot process image: {reason}\nPlease send exactly one image (≤ 5 MB) in a single message."
    await send_message_safe(
        bot,
        chat_id,
        msg,
        parse_mode="HTML",
        reply_to_message_id=reply_to,
    )

def _reject_multi_or_oversize_and_reply(chat_id: int, reason: str, reply_to: int | None = None):
    asyncio.create_task(_localized_group_image_error(chat_id, reason, reply_to))

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

def _is_mention(message: types.Message) -> bool:

    if not (consts.BOT_USERNAME and consts.BOT_ID):
        return False
    expected = (consts.BOT_USERNAME or "").lstrip("@").lower()
    raw = (message.text or message.caption or "")
    entities = (message.entities or []) + (message.caption_entities or [])
    for ent in entities:
        try:
            if ent.type == MessageEntityType.MENTION:
                mention = raw[ent.offset: ent.offset + ent.length]
                if mention.lstrip("@").lower() == expected:
                    return True
            if ent.type == MessageEntityType.TEXT_MENTION and ent.user and int(ent.user.id) == int(consts.BOT_ID):
                return True
        except Exception:
            continue
    if expected:
        raw_low = raw.lower()
        raw_low = re.sub(r'https?://\S+|\S+@\S+', ' ', raw_low)
        if re.search(rf'(^|[^/\w])@{re.escape(expected)}(?!\w)', raw_low):
            return True
    if message.reply_to_message and message.reply_to_message.from_user:
        try:
            if int(message.reply_to_message.from_user.id) == int(consts.BOT_ID):
                return True
        except Exception:
            pass
    return False

def _is_bot_command_to_us(message: types.Message, name: str) -> bool:
    raw = (message.text or message.caption or "") or ""
    if not raw:
        return False
    entities = (message.entities or []) + (message.caption_entities or [])
    for ent in entities:
        try:
            if ent.type == MessageEntityType.BOT_COMMAND:
                token = raw[ent.offset: ent.offset + ent.length]
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
                token = raw[ent.offset: ent.offset + ent.length]
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


def _is_effectively_empty(s: str) -> bool:
    t = _IS_MENTION_RE.sub(' ', (s or ''))
    t = re.sub(r'\s+', ' ', t).strip()
    return t == ''

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

def _is_chat_allowed(chat: types.Chat) -> bool:
    try:
        cid = int(chat.id)
    except Exception:
        return False
    allowed_ids = set(getattr(settings, "ALLOWED_GROUP_IDS", []) or [])
    return cid in allowed_ids

async def _resolve_battle_opponent_id(message: types.Message) -> int | None:

    def _norm_uname(u: str | None) -> str:
        return (u or "").lstrip("@").strip().lower()

    if message.reply_to_message and message.reply_to_message.from_user and not message.reply_to_message.from_user.is_bot:
        return int(message.reply_to_message.from_user.id)

    raw = message.text or message.caption or ""
    entities = (message.entities or []) + (message.caption_entities or [])
    entities_sorted = sorted(entities, key=lambda e: e.offset)

    cmd_m = _BATTLE_CMD_RE.search(raw or "")
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

@dp.message(F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]), F.content_type == ContentType.TEXT)
async def on_group_message(message: Message) -> None:

    try:
        cid = message.chat.id

        try:
            if not _is_chat_allowed(message.chat):
                logger.info("Ignore unauthorized group chat=%s uname=%s", cid, getattr(message.chat, "username", None))
                return
        except Exception:
            logger.exception("Whitelist check failed")
            return
        try:
            seen = await redis_client.set(
                f"seen:{cid}:{message.message_id}",
                1,
                nx=True,
                ex=43_200,
            )
            if not seen:
                logger.info("Drop duplicate group delivery chat=%s msg_id=%s", cid, message.message_id)
                return
        except Exception:
            logger.exception("failed to set seen-key in group (early)")

        username = None
        if message.from_user:
            if message.from_user.username:
                username = message.from_user.username.lower()
            else:
                username = str(message.from_user.id)

        is_channel_post = bool(
            (message.sender_chat and message.sender_chat.type == ChatType.CHANNEL)
            or
            (message.forward_from_chat and message.forward_from_chat.type == ChatType.CHANNEL)
        )

        async with redis_client.pipeline(transaction=True) as pipe:
            if message.from_user and message.from_user.username:
                pipe.hset(f"user_map:{cid}", mapping={message.from_user.username.lower(): message.from_user.id})
                pipe.expire(f"user_map:{cid}", MEMORY_TTL)
            pipe.set(f"last_message_ts:{cid}", time_module.time())
            if username:
                pipe.sadd(f"chat:{cid}:active_users", username)
            pipe.expire(f"chat:{cid}:active_users", MEMORY_TTL)
            pipe.expire(f"last_message_ts:{cid}", settings.GROUP_PING_ACTIVE_TTL_SECONDS)
            try:
                await pipe.execute()
            except asyncio.TimeoutError:
                logger.error("Redis pipeline timeout in group handler")

        if message.from_user:
            asyncio.create_task(record_activity(cid, message.from_user.id))

        try:
            if await apply_moderation_filters(cid, message):
                return
        except Exception:
            logger.exception("guard filters failed (continuing)")

        if is_channel_post:
            try:
                if not await is_from_linked_channel(message):
                    return
            except Exception:
                logger.exception("linked-channel check failed")
                return

            _cp_raw = (message.text or message.caption or "").strip()
            _cp_entities = _extract_entities(message)
            channel_log = {
                "text": sanitize_for_context(_cp_raw, _cp_entities) if _SANITIZE_CONTEXT else _cp_raw,
                "message_id": message.message_id,
                "timestamp": time_module.time(),
                "local": f"{_now_local_str()} {_tz_name()}",
            }
            await redis_client.lpush(
                f"mem:g:{cid}:channel_posts",
                json.dumps(channel_log, ensure_ascii=False),
            )
            await redis_client.expire(
                f"mem:g:{cid}:channel_posts",
                MEMORY_TTL,
            )

        if message.from_user and message.from_user.is_bot and not is_channel_post:
            return

        if message.reply_to_message and message.reply_to_message.from_user:
            if int(message.reply_to_message.from_user.id) != int(consts.BOT_ID) and not _is_mention(message):
                return

        raw_text = (message.text or message.caption or "").strip()
        ents = _extract_entities(message)
        ctx_text = sanitize_for_context(raw_text, ents) if _SANITIZE_CONTEXT else raw_text

        AUTOREPLY_ON_TOPIC = bool(getattr(settings, "GROUP_AUTOREPLY_ON_TOPIC", True))

        has_battle_cmd = bool(_BATTLE_CMD_RE.search(raw_text or ""))
        is_battle_cmd_to_us = _is_bot_command_to_us(message, "battle")

        trigger = None
        mentioned = _is_mention(message)
        mentions_other = _mentions_other_user(message)
        if is_channel_post:
            trigger = "channel_post"
        elif mentioned or is_battle_cmd_to_us:
            trigger = "mention"
        else:
            if mentions_other:
                return
            if AUTOREPLY_ON_TOPIC and raw_text and not mentioned and not mentions_other and not is_battle_cmd_to_us:
                trigger = "check_on_topic"
                logger.info(
                    "group check_on_topic: chat=%s user=%s msg_id=%s",
                    cid,
                    (message.from_user.id if message.from_user else None),
                    message.message_id
                )
        logger.debug(
            "group trigger=%s mentioned=%s channel_post=%s",
            trigger, mentioned, is_channel_post
        )

        if not trigger:
            return

        if trigger in ("mention", "check_on_topic") and not is_channel_post:
            if _is_effectively_empty(ctx_text):
                return

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
                bot, cid,
                f"{phrase} (resets at {reset_at})",
                reply_to_message_id=message.message_id,
                parse_mode="HTML",
            )
            return

        asyncio.create_task(inc_msg_count(cid))
        await redis_client.set(
            f"msg:{cid}:{message.message_id}",
            ctx_text,
            ex=getattr(settings, "REPLY_CONTEXT_TTL_SEC", 86400)
        )

        channel = message.sender_chat or message.forward_from_chat
        reply_to_id = (message.reply_to_message.message_id if message.reply_to_message else None)
        user_id_val = (channel.id if (is_channel_post and channel) else (message.from_user.id if message.from_user else cid))

        if trigger == "mention" and (
            is_battle_cmd_to_us
            or (has_battle_cmd and not _is_cmd_addressed_to_other_bot(message, "battle"))
        ):
            challenger_id = message.from_user.id if message.from_user else None
            if not challenger_id:
                return
            opponent_id = await _resolve_battle_opponent_id(message)
            if not opponent_id:
                opponent_id = consts.BOT_ID
            if int(opponent_id) == int(challenger_id):
                await send_message_safe(bot, cid, "🤔 You can't challenge yourself.", reply_to_message_id=message.message_id)
                return
            if int(opponent_id) != consts.BOT_ID and await redis_client.sismember("battle:opt_out", str(opponent_id)):
                await send_message_safe(bot, cid, "🚫 That user has opted out of Battles.", reply_to_message_id=message.message_id)
                return
            if await redis_client.sismember("battle:opt_out", str(challenger_id)):
                await send_message_safe(bot, cid, "🚫 You opted out of Battles. DM /battle_on to opt in.", reply_to_message_id=message.message_id)
                return
            try:
                await battle_service.launch_battle(str(challenger_id), str(opponent_id), chat_id=cid)
                try:
                    await delete_message_safe(bot, cid, message.message_id)
                except Exception:
                    pass
            except Exception:
                await send_message_safe(
                    bot, cid,
                    "❌ Couldn't start the battle (opponent might be unavailable or an internal error occurred).",
                    reply_to_message_id=message.message_id,
                )
            return
        
        payload = {
            "chat_id": cid,
            "text": ctx_text,
            "user_id": user_id_val,
            "reply_to": reply_to_id,
            "is_group": True,
            "msg_id": message.message_id,
            "is_channel_post": is_channel_post,
            "channel_id": channel.id if channel else None,
            "channel_title": getattr(channel, "title", None) if channel else None,
            "trigger": trigger,
            "enforce_on_topic": (trigger == "check_on_topic"),
            "entities": _extract_entities(message),
        }
        if message.from_user:
            try:
                await redis_client.sadd(f"all_users:{cid}", message.from_user.id)
            except Exception:
                logger.debug("sadd all_users failed", exc_info=True)

        try:
            if (ctx_text or "").strip():
                channel = message.sender_chat or message.forward_from_chat
                user_id_val = (channel.id if (is_channel_post and channel) else (message.from_user.id if message.from_user else cid))
                role = "channel" if is_channel_post else "user"
                asyncio.create_task(push_group_stm(cid, role, ctx_text, user_id=user_id_val))
        except Exception:
            logger.debug("push_group_stm (text) failed", exc_info=True)

        try:
            if trigger in ("mention", "check_on_topic", "channel_post") and ctx_text:
                line = f"[{int(time_module.time())}] [u:{user_id_val}] {ctx_text}"
                asyncio.create_task(append_group_recent(cid, [line]))
        except Exception:
            logger.debug("append_group_recent (text) failed", exc_info=True)

        try:
            has_link = any((e.get("type","").lower() in ("url", "text_link")) for e in ents)
            actor_name = (channel.title if (is_channel_post and channel) else (
                (message.from_user.full_name or message.from_user.username or str(getattr(message.from_user, "id", ""))) if message.from_user else None
            ))
            actor_id = int(channel.id) if (is_channel_post and channel) else int(getattr(message.from_user, "id", cid))
            asyncio.create_task(
                record_user_message(
                    cid,
                    actor_id,
                    display_name=(actor_name or "")[:128],
                    content_type="text",
                    addressed_to_bot=bool(trigger == "mention"),
                    has_link=bool(has_link),
                )
            )
        except Exception:
            logger.debug("analytics(record_user_message) failed", exc_info=True)

        buffer_message_for_response(payload)

        passive_moderate.delay({
            "chat_id":   cid,
            "user_id":   user_id_val,
            "message_id": message.message_id,
            "text":      ctx_text,
            "entities":  _extract_entities(message),
            "image_b64": payload.get("image_b64"),
            "image_mime": payload.get("image_mime"),
            "source":    "channel" if is_channel_post else "user",
        })

    except RedisError as e:
        logger.warning("Redis error in on_group_message, skipping noncritical ops: %s", e)
    except Exception:
         logger.exception("Error in on_group_message handler")

@dp.message(F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]), F.content_type == ContentType.VOICE)
async def on_group_voice(message: Message) -> None:
    try:
        cid = message.chat.id

        try:
            if not _is_chat_allowed(message.chat):
                logger.info("Ignore unauthorized group chat=%s uname=%s", cid, getattr(message.chat, "username", None))
                return
        except Exception:
            logger.exception("Whitelist check failed")
            return

        try:
            seen = await redis_client.set(
                f"seen:{cid}:{message.message_id}", 1, nx=True, ex=43_200
            )
            if not seen:
                logger.info("Drop duplicate group delivery (voice) chat=%s msg_id=%s", cid, message.message_id)
                return
        except Exception:
            logger.exception("failed to set seen-key in group (voice)")

        username = None

        try:
            if await apply_moderation_filters(cid, message):
                return
        except Exception:
            logger.exception("guard filters failed (voice, pre-transcribe)")

        if message.from_user:
            username = (message.from_user.username or str(message.from_user.id)).lower()

        is_channel_post = bool(
            (message.sender_chat and message.sender_chat.type == ChatType.CHANNEL)
            or (message.forward_from_chat and message.forward_from_chat.type == ChatType.CHANNEL)
        )

        try:
            async with redis_client.pipeline(transaction=True) as pipe:
                if message.from_user and message.from_user.username:
                    pipe.hset(
                        f"user_map:{cid}",
                        mapping={message.from_user.username.lower(): message.from_user.id}
                    )
                pipe.expire(f"user_map:{cid}", MEMORY_TTL)
                pipe.set(f"last_message_ts:{cid}", time_module.time())
                if username:
                    pipe.sadd(f"chat:{cid}:active_users", username)
                pipe.expire(f"chat:{cid}:active_users", MEMORY_TTL)
                pipe.expire(f"last_message_ts:{cid}", settings.GROUP_PING_ACTIVE_TTL_SECONDS)
                await pipe.execute()
        except Exception:
            logger.debug("Redis pipeline failed in voice handler", exc_info=True)

        if message.from_user:
            asyncio.create_task(record_activity(cid, message.from_user.id))

        if message.reply_to_message and message.reply_to_message.from_user:
            if int(message.reply_to_message.from_user.id) != int(consts.BOT_ID) and not _is_mention(message):
                return

        voice_file_id = getattr(getattr(message, "voice", None), "file_id", None)
        if not voice_file_id:
            return

        AUTOREPLY_ON_TOPIC = bool(getattr(settings, "GROUP_AUTOREPLY_ON_TOPIC", True))

        trigger = None
        mentioned = _is_mention(message)

        if is_channel_post:
            try:
                if not await is_from_linked_channel(message):
                    return
            except Exception:
                logger.exception("linked-channel check failed (voice)")
                return
            trigger = "channel_post"
        elif mentioned:
            trigger = "mention"
        else:
            if AUTOREPLY_ON_TOPIC:
                trigger = "check_on_topic"

        if not trigger:
            return

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
                bot, cid,
                f"{phrase} (resets at {reset_at})",
                reply_to_message_id=message.message_id,
                parse_mode="HTML",
            )
            return

        asyncio.create_task(inc_msg_count(cid))

        channel = message.sender_chat or message.forward_from_chat
        user_id_val = (channel.id if (is_channel_post and channel) else (message.from_user.id if message.from_user else cid))
        reply_to_id = (message.reply_to_message.message_id if message.reply_to_message else None)

        payload = {
            "chat_id": cid,
            "text": None,
            "user_id": user_id_val,
            "reply_to": reply_to_id,
            "is_group": True,
            "msg_id": message.message_id,
            "is_channel_post": is_channel_post,
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

        try:
            has_link = any((e.get("type","").lower() in ("url", "text_link")) for e in _extract_entities(message))
            actor_name = (channel.title if (is_channel_post and channel) else (
                (message.from_user.full_name or message.from_user.username or str(getattr(message.from_user, "id", ""))) if message.from_user else None
            ))
            actor_id = int(channel.id) if (is_channel_post and channel) else int(getattr(message.from_user, "id", cid))
            asyncio.create_task(
                record_user_message(
                    cid,
                    actor_id,
                    display_name=(actor_name or "")[:128],
                    content_type="voice",
                    addressed_to_bot=bool(trigger == "mention"),
                    has_link=bool(has_link),
                )
            )
        except Exception:
            logger.debug("analytics(record_user_message voice) failed", exc_info=True)
            
        buffer_message_for_response(payload)

        passive_moderate.delay({
            "chat_id":   cid,
            "user_id":   user_id_val,
            "message_id": message.message_id,
            "text":       "",
            "entities":  _extract_entities(message),
            "image_b64": payload.get("image_b64"),
            "image_mime": payload.get("image_mime"),
            "source":    "channel" if is_channel_post else "user",
        })

    except RedisError as e:
        logger.warning("Redis error in on_group_voice, skipping noncritical ops: %s", e)
    except Exception:
        logger.exception("Error in on_group_voice handler")

@dp.message(F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]), F.content_type == ContentType.PHOTO)
async def on_group_photo(message: Message) -> None:
    try:
        cid = message.chat.id

        try:
            if not _is_chat_allowed(message.chat):
                logger.info("Ignore unauthorized group chat=%s uname=%s", cid, getattr(message.chat, "username", None))
                return
        except Exception:
            logger.exception("Whitelist check failed")
            return

        if not _is_single_media(message):
            _reject_multi_or_oversize_and_reply(cid, "albums are not supported", reply_to=message.message_id)
            return

        try:
            seen = await redis_client.set(f"seen:{cid}:{message.message_id}", 1, nx=True, ex=43_200)
            if not seen:
                logger.info("Drop duplicate group delivery (photo) chat=%s msg_id=%s", cid, message.message_id)
                return
        except Exception:
            logger.exception("failed to set seen-key in group (photo)")

        is_channel_post = bool(
            (message.sender_chat and message.sender_chat.type == ChatType.CHANNEL)
            or (message.forward_from_chat and message.forward_from_chat.type == ChatType.CHANNEL)
        )

        username = None
        if message.from_user:
            username = (message.from_user.username or str(message.from_user.id)).lower()
        try:
            async with redis_client.pipeline(transaction=True) as pipe:
                if message.from_user and message.from_user.username:
                    pipe.hset(
                        f"user_map:{cid}",
                        mapping={message.from_user.username.lower(): message.from_user.id}
                    )
                pipe.expire(f"user_map:{cid}", MEMORY_TTL)
                pipe.set(f"last_message_ts:{cid}", time_module.time())
                if username:
                    pipe.sadd(f"chat:{cid}:active_users", username)
                pipe.expire(f"chat:{cid}:active_users", MEMORY_TTL)
                pipe.expire(f"last_message_ts:{cid}", settings.GROUP_PING_ACTIVE_TTL_SECONDS)
                await pipe.execute()
        except Exception:
            logger.debug("Redis pipeline failed in photo handler", exc_info=True)

        if message.from_user:
            asyncio.create_task(record_activity(cid, message.from_user.id))

        try:
            if await apply_moderation_filters(cid, message):
                return
        except Exception:
            logger.exception("guard filters failed (photo)")

        if message.reply_to_message and message.reply_to_message.from_user:
            if int(message.reply_to_message.from_user.id) != int(consts.BOT_ID) and not _is_mention(message):
                return

        caption = (message.caption or "").strip()
        ents = _extract_entities(message)
        ctx_caption = sanitize_for_context(caption, ents) if _SANITIZE_CONTEXT else caption
        mentioned = _is_mention(message)
        mentions_other = _mentions_other_user(message)

        AUTOREPLY_ON_TOPIC = bool(getattr(settings, "GROUP_AUTOREPLY_ON_TOPIC", True))
        trigger = None
        if is_channel_post:
            try:
                if not await is_from_linked_channel(message):
                    return
            except Exception:
                logger.exception("linked-channel check failed (photo)")
                return
            trigger = "channel_post"
        elif mentioned:
            trigger = "mention"
        else:
            if mentions_other:
                return
            if AUTOREPLY_ON_TOPIC and caption:
                trigger = "check_on_topic"

        if not trigger:
            return

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
                bot, cid,
                f"{phrase} (resets at {reset_at})",
                reply_to_message_id=message.message_id,
                parse_mode="HTML",
            )
            return

        biggest = message.photo[-1]

        tmp_path = None
        try:
            tmp_path = await _download_to_tmp(biggest, suffix=".jpg")
            if not tmp_path:
                _reject_multi_or_oversize_and_reply(cid, "download failed", reply_to=message.message_id)
                return
            img = await _strict_image_load(tmp_path)
            safe_jpeg = _sanitize_and_compress(img)
            if len(safe_jpeg) > _MAX_IMAGE_BYTES:
                _reject_multi_or_oversize_and_reply(cid, "file is larger than 5 MB after compression", reply_to=message.message_id)
                return

            memo = ("[Image attached]" + (f" {ctx_caption}" if ctx_caption else ""))

            asyncio.create_task(inc_msg_count(cid))

            await redis_client.set(
                f"msg:{cid}:{message.message_id}",
                memo,
                ex=getattr(settings, "REPLY_CONTEXT_TTL_SEC", 86400)
            )

            channel = message.sender_chat or message.forward_from_chat
            user_id_val = (channel.id if (is_channel_post and channel) else (message.from_user.id if message.from_user else cid))
            reply_to_id = (message.reply_to_message.message_id if message.reply_to_message else None)
            payload = {
                "chat_id": cid,
                "text": ctx_caption,
                "user_id": user_id_val,
                "reply_to": reply_to_id,
                "is_group": True,
                "msg_id": message.message_id,
                "is_channel_post": is_channel_post,
                "channel_id": channel.id if channel else None,
                "channel_title": getattr(channel, "title", None) if channel else None,
                "image_b64": base64.b64encode(safe_jpeg).decode("ascii"),
                "image_mime": "image/jpeg",
                "trigger": trigger,
                "enforce_on_topic": (trigger == "check_on_topic"),
                "entities": _extract_entities(message),
            }
            if message.from_user:
                with contextlib.suppress(Exception):
                    await redis_client.sadd(f"all_users:{cid}", message.from_user.id)

            try:
                channel = message.sender_chat or message.forward_from_chat
                user_id_val = (channel.id if (is_channel_post and channel) else (message.from_user.id if message.from_user else cid))
                role = "channel" if is_channel_post else "user"
                text_for_group = (ctx_caption or "").strip()
                if not text_for_group:
                    text_for_group = "[Image attached]"
                asyncio.create_task(push_group_stm(cid, role, text_for_group, user_id=user_id_val))
            except Exception:
                logger.debug("push_group_stm (photo) failed", exc_info=True)

            try:
                if trigger in ("mention", "check_on_topic", "channel_post"):
                    channel = message.sender_chat or message.forward_from_chat
                    user_id_val = (channel.id if (is_channel_post and channel) else (message.from_user.id if message.from_user else cid))
                    text_for_recent = (ctx_caption or "[Image attached]").strip()
                    line = f"[{int(time_module.time())}] [u:{user_id_val}] {text_for_recent}"
                    asyncio.create_task(append_group_recent(cid, [line]))
            except Exception:
                logger.debug("append_group_recent (photo) failed", exc_info=True)

            try:
                has_link = any((e.get("type","").lower() in ("url", "text_link")) for e in ents)
                actor_name = (channel.title if (is_channel_post and channel) else (
                    (message.from_user.full_name or message.from_user.username or str(getattr(message.from_user, "id", ""))) if message.from_user else None
                ))
                actor_id = int(channel.id) if (is_channel_post and channel) else int(getattr(message.from_user, "id", cid))
                asyncio.create_task(
                    record_user_message(
                        cid,
                        actor_id,
                        display_name=(actor_name or "")[:128],
                        content_type="photo",
                        addressed_to_bot=bool(trigger == "mention"),
                        has_link=bool(has_link),
                    )
                )
            except Exception:
                logger.debug("analytics(record_user_message photo) failed", exc_info=True)

            buffer_message_for_response(payload)

            passive_moderate.delay({
                "chat_id":   cid,
                "user_id":   user_id_val,
                "message_id": message.message_id,
                "text":       ctx_caption,
                "entities":  _extract_entities(message),
                "image_b64": payload.get("image_b64"),
                "image_mime": payload.get("image_mime"),
                "source":    "channel" if is_channel_post else "user",
            })

        except ValueError as ve:
            logger.warning("Image validation failed (group photo): %s", ve)
            _reject_multi_or_oversize_and_reply(cid, str(ve), reply_to=message.message_id)
        except Exception:
            logger.exception("Image processing failed (group photo)")
            _reject_multi_or_oversize_and_reply(cid, "internal error", reply_to=message.message_id)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                with contextlib.suppress(Exception):
                    os.remove(tmp_path)

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
            if not _is_chat_allowed(message.chat):
                logger.info("Ignore unauthorized group chat=%s uname=%s", cid, getattr(message.chat, "username", None))
                return
        except Exception:
            logger.exception("Whitelist check failed")
            return

        mime_lower = (doc.mime_type or "").lower()
        allowed_mimes = {"image/jpeg", "image/jpg", "image/pjpeg", "image/png", "image/x-png", "image/webp"}
        if mime_lower not in allowed_mimes:
            _reject_multi_or_oversize_and_reply(cid, "unsupported image format", reply_to=message.message_id)
            return

        try:
            if getattr(doc, "file_size", None) and int(doc.file_size) > _MAX_DOCUMENT_BYTES:
                _reject_multi_or_oversize_and_reply(cid, "file is too large", reply_to=message.message_id)
                return
        except Exception:
            logger.debug("doc.file_size check failed", exc_info=True)

        if not _is_single_media(message):
            _reject_multi_or_oversize_and_reply(cid, "albums are not supported", reply_to=message.message_id)
            return

        try:
            seen = await redis_client.set(f"seen:{cid}:{message.message_id}", 1, nx=True, ex=43_200)
            if not seen:
                logger.info("Drop duplicate group delivery (doc-image) chat=%s msg_id=%s", cid, message.message_id)
                return
        except Exception:
            logger.exception("failed to set seen-key in group (doc-image)")

        is_channel_post = bool(
            (message.sender_chat and message.sender_chat.type == ChatType.CHANNEL)
            or (message.forward_from_chat and message.forward_from_chat.type == ChatType.CHANNEL)
        )

        username = None
        if message.from_user:
            username = (message.from_user.username or str(message.from_user.id)).lower()
        try:
            async with redis_client.pipeline(transaction=True) as pipe:
                if message.from_user and message.from_user.username:
                    pipe.hset(
                        f"user_map:{cid}",
                        mapping={message.from_user.username.lower(): message.from_user.id}
                    )
                pipe.expire(f"user_map:{cid}", MEMORY_TTL)
                pipe.set(f"last_message_ts:{cid}", time_module.time())
                if username:
                    pipe.sadd(f"chat:{cid}:active_users", username)
                pipe.expire(f"chat:{cid}:active_users", MEMORY_TTL)
                pipe.expire(f"last_message_ts:{cid}", settings.GROUP_PING_ACTIVE_TTL_SECONDS)
                await pipe.execute()
        except Exception:
            logger.debug("Redis pipeline failed in doc-image handler", exc_info=True)

        if message.from_user:
            asyncio.create_task(record_activity(cid, message.from_user.id))

        try:
            if await apply_moderation_filters(cid, message):
                return
        except Exception:
            logger.exception("guard filters failed (doc-image)")

        if message.reply_to_message and message.reply_to_message.from_user:
            if int(message.reply_to_message.from_user.id) != int(consts.BOT_ID) and not _is_mention(message):
                return

        caption = (message.caption or "").strip()
        ents = _extract_entities(message)
        ctx_caption = sanitize_for_context(caption, ents) if _SANITIZE_CONTEXT else caption
        mentioned = _is_mention(message)
        mentions_other = _mentions_other_user(message)

        AUTOREPLY_ON_TOPIC = bool(getattr(settings, "GROUP_AUTOREPLY_ON_TOPIC", True))
        trigger = None
        if is_channel_post:
            try:
                if not await is_from_linked_channel(message):
                    return
            except Exception:
                logger.exception("linked-channel check failed (doc-image)")
                return
            trigger = "channel_post"
        elif mentioned:
            trigger = "mention"
        else:
            if mentions_other:
                return
            if AUTOREPLY_ON_TOPIC and caption:
                trigger = "check_on_topic"

        if not trigger:
            return

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
                bot, cid,
                f"{phrase} (resets at {reset_at})",
                reply_to_message_id=message.message_id,
                parse_mode="HTML",
            )
            return

        mime = mime_lower
        if mime in ("image/jpeg", "image/jpg", "image/pjpeg"):
            suffix = ".jpg"
        elif mime == "image/webp":
            suffix = ".webp"
        elif mime in ("image/png", "image/x-png"):
            suffix = ".png"
        else:
            suffix = ".png"

        tmp_path = None
        try:
            tmp_path = await _download_to_tmp(doc, suffix=suffix)
            if not tmp_path:
                _reject_multi_or_oversize_and_reply(cid, "download failed", reply_to=message.message_id)
                return
            img = await _strict_image_load(tmp_path)
            safe_jpeg = _sanitize_and_compress(img)
            if len(safe_jpeg) > _MAX_IMAGE_BYTES:
                _reject_multi_or_oversize_and_reply(cid, "file is larger than 5 MB after compression", reply_to=message.message_id)
                return

            memo = ("[Image attached]" + (f" {ctx_caption}" if ctx_caption else ""))
            
            asyncio.create_task(inc_msg_count(cid))

            await redis_client.set(
                f"msg:{cid}:{message.message_id}",
                memo,
                ex=getattr(settings, "REPLY_CONTEXT_TTL_SEC", 86400)
            )

            channel = message.sender_chat or message.forward_from_chat
            user_id_val = (channel.id if (is_channel_post and channel) else (message.from_user.id if message.from_user else cid))
            reply_to_id = (message.reply_to_message.message_id if message.reply_to_message else None)

            payload = {
                "chat_id": cid,
                "text": ctx_caption,
                "user_id": user_id_val,
                "reply_to": reply_to_id,
                "is_group": True,
                "msg_id": message.message_id,
                "is_channel_post": is_channel_post,
                "channel_id": channel.id if channel else None,
                "channel_title": getattr(channel, "title", None) if channel else None,
                "image_b64": base64.b64encode(safe_jpeg).decode("ascii"),
                "image_mime": "image/jpeg",
                "trigger": trigger,
                "enforce_on_topic": (trigger == "check_on_topic"),
                "entities": _extract_entities(message),
            }
            if message.from_user:
                with contextlib.suppress(Exception):
                    await redis_client.sadd(f"all_users:{cid}", message.from_user.id)

            try:
                channel = message.sender_chat or message.forward_from_chat
                user_id_val = (channel.id if (is_channel_post and channel) else (message.from_user.id if message.from_user else cid))
                role = "channel" if is_channel_post else "user"
                text_for_group = (ctx_caption or "").strip()
                if not text_for_group:
                    text_for_group = "[Image attached]"
                asyncio.create_task(push_group_stm(cid, role, text_for_group, user_id=user_id_val))
            except Exception:
                logger.debug("push_group_stm (doc-image) failed", exc_info=True)

            try:
                if trigger in ("mention", "check_on_topic", "channel_post"):
                    channel = message.sender_chat or message.forward_from_chat
                    user_id_val = (channel.id if (is_channel_post and channel) else (message.from_user.id if message.from_user else cid))
                    text_for_recent = (ctx_caption or "[Image attached]").strip()
                    line = f"[{int(time_module.time())}] [u:{user_id_val}] {text_for_recent}"
                    asyncio.create_task(append_group_recent(cid, [line]))
            except Exception:
                logger.debug("append_group_recent (doc-image) failed", exc_info=True)

            try:
                has_link = any((e.get("type","").lower() in ("url", "text_link")) for e in ents)
                actor_name = (channel.title if (is_channel_post and channel) else (
                    (message.from_user.full_name or message.from_user.username or str(getattr(message.from_user, "id", ""))) if message.from_user else None
                ))
                actor_id = int(channel.id) if (is_channel_post and channel) else int(getattr(message.from_user, "id", cid))
                asyncio.create_task(
                    record_user_message(
                        cid,
                        actor_id,
                        display_name=(actor_name or "")[:128],
                        content_type="document",
                        addressed_to_bot=bool(trigger == "mention"),
                        has_link=bool(has_link),
                    )
                )
            except Exception:
                logger.debug("analytics(record_user_message doc) failed", exc_info=True)
                
            buffer_message_for_response(payload)

            passive_moderate.delay({
                "chat_id":   cid,
                "user_id":   user_id_val,
                "message_id": message.message_id,
                "text":       ctx_caption,
                "entities":  _extract_entities(message),
                "image_b64": payload.get("image_b64"),
                "image_mime": payload.get("image_mime"),
                "source":    "channel" if is_channel_post else "user",
            })

        except ValueError as ve:
            logger.warning("Document image validation failed (group): %s", ve)
            _reject_multi_or_oversize_and_reply(cid, str(ve), reply_to=message.message_id)
        except Exception:
            logger.exception("Document image processing failed (group)")
            _reject_multi_or_oversize_and_reply(cid, "internal error", reply_to=message.message_id)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                with contextlib.suppress(Exception):
                    os.remove(tmp_path)

    except RedisError as e:
        logger.warning("Redis error in on_group_document_image, skipping noncritical ops: %s", e)
    except Exception:
        logger.exception("Error in on_group_document_image handler")