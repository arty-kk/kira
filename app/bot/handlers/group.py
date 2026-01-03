#app/bot/handlers/group.py
from __future__ import annotations

import asyncio
import base64
import contextlib
import html
import io
import json
import logging
import os
import random
import re
import tempfile
import time as time_module

from datetime import datetime, timedelta, time, timezone
from typing import Any, List, Optional
from zoneinfo import ZoneInfo

from PIL import Image, ImageOps, UnidentifiedImageError
from redis.exceptions import RedisError

from aiogram import F, types
from aiogram.enums import ChatType, ContentType, MessageEntityType
from aiogram.types import Message

from app.bot.components.dispatcher import dp
import app.bot.components.constants as consts
from app.bot.components.constants import redis_client
from app.bot.handlers.moderation import apply_moderation_filters, is_from_linked_channel
from app.bot.i18n import t
from app.bot.utils.debouncer import buffer_message_for_response
from app.bot.utils.telegram_safe import delete_message_safe, send_message_safe
from app.clients.telegram_client import get_bot
from app.config import settings
from app.core.memory import MEMORY_TTL, append_group_recent, inc_msg_count, push_group_stm, record_activity
from app.services.addons.analytics import record_user_message
from app.services.addons.passive_moderation import sanitize_for_context
from app.services.addons import group_battle as battle_service
from app.tasks.moderation import passive_moderate

logger = logging.getLogger(__name__)
bot = get_bot()

if not getattr(consts, "BOT_USERNAME", None):
    logger.warning("BOT_USERNAME is not set; commands with @target may be treated as addressed to other bots.")

# ---------------------------
# Pillow compatibility
# ---------------------------
try:
    RESAMPLING = Image.Resampling.LANCZOS
except AttributeError:
    RESAMPLING = Image.LANCZOS

# ---------------------------
# Settings / constants
# ---------------------------
_SANITIZE_CONTEXT = bool(getattr(settings, "MODERATION_SANITIZE_CONTEXT_ALWAYS", True))

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_SIDE = 2048
ALLOWED_FORMATS = {"JPEG", "JPG", "PNG", "WEBP"}
MAX_FRAMES = 1

MAX_DOCUMENT_BYTES = int(getattr(settings, "MAX_DOC_IMAGE_BYTES", 30 * 1024 * 1024))
MIN_JPEG_QUALITY = int(getattr(settings, "MIN_JPEG_QUALITY", 35))
MIN_SIDE = int(getattr(settings, "MIN_IMAGE_SIDE", 720))

Image.MAX_IMAGE_PIXELS = int(getattr(settings, "MAX_IMAGE_PIXELS", 36_000_000))

BATTLE_CMD_RE = re.compile(r"(^|\s)/battle(?:@[A-Za-z0-9_]{3,})?(?=\s|$|[.,!?])", re.IGNORECASE)
IS_MENTION_RE = re.compile(r"(?<!\S)@\w+\b")


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


def _is_mention(message: types.Message) -> bool:
    if not (consts.BOT_USERNAME and consts.BOT_ID):
        return False

    expected = (consts.BOT_USERNAME or "").lstrip("@").lower()
    raw = (message.text or message.caption or "")
    entities = (message.entities or []) + (message.caption_entities or [])

    for ent in entities:
        try:
            if ent.type == MessageEntityType.MENTION:
                mention = raw[ent.offset : ent.offset + ent.length]
                if mention.lstrip("@").lower() == expected:
                    return True
            if ent.type == MessageEntityType.TEXT_MENTION and ent.user and int(ent.user.id) == int(consts.BOT_ID):
                return True
        except Exception:
            continue

    if expected:
        raw_low = raw.lower()
        raw_low = re.sub(r"https?://\S+|\S+@\S+", " ", raw_low)
        if re.search(rf"(^|[^/\w])@{re.escape(expected)}(?!\w)", raw_low):
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
    try:
        if not (message.reply_to_message and message.reply_to_message.from_user):
            return False
        bid = getattr(consts, "BOT_ID", None)
        if not bid:
            return False
        return int(message.reply_to_message.from_user.id) == int(bid)
    except Exception:
        return False

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
        try:
            if int(message.reply_to_message.from_user.id) != int(consts.BOT_ID) and not _is_mention(message):
                return True
        except Exception:
            return True
    return False


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


async def _maybe_log_channel_post(cid: int, message: Message, raw_text: str, ents: List[dict]) -> bool:
    # returns False if must stop (not linked), True otherwise
    try:
        if not await is_from_linked_channel(message):
            return False
    except Exception:
        logger.exception("linked-channel check failed")
        return False

    try:
        channel_log = {
            "text": sanitize_for_context(raw_text, ents) if _SANITIZE_CONTEXT else raw_text,
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


def _dispatch_passive_moderation(message: Message, payload: dict, *, text: str, ents: List[dict], is_channel: bool, user_id_val: int) -> None:
    passive_moderate.delay(
        {
            "chat_id": message.chat.id,
            "user_id": user_id_val,
            "message_id": message.message_id,
            "text": text,
            "entities": ents,
            "image_b64": payload.get("image_b64"),
            "image_mime": payload.get("image_mime"),
            "source": "channel" if is_channel else "user",
        }
    )


# ---------------------------
# Image helpers (same ideas as private_new.py)
# ---------------------------
async def download_to_tmp(tg_obj: Any, suffix: str) -> str | None:
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name
        await bot.download(tg_obj, tmp_path)
        return tmp_path
    except Exception:
        logger.exception("Failed to download image")
        if tmp_path and os.path.exists(tmp_path):
            with contextlib.suppress(Exception):
                os.remove(tmp_path)
        return None


async def strict_image_load(tmp_path: str) -> Image.Image:
    try:
        with Image.open(tmp_path) as im:
            fmt = (im.format or "").upper()
            if fmt == "JPG":
                fmt = "JPEG"
            if fmt not in ALLOWED_FORMATS:
                raise ValueError(f"Unsupported image format: {fmt}")
            im.verify()

        with Image.open(tmp_path) as im2:
            im2.load()
            with contextlib.suppress(Exception):
                im2 = ImageOps.exif_transpose(im2)
            return im2.copy()

    except UnidentifiedImageError:
        raise ValueError("Not an image or corrupted file")
    except Image.DecompressionBombError:
        raise ValueError("Image too large (decompression bomb)")
    except Exception as e:
        raise ValueError(str(e))


def sanitize_and_compress(img: Image.Image) -> bytes:
    n_frames = int(getattr(img, "n_frames", 1) or 1)
    if n_frames > MAX_FRAMES:
        raise ValueError("Animated or multi-frame images are not allowed")

    with contextlib.suppress(Exception):
        img = ImageOps.exif_transpose(img)

    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")

    w, h = img.size
    if max(w, h) > MAX_SIDE:
        s = MAX_SIDE / float(max(w, h))
        img = img.resize((int(w * s), int(h * s)), resample=RESAMPLING)

    def _save_as_jpeg(jimg: Image.Image, q: int) -> bytes:
        buf = io.BytesIO()
        for progressive in (True, False):
            try:
                buf.seek(0)
                buf.truncate(0)
                jimg.save(
                    buf,
                    format="JPEG",
                    quality=q,
                    optimize=True,
                    progressive=progressive,
                    subsampling=2,
                    exif=b"",
                )
                return buf.getvalue()
            except OSError:
                continue

        buf.seek(0)
        buf.truncate(0)
        jimg.save(buf, format="JPEG", quality=q, progressive=False, subsampling=2, exif=b"")
        return buf.getvalue()

    quality_steps = [85, 80, 75, 70, 65, 60, 55, 50, 45, 40, MIN_JPEG_QUALITY]
    for _ in range(6):
        for q in quality_steps:
            data = _save_as_jpeg(img, q)
            if len(data) <= MAX_IMAGE_BYTES:
                return data

        cur_max = max(img.size)
        if cur_max <= MIN_SIDE:
            break

        new_max = max(MIN_SIDE, int(cur_max * 0.85))
        s = new_max / float(cur_max)
        img = img.resize(
            (max(1, int(img.size[0] * s)), max(1, int(img.size[1] * s))),
            resample=RESAMPLING,
        )

    img = img.resize((max(1, img.size[0] // 2), max(1, img.size[1] // 2)), resample=RESAMPLING)
    data = _save_as_jpeg(img, max(60, MIN_JPEG_QUALITY))
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Image too large after compression")
    return data


async def localized_group_image_error(chat_id: int, reason: str, reply_to: int | None) -> None:
    safe_reason = html.escape(reason or "", quote=True)
    msg = await tr(
        chat_id,
        "errors.image_generic",
        f"⚠️ Cannot process image: {safe_reason}\nPlease send exactly one image (≤ 5 MB) in a single message.",
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


async def _handle_group_image_file(
    message: Message,
    tg_obj: Any,
    *,
    suffix: str,
    reply_to: int | None,
) -> bytes | None:
    tmp_path: str | None = None
    try:
        tmp_path = await download_to_tmp(tg_obj, suffix=suffix)
        if not tmp_path:
            reject_image_and_reply(message.chat.id, "download failed", reply_to=reply_to)
            return None

        img = await strict_image_load(tmp_path)
        safe_jpeg = sanitize_and_compress(img)
        if len(safe_jpeg) > MAX_IMAGE_BYTES:
            reject_image_and_reply(message.chat.id, "file is larger than 5 MB after compression", reply_to=reply_to)
            return None

        return safe_jpeg
    except ValueError as ve:
        logger.warning("Image validation failed (group): %s", ve)
        reject_image_and_reply(message.chat.id, str(ve), reply_to=reply_to)
        return None
    except Exception:
        logger.exception("Image processing failed (group)")
        reject_image_and_reply(message.chat.id, "internal error", reply_to=reply_to)
        return None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            with contextlib.suppress(Exception):
                os.remove(tmp_path)


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


async def _maybe_handle_battle(message: Message, *, trigger: str, has_battle_cmd: bool, is_battle_cmd_to_us: bool) -> bool:
    # returns True if handled (and handler should return)
    if trigger != "mention":
        return False

    if not (
        is_battle_cmd_to_us
        or (has_battle_cmd and not _is_cmd_addressed_to_other_bot(message, "battle"))
    ):
        return False

    cid = message.chat.id
    challenger_id = message.from_user.id if message.from_user else None
    if not challenger_id:
        return True

    opponent_id = await _resolve_battle_opponent_id(message)
    if not opponent_id:
        opponent_id = consts.BOT_ID

    if int(opponent_id) == int(challenger_id):
        await send_message_safe(bot, cid, "🤔 You can't challenge yourself.", reply_to_message_id=message.message_id)
        return True

    if int(opponent_id) != consts.BOT_ID and await redis_client.sismember("battle:opt_out", str(opponent_id)):
        await send_message_safe(bot, cid, "🚫 That user has opted out of Battles.", reply_to_message_id=message.message_id)
        return True

    if await redis_client.sismember("battle:opt_out", str(challenger_id)):
        await send_message_safe(bot, cid, "🚫 You opted out of Battles. DM /battle_on to opt in.", reply_to_message_id=message.message_id)
        return True

    try:
        await battle_service.launch_battle(str(challenger_id), str(opponent_id), chat_id=cid)
        with contextlib.suppress(Exception):
            await delete_message_safe(bot, cid, message.message_id)
    except Exception:
        await send_message_safe(
            bot,
            cid,
            "❌ Couldn't start the battle (opponent might be unavailable or an internal error occurred).",
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
            if not _is_chat_allowed(message.chat):
                logger.info("Ignore unauthorized group chat=%s uname=%s", cid, getattr(message.chat, "username", None))
                return
        except Exception:
            logger.exception("Whitelist check failed")
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

        # channel post path: must be from linked channel; also log
        if is_channel:
            raw_text = (message.text or message.caption or "").strip()
            ents = _extract_entities(message)
            ok = await _maybe_log_channel_post(cid, message, raw_text, ents)
            if not ok:
                return

        # ignore bot users unless channel post
        if message.from_user and message.from_user.is_bot and not is_channel:
            return

        if _reply_gate_requires_mention(message):
            return

        raw_text = (message.text or message.caption or "").strip()
        ents = _extract_entities(message)
        ctx_text = sanitize_for_context(raw_text, ents) if _SANITIZE_CONTEXT else raw_text

        AUTOREPLY_ON_TOPIC = bool(getattr(settings, "GROUP_AUTOREPLY_ON_TOPIC", True))

        has_battle_cmd = bool(BATTLE_CMD_RE.search(raw_text or ""))
        is_battle_cmd_to_us = _is_bot_command_to_us(message, "battle")

        mentioned = _is_mention(message)
        mentions_other = _mentions_other_user(message)

        trigger: str | None = None
        if is_channel:
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
                    message.message_id,
                )

        if not trigger:
            return

        if trigger in ("mention", "check_on_topic") and not is_channel:
            if _is_effectively_empty(ctx_text):
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
            ctx_text,
            role="user",
            speaker_id=user_id_val,
            source=("channel" if is_channel else "user"),
        )

        # battle shortcut
        if await _maybe_handle_battle(message, trigger=trigger, has_battle_cmd=has_battle_cmd, is_battle_cmd_to_us=is_battle_cmd_to_us):
            return

        channel = _channel_obj(message)

        payload = {
            "chat_id": cid,
            "text": ctx_text,
            "user_id": user_id_val,
            "reply_to": reply_to_id,
            "is_group": True,
            "msg_id": message.message_id,
            "is_channel_post": is_channel,
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
            text_for_stm=(ctx_text or "").strip(),
            text_for_recent=(ctx_text or "").strip(),
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
            text=ctx_text,
            ents=ents,
            is_channel=is_channel,
            user_id_val=user_id_val,
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
            if not _is_chat_allowed(message.chat):
                logger.info("Ignore unauthorized group chat=%s uname=%s", cid, getattr(message.chat, "username", None))
                return
        except Exception:
            logger.exception("Whitelist check failed")
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
        AUTOREPLY_ON_TOPIC = bool(getattr(settings, "GROUP_AUTOREPLY_ON_TOPIC", True))
        mentioned = _is_mention(message)

        trigger: str | None = None
        if is_channel:
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
        )

    except RedisError as e:
        logger.warning("Redis error in on_group_voice, skipping noncritical ops: %s", e)
    except Exception:
        logger.exception("Error in on_group_voice handler")


async def _handle_group_image_message_common(
    message: Message,
    *,
    tg_obj: Any,
    suffix: str,
    content_type_for_analytics: str,
) -> None:
    cid = message.chat.id

    if not is_single_media(message):
        reject_image_and_reply(cid, "albums are not supported", reply_to=message.message_id)
        return

    # moderation guard
    try:
        if await apply_moderation_filters(cid, message):
            return
    except Exception:
        logger.exception("guard filters failed (image)")

    if _reply_gate_requires_mention(message):
        return

    is_channel = _is_channel_post(message)

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
    ctx_caption = sanitize_for_context(caption, ents) if _SANITIZE_CONTEXT else caption

    mentioned = _is_mention(message)
    mentions_other = _mentions_other_user(message)
    AUTOREPLY_ON_TOPIC = bool(getattr(settings, "GROUP_AUTOREPLY_ON_TOPIC", True))

    trigger: str | None = None
    if is_channel:
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

    if not await _ensure_daily_limit(cid, message.message_id):
        return

    safe_jpeg = await _handle_group_image_file(message, tg_obj, suffix=suffix, reply_to=message.message_id)
    if not safe_jpeg:
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

    memo = "[Image]" + (f" {ctx_caption}" if ctx_caption else "")
    await _store_context(
        cid,
        message.message_id,
        memo,
        role="user",
        speaker_id=user_id_val,
        source=("channel" if is_channel else "user"),
    )

    channel = _channel_obj(message)

    payload = {
        "chat_id": cid,
        "text": ctx_caption,
        "user_id": user_id_val,
        "reply_to": reply_to_id,
        "is_group": True,
        "msg_id": message.message_id,
        "is_channel_post": is_channel,
        "channel_id": channel.id if channel else None,
        "channel_title": getattr(channel, "title", None) if channel else None,
        "image_b64": base64.b64encode(safe_jpeg).decode("ascii"),
        "image_mime": "image/jpeg",
        "trigger": trigger,
        "enforce_on_topic": (trigger == "check_on_topic"),
        "entities": ents,
    }

    if message.from_user:
        with contextlib.suppress(Exception):
            await redis_client.sadd(f"all_users:{cid}", message.from_user.id)

    text_for_stm = (ctx_caption or "").strip() or "[Image]"
    text_for_recent = (ctx_caption or "[Image]").strip()

    await _push_group_stm_and_recent(
        cid,
        trigger=trigger,
        is_channel=is_channel,
        user_id_val=user_id_val,
        text_for_stm=text_for_stm,
        text_for_recent=text_for_recent,
    )

    has_link = any((e.get("type", "").lower() in ("url", "text_link")) for e in ents)
    _analytics_best_effort(
        cid,
        message,
        content_type=content_type_for_analytics,
        addressed_to_bot=(trigger == "mention"),
        has_link=bool(has_link),
        is_channel=is_channel,
    )

    buffer_message_for_response(payload)
    _dispatch_passive_moderation(
        message,
        payload,
        text=ctx_caption,
        ents=ents,
        is_channel=is_channel,
        user_id_val=user_id_val,
    )


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
            tg_obj=biggest,
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
            if not _is_chat_allowed(message.chat):
                logger.info("Ignore unauthorized group chat=%s uname=%s", cid, getattr(message.chat, "username", None))
                return
        except Exception:
            logger.exception("Whitelist check failed")
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
                reject_image_and_reply(cid, "file is too large", reply_to=message.message_id)
                return
        except Exception:
            logger.debug("doc.file_size check failed", exc_info=True)

        await _handle_group_image_message_common(
            message,
            tg_obj=doc,
            suffix=_doc_suffix(mime_lower),
            content_type_for_analytics="document",
        )

    except RedisError as e:
        logger.warning("Redis error in on_group_document_image, skipping noncritical ops: %s", e)
    except Exception:
        logger.exception("Error in on_group_document_image handler")
