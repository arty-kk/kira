#app/tasks/api_worker.py
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import tempfile

from contextlib import suppress
from typing import Any, Dict, Set

from app.config import settings
from app.clients.openai_client import get_openai
from app.core.media_limits import (
    ALLOWED_IMAGE_MIMES,
    ALLOWED_VOICE_MIMES,
    API_MAX_IMAGE_BYTES,
    API_MAX_VOICE_BYTES,
    clean_base64_payload,
    decode_base64_payload,
)
from app.core.memory import get_redis_queue, close_redis_pools
from app.services.responder import respond_to_user

logger = logging.getLogger(__name__)

API_QUEUE_KEY = getattr(settings, "API_QUEUE_KEY", "queue:api")
PROCESSING_KEY = f"{API_QUEUE_KEY}:processing"

JOB_KEY_PREFIX = "api:job:"
RESULT_TTL_SEC = int(getattr(settings, "API_RESULT_TTL_SEC", 600))

MAX_INFLIGHT_TASKS = int(getattr(settings, "API_WORKER_MAX_INFLIGHT", 64))
RESPOND_TIMEOUT = int(
    getattr(settings, "API_RESPOND_TIMEOUT_SEC",
            getattr(settings, "API_CALL_TIMEOUT_SEC", 60))
)
JOB_HEARTBEAT_INTERVAL_SEC = int(getattr(settings, "API_JOB_HEARTBEAT_INTERVAL_SEC", 10))

PROCESSING_TASKS: Set[asyncio.Task] = set()

VOICE_TRANSCRIPTION_MODEL = getattr(
    settings,
    "TRANSCRIPTION_MODEL",
    os.environ.get("TRANSCRIPTION_MODEL", "whisper-1"),
)
VOICE_TRANSCRIPTION_TIMEOUT = int(
    getattr(settings, "API_VOICE_TRANSCRIPTION_TIMEOUT_SEC", 40)
)

JOB_TTL_BUFFER_SEC = 30
JOB_TTL_SEC = max(
    int(getattr(settings, "API_JOB_TTL_SEC", 180)),
    RESPOND_TIMEOUT + JOB_TTL_BUFFER_SEC + VOICE_TRANSCRIPTION_TIMEOUT,
)
INFLIGHT_STALE_AFTER_SEC = RESPOND_TIMEOUT + JOB_TTL_BUFFER_SEC + VOICE_TRANSCRIPTION_TIMEOUT


def _guess_audio_suffix(mime: str | None) -> str:
    if not mime:
        return ".ogg"
    m = mime.lower()
    if "wav" in m:
        return ".wav"
    if "mp3" in m or "mpeg" in m:
        return ".mp3"
    if "mp4" in m or "m4a" in m or "aac" in m:
        return ".m4a"
    return ".ogg"


async def _transcribe_voice_bytes(audio: bytes, mime: str | None) -> str:
    if not audio:
        return ""

    if len(audio) > API_MAX_VOICE_BYTES:
        logger.warning(
            "api_worker: voice payload too large: %d bytes > %d",
            len(audio),
            API_MAX_VOICE_BYTES,
        )
        return ""

    tmp_path = None
    try:
        suffix = _guess_audio_suffix(mime)
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(audio)
            tmp_path = tmp.name

        client = get_openai()

        async def _do() -> str:
            with open(tmp_path, "rb") as f:
                resp = await client.audio.transcriptions.create(
                    model=VOICE_TRANSCRIPTION_MODEL,
                    file=f,
                    response_format="text",
                )
            if isinstance(resp, str):
                return resp.strip()
            return getattr(resp, "text", "").strip()

        return await asyncio.wait_for(_do(), timeout=VOICE_TRANSCRIPTION_TIMEOUT)
    except Exception as e:
        logger.warning("api_worker: voice transcription failed: %s", e)
        return ""
    finally:
        if tmp_path and os.path.exists(tmp_path):
            with suppress(Exception):
                os.remove(tmp_path)


async def _mark_done(redis, job_key: str) -> None:
    try:
        await redis.set(job_key, "done", ex=JOB_TTL_SEC)
    except Exception:
        with suppress(Exception):
            await redis.delete(job_key)


async def _heartbeat_job(redis, job_key: str, stop_evt: asyncio.Event) -> None:
    try:
        while not stop_evt.is_set():
            await asyncio.sleep(JOB_HEARTBEAT_INTERVAL_SEC)
            if stop_evt.is_set():
                return
            try:
                ts = int(time.time())
                await redis.set(job_key, f"inflight:{ts}", ex=JOB_TTL_SEC)
            except Exception as e:
                logger.warning("api_worker: heartbeat failed %s: %s", job_key, e)
    except asyncio.CancelledError:
        pass


async def _handle_job(raw: str, redis_queue) -> None:
    if not raw:
        return

    try:
        job = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("api_worker: invalid JSON job, dropping: %r", raw[:200])
        with suppress(Exception):
            await redis_queue.lrem(PROCESSING_KEY, 1, raw)
        return

    request_id = job.get("request_id")
    text = (job.get("text") or "").strip()
    chat_id = job.get("chat_id")
    memory_uid = job.get("memory_uid")
    persona_owner_id = job.get("persona_owner_id")
    billing_tier = job.get("billing_tier")
    if isinstance(billing_tier, (bytes, bytearray)):
        billing_tier = billing_tier.decode("utf-8", "ignore")
    if billing_tier is not None and not isinstance(billing_tier, str):
        billing_tier = str(billing_tier)
    billing_tier = (billing_tier or "").strip().lower() or None
    if billing_tier not in ("paid", "free", "none"):
        billing_tier = None
    result_key = job.get("result_key")
    msg_id = job.get("msg_id")
    image_b64 = job.get("image_b64")
    image_mime = (job.get("image_mime") or "").lower() or None
    voice_b64 = job.get("voice_b64")
    voice_mime = (job.get("voice_mime") or "").lower() or None
    allow_web = bool(job.get("allow_web") or False)
    enqueued_at = job.get("enqueued_at")
    if isinstance(enqueued_at, (bytes, bytearray)):
        enqueued_at = enqueued_at.decode("utf-8", "ignore")
    try:
        enqueued_at = float(enqueued_at) if enqueued_at is not None else None
    except (TypeError, ValueError):
        enqueued_at = None

    if not request_id or not isinstance(result_key, str):
        logger.error("api_worker: missing ids in job: %r", job)
        with suppress(Exception):
            await redis_queue.lrem(PROCESSING_KEY, 1, raw)
        return

    async def _send_struct_error(status: int, code: str, message: str) -> None:
        payload = {
            "ok": False,
            "error": {
                "status": status,
                "code": code,
                "message": message,
            },
            "latency_ms": 0,
            "request_id": request_id,
        }
        try:
            data = json.dumps(payload, ensure_ascii=False)
        except Exception:
            logger.exception("api_worker: encode struct-error failed for %s", request_id)
            return

        try:
            pipe = redis_queue.pipeline()
            pipe.rpush(result_key, data)
            pipe.expire(result_key, RESULT_TTL_SEC)
            await pipe.execute()
        except Exception:
            logger.exception(
                "api_worker: push struct-error failed key=%s req=%s",
                result_key,
                request_id,
            )

    try:
        chat_id = int(chat_id)
        memory_uid = int(memory_uid)
    except Exception:
        logger.error("api_worker: bad chat_id/memory_uid in %s: %r", request_id, job)
        await _send_struct_error(
            500,
            "invalid_job",
            "Invalid chat_id or memory_uid in job payload.",
        )
        with suppress(Exception):
            await redis_queue.lrem(PROCESSING_KEY, 1, raw)
        return

    try:
        msg_id = int(msg_id)
    except Exception:
        msg_id = None

    if persona_owner_id is None:
        logger.error("api_worker: missing persona_owner_id in %s: %r", request_id, job)
        await _send_struct_error(
            500,
            "invalid_job",
            "Missing persona_owner_id in job payload.",
        )
        with suppress(Exception):
            await redis_queue.lrem(PROCESSING_KEY, 1, raw)
        return

    try:
        persona_owner_id = int(persona_owner_id)
    except Exception:
        logger.error("api_worker: bad persona_owner_id in %s: %r", request_id, job)
        await _send_struct_error(
            500,
            "invalid_job",
            "Invalid persona_owner_id in job payload.",
        )
        with suppress(Exception):
            await redis_queue.lrem(PROCESSING_KEY, 1, raw)
        return

    voice_in = False

    job_key = JOB_KEY_PREFIX + request_id

    try:
        inflight_value = f"inflight:{int(time.time())}"
        ok = await redis_queue.set(job_key, inflight_value, ex=JOB_TTL_SEC, nx=True)
    except Exception as e:
        logger.warning("api_worker: inflight set failed %s: %s", job_key, e)
        ok = False

    if not ok:
        try:
            existing_value = await redis_queue.get(job_key)
        except Exception:
            existing_value = None

        if isinstance(existing_value, (bytes, bytearray)):
            existing_value = existing_value.decode("utf-8", "ignore")

        inflight_ts = None
        if isinstance(existing_value, str) and existing_value.startswith("inflight:"):
            try:
                inflight_ts = int(existing_value.split(":", 1)[1])
            except (TypeError, ValueError):
                inflight_ts = None

        now = int(time.time())
        inflight_age = None if inflight_ts is None else now - inflight_ts

        if inflight_age is None or inflight_age > RESPOND_TIMEOUT:
            try:
                await redis_queue.set(job_key, f"inflight:{now}", ex=JOB_TTL_SEC)
            except Exception as e:
                logger.warning("api_worker: inflight overwrite failed %s: %s", job_key, e)
                with suppress(Exception):
                    await redis_queue.lrem(PROCESSING_KEY, 1, raw)
                return
        else:
            await _send_struct_error(
                409,
                "duplicate_request",
                "Request is already in progress",
            )
            with suppress(Exception):
                await redis_queue.lrem(PROCESSING_KEY, 1, raw)
            return

    start = time.perf_counter()
    heartbeat_stop = asyncio.Event()
    heartbeat_task = asyncio.create_task(_heartbeat_job(redis_queue, job_key, heartbeat_stop))
    error: Dict[str, Any] | None = None
    reply_text: str | None = None

    try:
        has_text = bool(text)
        has_image = bool(image_b64)
        has_voice = bool(voice_b64)

        if not (has_text or has_image or has_voice):
            error = {
                "status": 400,
                "code": "empty_message",
                "message": "Provide message, image_b64 or voice_b64.",
            }
        else:
            if has_voice:
                voice_b64 = clean_base64_payload(voice_b64)
            if has_image:
                image_b64 = clean_base64_payload(image_b64)

            if has_voice and voice_mime and voice_mime not in ALLOWED_VOICE_MIMES:
                error = {
                    "status": 400,
                    "code": "invalid_voice_mime",
                    "message": "voice_mime must be a supported audio format.",
                }
            voice_bytes = b""
            if not error and has_voice:
                voice_bytes = decode_base64_payload(voice_b64)
                if not voice_bytes:
                    error = {
                        "status": 400,
                        "code": "invalid_payload",
                        "message": "voice_b64 must be valid base64.",
                    }
                elif len(voice_bytes) > API_MAX_VOICE_BYTES:
                    error = {
                        "status": 400,
                        "code": "invalid_payload",
                        "message": f"voice_b64 exceeds {API_MAX_VOICE_BYTES} bytes after decoding.",
                    }

            if not error and has_voice and not has_text:
                transcript = await _transcribe_voice_bytes(voice_bytes, voice_mime)
                if transcript:
                    text = transcript
                    has_text = True
                    voice_in = True
                else:
                    error = {
                        "status": 400,
                        "code": "voice_transcription_failed",
                        "message": "Failed to transcribe voice_b64.",
                    }

            if has_voice and has_text and not voice_in:
                logger.debug(
                    "api_worker: both message and voice_b64 in %s; using text message.",
                    request_id,
                )

            if not error and has_image:
                if not image_mime or image_mime not in ALLOWED_IMAGE_MIMES:
                    error = {
                        "status": 400,
                        "code": "invalid_image_mime",
                        "message": "image_mime must be one of: image/jpeg, image/jpg, image/png, image/webp.",
                    }
                else:
                    img_bytes = decode_base64_payload(image_b64)
                    if not img_bytes:
                        error = {
                            "status": 400,
                            "code": "invalid_payload",
                            "message": "image_b64 must be valid base64.",
                        }
                    elif len(img_bytes) > API_MAX_IMAGE_BYTES:
                        error = {
                            "status": 400,
                            "code": "invalid_payload",
                            "message": f"image_b64 exceeds {API_MAX_IMAGE_BYTES} bytes after decoding.",
                        }

            if not error:
                effective_text = text or ""
                try:
                    reply = await asyncio.wait_for(
                        respond_to_user(
                            text=effective_text,
                            chat_id=chat_id,
                            user_id=memory_uid,
                            trigger="api",
                            group_mode=False,
                            is_channel_post=False,
                            channel_title=None,
                            reply_to=None,
                            msg_id=msg_id,
                            voice_in=voice_in,
                            image_b64=image_b64 if has_image else None,
                            image_mime=image_mime if has_image else None,
                            allow_web=allow_web,
                            enforce_on_topic=False,
                            expect_voice_out=False,
                            billing_tier=billing_tier,
                            persona_owner_id=persona_owner_id,
                            memory_uid=memory_uid,
                        ),
                        timeout=RESPOND_TIMEOUT,
                    )
                    reply_text = (reply or "").strip() or "…"
                except asyncio.TimeoutError:
                    logger.error(
                        "api_worker: respond_to_user timeout %ss (req=%s chat=%s)",
                        RESPOND_TIMEOUT, request_id, chat_id,
                    )
                    error = {
                        "status": 504,
                        "code": "upstream_timeout",
                        "message": "Model did not respond in time",
                    }
                except Exception as e:
                    logger.exception(
                        "api_worker: respond_to_user failed (req=%s chat=%s): %s",
                        request_id, chat_id, e,
                    )
                    error = {
                        "status": 500,
                        "code": "internal_error",
                        "message": "Unexpected internal error in worker",
                    }
    finally:
        heartbeat_stop.set()
        with suppress(asyncio.CancelledError):
            await heartbeat_task

        latency_ms = int((time.perf_counter() - start) * 1000)
        latency_breakdown = None
        if enqueued_at is not None:
            queue_latency_ms = max(0, int((time.time() - enqueued_at) * 1000))
            latency_breakdown = {
                "queue_latency_ms": queue_latency_ms,
                "worker_latency_ms": latency_ms,
            }

        if error:
            payload = {
                "ok": False,
                "error": error,
                "latency_ms": latency_ms,
                "request_id": request_id,
            }
        else:
            payload = {
                "ok": True,
                "reply": reply_text,
                "latency_ms": latency_ms,
                "request_id": request_id,
            }
        if latency_breakdown is not None:
            payload["latency_breakdown"] = latency_breakdown

        try:
            data = json.dumps(payload, ensure_ascii=False)
        except Exception as e:
            logger.exception("api_worker: encode result failed %s: %s", request_id, e)
            data = json.dumps(
                {
                    "ok": False,
                    "error": {
                        "status": 500,
                        "code": "internal_error",
                        "message": "Failed to encode worker result",
                    },
                    "latency_ms": latency_ms,
                    "request_id": request_id,
                },
                ensure_ascii=False,
            )

        try:
            pipe = redis_queue.pipeline()
            pipe.rpush(result_key, data)
            pipe.expire(result_key, RESULT_TTL_SEC)
            await pipe.execute()
        except Exception as e:
            logger.exception(
                "api_worker: push result failed key=%s req=%s: %s",
                result_key, request_id, e,
            )

        with suppress(Exception):
            await _mark_done(redis_queue, job_key)
        with suppress(Exception):
            await redis_queue.lrem(PROCESSING_KEY, 1, raw)


async def _sweeper_loop(stop_evt: asyncio.Event, redis_queue) -> None:
    while not stop_evt.is_set():
        try:
            items = await redis_queue.lrange(PROCESSING_KEY, 0, -1)
            if not items:
                await asyncio.sleep(5)
                continue

            for raw in items:
                try:
                    job = json.loads(raw)
                    request_id = job.get("request_id")
                    if not request_id:
                        with suppress(Exception):
                            await redis_queue.lrem(PROCESSING_KEY, 1, raw)
                        continue
                    job_key = JOB_KEY_PREFIX + request_id
                except Exception:
                    with suppress(Exception):
                        await redis_queue.lrem(PROCESSING_KEY, 1, raw)
                    continue

                try:
                    val = await redis_queue.get(job_key)
                except Exception:
                    val = None

                if isinstance(val, (bytes, bytearray)):
                    val = val.decode("utf-8", "ignore")

                if not val:
                    # Маркера нет — считаем застрявшей задачей, возвращаем в очередь
                    with suppress(Exception):
                        await redis_queue.lrem(PROCESSING_KEY, 1, raw)
                    with suppress(Exception):
                        await redis_queue.lpush(API_QUEUE_KEY, raw)
                elif isinstance(val, str) and val.startswith("done"):
                    with suppress(Exception):
                        await redis_queue.lrem(PROCESSING_KEY, 1, raw)
                elif isinstance(val, str) and val.startswith("inflight:"):
                    inflight_ts = None
                    try:
                        inflight_ts = int(val.split(":", 1)[1])
                    except (TypeError, ValueError):
                        inflight_ts = None

                    if inflight_ts is None:
                        continue

                    now = int(time.time())
                    if now - inflight_ts > INFLIGHT_STALE_AFTER_SEC:
                        with suppress(Exception):
                            await redis_queue.lrem(PROCESSING_KEY, 1, raw)
                        with suppress(Exception):
                            await redis_queue.lpush(API_QUEUE_KEY, raw)

            await asyncio.sleep(5)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("api_worker: sweeper error: %s", e)
            await asyncio.sleep(5)


async def _worker_loop(stop_evt: asyncio.Event) -> None:
    redis_queue = get_redis_queue()
    logger.info("api_worker: starting; queue=%s", API_QUEUE_KEY)

    try:
        pending = await redis_queue.lrange(PROCESSING_KEY, 0, -1)
        if pending:
            await redis_queue.rpush(API_QUEUE_KEY, *pending)
            await redis_queue.delete(PROCESSING_KEY)
            logger.info(
                "api_worker: requeued %d pending from %s",
                len(pending), PROCESSING_KEY,
            )
    except Exception as e:
        logger.warning("api_worker: requeue-on-start failed: %s", e)

    sweeper = asyncio.create_task(_sweeper_loop(stop_evt, redis_queue))

    try:
        while not stop_evt.is_set():
            while PROCESSING_TASKS and len(PROCESSING_TASKS) >= MAX_INFLIGHT_TASKS:
                done, _ = await asyncio.wait(
                    PROCESSING_TASKS,
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=1,
                )

            try:
                raw = await redis_queue.brpoplpush(API_QUEUE_KEY, PROCESSING_KEY, timeout=1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("api_worker: Redis error in main loop: %s", e)
                await asyncio.sleep(1)
                continue

            if not raw:
                continue

            task = asyncio.create_task(_handle_job(raw, redis_queue))
            PROCESSING_TASKS.add(task)
            task.add_done_callback(lambda t: PROCESSING_TASKS.discard(t))

    finally:
        sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await sweeper

        if PROCESSING_TASKS:
            logger.info(
                "api_worker: waiting for %d in-flight job(s)",
                len(PROCESSING_TASKS),
            )
            done, pending = await asyncio.wait(PROCESSING_TASKS, timeout=15)
            for t in pending:
                t.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.gather(*pending, return_exceptions=True)


async def _async_main() -> None:
    level = os.environ.get("API_WORKER_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s:%(lineno)d  %(message)s",
        force=True,
    )

    stop_evt = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig_name in ("SIGINT", "SIGTERM"):
        try:
            import signal
            sig = getattr(signal, sig_name, None)
            if sig:
                loop.add_signal_handler(sig, stop_evt.set)
        except Exception:
            pass

    worker = asyncio.create_task(_worker_loop(stop_evt))
    logger.info("api_worker: loop started")

    await stop_evt.wait()
    logger.info("api_worker: stop signal received")

    worker.cancel()
    with suppress(asyncio.CancelledError):
        await worker

    try:
        await close_redis_pools()
    except Exception:
        pass

    logger.info("api_worker: shutdown complete")


def main() -> None:
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
