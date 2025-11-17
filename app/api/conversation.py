#app/api/conversation.py
import asyncio
import hashlib
import time
import logging
import json
import secrets

from typing import Optional, Literal, Dict, Any, List
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field, constr, model_validator
from sqlalchemy import select

from app.config import settings
from app.core.db import session_scope
from app.core.memory import get_redis, get_redis_queue, register_api_memory_uid
from app.core.models import User, ApiKey
from app.api.api_keys import authenticate_key, inc_stats
from app.emo_engine.registry import update_cached_personas_for_owner
from app.emo_engine.persona.constants.user_prefs import normalize_prefs, merge_prefs

router = APIRouter(prefix="/api/v1", tags=["conversation"])

_API_QUEUE_KEY = getattr(settings, "API_QUEUE_KEY", "queue:api")

class PersonaConfig(BaseModel):
    name: Optional[constr(min_length=1, max_length=64)] = None
    age: Optional[int] = Field(None, ge=1, le=120)
    gender: Optional[Literal["male", "female"]] = None

    zodiac: Optional[Literal[
        "Aries","Taurus","Gemini","Cancer","Leo","Virgo","Libra",
        "Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"
    ]] = None

    temperament: Optional[Dict[str, float]] = Field(
        None,
        description="Keys: sanguine, choleric, phlegmatic, melancholic. Values will be normalized to sum 1.0."
    )

    sociality: Optional[Literal["introvert","ambivert","extrovert"]] = None

    archetypes: Optional[List[Literal[
        "Nomad","Architect","Mirror","Spark","Ghost","Anchor","Muse","Trickster",
        "Hero","Sage","Explorer","Creator","Caregiver","Rebel","Lover","Jester"
    ]]] = None

    role: Optional[constr(min_length=1, max_length=1000)] = None


class ConversationRequest(BaseModel):
    user_id: constr(min_length=1, max_length=128) = Field(
        ...,
        description="User ID in your application",
    )

    message: Optional[constr(min_length=1, max_length=4000)] = Field(
        None,
        description="Text message. Optional if image_b64 or voice_b64 provided.",
    )

    image_b64: Optional[str] = Field(
        None,
        description="Base64-encoded image (JPEG/PNG/WEBP). Up to ~5MB decoded.",
    )
    image_mime: Optional[str] = Field(
        None,
        description="MIME type for image_b64: image/jpeg, image/jpg, image/png, image/webp.",
    )

    voice_b64: Optional[str] = Field(
        None,
        description="Base64-encoded voice audio (ogg/opus, mp3, wav, m4a, etc.) "
                    "used for transcription if message is not provided.",
    )
    voice_mime: Optional[str] = Field(
        None,
        description="Optional MIME type for voice_b64 (audio/ogg, audio/mpeg, audio/wav, ...).",
    )

    persona: Optional[PersonaConfig] = None

    @model_validator(mode="after")
    def validate_content(self):
        msg = (self.message or "").strip()
        has_img = bool(self.image_b64)
        has_voice = bool(self.voice_b64)

        if not (msg or has_img or has_voice):
            raise ValueError(
                "At least one of message, image_b64 or voice_b64 must be provided."
            )

        img_mime = (self.image_mime or "").strip()
        if self.image_b64 and not img_mime:
            raise ValueError("image_mime is required when image_b64 is provided.")
        if img_mime and not self.image_b64:
            raise ValueError("image_b64 must be provided when image_mime is set.")

        return self


class ConversationResponse(BaseModel):
    reply: str
    latency_ms: int
    request_id: str


def _make_chat_id(persona_owner_id: int, external_user_id: str) -> int:
    norm_uid = (external_user_id or "").strip()
    raw = f"api:{persona_owner_id}:{norm_uid}".encode("utf-8")
    base = int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")
    return (1 << 62) | (base & ((1 << 62) - 1))


async def _auth_api_key(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
):
    raw = None
    if authorization and authorization.lower().startswith("bearer "):
        raw = authorization.split(None, 1)[1].strip()
    elif x_api_key:
        raw = x_api_key.strip()

    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "missing_api_key", "message": "API key is required"},
        )

    async with session_scope(
        read_only=True,
        stmt_timeout_ms=settings.API_DB_TIMEOUT_AUTH_MS,
    ) as db:
        api_key = await authenticate_key(db, raw)
        if not api_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "invalid_api_key", "message": "Invalid or inactive API key"},
            )
        return {"id": api_key.id, "user_id": api_key.user_id}


def _get_persona_owner_id(owner_user_id: int, api_key_id: int) -> int:
    if getattr(settings, "API_PERSONA_PER_KEY", True):
        return api_key_id
    return owner_user_id


async def _check_rate_limit(request: Request, api_key_id: int) -> None:

    try:
        redis = get_redis()
        if redis is None:
            return

        now = int(time.time())
        window = now // 60

        per_min = settings.API_RATELIMIT_PER_MIN
        burst_factor = settings.API_RATELIMIT_BURST_FACTOR
        if per_min <= 0 or burst_factor <= 0:
            return
        key = f"rl:api:key:{api_key_id}:{window}"

        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, 70)
        if count > per_min * burst_factor:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={"code": "rate_limited", "message": "Too many requests for this API key"},
                headers={"Retry-After": "60"},
            )

        fwd = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        real = (request.headers.get("X-Real-IP") or "").strip()
        ip = fwd or real or (request.client.host if request.client else None) or "unknown"
        if ip != "unknown":
            ip_key = f"rl:api:ip:{ip}:{window}"
            ip_limit = settings.API_RATELIMIT_PER_IP_PER_MIN
            if ip_limit <= 0:
                return
            ic = await redis.incr(ip_key)
            if ic == 1:
                await redis.expire(ip_key, 70)
            if ic > ip_limit:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail={"code": "rate_limited_ip", "message": "Too many requests from this IP"},
                    headers={"Retry-After": "60"},
                )
    except HTTPException:
        raise
    except Exception:
        logging.warning("Rate limiter failed; allowing request (fail-open).", exc_info=True)
        return

@router.post(
    "/conversation",
    response_model=ConversationResponse,
    response_model_exclude_none=True,
)
async def conversation_endpoint(
    payload: ConversationRequest,
    request: Request,
    api_key=Depends(_auth_api_key),
):
    owner_id = int(api_key["user_id"])
    api_key_id = int(api_key["id"])
    persona_owner_id = _get_persona_owner_id(owner_id, api_key_id)

    await _check_rate_limit(request, api_key_id)

    billing_tier: Literal["free", "paid"] | None = None
    norm_prefs: Dict[str, Any] | None = None

    async with session_scope(
        stmt_timeout_ms=settings.API_DB_TIMEOUT_MS,
    ) as db:
        res = await db.execute(
            select(User)
            .where(User.id == owner_id)
            .with_for_update()
        )
        user = res.scalar_one_or_none()

        if not user or (user.free_requests <= 0 and user.paid_requests <= 0):
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    "code": "no_requests",
                    "message": "Not enough requests on your balance for API usage",
                },
            )

        if user.free_requests > 0:
            billing_tier = "free"
            user.free_requests -= 1
        else:
            billing_tier = "paid"
            user.paid_requests -= 1

        user.used_requests += 1
        await db.flush()

        if payload.persona is not None:
            raw_prefs = payload.persona.model_dump(exclude_unset=True, exclude_none=True)
            norm = normalize_prefs(raw_prefs)
            if norm:
                if getattr(settings, "API_PERSONA_PER_KEY", True):
                    ak = await db.get(ApiKey, api_key_id)
                    if not ak:
                        raise HTTPException(
                            status_code=status.HTTP_401_UNAUTHORIZED,
                            detail={"code": "invalid_api_key", "message": "Invalid or inactive API key"},
                        )
                    ak.persona_prefs = merge_prefs(ak.persona_prefs, norm)
                else:
                    user.persona_prefs = merge_prefs(user.persona_prefs, norm)
                norm_prefs = norm

        await db.flush()

    if norm_prefs:
        try:
            await update_cached_personas_for_owner(persona_owner_id, norm_prefs)
        except Exception:
            logging.exception(
                "Failed to update cached personas for persona_owner_id=%s", persona_owner_id
            )

    chat_id = _make_chat_id(persona_owner_id, payload.user_id)
    memory_uid = chat_id

    redis = get_redis()
    seq_key = f"api:msgseq:{chat_id}"
    try:
        if redis is not None:
            msg_id = await redis.incr(seq_key)
            if msg_id == 1:
                ttl = int(getattr(settings, "API_MSGSEQ_TTL_SEC", 7 * 24 * 3600))
                await redis.expire(seq_key, max(60, ttl))
        else:
            msg_id = None
    except Exception:
        msg_id = None
    if msg_id is None:
        msg_id = int(time.time() * 1000)
        request_id = f"{chat_id}-{msg_id}-{secrets.token_hex(3)}"
    else:
        request_id = f"{chat_id}-{msg_id}"
    logging.info(
        "API /conversation owner_id=%s api_key_id=%s persona_owner_id=%s chat_id=%s request_id=%s",
        owner_id,
        api_key_id,
        persona_owner_id,
        chat_id,
        request_id,
    )

    if getattr(settings, "API_PERSONA_PER_KEY", True):
        try:
            await register_api_memory_uid(api_key_id, memory_uid)
        except Exception:
            logging.exception(
                "Failed to register api memory uid api_key_id=%s memory_uid=%s",
                api_key_id,
                memory_uid,
            )

    result_key = f"api:resp:{request_id}"

    job = {
        "request_id": request_id,
        "text": (payload.message or "").strip(),
        "image_b64": payload.image_b64,
        "image_mime": payload.image_mime,
        "voice_b64": payload.voice_b64,
        "voice_mime": payload.voice_mime,
        "chat_id": chat_id,
        "memory_uid": memory_uid,
        "persona_owner_id": persona_owner_id,
        "billing_tier": billing_tier,
        "result_key": result_key,
        "msg_id": msg_id,
        "allow_web": bool(billing_tier == "paid"),
    }

    start = time.perf_counter()
    try:
        result = await _send_job_and_wait(request_id=request_id, job=job)
    except HTTPException as e:
        if 500 <= e.status_code < 600:
            await _refund_request(owner_id, billing_tier)
        raise
    except asyncio.TimeoutError:
        await _refund_request(owner_id, billing_tier)
        logging.exception(
            "API: worker timeout chat_id=%s owner_id=%s request_id=%s",
            chat_id,
            owner_id,
            request_id,
        )
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={
                "code": "upstream_timeout",
                "message": "Model did not respond in time. Please retry.",
                "request_id": request_id,
            },
        )
    except Exception:
        await _refund_request(owner_id, billing_tier)
        logging.exception(
            "API: _send_job_and_wait failed chat_id=%s owner_id=%s request_id=%s",
            chat_id,
            owner_id,
            request_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "internal_error",
                "message": "Unexpected internal error while processing the request",
                "request_id": request_id,
            },
        )

    if not result.get("ok", False):
        err = result.get("error") or {}
        status_code = int(err.get("status") or 500)
        if 500 <= status_code < 600:
            await _refund_request(owner_id, billing_tier)
        raise HTTPException(
            status_code=status_code,
            detail={
                "code": err.get("code") or "worker_error",
                "message": err.get("message") or "Worker failed to process request",
                "request_id": request_id,
            },
        )

    reply = (result.get("reply") or "").strip()
    worker_latency_ms = int(result.get("latency_ms") or 0)
    total_latency_ms = int((time.perf_counter() - start) * 1000)
    latency_ms = worker_latency_ms or total_latency_ms

    try:
        async with session_scope(
            stmt_timeout_ms=settings.API_DB_TIMEOUT_AUTH_MS,
        ) as db:
            await inc_stats(db, api_key_id, latency_ms)
    except Exception:
        logging.exception("API: failed to update usage stats (non-fatal)")

    return ConversationResponse(
        reply=reply,
        latency_ms=latency_ms,
        request_id=request_id,
    )


async def _refund_request(owner_id: int, billing_tier: Optional[str]) -> None:

    if billing_tier not in ("free", "paid"):
        return
    async with session_scope(
        stmt_timeout_ms=settings.API_DB_TIMEOUT_MS,
    ) as db:
        res = await db.execute(
            select(User).where(User.id == owner_id).with_for_update()
        )
        user = res.scalar_one_or_none()
        if not user:
            return
        if billing_tier == "free":
            user.free_requests += 1
        elif billing_tier == "paid":
            user.paid_requests += 1
        if user.used_requests > 0:
            user.used_requests -= 1
        await db.flush()


async def _send_job_and_wait(*, request_id: str, job: Dict[str, Any]) -> Dict[str, Any]:

    redis_q = get_redis_queue()
    if redis_q is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "queue_unavailable",
                "message": "API queue client is not available",
                "request_id": request_id,
            },
        )

    try:
        payload = json.dumps(job, ensure_ascii=False)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "internal_error",
                "message": "Failed to encode internal job payload",
                "request_id": request_id,
            },
        )

    result_key = job["result_key"]

    try:
        await redis_q.lpush(_API_QUEUE_KEY, payload)
    except Exception:
        logging.exception("API: enqueue failed request_id=%s", request_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "queue_unavailable",
                "message": "API queue temporarily unavailable",
                "request_id": request_id,
            },
        )

    timeout_sec = max(1, int(getattr(settings, "API_CALL_TIMEOUT_SEC", 60)))
    try:
        res = await redis_q.blpop(result_key, timeout=timeout_sec)
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.exception("API: wait result failed request_id=%s", request_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "queue_unavailable",
                "message": "Failed to receive reply from worker",
                "request_id": request_id,
            },
        )

    if not res:
        raise asyncio.TimeoutError()

    _, data = res
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8", "ignore")

    try:
        parsed = json.loads(data)
    except Exception:
        logging.exception("API: invalid JSON result for request_id=%s: %r", request_id, str(data)[:200])
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "internal_error",
                "message": "Worker returned invalid data",
                "request_id": request_id,
            },
        )

    return parsed