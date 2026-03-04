import json
import unittest
from unittest.mock import AsyncMock, patch

from redis.exceptions import TimeoutError as RedisTimeoutError

from app.core import memory


class _InMemoryRedisState:
    def __init__(self):
        self.kv = {}
        self.list_store = {}
        self.execute_calls = 0


class _FakePipeline:
    def __init__(self, owner):
        self.owner = owner
        self.ops = []

    def rpush(self, key, value):
        self.ops.append(("rpush", key, value))

    def expire(self, key, ttl):
        self.ops.append(("expire", key, ttl))

    async def execute(self):
        self.owner.state.execute_calls += 1
        should_fail_once = self.owner.fail_execute and self.owner.state.execute_calls == 1
        should_fail_always = self.owner.fail_always
        if self.owner.apply_on_fail or (not should_fail_once and not should_fail_always):
            for op, key, value in self.ops:
                if op == "rpush":
                    self.owner.state.list_store.setdefault(key, []).append(value)
                elif op == "expire":
                    self.owner.state.kv[f"ttl:{key}"] = value
        if should_fail_once:
            raise RedisTimeoutError("temporary timeout after apply")
        if should_fail_always:
            raise RedisTimeoutError("still broken")
        return [True]

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeRedis:
    def __init__(self, state, *, fail_execute=False, fail_always=False, apply_on_fail=True):
        self.state = state
        self.fail_execute = fail_execute
        self.fail_always = fail_always
        self.apply_on_fail = apply_on_fail

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.state.kv:
            return False
        self.state.kv[key] = value
        if ex is not None:
            self.state.kv[f"ttl:{key}"] = ex
        return True

    async def lrange(self, key, start, end):
        data = self.state.list_store.get(key, [])
        n = len(data)
        s = n + start if start < 0 else start
        e = n + end if end < 0 else end
        s = max(s, 0)
        e = min(e, n - 1)
        if n == 0 or s > e:
            return []
        return data[s : e + 1]

    def pipeline(self, transaction=True):
        return _FakePipeline(self)


class PushMessageRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_push_message_retries_and_keeps_single_entry(self):
        state = _InMemoryRedisState()
        first = _FakeRedis(state, fail_execute=True)
        second = _FakeRedis(state)
        key_stm = memory._k_stm(chat_id=321, user_id=123, namespace="default")

        def _safe_create_task(coro):
            coro.close()
            return None

        with (
            patch.object(memory, "get_redis", side_effect=[first, second]),
            patch.object(memory, "_register_user_key", AsyncMock(return_value=None)),
            patch.object(memory.asyncio, "create_task", side_effect=_safe_create_task),
        ):
            await memory.push_message(chat_id=321, role="user", content="hello", user_id=123)

        entries = state.list_store.get(key_stm, [])
        self.assertEqual(len(entries), 1)
        payload = json.loads(entries[0])
        self.assertEqual(payload["content"], "hello")
        self.assertIn("msg_id", payload)
        self.assertEqual(state.execute_calls, 1)

    async def test_push_message_logs_exception_after_retry_exhausted(self):
        state = _InMemoryRedisState()
        first = _FakeRedis(state, fail_always=True, apply_on_fail=False)
        second = _FakeRedis(state, fail_always=True, apply_on_fail=False)

        def _safe_create_task(coro):
            coro.close()
            return None

        with (
            patch.object(memory, "get_redis", side_effect=[first, second]),
            patch.object(memory, "_register_user_key", AsyncMock(return_value=None)),
            patch.object(memory.asyncio, "create_task", side_effect=_safe_create_task),
            self.assertLogs("app.core.memory", level="ERROR") as logs,
        ):
            await memory.push_message(chat_id=321, role="user", content="hello", user_id=123)

        self.assertTrue(any("push_message STM write error" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
