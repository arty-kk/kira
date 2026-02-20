import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.bot.utils import debouncer


class DummyRedis:
    async def get(self, _key):
        return 0


class DebouncerModeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._orig_enqueue = debouncer._enqueue
        debouncer.message_buffers.clear()
        debouncer.pending_tasks.clear()
        debouncer._locks.clear()
        debouncer.total_buffered = 0
        debouncer.consts.redis_queue = DummyRedis()

    def tearDown(self) -> None:
        debouncer._enqueue = self._orig_enqueue

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

    async def test_merge_mode_collects_unique_reservation_ids(self) -> None:
        captured = []

        async def _capture(payload):
            captured.append(payload)

        debouncer.DEBOUNCE_MODE = "merge"
        debouncer._enqueue = _capture

        key = "1:1"
        debouncer.message_buffers[key] = [
            {"chat_id": 1, "user_id": 1, "text": "hi", "msg_id": 1, "reservation_id": 10},
            {"chat_id": 1, "user_id": 1, "text": "there", "msg_id": 2, "reservation_id": 20},
            {"chat_id": 1, "user_id": 1, "text": "again", "msg_id": 3, "reservation_id": 10},
        ]
        debouncer.total_buffered = 3

        await debouncer.schedule_response(key)

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["reservation_ids"], [10, 20])
        self.assertEqual(captured[0]["reservation_id"], 10)

    async def test_enqueue_rejects_invalid_payload_before_redis_and_refunds(self) -> None:
        lpush_mock = AsyncMock()
        refund_mock = AsyncMock()
        with (
            patch.object(debouncer, "refund_reservation_by_id", refund_mock),
            patch.object(debouncer, "consts") as consts_mock,
        ):
            consts_mock.redis_queue.lpush = lpush_mock
            await debouncer._enqueue(
                {
                    "chat_id": 1,
                    "user_id": 1,
                    "text": "hi",
                    "msg_id": 0,
                    "reservation_ids": [11, 12, 11],
                    "reservation_id": 12,
                    "is_group": True,
                    "is_channel_post": False,
                    "entities": [],
                }
            )

        lpush_mock.assert_not_awaited()
        self.assertEqual([call.args[0] for call in refund_mock.await_args_list], [11, 12])


class DebouncerDropRefundTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        debouncer.message_buffers.clear()
        debouncer.pending_tasks.clear()
        debouncer._locks.clear()
        debouncer.total_buffered = 0

    async def test_per_chat_drop_refunds_unique_reservations(self) -> None:
        refund_mock = AsyncMock()
        with (
            patch.object(debouncer, "MAX_BUFFER_PER_CHAT", 1),
            patch.object(debouncer, "GLOBAL_MAX_BUFFERS", 100),
            patch.object(debouncer, "refund_reservation_by_id", refund_mock),
            patch.object(debouncer, "schedule_response", AsyncMock(return_value=None)),
        ):
            debouncer.buffer_message_for_response(
                {
                    "chat_id": 1,
                    "user_id": 1,
                    "text": "first",
                    "reservation_id": "10",
                    "reservation_ids": [10, "11", 0, -1, "bad", 11],
                }
            )
            await asyncio.sleep(0)
            debouncer.buffer_message_for_response(
                {
                    "chat_id": 1,
                    "user_id": 1,
                    "text": "second",
                }
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        self.assertEqual([call.args[0] for call in refund_mock.await_args_list], [10, 11])

    async def test_global_drop_same_key_refunds(self) -> None:
        refund_mock = AsyncMock()
        with (
            patch.object(debouncer, "MAX_BUFFER_PER_CHAT", 10),
            patch.object(debouncer, "GLOBAL_MAX_BUFFERS", 1),
            patch.object(debouncer, "refund_reservation_by_id", refund_mock),
            patch.object(debouncer, "schedule_response", AsyncMock(return_value=None)),
        ):
            debouncer.buffer_message_for_response(
                {"chat_id": 1, "user_id": 1, "text": "first", "reservation_id": 20}
            )
            await asyncio.sleep(0)
            debouncer.buffer_message_for_response(
                {"chat_id": 1, "user_id": 1, "text": "second"}
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        refund_mock.assert_awaited_once_with(20)

    async def test_global_drop_other_key_refunds(self) -> None:
        refund_mock = AsyncMock()
        debouncer.message_buffers["1:1"] = [
            {"chat_id": 1, "user_id": 1, "text": "old", "reservation_id": 30, "ts": 1.0}
        ]
        debouncer.message_buffers["2:2"] = [
            {"chat_id": 2, "user_id": 2, "text": "newer", "ts": 2.0}
        ]
        debouncer.total_buffered = 2

        with (
            patch.object(debouncer, "MAX_BUFFER_PER_CHAT", 10),
            patch.object(debouncer, "GLOBAL_MAX_BUFFERS", 2),
            patch.object(debouncer, "refund_reservation_by_id", refund_mock),
            patch.object(debouncer, "schedule_response", AsyncMock(return_value=None)),
        ):
            debouncer.buffer_message_for_response(
                {"chat_id": 2, "user_id": 2, "text": "latest"}
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        refund_mock.assert_awaited_once_with(30)

    async def test_drop_without_reservation_logs_event(self) -> None:
        refund_mock = AsyncMock()
        info_mock = MagicMock()

        def _is_logged(event_name: str) -> bool:
            for call in info_mock.call_args_list:
                if call.args and call.args[0] == event_name:
                    return True
            return False

        with (
            patch.object(debouncer, "MAX_BUFFER_PER_CHAT", 1),
            patch.object(debouncer, "GLOBAL_MAX_BUFFERS", 100),
            patch.object(debouncer, "refund_reservation_by_id", refund_mock),
            patch.object(debouncer, "schedule_response", AsyncMock(return_value=None)),
            patch.object(debouncer.logger, "info", info_mock),
        ):
            debouncer.buffer_message_for_response({"chat_id": 1, "user_id": 1, "text": "first"})
            await asyncio.sleep(0)
            debouncer.buffer_message_for_response({"chat_id": 1, "user_id": 1, "text": "second"})
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        refund_mock.assert_not_awaited()
        self.assertTrue(_is_logged("dropped_without_reservation"))



if __name__ == "__main__":
    unittest.main()
