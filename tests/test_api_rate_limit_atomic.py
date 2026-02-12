import asyncio
import types
import unittest
from unittest import mock

from fastapi import HTTPException

from app.api import conversation
from app.config import settings


class _AtomicFakeRedis:
    def __init__(self, fail_first_eval: bool = False) -> None:
        self.store = {}
        self.ttl = {}
        self._scripts = {}
        self._sha = "sha-rate-limit"
        self._fail_first_eval = fail_first_eval
        self.eval_calls = 0

    async def eval(self, script: str, numkeys: int, *args):
        self.eval_calls += 1
        if self._fail_first_eval and self.eval_calls == 1:
            raise RuntimeError("simulated network error before atomic execution")
        return self._run_script(script, numkeys, *args)

    async def evalsha(self, sha: str, numkeys: int, *args):
        script = self._scripts.get(sha)
        if script is None:
            raise RuntimeError("NOSCRIPT No matching script")
        return self._run_script(script, numkeys, *args)

    async def script_load(self, script: str) -> str:
        self._scripts[self._sha] = script
        return self._sha

    def _run_script(self, script: str, numkeys: int, *args):
        del script
        api_key, ip_key = args[:numkeys]
        ttl, check_ip, api_limit, ip_limit = [int(v) for v in args[numkeys:]]

        api_count = int(self.store.get(api_key, 0)) + 1
        self.store[api_key] = api_count
        if api_count == 1:
            self.ttl[api_key] = ttl

        ip_count = 0
        if check_ip == 1:
            ip_count = int(self.store.get(ip_key, 0)) + 1
            self.store[ip_key] = ip_count
            if ip_count == 1:
                self.ttl[ip_key] = ttl

        api_exceeded = 1 if api_count > api_limit else 0
        ip_exceeded = 1 if check_ip == 1 and ip_count > ip_limit else 0
        return [api_count, ip_count, api_exceeded, ip_exceeded]


class _AlwaysFailRedis:
    async def eval(self, script: str, numkeys: int, *args):
        del script, numkeys, args
        raise RuntimeError("redis is down")

    async def evalsha(self, sha: str, numkeys: int, *args):
        del sha, numkeys, args
        raise RuntimeError("redis is down")


class ApiRateLimitAtomicTests(unittest.TestCase):
    def setUp(self) -> None:
        self._per_min = settings.API_RATELIMIT_PER_MIN
        self._burst = settings.API_RATELIMIT_BURST_FACTOR
        self._per_ip = settings.API_RATELIMIT_PER_IP_PER_MIN
        self._sha_backup = conversation._RATE_LIMIT_LUA_SHA

    def tearDown(self) -> None:
        settings.API_RATELIMIT_PER_MIN = self._per_min
        settings.API_RATELIMIT_BURST_FACTOR = self._burst
        settings.API_RATELIMIT_PER_IP_PER_MIN = self._per_ip
        conversation._RATE_LIMIT_LUA_SHA = self._sha_backup

    def test_retry_does_not_double_increment_after_atomic_failure(self) -> None:
        settings.API_RATELIMIT_PER_MIN = 1
        settings.API_RATELIMIT_BURST_FACTOR = 1
        settings.API_RATELIMIT_PER_IP_PER_MIN = 0
        conversation._RATE_LIMIT_LUA_SHA = None

        request = types.SimpleNamespace(headers={}, client=None)
        fake_redis = _AtomicFakeRedis(fail_first_eval=True)

        with (
            mock.patch.object(conversation, "get_redis", return_value=fake_redis),
            mock.patch.object(conversation.time, "time", return_value=120),
        ):
            asyncio.run(conversation._check_rate_limit(request, api_key_id=77))

        self.assertEqual(fake_redis.store.get("rl:api:key:77:2"), 1)

    def test_noscript_falls_back_to_eval_without_extra_retry_cycle(self) -> None:
        settings.API_RATELIMIT_PER_MIN = 2
        settings.API_RATELIMIT_BURST_FACTOR = 1
        settings.API_RATELIMIT_PER_IP_PER_MIN = 0
        conversation._RATE_LIMIT_LUA_SHA = "missing-sha"

        request = types.SimpleNamespace(headers={}, client=None)
        fake_redis = _AtomicFakeRedis()

        with (
            mock.patch.object(conversation, "get_redis", return_value=fake_redis),
            mock.patch.object(conversation.time, "time", return_value=300),
        ):
            asyncio.run(conversation._check_rate_limit(request, api_key_id=9))

        self.assertEqual(fake_redis.eval_calls, 1)
        self.assertEqual(fake_redis.store.get("rl:api:key:9:5"), 1)

    def test_atomic_call_enforces_api_limit_threshold(self) -> None:
        settings.API_RATELIMIT_PER_MIN = 1
        settings.API_RATELIMIT_BURST_FACTOR = 1
        settings.API_RATELIMIT_PER_IP_PER_MIN = 0
        conversation._RATE_LIMIT_LUA_SHA = None

        request = types.SimpleNamespace(headers={}, client=None)
        fake_redis = _AtomicFakeRedis()

        with (
            mock.patch.object(conversation, "get_redis", return_value=fake_redis),
            mock.patch.object(conversation.time, "time", return_value=180),
        ):
            asyncio.run(conversation._check_rate_limit(request, api_key_id=1))
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(conversation._check_rate_limit(request, api_key_id=1))

        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.detail["code"], "rate_limited")
        self.assertEqual(ctx.exception.headers.get("Retry-After"), "60")

    def test_atomic_call_enforces_ip_limit_threshold(self) -> None:
        settings.API_RATELIMIT_PER_MIN = 10
        settings.API_RATELIMIT_BURST_FACTOR = 1
        settings.API_RATELIMIT_PER_IP_PER_MIN = 1
        conversation._RATE_LIMIT_LUA_SHA = None

        request = types.SimpleNamespace(
            headers={},
            client=types.SimpleNamespace(host="198.51.100.10"),
        )
        fake_redis = _AtomicFakeRedis()

        with (
            mock.patch.object(conversation, "get_redis", return_value=fake_redis),
            mock.patch.object(conversation.time, "time", return_value=240),
        ):
            asyncio.run(conversation._check_rate_limit(request, api_key_id=5))
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(conversation._check_rate_limit(request, api_key_id=5))

        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.detail["code"], "rate_limited_ip")
        self.assertEqual(ctx.exception.headers.get("Retry-After"), "60")

    def test_rate_limiter_returns_503_when_redis_unavailable(self) -> None:
        settings.API_RATELIMIT_PER_MIN = 10
        settings.API_RATELIMIT_BURST_FACTOR = 1
        settings.API_RATELIMIT_PER_IP_PER_MIN = 0
        conversation._RATE_LIMIT_LUA_SHA = None

        request = types.SimpleNamespace(headers={}, client=None)
        scenarios = {
            "redis-client-is-none": None,
            "redis-always-errors": _AlwaysFailRedis(),
        }

        for name, redis_value in scenarios.items():
            with self.subTest(name=name):
                with (
                    mock.patch.object(conversation, "get_redis", return_value=redis_value),
                    mock.patch.object(conversation, "_RATE_LIMIT_REDIS_RETRIES", 1),
                    mock.patch.object(conversation, "_RATE_LIMIT_REDIS_RETRY_DELAY_SEC", 0),
                ):
                    with self.assertRaises(HTTPException) as ctx:
                        asyncio.run(conversation._check_rate_limit(request, api_key_id=101))

                self.assertEqual(ctx.exception.status_code, 503)
                self.assertEqual(ctx.exception.detail["code"], "rate_limiter_unavailable")


if __name__ == "__main__":
    unittest.main()
