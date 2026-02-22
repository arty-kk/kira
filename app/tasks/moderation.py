#app/tasks/moderation.py
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

from celery import shared_task

import app.bot.components.constants as consts
from app.config import settings
from app.core.media_limits import decode_base64_payload
from app.tasks.celery_app import _run


logger = logging.getLogger(__name__)

MODERATION_TIMEOUT = int(getattr(settings, "MODERATION_TIMEOUT", 30))
MODERATION_MAX_IMAGE_BYTES = int(getattr(settings, "CELERY_MODERATION_MAX_IMAGE_BYTES", 5 * 1024 * 1024))
MODERATION_MAX_PAYLOAD_BYTES = int(getattr(settings, "CELERY_MODERATION_MAX_PAYLOAD_BYTES", 256 * 1024))

_METRICS_RETRY_COUNT = "metrics:celery:moderation:retry_count"
_METRICS_ERROR_COUNT = "metrics:celery:moderation:error_count"
_METRICS_LATENCY_COUNT = "metrics:celery:moderation:latency_count"
_METRICS_LATENCY_TOTAL_MS = "metrics:celery:moderation:latency_total_ms"

_REQUIRED_PAYLOAD_FIELDS = ("chat_id", "user_id", "message_id")


def prepare_moderation_payload(payload: dict[str, Any], *, context: str) -> dict[str, Any]:
    safe_payload: dict[str, Any] = dict(payload)
    image_b64 = safe_payload.get("image_b64")
    if not image_b64:
        return safe_payload

    image_bytes = decode_base64_payload(str(image_b64))
    if not image_bytes:
        safe_payload.pop("image_b64", None)
        safe_payload.pop("image_mime", None)
        logger.warning(
            "moderation payload image stripped (%s): invalid base64",
            context,
        )
        return safe_payload

    if MODERATION_MAX_IMAGE_BYTES > 0 and image_bytes and len(image_bytes) > MODERATION_MAX_IMAGE_BYTES:
        safe_payload.pop("image_b64", None)
        safe_payload.pop("image_mime", None)
        logger.warning(
            "moderation payload image stripped (%s): decoded image exceeds limit (%s > %s)",
            context,
            len(image_bytes),
            MODERATION_MAX_IMAGE_BYTES,
        )
        return safe_payload

    if MODERATION_MAX_PAYLOAD_BYTES > 0:
        payload_bytes = len(json.dumps(safe_payload, ensure_ascii=False).encode("utf-8"))
        if payload_bytes > MODERATION_MAX_PAYLOAD_BYTES:
            safe_payload.pop("image_b64", None)
            safe_payload.pop("image_mime", None)
            logger.warning(
                "moderation payload image stripped (%s): payload exceeds limit (%s > %s)",
                context,
                payload_bytes,
                MODERATION_MAX_PAYLOAD_BYTES,
            )

    return safe_payload


async def _metrics_incr(key: str, value: int = 1) -> None:
    try:
        await consts.redis_client.incrby(key, int(value))
    except Exception:
        logger.debug("moderation metrics write failed key=%s", key, exc_info=True)


async def _metrics_latency(latency_ms: int) -> None:
    try:
        await consts.redis_client.incrby(_METRICS_LATENCY_TOTAL_MS, int(max(0, latency_ms)))
        await consts.redis_client.incr(_METRICS_LATENCY_COUNT)
    except Exception:
        logger.debug("moderation latency metrics write failed", exc_info=True)


def _run_metrics(coro: Any, *, label: str) -> None:
    try:
        _run(coro)
    except Exception:
        with contextlib.suppress(Exception):
            close = getattr(coro, "close", None)
            if callable(close):
                close()
        logger.debug("moderation metrics dispatch failed: %s", label, exc_info=True)


def _safe_payload_context(payload: dict[str, Any]) -> dict[str, Any]:
    return {field: payload.get(field) for field in _REQUIRED_PAYLOAD_FIELDS if field in payload}


def _parse_required_int(payload: dict[str, Any], field: str) -> int:
    if field not in payload:
        raise ValueError(f"missing field '{field}'")

    raw_value = payload[field]
    if isinstance(raw_value, bool):
        raise ValueError(f"field '{field}' must be int-convertible, got bool")

    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        raise ValueError(f"field '{field}' must be int-convertible") from None

    if field in {"chat_id", "user_id"} and parsed == 0:
        raise ValueError(f"field '{field}' must be != 0")
    if field == "message_id" and parsed <= 0:
        raise ValueError("field 'message_id' must be > 0")

    return parsed


@shared_task(
    name="moderation.passive_moderate",
    bind=True,
    acks_late=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": 3},
    soft_time_limit=MODERATION_TIMEOUT,
    time_limit=MODERATION_TIMEOUT + 5,
)
def passive_moderate(self, payload: dict) -> str:

    from app.bot.handlers.moderation import handle_passive_moderation

    started = time.monotonic()
    retries = int(getattr(getattr(self, "request", None), "retries", 0) or 0)

    if not isinstance(payload, dict):
        logger.warning(
            "passive_moderate invalid payload: payload must be dict; context=%s",
            {"payload_type": type(payload).__name__},
        )
        return "invalid_payload"

    try:
        chat_id = _parse_required_int(payload, "chat_id")
        user_id = _parse_required_int(payload, "user_id")
        message_id = _parse_required_int(payload, "message_id")
    except ValueError as exc:
        logger.warning(
            "passive_moderate invalid payload: %s; context=%s",
            exc,
            _safe_payload_context(payload),
        )
        return "invalid_payload"

    async def _do() -> str:
        return await asyncio.wait_for(
            handle_passive_moderation(
                chat_id=chat_id,
                message=None,
                text=payload.get("text", ""),
                entities=payload.get("entities") or [],
                image_b64=payload.get("image_b64"),
                image_mime=payload.get("image_mime"),
                source=payload.get("source", "user"),
                user_id=user_id,
                message_id=message_id,
                is_comment_context=payload.get("is_comment_context"),
                chat_title=payload.get("chat_title"),
            ),
            timeout=MODERATION_TIMEOUT,
        )

    try:
        logger.info(
            "PASSIVE_MODERATION_JOB_START: chat_id=%s msg_id=%s user_id=%s source=%s is_comment_context=%s retries=%s",
            chat_id,
            message_id,
            user_id,
            payload.get("source", "user"),
            payload.get("is_comment_context"),
            retries,
        )
        result = _run(_do())
        logger.info(
            "PASSIVE_MODERATION_JOB_RESULT: chat_id=%s msg_id=%s user_id=%s status=%s",
            chat_id,
            message_id,
            user_id,
            result,
        )
        return result
    except Exception:
        _run_metrics(_metrics_incr(_METRICS_ERROR_COUNT), label="error_count")
        raise
    finally:
        latency_ms = int((time.monotonic() - started) * 1000)
        _run_metrics(_metrics_latency(latency_ms), label="latency")
        if retries > 0:
            _run_metrics(_metrics_incr(_METRICS_RETRY_COUNT), label="retry_count")
