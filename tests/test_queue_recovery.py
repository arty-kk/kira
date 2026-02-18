import unittest

from app.core.queue_recovery import QueueRecoveryResult, requeue_processing_on_start


class WatchError(Exception):
    pass


class _Pipeline:
    def __init__(self, redis):
        self.redis = redis
        self.commands = []

    async def watch(self, key):
        if self.redis.fail_watch_once:
            self.redis.fail_watch_once = False
            raise WatchError("conflict")
        self.redis.watch_calls += 1

    async def lrange(self, key, start, end):
        self.redis.lrange_calls += 1
        values = list(self.redis.data.get(key, []))
        if end == -1:
            return values[start:]
        return values[start : end + 1]

    def multi(self):
        return None

    def rpush(self, key, *values):
        self.commands.append(("rpush", key, values))

    def ltrim(self, key, start, end):
        self.commands.append(("ltrim", key, start, end))

    async def execute(self):
        for cmd in self.commands:
            if cmd[0] == "rpush":
                _, key, values = cmd
                self.redis.data.setdefault(key, []).extend(values)
            if cmd[0] == "ltrim":
                _, key, start, end = cmd
                values = list(self.redis.data.get(key, []))
                self.redis.data[key] = values[start:] if end == -1 else values[start : end + 1]
        self.redis.execute_calls += 1
        return [1, 1]

    async def reset(self):
        return None


class _Redis:
    def __init__(self):
        self.data = {
            "q:test": [],
            "q:test:processing": [],
        }
        self.locked = False
        self.eval_exception = None
        self.watch_calls = 0
        self.lrange_calls = 0
        self.execute_calls = 0
        self.fail_watch_once = False

    async def set(self, key, value, nx=False, ex=None):
        if nx and self.locked:
            return False
        self.locked = True
        return True

    async def eval(self, _script, _numkeys, processing_key, queue_key):
        if self.eval_exception:
            raise self.eval_exception
        pending = list(self.data.get(processing_key, []))
        moved = len(pending)
        if moved:
            self.data.setdefault(queue_key, []).extend(pending)
            self.data[processing_key] = self.data[processing_key][moved:]
        return moved

    def pipeline(self):
        return _Pipeline(self)


class QueueRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_lock_not_acquired(self):
        redis = _Redis()
        redis.locked = True

        result = await requeue_processing_on_start(
            redis,
            queue_key="q:test",
            processing_key="q:test:processing",
            lock_ttl=60,
        )

        self.assertEqual(result, QueueRecoveryResult(moved_count=0, lock_acquired=False))

    async def test_eval_success_with_moved(self):
        redis = _Redis()
        redis.data["q:test:processing"] = ["a", "b"]

        result = await requeue_processing_on_start(
            redis,
            queue_key="q:test",
            processing_key="q:test:processing",
            lock_ttl=60,
        )

        self.assertEqual(result, QueueRecoveryResult(moved_count=2, lock_acquired=True))
        self.assertEqual(redis.data["q:test"], ["a", "b"])
        self.assertEqual(redis.data["q:test:processing"], [])

    async def test_eval_success_with_zero_moved(self):
        redis = _Redis()

        result = await requeue_processing_on_start(
            redis,
            queue_key="q:test",
            processing_key="q:test:processing",
            lock_ttl=60,
        )

        self.assertEqual(result, QueueRecoveryResult(moved_count=0, lock_acquired=True))

    async def test_eval_unavailable_fallback_path(self):
        redis = _Redis()
        redis.data["q:test:processing"] = ["x"]
        redis.eval_exception = RuntimeError("unknown command 'EVAL'")

        result = await requeue_processing_on_start(
            redis,
            queue_key="q:test",
            processing_key="q:test:processing",
            lock_ttl=60,
        )

        self.assertEqual(result, QueueRecoveryResult(moved_count=1, lock_acquired=True))
        self.assertEqual(redis.lrange_calls, 1)
        self.assertEqual(redis.execute_calls, 1)

    async def test_eval_unavailable_fallback_handles_watch_retry(self):
        redis = _Redis()
        redis.data["q:test:processing"] = ["x"]
        redis.eval_exception = RuntimeError("unknown command eval")
        redis.fail_watch_once = True

        result = await requeue_processing_on_start(
            redis,
            queue_key="q:test",
            processing_key="q:test:processing",
            lock_ttl=60,
        )

        self.assertEqual(result, QueueRecoveryResult(moved_count=1, lock_acquired=True))
        self.assertEqual(redis.watch_calls, 1)


if __name__ == "__main__":
    unittest.main()
