#app/api/api_keys.py
import hashlib
import secrets

from typing import Optional, Tuple
from sqlalchemy import select, update, func
from sqlalchemy.dialects.postgresql import insert

from app.config import settings
from app.core.models import ApiKey, ApiKeyStats
from app.core.memory import get_redis


API_KEY_PREFIX = "pk_"
API_KEY_BYTES = 32


def _hash_api_key(raw: str) -> str:
    salt = settings.API_KEY_HASH_SECRET
    if not salt:
        raise RuntimeError("API_KEY_HASH_SECRET must be set")
    data = (salt + raw).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _cache_key(key_hash: str) -> str:
    return f"api:key:{key_hash}"


async def create_key(db, user_id: int, label: Optional[str] = None) -> Tuple[ApiKey, str]:

    secret = API_KEY_PREFIX + secrets.token_urlsafe(API_KEY_BYTES)
    key_hash = _hash_api_key(secret)

    api_key = ApiKey(user_id=user_id, key_hash=key_hash, label=label, active=True)
    db.add(api_key)
    await db.flush()

    db.add(ApiKeyStats(api_key_id=api_key.id))

    return api_key, secret


async def cache_active_key(api_key: ApiKey) -> None:
    redis = get_redis()
    if redis is None:
        return

    try:
        ttl = settings.API_KEY_CACHE_TTL_SEC
        ck = _cache_key(api_key.key_hash)
        await redis.hset(
            ck,
            mapping={
                "id": api_key.id,
                "user_id": api_key.user_id,
                "active": 1,
            },
        )
        await redis.expire(ck, max(10, ttl))
    except Exception:
        pass


async def deactivate_key(
    db,
    user_id: int,
    api_key_id: Optional[int] = None,
    raw_key: Optional[str] = None,
) -> None:

    if api_key_id is None and raw_key is None:
        raise ValueError("api_key_id or raw_key is required")

    if raw_key is not None and api_key_id is None:
        key_hash = _hash_api_key(raw_key)
        res = await db.execute(
            select(ApiKey).where(
                ApiKey.user_id == user_id,
                ApiKey.key_hash == key_hash,
            )
        )
    else:
        res = await db.execute(
            select(ApiKey).where(
                ApiKey.user_id == user_id,
                ApiKey.id == api_key_id,
            )
        )

    api_key = res.scalar_one_or_none()
    if not api_key:
        return

    api_key.active = False
    await db.flush()

    redis = get_redis()
    if redis is not None:
        try:
            ck = _cache_key(api_key.key_hash)
            ttl = settings.API_KEY_CACHE_NEGATIVE_TTL_SEC
            await redis.hset(ck, mapping={"active": 0})
            await redis.expire(ck, max(10, ttl))
        except Exception:
            pass


async def list_keys_for_user(db, user_id: int) -> list[ApiKey]:
    res = await db.execute(
        select(ApiKey)
        .where(ApiKey.user_id == user_id)
        .order_by(ApiKey.id.asc())
    )
    return list(res.scalars().all())


async def get_key_for_user(db, user_id: int) -> Optional[ApiKey]:
    res = await db.execute(
        select(ApiKey)
        .where(
            ApiKey.user_id == user_id,
            ApiKey.active.is_(True),
        )
        .order_by(ApiKey.created_at.desc(), ApiKey.id.desc())
        .limit(1)
    )
    return res.scalars().first()


async def get_key_and_stats_for_user(db, user_id: int) -> Tuple[Optional[ApiKey], Optional[ApiKeyStats]]:
    api_key = await get_key_for_user(db, user_id)
    if not api_key:
        return None, None
    stats = await db.get(ApiKeyStats, api_key.id)
    return api_key, stats


async def authenticate_key(db, raw_key: str) -> Optional[ApiKey]:

    if not raw_key:
        return None
    key_hash = _hash_api_key(raw_key.strip())

    redis = get_redis()
    ck = _cache_key(key_hash)

    if redis is not None:
        try:
            cached = await redis.hgetall(ck)
        except Exception:
            cached = None

        if cached:
            def _cached_get(key: str):
                return cached.get(key) if key in cached else cached.get(key.encode("utf-8"))

            def _parse_cached_int(key: str) -> Optional[int]:
                raw = _cached_get(key)
                if raw is None:
                    return None
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="ignore")
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    return None

            cached_active = _parse_cached_int("active")
            if cached_active == 0:
                return None
            cached_positive = cached_active == 1
        else:
            cached_positive = False
    else:
        cached_positive = False

    res = await db.execute(
        select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.active.is_(True))
    )
    api_key = res.scalar_one_or_none()

    if not api_key:
        if redis is not None:
            try:
                ttl = settings.API_KEY_CACHE_NEGATIVE_TTL_SEC
                await redis.hset(ck, mapping={"active": 0})
                await redis.expire(ck, max(10, ttl))
            except Exception:
                pass
        return None

    if redis is not None and not cached_positive:
        await cache_active_key(api_key)
    return api_key


async def inc_stats(db, api_key_id: int, latency_ms: int) -> Optional[ApiKeyStats]:

    try:
        latency = int(latency_ms)
    except (TypeError, ValueError):
        latency = 0
    if latency < 0:
        latency = 0

    stmt = (
        insert(ApiKeyStats)
        .values(
            api_key_id=api_key_id,
            messages_in=1,
            messages_out=1,
            total_latency_ms=latency,
        )
        .on_conflict_do_update(
            index_elements=[ApiKeyStats.api_key_id],
            set_={
                "messages_in": ApiKeyStats.messages_in + 1,
                "messages_out": ApiKeyStats.messages_out + 1,
                "total_latency_ms": ApiKeyStats.total_latency_ms + latency,
            },
        )
        .returning(ApiKeyStats)
    )
    res = await db.execute(stmt)
    stats = res.scalar_one()

    await db.execute(
        update(ApiKey)
        .where(ApiKey.id == api_key_id)
        .values(last_used_at=func.now())
    )
    return stats
