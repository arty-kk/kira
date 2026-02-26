import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://user:pass@localhost/testdb")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("REDIS_URL_QUEUE", "redis://localhost:6379/1")
os.environ.setdefault("REDIS_URL_VECTOR", "redis://localhost:6379/2")

from app.tasks import cleanup


class _FakePipeline:
    def __init__(self, redis, mode):
        self.redis = redis
        self.mode = mode
        self.commands = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def exists(self, key):
        self.commands.append(("exists", key))

    def zrem(self, key, member):
        self.commands.append(("zrem", key, member))
        self.redis.zrem_calls.append((key, member))

    async def execute(self):
        if self.mode == "exists":
            return [0 for _ in self.commands]
        return [1 for _ in self.commands]


class _FakeRedis:
    def __init__(self):
        self.lock_value = None
        self.deleted = []
        self.zrem_calls = []
        self.pipeline_modes = ["exists"]

    async def set(self, key, value, ex=None, nx=None):
        self.lock_value = value
        return True

    async def eval(self, *args, **kwargs):
        return 1

    async def get(self, key):
        return self.lock_value

    async def delete(self, key):
        self.deleted.append(key)
        return 1

    async def exists(self, key):
        return 0

    def pipeline(self, transaction=False):
        mode = self.pipeline_modes.pop(0) if self.pipeline_modes else "zrem"
        return _FakePipeline(self, mode)


class CleanupTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_nonbuyers_skip_user_when_activity_check_raises(self):
        fake_redis = _FakeRedis()

        async def _one_chunk(*args, **kwargs):
            yield [101]

        is_recently_active_mock = AsyncMock(side_effect=RuntimeError("boom"))

        with (
            patch.object(cleanup, "get_redis", return_value=fake_redis),
            patch.object(cleanup, "_iter_nonbuyer_ids", _one_chunk),
            patch.object(cleanup, "is_recently_active", is_recently_active_mock),
            patch.object(cleanup, "delete_user_redis_data", AsyncMock()) as delete_mock,
            patch.object(cleanup.logger, "warning") as warning_mock,
        ):
            await cleanup.cleanup_nonbuyers()

        self.assertEqual(fake_redis.zrem_calls, [])
        delete_mock.assert_not_awaited()
        self.assertEqual(is_recently_active_mock.await_count, 1)
        warning_mock.assert_called_once()
        args = warning_mock.call_args.args
        self.assertIn("ошибка проверки активности в cleanup_nonbuyers", args[0])
        self.assertEqual(args[1], 101)
        self.assertIsInstance(args[2], RuntimeError)


if __name__ == "__main__":
    unittest.main()
