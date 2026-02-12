import asyncio
import threading
import unittest
from unittest import mock

from app.core import memory


class _FailingPipeline:
    def hincrby(self, *_args, **_kwargs):
        return None

    def expire(self, *_args, **_kwargs):
        return None

    async def execute(self):
        raise RuntimeError("redis unavailable")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FailingRedis:
    def pipeline(self, *args, **kwargs):
        return _FailingPipeline()


class SpamFallbackLoopLockTests(unittest.TestCase):
    def setUp(self):
        memory._local_spam.clear()
        memory._local_spam_order.clear()
        with memory._local_spam_locks_guard:
            memory._local_spam_locks.clear()

    def _run_in_thread_loop(
        self,
        chat_id: int,
        user_id: int,
        out: list[bool],
        errors: list[BaseException],
        barrier: threading.Barrier | None = None,
    ) -> threading.Thread:
        def _target():
            try:
                if barrier is not None:
                    barrier.wait(timeout=2)
                out.append(asyncio.run(memory.is_spam(chat_id, user_id)))
            except BaseException as exc:  # pragma: no cover - test failure path
                errors.append(exc)

        thread = threading.Thread(target=_target)
        thread.start()
        return thread

    def test_fallback_works_in_different_event_loops(self):
        results: list[bool] = []
        errors: list[BaseException] = []

        with mock.patch.object(memory, "get_redis", return_value=_FailingRedis()):
            barrier = threading.Barrier(3)
            t1 = self._run_in_thread_loop(1, 100, results, errors, barrier=barrier)
            t2 = self._run_in_thread_loop(1, 100, results, errors, barrier=barrier)
            barrier.wait(timeout=2)
            t1.join()
            t2.join()

            for _ in range(4):
                results.append(asyncio.run(memory.is_spam(1, 100)))
            seventh = asyncio.run(memory.is_spam(1, 100))

        self.assertEqual(errors, [])
        self.assertEqual(results, [False, False, False, False, False, False])
        self.assertTrue(seventh)


if __name__ == "__main__":
    unittest.main()
