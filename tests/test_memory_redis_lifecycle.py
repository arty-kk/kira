import unittest
from unittest.mock import patch

from app.core import memory


class MemoryRedisLifecycleTests(unittest.TestCase):
    def test_get_redis_allows_no_running_loop(self) -> None:
        with patch("app.core.memory._create_client", return_value=object()):
            first = memory.get_redis()
            second = memory.get_redis()

        self.assertIs(first, second)


if __name__ == "__main__":
    unittest.main()
