import asyncio
import unittest

from app.core.memory import SafeRedis


class _ApplyThenTimeoutClient:
    def __init__(self):
        self.applied = {"incr": 0, "hincrby": 0, "rpush": 0}

    async def incr(self, _key):
        self.applied["incr"] += 1
        raise asyncio.TimeoutError("response timeout after apply")

    async def hincrby(self, _key, _field, _delta):
        self.applied["hincrby"] += 1
        raise asyncio.TimeoutError("response timeout after apply")

    async def rpush(self, _key, _value):
        self.applied["rpush"] += 1
        raise asyncio.TimeoutError("response timeout after apply")


class _ReadonlyRetryClient:
    def __init__(self):
        self.get_calls = 0

    async def get(self, _key):
        self.get_calls += 1
        if self.get_calls == 1:
            raise asyncio.TimeoutError("temporary timeout")
        return "ok"


class _UnknownCommandClient:
    def __init__(self):
        self.calls = 0

    async def customwrite(self, _key):
        self.calls += 1
        raise asyncio.TimeoutError("timeout")


class _Pipeline:
    def __init__(self):
        self.execute_calls = 0

    async def execute(self):
        self.execute_calls += 1
        raise asyncio.TimeoutError("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _PipelineClient:
    def __init__(self):
        self.pipe = _Pipeline()

    def pipeline(self, *args, **kwargs):
        return self.pipe


class SafeRedisRetryPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_idempotent_commands_are_not_retried_after_timeout(self):
        client = _ApplyThenTimeoutClient()
        redis = SafeRedis(client, attempts=3)

        with self.assertLogs("app.core.memory", level="WARNING") as logs:
            with self.assertRaises(asyncio.TimeoutError):
                await redis.incr("k")
            with self.assertRaises(asyncio.TimeoutError):
                await redis.hincrby("h", "f", 1)
            with self.assertRaises(asyncio.TimeoutError):
                await redis.rpush("l", "v")

        self.assertEqual(client.applied["incr"], 1)
        self.assertEqual(client.applied["hincrby"], 1)
        self.assertEqual(client.applied["rpush"], 1)
        self.assertTrue(any("retry skipped due to non-idempotent semantics" in msg for msg in logs.output))

    async def test_readonly_command_keeps_retry(self):
        client = _ReadonlyRetryClient()
        redis = SafeRedis(client, attempts=3)

        result = await redis.get("k")

        self.assertEqual(result, "ok")
        self.assertEqual(client.get_calls, 2)


    async def test_unknown_command_uses_conservative_single_attempt(self):
        client = _UnknownCommandClient()
        redis = SafeRedis(client, attempts=3)

        with self.assertRaises(asyncio.TimeoutError):
            await redis.customwrite("k")

        self.assertEqual(client.calls, 1)

    async def test_pipeline_execute_has_single_attempt(self):
        client = _PipelineClient()
        redis = SafeRedis(client, attempts=3)

        with self.assertRaises(asyncio.TimeoutError):
            async with redis.pipeline(transaction=False) as pipe:
                await pipe.execute()

        self.assertEqual(client.pipe.execute_calls, 1)


if __name__ == "__main__":
    unittest.main()
