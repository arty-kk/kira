from __future__ import annotations

import asyncio
import base64
import contextlib
import html
import json
import logging
import os
import tempfile
import time
from typing import Any

from celery import shared_task

import app.bot.components.constants as consts
from app.bot.utils.telegram_safe import send_message_safe
from app.clients.telegram_client import get_bot
from app.config import settings
from app.core.memory import append_group_recent, inc_msg_count, push_group_stm
from app.services.user.user_service import refund_reservation_by_id

from app.tasks.moderation import passive_moderate, prepare_moderation_payload
from app.tasks.queue_schema import validate_bot_job
from app.core.media_utils import MAX_IMAGE_BYTES, sanitize_and_compress, strict_image_load

logger = logging.getLogger(__name__)

MEDIA_PREPROCESS_TIMEOUT_SEC = float(getattr(settings, "MEDIA_PREPROCESS_TIMEOUT_SEC", 20.0))
MEDIA_MAX_INPUT_BYTES = int(getattr(settings, "MEDIA_MAX_INPUT_BYTES", 30 * 1024 * 1024))
BOT_QUEUE_MAX_PAYLOAD_BYTES = int(getattr(settings, "BOT_QUEUE_MAX_PAYLOAD_BYTES", 64 * 1024))


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _error_text(reason: str) -> str:
    return f"⚠️ Не получилось обработать изображение: {reason}. Отправь одно изображение в формате JPEG/PNG/WEBP."


async def _send_error(chat_id: int, reason: str, reply_to: int | None) -> None:
    bot = get_bot()
    safe_reason = html.escape(reason or "", quote=True)
    await send_message_safe(
        bot,
        chat_id,
        _error_text(safe_reason),
        parse_mode="HTML",
        reply_to_message_id=reply_to,
    )


async def _download_file_to_tmp(*, file_id: str, suffix: str, timeout_s: float) -> str:
    bot = get_bot()
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name
        tg_file = await asyncio.wait_for(bot.get_file(file_id), timeout=timeout_s)
        await asyncio.wait_for(bot.download(tg_file, tmp_path), timeout=timeout_s)
        if os.path.getsize(tmp_path) > MEDIA_MAX_INPUT_BYTES:
            raise ValueError("входной файл слишком большой для обработки")
        return tmp_path
    except asyncio.TimeoutError as exc:
        raise ValueError("превышено время загрузки") from exc
    except Exception:
        if tmp_path and os.path.exists(tmp_path):
            with contextlib.suppress(Exception):
                os.remove(tmp_path)
        raise


async def _enqueue(payload: dict[str, Any]) -> bool:
    reservation_id = _safe_int(payload.get("reservation_id"), 0)
    chat_id = _safe_int(payload.get("chat_id"), 0)
    user_id = _safe_int(payload.get("user_id"), 0)
    msg_id = _safe_int(payload.get("msg_id"), 0)
    err = validate_bot_job(payload)
    if err:
        logger.warning(
            "media.preprocess_group_image.enqueue_reject",
            extra={
                "reason": err,
                "chat_id": chat_id,
                "user_id": user_id,
                "msg_id": msg_id,
            },
        )
        if reservation_id:
            await refund_reservation_by_id(reservation_id)
        return False

    data = json.dumps(payload, ensure_ascii=False)
    if BOT_QUEUE_MAX_PAYLOAD_BYTES > 0 and len(data.encode("utf-8")) > BOT_QUEUE_MAX_PAYLOAD_BYTES:
        logger.warning("media.preprocess_group_image: payload too large, drop")
        if reservation_id:
            await refund_reservation_by_id(reservation_id)
        return False

    await consts.redis_queue.lpush(settings.QUEUE_KEY, data)
    return True


async def _store_context_and_recent(payload: dict[str, Any], *, log_caption: str) -> None:
    cid = _safe_int(payload.get("chat_id"))
    msg_id = _safe_int(payload.get("message_id"))
    user_id = _safe_int(payload.get("user_id"), cid)
    trigger = str(payload.get("trigger") or "")
    is_channel = bool(payload.get("is_channel_post"))

    source = "channel" if is_channel else "user"
    context = {"role": "user", "text": "[Image]" + (f" {log_caption}" if log_caption else ""), "speaker_id": user_id, "source": source}

    asyncio.create_task(inc_msg_count(cid))
    await consts.redis_client.set(
        f"msg:{cid}:{msg_id}",
        json.dumps(context, ensure_ascii=False, separators=(",", ":")),
        ex=getattr(settings, "REPLY_CONTEXT_TTL_SEC", 86400),
    )

    text_for_stm = (log_caption or "").strip() or "[Image]"
    text_for_recent = (log_caption or "[Image]").strip()
    role = "channel" if is_channel else "user"

    if text_for_stm:
        asyncio.create_task(push_group_stm(cid, role, text_for_stm, user_id=user_id))

    if trigger in ("mention", "check_on_topic", "channel_post"):
        line = f"[{int(time.time())}] [u:{user_id}] {text_for_recent}"
        asyncio.create_task(append_group_recent(cid, [line]))


async def _preprocess(payload: dict[str, Any]) -> str:
    chat_id = _safe_int(payload.get("chat_id"))
    message_id = _safe_int(payload.get("message_id"))
    file_id = (payload.get("file_id") or payload.get("document_id") or "").strip()
    suffix = str(payload.get("suffix") or ".jpg")
    skip_responder_enqueue = bool(payload.get("skip_responder_enqueue"))

    if not file_id:
        if not skip_responder_enqueue:
            await _send_error(chat_id, "не найден file_id", message_id)
        return "skipped:no_file_id"

    tmp_path: str | None = None
    try:
        tmp_path = await _download_file_to_tmp(file_id=file_id, suffix=suffix, timeout_s=MEDIA_PREPROCESS_TIMEOUT_SEC)

        img = await asyncio.wait_for(strict_image_load(tmp_path), timeout=MEDIA_PREPROCESS_TIMEOUT_SEC)
        safe_jpeg = await asyncio.wait_for(
            asyncio.to_thread(sanitize_and_compress, img, max_image_bytes=MAX_IMAGE_BYTES),
            timeout=MEDIA_PREPROCESS_TIMEOUT_SEC,
        )
        if len(safe_jpeg) > MAX_IMAGE_BYTES:
            raise ValueError("не удалось ужать до 5MB")

        jpeg_b64 = base64.b64encode(safe_jpeg).decode("ascii")

        log_caption = str(payload.get("caption_log") or payload.get("caption") or "").strip()

        responder_payload = {
            "chat_id": chat_id,
            "text": payload.get("caption") or "",
            "user_id": _safe_int(payload.get("user_id"), chat_id),
            "reply_to": payload.get("reply_to"),
            "is_group": True,
            "msg_id": message_id,
            "is_channel_post": bool(payload.get("is_channel_post")),
            "is_comment_context": bool(payload.get("is_comment_context")),
            "channel_id": payload.get("channel_id"),
            "linked_chat_id": payload.get("linked_chat_id"),
            "channel_title": payload.get("channel_title"),
            "image_b64": jpeg_b64,
            "image_mime": "image/jpeg",
            "trigger": payload.get("trigger"),
            "enforce_on_topic": bool(payload.get("enforce_on_topic")),
            "allow_web": bool(payload.get("allow_web")),
            "entities": payload.get("entities") or [],
        }

        if not skip_responder_enqueue:
            enqueue_ok = await _enqueue(responder_payload)
            if not enqueue_ok:
                return "skipped:enqueue"

            await _store_context_and_recent(payload, log_caption=log_caption)

        moderation_payload = prepare_moderation_payload(
            {
                "chat_id": chat_id,
                "user_id": _safe_int(payload.get("user_id"), chat_id),
                "message_id": message_id,
                "text": log_caption,
                "entities": payload.get("entities") or [],
                "image_b64": jpeg_b64,
                "image_mime": "image/jpeg",
                "source": "channel" if bool(payload.get("is_channel_post")) else "user",
                "is_comment_context": payload.get("is_comment_context"),
                "trusted_repost": bool(payload.get("trusted_repost")),
                "chat_title": payload.get("chat_title"),
            },
            context="media.preprocess_group_image",
        )
        passive_moderate.delay(moderation_payload)
        return "ok"
    except ValueError as exc:
        logger.warning("media.preprocess_group_image validation failed chat=%s msg=%s err=%s", chat_id, message_id, exc)
        if not skip_responder_enqueue:
            await _send_error(chat_id, str(exc), message_id)
        return "skipped:validation"
    except asyncio.TimeoutError:
        logger.warning("media.preprocess_group_image timeout chat=%s msg=%s", chat_id, message_id)
        if not skip_responder_enqueue:
            await _send_error(chat_id, "время обработки истекло", message_id)
        return "skipped:timeout"
    except Exception:
        logger.exception("media.preprocess_group_image failed chat=%s msg=%s", chat_id, message_id)
        if not skip_responder_enqueue:
            await _send_error(chat_id, "внутренняя ошибка", message_id)
        return "error"
    finally:
        if tmp_path and os.path.exists(tmp_path):
            with contextlib.suppress(Exception):
                os.remove(tmp_path)


@shared_task(name="media.preprocess_group_image", bind=True, acks_late=True)
def preprocess_group_image(self, payload: dict[str, Any]) -> str:
    return run_coro_sync(_preprocess(payload))
