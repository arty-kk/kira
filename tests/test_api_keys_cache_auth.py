import asyncio
import unittest
from unittest import mock

from app.api import api_keys
from app.config import settings


class _FakeRedis:
    def __init__(self, initial_hashes=None) -> None:
        self.hashes = dict(initial_hashes or {})
        self.hset_calls = []
        self.expire_calls = []

    async def hgetall(self, key):
        value = self.hashes.get(key)
        if value is None:
            return {}
        return dict(value)

    async def hset(self, key, mapping):
        self.hset_calls.append((key, dict(mapping)))
        current = dict(self.hashes.get(key, {}))
        current.update(mapping)
        self.hashes[key] = current

    async def expire(self, key, ttl):
        self.expire_calls.append((key, ttl))


class _ForbiddenDbExecute:
    async def execute(self, *_args, **_kwargs):
        raise AssertionError("db.execute must not be called")


class _DbAuthResult:
    def __init__(self, api_key):
        self.api_key = api_key
        self.execute_calls = 0

    async def execute(self, *_args, **_kwargs):
        self.execute_calls += 1

        class _Res:
            def __init__(self, key):
                self._key = key

            def scalar_one_or_none(self):
                return self._key

        return _Res(self.api_key)


class _CreateDeactivateDb:
    def __init__(self) -> None:
        self._next_id = 101
        self.added = []
        self.active_key = None

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, api_keys.ApiKey):
            obj.id = self._next_id
            self._next_id += 1
            self.active_key = obj

    async def flush(self):
        return None

    async def execute(self, *_args, **_kwargs):
        if self.active_key is None:
            raise AssertionError("No key for deactivation")

        class _Res:
            def __init__(self, key):
                self._key = key

            def scalar_one_or_none(self):
                return self._key

        return _Res(self.active_key)


class _TransactionalCreateDb:
    def __init__(self) -> None:
        self._next_id = 501
        self._pending = None
        self._committed = {}

    def add(self, obj):
        if isinstance(obj, api_keys.ApiKey):
            obj.id = self._next_id
            self._next_id += 1
            self._pending = obj

    async def flush(self):
        return None

    async def execute(self, *_args, **_kwargs):
        key = self._pending if self._pending and self._pending.key_hash in self._committed else None

        class _Res:
            def __init__(self, api_key):
                self._api_key = api_key

            def scalar_one_or_none(self):
                return self._api_key

        return _Res(key)

    def rollback(self):
        self._pending = None


class ApiKeysCacheAuthTests(unittest.TestCase):
    def test_cache_hit_active_true_validates_in_db(self) -> None:
        key_hash = "hash-cache-hit"
        redis = _FakeRedis(
            {
                f"api:key:{key_hash}": {
                    b"id": b"7",
                    "user_id": "42",
                    b"active": b"1",
                }
            }
        )
        db_api_key = api_keys.ApiKey(id=9, user_id=99, key_hash=key_hash, active=True)
        db = _DbAuthResult(db_api_key)

        with (
            mock.patch.object(api_keys, "get_redis", return_value=redis),
            mock.patch.object(api_keys, "_hash_api_key", return_value=key_hash),
        ):
            result = asyncio.run(api_keys.authenticate_key(db, "raw-token"))

        self.assertEqual(db.execute_calls, 1)
        self.assertIsNotNone(result)
        self.assertEqual(result.id, 9)
        self.assertEqual(result.user_id, 99)
        self.assertEqual(result.key_hash, key_hash)
        self.assertTrue(result.active)

    def test_positive_cache_hit_without_db_record_sets_negative_cache(self) -> None:
        key_hash = "hash-cache-miss"
        redis = _FakeRedis({f"api:key:{key_hash}": {b"active": b"1"}})
        db = _DbAuthResult(None)

        original_negative_ttl = settings.API_KEY_CACHE_NEGATIVE_TTL_SEC
        settings.API_KEY_CACHE_NEGATIVE_TTL_SEC = 17
        try:
            with (
                mock.patch.object(api_keys, "get_redis", return_value=redis),
                mock.patch.object(api_keys, "_hash_api_key", return_value=key_hash),
            ):
                result = asyncio.run(api_keys.authenticate_key(db, "raw-token"))

            self.assertIsNone(result)
            self.assertEqual(db.execute_calls, 1)
            self.assertIn((f"api:key:{key_hash}", {"active": 0}), redis.hset_calls)
            self.assertIn((f"api:key:{key_hash}", max(10, settings.API_KEY_CACHE_NEGATIVE_TTL_SEC)), redis.expire_calls)
        finally:
            settings.API_KEY_CACHE_NEGATIVE_TTL_SEC = original_negative_ttl

    def test_deactivation_negative_cache_blocks_without_db(self) -> None:
        key_hash = "hash-negative"
        redis = _FakeRedis({f"api:key:{key_hash}": {b"active": b"0"}})
        db = _ForbiddenDbExecute()

        with (
            mock.patch.object(api_keys, "get_redis", return_value=redis),
            mock.patch.object(api_keys, "_hash_api_key", return_value=key_hash),
        ):
            result = asyncio.run(api_keys.authenticate_key(db, "raw-token"))

        self.assertIsNone(result)

    def test_eventual_consistency_window_after_state_change(self) -> None:
        db = _CreateDeactivateDb()
        redis = _FakeRedis()

        original_ttl = settings.API_KEY_CACHE_TTL_SEC
        original_negative_ttl = settings.API_KEY_CACHE_NEGATIVE_TTL_SEC
        original_hash_secret = settings.API_KEY_HASH_SECRET
        settings.API_KEY_CACHE_TTL_SEC = 31
        settings.API_KEY_CACHE_NEGATIVE_TTL_SEC = 17
        settings.API_KEY_HASH_SECRET = "test-hash-secret"

        try:
            with mock.patch.object(api_keys, "get_redis", return_value=redis):
                created, raw_secret = asyncio.run(api_keys.create_key(db, user_id=55, label="cache-flow"))
                self.assertTrue(raw_secret.startswith(api_keys.API_KEY_PREFIX))
                asyncio.run(api_keys.cache_active_key(created))

                cache_key = f"api:key:{created.key_hash}"
                self.assertIn(
                    (cache_key, max(10, settings.API_KEY_CACHE_TTL_SEC)),
                    redis.expire_calls,
                )

                class _ForbiddenDbAfterDeactivate:
                    async def execute(self, *_args, **_kwargs):
                        raise AssertionError("db.execute must not be called after negative cache")

                asyncio.run(
                    api_keys.deactivate_key(
                        db,
                        user_id=55,
                        api_key_id=created.id,
                    )
                )

                self.assertIn(
                    (cache_key, max(10, settings.API_KEY_CACHE_NEGATIVE_TTL_SEC)),
                    redis.expire_calls,
                )

                result = asyncio.run(api_keys.authenticate_key(_ForbiddenDbAfterDeactivate(), raw_secret))

            self.assertIsNone(result)
        finally:
            settings.API_KEY_CACHE_TTL_SEC = original_ttl
            settings.API_KEY_CACHE_NEGATIVE_TTL_SEC = original_negative_ttl
            settings.API_KEY_HASH_SECRET = original_hash_secret

    def test_key_created_before_rollback_is_not_authenticatable(self) -> None:
        db = _TransactionalCreateDb()
        redis = _FakeRedis()

        original_hash_secret = settings.API_KEY_HASH_SECRET
        settings.API_KEY_HASH_SECRET = "test-hash-secret"

        try:
            with mock.patch.object(api_keys, "get_redis", return_value=redis):
                _created, raw_secret = asyncio.run(api_keys.create_key(db, user_id=77, label="rollback"))
                db.rollback()
                result = asyncio.run(api_keys.authenticate_key(db, raw_secret))

            self.assertIsNone(result)
        finally:
            settings.API_KEY_HASH_SECRET = original_hash_secret


if __name__ == "__main__":
    unittest.main()
