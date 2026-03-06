#app/tasks/moderation.py
from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import secrets
import time
from typing import Any

from celery import shared_task

import app.bot.components.constants as consts
from app.config import settings
from app.core.media_limits import decode_base64_payload
from app.tasks.celery_app import run_coro_sync



logger = logging.getLogger(__name__)

MODERATION_TIMEOUT = int(getattr(settings, "MODERATION_TIMEOUT", 30))
MODERATION_INFLIGHT_TTL = max(
    int(getattr(settings, "MODERATION_INFLIGHT_TTL", MODERATION_TIMEOUT + 15)),
    MODERATION_TIMEOUT + 1,
)
MODERATION_MAX_IMAGE_BYTES = int(getattr(settings, "CELERY_MODERATION_MAX_IMAGE_BYTES", 5 * 1024 * 1024))
MODERATION_MAX_PAYLOAD_BYTES = int(getattr(settings, "CELERY_MODERATION_MAX_PAYLOAD_BYTES", 256 * 1024))

_METRICS_RETRY_COUNT = "metrics:celery:moderation:retry_count"
_METRICS_ERROR_COUNT = "metrics:celery:moderation:error_count"
_METRICS_LATENCY_COUNT = "metrics:celery:moderation:latency_count"
_METRICS_LATENCY_TOTAL_MS = "metrics:celery:moderation:latency_total_ms"

_REQUIRED_PAYLOAD_FIELDS = ("chat_id", "user_id", "message_id")

_MODERATION_INFLIGHT_RELEASE_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
  return redis.call("DEL", KEYS[1])
else
  return 0
end
"""


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
        run_coro_sync(coro)
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


def _moderation_inflight_key(chat_id: int, message_id: int) -> str:
    return f"mod:inflight:{chat_id}:{message_id}"


async def _is_prefilter_blocked(chat_id: int, message_id: int) -> bool:
    key = f"mod:prefilter_blocked:{int(chat_id)}:{int(message_id)}"
    try:
        raw = await consts.redis_client.get(key)
    except Exception:
        logger.debug("prefilter blocked marker read failed key=%s", key, exc_info=True)
        return False
    return str(raw or "").strip().lower() in {"1", "true", "yes", "blocked"}


def _is_closed_transport_runtime_error(exc: Exception) -> bool:
    if not isinstance(exc, RuntimeError):
        return False
    message = str(exc).lower()
    return "handler is closed" in message or "transport closed" in message


async def _reset_redis_connection() -> None:
    client = consts.redis_client
    close = getattr(client, "aclose", None)
    if callable(close):
        result = close()
        if inspect.isawaitable(result):
            await result
        return

    pool = getattr(client, "connection_pool", None)
    disconnect = getattr(pool, "disconnect", None)
    if callable(disconnect):
        result = disconnect()
        if inspect.isawaitable(result):
            await result


async def _set_moderation_inflight(key: str, token: str) -> bool:
    try:
        return bool(await consts.redis_client.set(key, token, ex=MODERATION_INFLIGHT_TTL, nx=True))
    except Exception as exc:
        if _is_closed_transport_runtime_error(exc):
            logger.warning("failed to set moderation inflight key=%s: %s; reconnecting and retrying once", key, exc)
            try:
                await _reset_redis_connection()
                return bool(await consts.redis_client.set(key, token, ex=MODERATION_INFLIGHT_TTL, nx=True))
            except Exception:
                logger.warning("failed to set moderation inflight key=%s after reconnect", key)
                return False
        logger.warning("failed to set moderation inflight key=%s", key, exc_info=True)
        return False


async def _clear_moderation_inflight(key: str, token: str) -> None:
    try:
        await consts.redis_client.eval(_MODERATION_INFLIGHT_RELEASE_LUA, 1, key, token)
    except Exception as exc:
        if _is_closed_transport_runtime_error(exc):
            logger.warning("failed to clear moderation inflight key=%s: %s; reconnecting and retrying once", key, exc)
            try:
                await _reset_redis_connection()
                await consts.redis_client.eval(_MODERATION_INFLIGHT_RELEASE_LUA, 1, key, token)
                return
            except Exception:
                logger.warning("failed to clear moderation inflight key=%s after reconnect", key)
                return
        logger.warning("failed to clear moderation inflight key=%s", key, exc_info=True)


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

    try:
        if run_coro_sync(_is_prefilter_blocked(chat_id, message_id)):
            logger.info(
                "PASSIVE_MODERATION_PREFILTER_BLOCKED_SKIP: chat_id=%s msg_id=%s user_id=%s",
                chat_id,
                message_id,
                user_id,
            )
            return "blocked"
    except Exception:
        logger.debug("prefilter blocked check failed", exc_info=True)

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
                message_thread_id=payload.get("message_thread_id"),
                reply_to_message_id=payload.get("reply_to_message_id"),
                linked_chat_id=payload.get("linked_chat_id"),
                is_topic_message=payload.get("is_topic_message"),
            ),
            timeout=MODERATION_TIMEOUT,
        )

    inflight_key = _moderation_inflight_key(chat_id, message_id)
    inflight_token = secrets.token_urlsafe(16)
    try:
        run_coro_sync(_set_moderation_inflight(inflight_key, inflight_token))
    except Exception:
        logger.warning("failed to dispatch moderation inflight set key=%s", inflight_key, exc_info=True)

    try:
        logger.info(
            "PASSIVE_MODERATION_JOB_START: chat_id=%s msg_id=%s user_id=%s source=%s is_comment_context=%s linked_chat_id=%s thread_id=%s reply_to_msg_id=%s is_topic_message=%s retries=%s",
            chat_id,
            message_id,
            user_id,
            payload.get("source", "user"),
            payload.get("is_comment_context"),
            payload.get("linked_chat_id"),
            payload.get("message_thread_id"),
            payload.get("reply_to_message_id"),
            payload.get("is_topic_message"),
            retries,
        )
        result = run_coro_sync(_do())
        logger.info(
            "PASSIVE_MODERATION_JOB_RESULT: chat_id=%s msg_id=%s user_id=%s status=%s linked_chat_id=%s thread_id=%s reply_to_msg_id=%s is_topic_message=%s",
            chat_id,
            message_id,
            user_id,
            result,
            payload.get("linked_chat_id"),
            payload.get("message_thread_id"),
            payload.get("reply_to_message_id"),
            payload.get("is_topic_message"),
        )
        return result
    except Exception:
        _run_metrics(_metrics_incr(_METRICS_ERROR_COUNT), label="error_count")
        raise
    finally:
        _run_metrics(_clear_moderation_inflight(inflight_key, inflight_token), label="inflight_release")
        latency_ms = int((time.monotonic() - started) * 1000)
        _run_metrics(_metrics_latency(latency_ms), label="latency")
        if retries > 0:
            _run_metrics(_metrics_incr(_METRICS_RETRY_COUNT), label="retry_count")
