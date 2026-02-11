import json
import unittest
from unittest.mock import AsyncMock, patch

from app.tasks import media


class _FakeRedis:
    def __init__(self):
        self.values = {}
        self.queue = []

    async def set(self, key, value, ex=None):
        self.values[key] = (value, ex)
        return True

    async def lpush(self, key, value):
        self.queue.append((key, value))
        return 1


class MediaTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_preprocess_happy_path_download_sanitize_store_enqueue(self) -> None:
        fake_redis = _FakeRedis()
        payload = {
            "chat_id": 10,
            "message_id": 20,
            "user_id": 30,
            "trigger": "mention",
            "reply_to": 0,
            "is_channel_post": False,
            "channel_id": None,
            "channel_title": None,
            "entities": [],
            "caption": "cap",
            "caption_log": "cap",
            "file_id": "abc",
            "mime_type": "image/jpeg",
            "suffix": ".jpg",
            "enforce_on_topic": False,
            "allow_web": False,
        }
        with (
            patch.object(media, "consts") as consts_mock,
            patch.object(media, "_download_file_to_tmp", AsyncMock(return_value="/tmp/f.jpg")),
            patch.object(media, "strict_image_load", AsyncMock(return_value=object())),
            patch.object(media, "sanitize_and_compress", return_value=b"jpeg"),
            patch.object(media, "_send_error", AsyncMock()),
            patch.object(media.passive_moderate, "delay"),
            patch.object(media, "push_group_stm"),
            patch.object(media, "append_group_recent"),
            patch.object(media, "inc_msg_count"),
            patch("app.tasks.media.os.path.exists", return_value=False),
        ):
            consts_mock.redis_client = fake_redis
            consts_mock.redis_queue = fake_redis
            result = await media._preprocess(payload)

        self.assertEqual(result, "ok")
        self.assertIn("media:preprocessed:10:20", fake_redis.values)
        self.assertEqual(len(fake_redis.queue), 1)
        queued = json.loads(fake_redis.queue[0][1])
        self.assertEqual(queued["chat_id"], 10)
        self.assertEqual(queued["msg_id"], 20)
        self.assertEqual(queued["image_mime"], "image/jpeg")

    async def test_preprocess_fail_path_oversize(self) -> None:
        payload = {
            "chat_id": 10,
            "message_id": 20,
            "user_id": 30,
            "file_id": "abc",
        }
        with (
            patch.object(media, "_download_file_to_tmp", AsyncMock(return_value="/tmp/f.jpg")),
            patch.object(media, "strict_image_load", AsyncMock(return_value=object())),
            patch.object(media, "sanitize_and_compress", return_value=b"x" * (media.MAX_IMAGE_BYTES + 1)),
            patch.object(media, "_send_error", AsyncMock()) as send_error,
            patch.object(media.passive_moderate, "delay"),
            patch("app.tasks.media.os.path.exists", return_value=False),
        ):
            result = await media._preprocess(payload)

        self.assertEqual(result, "skipped:validation")
        send_error.assert_awaited()

    async def test_preprocess_skips_context_when_enqueue_failed(self) -> None:
        fake_redis = _FakeRedis()
        payload = {
            "chat_id": 10,
            "message_id": 20,
            "user_id": 30,
            "file_id": "abc",
            "caption": "cap",
        }
        with (
            patch.object(media, "consts") as consts_mock,
            patch.object(media, "_download_file_to_tmp", AsyncMock(return_value="/tmp/f.jpg")),
            patch.object(media, "strict_image_load", AsyncMock(return_value=object())),
            patch.object(media, "sanitize_and_compress", return_value=b"jpeg"),
            patch.object(media, "_enqueue", AsyncMock(return_value=False)),
            patch.object(media, "_store_context_and_recent", AsyncMock()) as store_context,
            patch.object(media.passive_moderate, "delay") as passive_delay,
            patch("app.tasks.media.os.path.exists", return_value=False),
        ):
            consts_mock.redis_client = fake_redis
            consts_mock.redis_queue = fake_redis
            result = await media._preprocess(payload)

        self.assertEqual(result, "skipped:enqueue")
        store_context.assert_not_awaited()
        passive_delay.assert_not_called()

    def test_smoke_queue_payload_contract_is_compatible(self) -> None:
        sample = {
            "chat_id": 10,
            "user_id": 30,
            "text": "cap",
            "msg_id": 20,
            "reply_to": 0,
            "is_group": True,
            "is_channel_post": False,
            "image_b64": "abcd",
            "image_mime": "image/jpeg",
            "trigger": "mention",
            "enforce_on_topic": False,
            "allow_web": False,
            "entities": [],
        }
        self.assertIsNone(media.validate_bot_job(sample))


if __name__ == "__main__":
    unittest.main()
