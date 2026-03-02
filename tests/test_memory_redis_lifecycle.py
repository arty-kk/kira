import unittest

from app.core import memory


class MemoryRedisLifecycleTests(unittest.TestCase):
    def test_get_redis_requires_running_loop(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "active asyncio event loop"):
            memory.get_redis()


if __name__ == "__main__":
    unittest.main()
