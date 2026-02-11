#app/api/conversation.py
import asyncio
import hashlib
import time
import logging
import json
import secrets
import ipaddress

from typing import Optional, Literal, Dict, Any, List
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field, constr, field_validator, model_validator
from sqlalchemy import case, literal, or_, select, update, cast
from sqlalchemy.dialects.postgresql import JSONB

from app.config import settings
from app.core.db import session_scope
from app.core.models import User, ApiKey, RefundOutbox
from app.core.media_limits import (
    ALLOWED_IMAGE_MIMES,
    ALLOWED_VOICE_MIMES,
    API_MAX_IMAGE_BYTES,
    API_MAX_VOICE_BYTES,
    clean_base64_payload,
    decode_base64_payload,
)
from app.core.memory import get_redis, get_redis_queue, register_api_memory_uid
from app.api.api_keys import authenticate_key, inc_stats
from app.emo_engine.registry import update_cached_personas_for_owner
from app.emo_engine.persona.constants.user_prefs import normalize_prefs
from app.tasks.queue_schema import validate_api_job

router = APIRouter(prefix="/api/v1", tags=["conversation"])

logger = logging.getLogger(__name__)

_REFUND_RETRY_ATTEMPTS = 3
_REFUND_RETRY_BACKOFF_BASE_SEC = 0.2

_RATE_LIMIT_REDIS_RETRIES = 2
_RATE_LIMIT_REDIS_RETRY_DELAY_SEC = 0.05
_RATE_LIMIT_WINDOW_SEC = 60
_RATE_LIMIT_KEY_TTL_SEC = 70

_RATE_LIMIT_LUA_SCRIPT = """
local api_key = KEYS[1]
local ip_key = KEYS[2]

local ttl = tonumber(ARGV[1])
local check_ip = tonumber(ARGV[2])
local api_limit = tonumber(ARGV[3])
local ip_limit = tonumber(ARGV[4])

local api_count = redis.call('INCR', api_key)
if api_count == 1 then
    redis.call('EXPIRE', api_key, ttl)
end

local ip_count = 0
if check_ip == 1 then
    ip_count = redis.call('INCR', ip_key)
    if ip_count == 1 then
        redis.call('EXPIRE', ip_key, ttl)
    end
end

local api_exceeded = 0
if api_count > api_limit then
    api_exceeded = 1
end

local ip_exceeded = 0
if check_ip == 1 and ip_count > ip_limit then
    ip_exceeded = 1
end

return {api_count, ip_count, api_exceeded, ip_exceeded}
"""
_RATE_LIMIT_LUA_SHA: Optional[str] = None


def _get_api_queue_key() -> str:
    return getattr(settings, "API_QUEUE_KEY", "queue:api")


def _idempotency_redis_key(api_key_id: int, idem_key: str) -> str:
    return f"api:idem:{api_key_id}:{idem_key}"


def _normalize_idempotency_key(value: Optional[str]) -> Optional[str]:
    max_len = 256
    if value is None:
        return None
    key = value.strip()
    if not key:
        return None
    if len(key) > max_len:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_idempotency_key",
                "message": f"Idempotency-Key length must be <= {max_len} characters.",
            },
        )
    return key


def _build_idempotency_request_hash(payload: "ConversationRequest") -> str:
    persona_payload = None
    if payload.persona is not None:
        persona_payload = payload.persona.model_dump(exclude_none=True)

    normalized_payload = {
        "user_id": payload.user_id,
        "message": (payload.message or "").strip() or None,
        "image_b64": payload.image_b64,
        "image_mime": payload.image_mime,
        "voice_b64": payload.voice_b64,
        "voice_mime": payload.voice_mime,
        "persona": persona_payload,
    }
    canonical_payload = json.dumps(
        normalized_payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


def _read_final_idempotency_record(raw_value: str, current_request_hash: Optional[str]) -> tuple[int, dict]:
    try:
        parsed = json.loads(raw_value)
        status_code = int(parsed.get("status_code", 500))
        body = parsed.get("body") or {}
        cached_request_hash = parsed.get("request_hash")
    except Exception:
        return 500, {}

    if cached_request_hash and current_request_hash and cached_request_hash != current_request_hash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "idempotency_key_reused_with_different_payload",
                "message": "Idempotency-Key was already used with a different request payload.",
            },
        )

    return status_code, body


def _is_trusted_proxy(client_host: Optional[str]) -> bool:
    trusted_entries = getattr(settings, "TRUSTED_PROXY_IPS", None) or []
    if not client_host or not trusted_entries:
        return False

    try:
        client_ip = ipaddress.ip_address(client_host)
    except ValueError:
        client_ip = None

    for entry in trusted_entries:
        if not entry:
            continue
        if "/" in entry:
            try:
                network = ipaddress.ip_network(entry, strict=False)
            except ValueError:
                continue
            if client_ip and client_ip in network:
                return True
            continue

        try:
            entry_ip = ipaddress.ip_address(entry)
        except ValueError:
            entry_ip = None

        if entry_ip and client_ip and entry_ip == client_ip:
            return True
        if not entry_ip and entry.lower() == client_host.lower():
            return True

    return False


def _resolve_rate_limit_ip(request: Request) -> str:
    def _is_valid_ip(value: str) -> bool:
        if not value:
            return False
        try:
            ipaddress.ip_address(value)
            return True
        except ValueError:
            return False

    client_host = request.client.host if request.client else None
    if not client_host:
        return "unknown"

    if not _is_trusted_proxy(client_host):
        return client_host

    fwd = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    real = (request.headers.get("X-Real-IP") or "").strip()
    if _is_valid_ip(fwd):
        return fwd
    if _is_valid_ip(real):
        return real
    return client_host


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


def _build_persona_profile_id(persona: Optional["PersonaConfig"]) -> Optional[str]:
    if persona is None:
        return None
    try:
        data = persona.model_dump(exclude_none=True)
    except Exception:
        return None
    if not data:
        return None
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _scoped_memory_uid(base_uid: int, profile_id: str) -> int:
    raw = f"{base_uid}:{profile_id}".encode("utf-8")
    digest = hashlib.sha256(raw).digest()
    return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)


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
        description="Optional MIME type for voice_b64 (audio/ogg, audio/mpeg, audio/wav, ...). "
                    "If omitted, the server may detect the format; if detection fails, "
                    "provide voice_mime explicitly.",
    )

    persona: Optional[PersonaConfig] = None

    @field_validator("user_id", mode="before")
    @classmethod
    def normalize_user_id(cls, value):
        if not isinstance(value, str):
            return value

        normalized = value.strip()
        if not normalized:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "invalid_payload",
                    "message": "user_id must not be empty.",
                },
            )
        return normalized

    @model_validator(mode="after")
    def validate_content(self):
        def _payload_error(message: str) -> None:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_payload", "message": message},
            )

        def _approx_b64_size(b64_value: str) -> tuple[str, int]:
            cleaned = clean_base64_payload(b64_value)
            padding = len(cleaned) - len(cleaned.rstrip("="))
            approx = (len(cleaned) * 3) // 4 - padding
            return cleaned, max(approx, 0)

        msg = (self.message or "").strip()
        has_img = bool(self.image_b64)
        has_voice = bool(self.voice_b64)

        if not (msg or has_img or has_voice):
            _payload_error("At least one of message, image_b64 or voice_b64 must be provided.")

        img_mime = (self.image_mime or "").strip()
        if self.image_b64 and not img_mime:
            _payload_error("image_mime is required when image_b64 is provided.")
        if img_mime and not self.image_b64:
            _payload_error("image_b64 must be provided when image_mime is set.")
        if img_mime:
            normalized_img_mime = img_mime.lower()
            if normalized_img_mime not in ALLOWED_IMAGE_MIMES:
                _payload_error(
                    "image_mime must be one of: image/jpeg, image/jpg, image/png, image/webp."
                )
            self.image_mime = normalized_img_mime

        voice_mime = (self.voice_mime or "").strip()
        if voice_mime:
            normalized_voice_mime = voice_mime.lower()
            if normalized_voice_mime not in ALLOWED_VOICE_MIMES:
                _payload_error(
                    "voice_mime must be one of: audio/ogg, audio/opus, audio/mpeg, "
                    "audio/mp3, audio/wav, audio/x-wav, audio/webm, audio/mp4, "
                    "audio/m4a, audio/aac."
                )
            self.voice_mime = normalized_voice_mime

        strict_validation = bool(getattr(settings, "API_STRICT_BASE64_VALIDATION", False))
        if self.image_b64:
            img_b64_clean, img_size = _approx_b64_size(self.image_b64)
            if img_size > API_MAX_IMAGE_BYTES:
                _payload_error(f"image_b64 exceeds {API_MAX_IMAGE_BYTES} bytes after decoding.")
            if strict_validation:
                if not decode_base64_payload(img_b64_clean):
                    _payload_error("image_b64 must be valid base64.")
            self.image_b64 = img_b64_clean

        if self.voice_b64:
            voice_b64_clean, voice_size = _approx_b64_size(self.voice_b64)
            if voice_size > API_MAX_VOICE_BYTES:
                _payload_error(f"voice_b64 exceeds {API_MAX_VOICE_BYTES} bytes after decoding.")
            if strict_validation:
                if not decode_base64_payload(voice_b64_clean):
                    _payload_error("voice_b64 must be valid base64.")
            self.voice_b64 = voice_b64_clean

        return self


class LatencyBreakdown(BaseModel):
    queue_latency_ms: int = Field(0, description="Queue wait time in milliseconds.")
    worker_latency_ms: int = Field(0, description="Worker processing time in milliseconds.")
    total_latency_ms: int = Field(..., description="End-to-end request latency in milliseconds.")


class ConversationResponse(BaseModel):
    reply: str
    latency_ms: int
    latency_breakdown: Optional[LatencyBreakdown] = Field(
        None,
        description="Latency breakdown with stable fields (zeros when unavailable).",
    )
    request_id: str


def _make_chat_id(persona_owner_id: int, external_user_id: str) -> int:
    raw = f"api:{persona_owner_id}:{external_user_id}".encode("utf-8")
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
    global _RATE_LIMIT_LUA_SHA

    per_min = settings.API_RATELIMIT_PER_MIN
    burst_factor = settings.API_RATELIMIT_BURST_FACTOR
    if per_min <= 0 or burst_factor <= 0:
        return

    ip_limit = settings.API_RATELIMIT_PER_IP_PER_MIN
    ip = _resolve_rate_limit_ip(request)
    check_ip = ip != "unknown" and ip_limit > 0

    for attempt in range(1, _RATE_LIMIT_REDIS_RETRIES + 2):
        redis = get_redis()
        if redis is None:
            last_error: Exception = RuntimeError("redis client unavailable")
        else:
            try:
                now = int(time.time())
                window = now // _RATE_LIMIT_WINDOW_SEC
                api_key = f"rl:api:key:{api_key_id}:{window}"
                ip_key = f"rl:api:ip:{ip}:{window}" if check_ip else ""

                result = None
                if _RATE_LIMIT_LUA_SHA:
                    try:
                        result = await redis.evalsha(
                            _RATE_LIMIT_LUA_SHA,
                            2,
                            api_key,
                            ip_key,
                            _RATE_LIMIT_KEY_TTL_SEC,
                            int(check_ip),
                            int(per_min * burst_factor),
                            int(ip_limit),
                        )
                    except Exception as exc:
                        if "NOSCRIPT" in str(exc).upper():
                            _RATE_LIMIT_LUA_SHA = None
                        else:
                            raise

                if result is None:
                    result = await redis.eval(
                        _RATE_LIMIT_LUA_SCRIPT,
                        2,
                        api_key,
                        ip_key,
                        _RATE_LIMIT_KEY_TTL_SEC,
                        int(check_ip),
                        int(per_min * burst_factor),
                        int(ip_limit),
                    )
                    if hasattr(redis, "script_load"):
                        try:
                            _RATE_LIMIT_LUA_SHA = await redis.script_load(_RATE_LIMIT_LUA_SCRIPT)
                        except Exception:
                            _RATE_LIMIT_LUA_SHA = None

                _, _, api_exceeded, ip_exceeded = [int(v) for v in result]

                if api_exceeded:
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail={"code": "rate_limited", "message": "Too many requests for this API key"},
                        headers={"Retry-After": "60"},
                    )

                if check_ip and ip_exceeded:
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail={"code": "rate_limited_ip", "message": "Too many requests from this IP"},
                        headers={"Retry-After": "60"},
                    )
                return
            except HTTPException:
                raise
            except Exception as exc:
                last_error = exc

        if attempt <= _RATE_LIMIT_REDIS_RETRIES:
            exc_info = (type(last_error), last_error, last_error.__traceback__)
            logger.warning(
                "Rate limiter redis attempt %s/%s failed; retrying",
                attempt,
                _RATE_LIMIT_REDIS_RETRIES + 1,
                exc_info=exc_info,
            )
            await asyncio.sleep(_RATE_LIMIT_REDIS_RETRY_DELAY_SEC)
            continue

        exc_info = (type(last_error), last_error, last_error.__traceback__)
        logger.error(
            "Rate limiter redis unavailable after %s attempts",
            _RATE_LIMIT_REDIS_RETRIES + 1,
            exc_info=exc_info,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "rate_limiter_unavailable",
                "message": "Rate limiter is temporarily unavailable",
            },
        )

@router.post(
    "/conversation",
    response_model=ConversationResponse,
    response_model_exclude_none=True,
)
async def conversation_endpoint(
    payload: ConversationRequest,
    request: Request,
    api_key=Depends(_auth_api_key),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    owner_id = int(api_key["user_id"])
    api_key_id = int(api_key["id"])
    persona_owner_id = _get_persona_owner_id(owner_id, api_key_id)

    idem_key = _normalize_idempotency_key(idempotency_key)
    current_request_hash = _build_idempotency_request_hash(payload) if idem_key else None
    idem_redis = None
    idem_cache_key = None
    idem_lock_acquired = False
    if idem_key:
        idem_redis = get_redis()
        if idem_redis is not None:
            idem_cache_key = _idempotency_redis_key(api_key_id, idem_key)
            cached = None
            try:
                cached = await idem_redis.get(idem_cache_key)
            except Exception:
                cached = None
            if cached:
                if isinstance(cached, (bytes, bytearray)):
                    cached = cached.decode("utf-8", "ignore")
                if str(cached).startswith("inflight:"):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "code": "idempotency_in_flight",
                            "message": "Request with this Idempotency-Key is already in progress.",
                        },
                    )
                status_code, body = _read_final_idempotency_record(cached, current_request_hash)
                if status_code < 400:
                    return ConversationResponse(**body)
                raise HTTPException(status_code=status_code, detail=body.get("detail") or body)

            inflight_ttl = int(
                getattr(
                    settings,
                    "API_IDEMPOTENCY_INFLIGHT_TTL_SEC",
                    int(getattr(settings, "API_CALL_TIMEOUT_SEC", 135)) + 20,
                )
            )
            try:
                idem_lock_acquired = await idem_redis.set(
                    idem_cache_key,
                    f"inflight:{time.time()}",
                    nx=True,
                    ex=max(1, inflight_ttl),
                )
            except Exception:
                idem_lock_acquired = False
            if not idem_lock_acquired:
                existing = None
                try:
                    existing = await idem_redis.get(idem_cache_key)
                except Exception:
                    existing = None
                if existing:
                    if isinstance(existing, (bytes, bytearray)):
                        existing = existing.decode("utf-8", "ignore")
                    if str(existing).startswith("inflight:"):
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail={
                                "code": "idempotency_in_flight",
                                "message": "Request with this Idempotency-Key is already in progress.",
                            },
                        )
                    status_code, body = _read_final_idempotency_record(existing, current_request_hash)
                    if status_code < 400:
                        return ConversationResponse(**body)
                    raise HTTPException(status_code=status_code, detail=body.get("detail") or body)

    async def _store_idempotency_result(status_code: int, body: dict) -> None:
        if not (idem_lock_acquired and idem_redis is not None and idem_cache_key):
            return
        detail = body.get("detail") if isinstance(body, dict) else None
        detail_code = detail.get("code") if isinstance(detail, dict) else None
        should_store = False
        if 200 <= status_code < 300:
            should_store = True
        elif 400 <= status_code < 500 and detail_code in {"invalid_payload", "payload_too_large"}:
            should_store = True
        if not should_store:
            try:
                await idem_redis.delete(idem_cache_key)
            except Exception:
                logger.exception("Failed to clear idempotency result")
            return
        ttl = int(getattr(settings, "API_IDEMPOTENCY_TTL_SEC", 3600))
        try:
            await idem_redis.set(
                idem_cache_key,
                json.dumps(
                    {
                        "status_code": status_code,
                        "body": body,
                        "request_hash": current_request_hash,
                    },
                    ensure_ascii=False,
                ),
                ex=max(60, ttl),
            )
        except Exception:
            logger.exception("Failed to store idempotency result")

    try:
        await _check_rate_limit(request, api_key_id)

        billing_tier: Literal["free", "paid"] | None = None
        norm_prefs: Dict[str, Any] | None = None

        async with session_scope(
            stmt_timeout_ms=settings.API_DB_TIMEOUT_MS,
        ) as db:
            billing_snapshot = User.__table__.alias("billing_snapshot")
            billing_stmt = (
                update(User)
                .where(User.id == owner_id)
                .where(or_(User.free_requests > 0, User.paid_requests > 0))
                .where(User.id == billing_snapshot.c.id)
                .values(
                    free_requests=case(
                        (billing_snapshot.c.free_requests > 0, billing_snapshot.c.free_requests - 1),
                        else_=billing_snapshot.c.free_requests,
                    ),
                    paid_requests=case(
                        (billing_snapshot.c.free_requests > 0, billing_snapshot.c.paid_requests),
                        else_=billing_snapshot.c.paid_requests - 1,
                    ),
                    used_requests=billing_snapshot.c.used_requests + 1,
                )
                .returning(
                    case(
                        (billing_snapshot.c.free_requests > 0, literal("free")),
                        else_=literal("paid"),
                    ).label("billing_tier")
                )
            )
            billing_tier = (await db.execute(billing_stmt)).scalar_one_or_none()

            if not billing_tier:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail={
                        "code": "no_requests",
                        "message": "Not enough requests on your balance for API usage",
                    },
                )

            if payload.persona is not None:
                raw_prefs = payload.persona.model_dump(exclude_unset=True, exclude_none=True)
                norm = normalize_prefs(raw_prefs)
                if norm:
                    norm_json = cast(literal(norm), JSONB)
                    if getattr(settings, "API_PERSONA_PER_KEY", True):
                        result = await db.execute(
                            update(ApiKey)
                            .where(ApiKey.id == api_key_id)
                            .values(persona_prefs=ApiKey.persona_prefs.op("||")(norm_json))
                        )
                        if result.rowcount == 0:
                            raise HTTPException(
                                status_code=status.HTTP_401_UNAUTHORIZED,
                                detail={"code": "invalid_api_key", "message": "Invalid or inactive API key"},
                            )
                    else:
                        result = await db.execute(
                            update(User)
                            .where(User.id == owner_id)
                            .values(persona_prefs=User.persona_prefs.op("||")(norm_json))
                        )
                        if result.rowcount == 0:
                            raise HTTPException(
                                status_code=status.HTTP_401_UNAUTHORIZED,
                                detail={"code": "invalid_user", "message": "Invalid or inactive user"},
                            )
                    norm_prefs = norm

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
        persona_profile_id = _build_persona_profile_id(payload.persona)
        scoped_memory_uid = (
            _scoped_memory_uid(memory_uid, persona_profile_id)
            if persona_profile_id
            else memory_uid
        )
        logging.info(
            "API /conversation owner_id=%s api_key_id=%s persona_owner_id=%s chat_id=%s request_id=%s persona_profile_id=%s",
            owner_id,
            api_key_id,
            persona_owner_id,
            chat_id,
            request_id,
            persona_profile_id,
        )

        if getattr(settings, "API_PERSONA_PER_KEY", True):
            try:
                await register_api_memory_uid(api_key_id, scoped_memory_uid)
            except Exception:
                logging.exception(
                    "Failed to register api memory uid api_key_id=%s memory_uid=%s",
                    api_key_id,
                    scoped_memory_uid,
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
            "memory_uid": scoped_memory_uid,
            "persona_owner_id": persona_owner_id,
            "persona_profile_id": persona_profile_id,
            "api_key_id": api_key_id,
            "billing_tier": billing_tier,
            "result_key": result_key,
            "msg_id": msg_id,
            "allow_web": bool(billing_tier == "paid"),
            "enqueued_at": time.time(),
        }

        start = time.perf_counter()

        async def _handle_refund_compensation_failure(
            original_error_code: str,
            compensation_error: RuntimeError,
        ) -> None:
            logger.critical(
                "Refund compensation failed request_id=%s owner_id=%s original_error_code=%s",
                request_id,
                owner_id,
                original_error_code,
                exc_info=compensation_error,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "code": "refund_compensation_failed",
                    "message": "Request failed with a risk of billing desynchronization.",
                    "request_id": request_id,
                },
            )

        try:
            result = await _send_job_and_wait(request_id=request_id, job=job)
        except HTTPException as e:
            detail = e.detail if isinstance(e.detail, dict) else {}
            err_code = detail.get("code")
            if 500 <= e.status_code < 600 or err_code in {
                "invalid_payload",
                "payload_too_large",
            }:
                original_error_code = str(err_code or e.status_code)
                try:
                    await _safe_refund_request(
                        owner_id,
                        billing_tier,
                        request_id=request_id,
                        reason=f"http_exception:{original_error_code}",
                    )
                except RuntimeError as compensation_error:
                    await _handle_refund_compensation_failure(
                        original_error_code,
                        compensation_error,
                    )
            raise
        except asyncio.TimeoutError:
            try:
                await _safe_refund_request(
                    owner_id,
                    billing_tier,
                    request_id=request_id,
                    reason="timeout",
                )
            except RuntimeError as compensation_error:
                await _handle_refund_compensation_failure(
                    "upstream_timeout",
                    compensation_error,
                )
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
            try:
                await _safe_refund_request(
                    owner_id,
                    billing_tier,
                    request_id=request_id,
                    reason="unexpected_exception",
                )
            except RuntimeError as compensation_error:
                await _handle_refund_compensation_failure(
                    "internal_error",
                    compensation_error,
                )
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
            err_code = err.get("code")
            if status_code >= 500 or err_code in {
                "invalid_payload",
                "voice_transcription_failed",
                "duplicate_request",
            }:
                original_error_code = str(err_code or status_code)
                try:
                    await _safe_refund_request(
                        owner_id,
                        billing_tier,
                        request_id=request_id,
                        reason=f"worker_error:{original_error_code}",
                    )
                except RuntimeError as compensation_error:
                    await _handle_refund_compensation_failure(
                        original_error_code,
                        compensation_error,
                    )
            raise HTTPException(
                status_code=status_code,
                detail={
                    "code": err_code or "worker_error",
                    "message": err.get("message") or "Worker failed to process request",
                    "request_id": request_id,
                },
            )

        reply = (result.get("reply") or "").strip()
        total_latency_ms = int((time.perf_counter() - start) * 1000)

        def _coerce_int(value: Any) -> Optional[int]:
            if value is None:
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        raw_breakdown = result.get("latency_breakdown")
        latency_breakdown_data = raw_breakdown if isinstance(raw_breakdown, dict) else {}
        queue_latency_ms = _coerce_int(latency_breakdown_data.get("queue_latency_ms")) or 0
        worker_latency_ms = _coerce_int(latency_breakdown_data.get("worker_latency_ms"))
        if worker_latency_ms is None:
            worker_latency_ms = int(result.get("latency_ms") or 0)
        worker_latency_ms = worker_latency_ms or 0

        metrics_data = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
        llm_call_ms = _coerce_int(metrics_data.get("llm_call_ms"))
        memory_retrieval_ms = _coerce_int(metrics_data.get("memory_retrieval_ms"))
        queue_wait_ms = _coerce_int(metrics_data.get("queue_wait_ms"))
        total_ms = _coerce_int(metrics_data.get("total_ms"))

        latency_ms = worker_latency_ms or total_latency_ms
        latency_breakdown = LatencyBreakdown(
            queue_latency_ms=queue_latency_ms,
            worker_latency_ms=worker_latency_ms,
            total_latency_ms=total_latency_ms,
        )

        logger.info(
            "API /conversation completed",
            extra={
                "request_id": request_id,
                "queue_latency_ms": queue_latency_ms,
                "worker_latency_ms": worker_latency_ms,
                "total_latency_ms": total_latency_ms,
                "queue_wait_ms": queue_wait_ms,
                "llm_call_ms": llm_call_ms,
                "memory_retrieval_ms": memory_retrieval_ms,
                "responder_total_ms": total_ms,
                "billing_tier": billing_tier,
            },
        )

        try:
            async with session_scope(
                stmt_timeout_ms=settings.API_DB_TIMEOUT_AUTH_MS,
            ) as db:
                await inc_stats(db, api_key_id, latency_ms)
        except Exception:
            logging.exception("API: failed to update usage stats (non-fatal)")

        response = ConversationResponse(
            reply=reply,
            latency_ms=latency_ms,
            latency_breakdown=latency_breakdown,
            request_id=request_id,
        )
        await _store_idempotency_result(200, response.model_dump(exclude_none=True))
        return response
    except HTTPException as exc:
        await _store_idempotency_result(exc.status_code, {"detail": exc.detail})
        raise


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


async def _store_refund_outbox_task(
    owner_id: int,
    billing_tier: Optional[str],
    *,
    request_id: str,
    reason: str,
    attempts: int,
    last_error: Optional[str],
) -> Optional[int]:
    try:
        async with session_scope(
            stmt_timeout_ms=settings.API_DB_TIMEOUT_MS,
        ) as db:
            row = RefundOutbox(
                owner_id=owner_id,
                billing_tier=billing_tier,
                request_id=request_id,
                reason=reason,
                status="pending",
                attempts=max(0, int(attempts or 0)),
                last_error=last_error,
            )
            db.add(row)
            await db.flush()
            return int(row.id)
    except Exception:
        logger.exception(
            "Failed to store deferred refund task owner_id=%s billing_tier=%s request_id=%s reason=%s",
            owner_id,
            billing_tier,
            request_id,
            reason,
        )
        return None


async def _safe_refund_request(
    owner_id: int,
    billing_tier: Optional[str],
    *,
    request_id: str,
    reason: str,
) -> bool:
    if billing_tier not in ("free", "paid"):
        return True

    last_error: Optional[str] = None
    for attempt in range(1, _REFUND_RETRY_ATTEMPTS + 1):
        try:
            await _refund_request(owner_id, billing_tier)
            return True
        except Exception as exc:
            last_error = repr(exc)
            logger.exception(
                "Refund attempt failed owner_id=%s billing_tier=%s request_id=%s reason=%s attempt=%s",
                owner_id,
                billing_tier,
                request_id,
                reason,
                attempt,
            )
            if attempt < _REFUND_RETRY_ATTEMPTS:
                delay = _REFUND_RETRY_BACKOFF_BASE_SEC * (2 ** (attempt - 1))
                await asyncio.sleep(delay)

    outbox_id = await _store_refund_outbox_task(
        owner_id,
        billing_tier,
        request_id=request_id,
        reason=reason,
        attempts=_REFUND_RETRY_ATTEMPTS,
        last_error=last_error,
    )
    if outbox_id is None:
        raise RuntimeError(
            "Refund compensation failed after retry and outbox-save failure "
            f"owner_id={owner_id} request_id={request_id} reason={reason}"
        )
    return False


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

    err = validate_api_job(job)
    if err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_payload",
                "message": "Invalid job payload",
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

    max_payload = int(getattr(settings, "API_QUEUE_MAX_PAYLOAD_BYTES", 128 * 1024))
    payload_size = len(payload.encode("utf-8"))
    if max_payload > 0 and payload_size > max_payload:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "code": "payload_too_large",
                "message": "Job payload exceeds the maximum allowed size",
                "request_id": request_id,
            },
        )

    result_key = job["result_key"]

    try:
        await redis_q.lpush(_get_api_queue_key(), payload)
        try:
            depth = await redis_q.llen(_get_api_queue_key())
            logger.info("API queue depth=%s request_id=%s", depth, request_id)
        except Exception:
            pass
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
