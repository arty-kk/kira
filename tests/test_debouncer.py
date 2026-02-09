import unittest

from app.bot.utils import debouncer


class DummyRedis:
    async def get(self, _key):
        return 0


class DebouncerModeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        debouncer.message_buffers.clear()
        debouncer.pending_tasks.clear()
        debouncer._locks.clear()
        debouncer.total_buffered = 0
        debouncer.consts.redis_queue = DummyRedis()

    async def _run_and_collect(self, mode: str):
        captured = []

        async def _capture(payload):
            captured.append(payload)

        debouncer.DEBOUNCE_MODE = mode
        debouncer._enqueue = _capture

        key = "1:1"
        debouncer.message_buffers[key] = [
            {"chat_id": 1, "user_id": 1, "text": "hi", "msg_id": 1},
            {"chat_id": 1, "user_id": 1, "text": "there", "msg_id": 2},
        ]
        debouncer.total_buffered = 2

        await debouncer.schedule_response(key)
        return captured

    async def test_single_mode_sends_each_message(self) -> None:
        captured = await self._run_and_collect("single")
        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[0]["text"], "hi")
        self.assertEqual(captured[1]["text"], "there")

    async def test_merge_mode_batches_text(self) -> None:
        captured = await self._run_and_collect("merge")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["text"], "hi\nthere")
        self.assertEqual(captured[0]["merged_msg_ids"], [1, 2])

    async def test_human_mode_batches_text(self) -> None:
        captured = await self._run_and_collect("human")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["text"], "hi\nthere")
        self.assertEqual(captured[0]["merged_msg_ids"], [1, 2])


if __name__ == "__main__":
    unittest.main()
