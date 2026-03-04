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


class _WhitelistRetryClient:
    def __init__(self):
        self.calls = {"delete": 0, "unlink": 0, "zrem": 0, "hdel": 0, "srem": 0}

    async def delete(self, _key):
        self.calls["delete"] += 1
        if self.calls["delete"] == 1:
            raise asyncio.TimeoutError("temporary timeout")
        return 1

    async def unlink(self, _key):
        self.calls["unlink"] += 1
        if self.calls["unlink"] == 1:
            raise asyncio.TimeoutError("temporary timeout")
        return 1

    async def zrem(self, _key, _member):
        self.calls["zrem"] += 1
        if self.calls["zrem"] == 1:
            raise asyncio.TimeoutError("temporary timeout")
        return 1

    async def hdel(self, _key, _field):
        self.calls["hdel"] += 1
        if self.calls["hdel"] == 1:
            raise asyncio.TimeoutError("temporary timeout")
        return 1

    async def srem(self, _key, _member):
        self.calls["srem"] += 1
        if self.calls["srem"] == 1:
            raise asyncio.TimeoutError("temporary timeout")
        return 1


class _UnknownCommandClient:
    def __init__(self):
        self.calls = 0

    async def customwrite(self, _key):
        self.calls += 1
        raise asyncio.TimeoutError("timeout")


class _Pipeline:
    def __init__(self, *, always_timeout=False, command_stack=None):
        self.execute_calls = 0
        self.always_timeout = always_timeout
        self.command_stack = command_stack or []

    async def execute(self):
        self.execute_calls += 1
        if self.always_timeout or self.execute_calls == 1:
            raise asyncio.TimeoutError("timeout")
        return [True]

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False




class _PipelineNoStack:
    def __init__(self):
        self.execute_calls = 0

    async def execute(self):
        self.execute_calls += 1
        raise asyncio.TimeoutError("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _PipelineNoStackClient:
    def __init__(self):
        self.pipe = _PipelineNoStack()

    def pipeline(self, *args, **kwargs):
        return self.pipe

class _PipelineClient:
    def __init__(self, *, always_timeout=False, command_stack=None):
        self.pipe = _Pipeline(always_timeout=always_timeout, command_stack=command_stack)

    def pipeline(self, *args, **kwargs):
        return self.pipe


class SafeRedisRetryPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_safe_redis_non_idempotent_commands_are_not_retried_after_timeout(self):
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

    async def test_direct_safe_redis_readonly_command_keeps_retry(self):
        client = _ReadonlyRetryClient()
        redis = SafeRedis(client, attempts=3)

        result = await redis.get("k")

        self.assertEqual(result, "ok")
        self.assertEqual(client.get_calls, 2)

    async def test_direct_safe_redis_retry_is_enabled_for_whitelisted_commands(self):
        client = _WhitelistRetryClient()
        redis = SafeRedis(client, attempts=3)

        command_cases = [
            ("delete", ("k",)),
            ("unlink", ("k",)),
            ("zrem", ("z", "m")),
            ("hdel", ("h", "f")),
            ("srem", ("s", "m")),
        ]
        for command_name, args in command_cases:
            with self.subTest(command=command_name):
                result = await getattr(redis, command_name)(*args)
                self.assertEqual(result, 1)
                self.assertEqual(client.calls[command_name], 2)

    async def test_direct_safe_redis_unknown_command_uses_conservative_single_attempt(self):
        client = _UnknownCommandClient()
        redis = SafeRedis(client, attempts=3)

        with self.assertRaises(asyncio.TimeoutError):
            await redis.customwrite("k")

        self.assertEqual(client.calls, 1)


    async def test_pipeline_execute_does_not_retry_non_idempotent_stack(self):
        client = _PipelineClient(
            always_timeout=True,
            command_stack=[(("RPUSH", "k", "v"), {})],
        )
        redis = SafeRedis(client, attempts=3)

        with self.assertRaises(asyncio.TimeoutError):
            async with redis.pipeline(transaction=True) as pipe:
                await pipe.execute()

        self.assertEqual(client.pipe.execute_calls, 1)


    async def test_pipeline_execute_does_not_retry_without_command_stack(self):
        client = _PipelineNoStackClient()
        redis = SafeRedis(client, attempts=3)

        with self.assertRaises(asyncio.TimeoutError):
            async with redis.pipeline(transaction=True) as pipe:
                await pipe.execute()

        self.assertEqual(client.pipe.execute_calls, 1)

    async def test_pipeline_execute_retries_once_for_transport_errors(self):
        client = _PipelineClient()
        redis = SafeRedis(client, attempts=3)

        async with redis.pipeline(transaction=False) as pipe:
            result = await pipe.execute()

        self.assertEqual(result, [True])
        self.assertEqual(client.pipe.execute_calls, 2)


if __name__ == "__main__":
    unittest.main()
