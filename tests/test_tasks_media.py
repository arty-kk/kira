import json
import types
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
        self.assertFalse(any(str(k).startswith("media:preprocessed:") for k in fake_redis.values))
        self.assertEqual(len(fake_redis.queue), 1)
        queued = json.loads(fake_redis.queue[0][1])
        self.assertEqual(queued["chat_id"], 10)
        self.assertEqual(queued["msg_id"], 20)
        self.assertEqual(queued["image_mime"], "image/jpeg")

    async def test_enqueue_rejects_payload_without_msg_id_before_redis_and_refunds(self) -> None:
        fake_redis = _FakeRedis()
        payload = {
            "chat_id": 10,
            "user_id": 30,
            "text": "cap",
            "reservation_id": 42,
            "is_group": True,
            "is_channel_post": False,
            "entities": [],
        }
        with (
            patch.object(media, "consts") as consts_mock,
            patch.object(media, "refund_reservation_by_id", AsyncMock()) as refund_mock,
        ):
            consts_mock.redis_queue = fake_redis
            ok = await media._enqueue(payload)

        self.assertFalse(ok)
        self.assertEqual(fake_redis.queue, [])
        refund_mock.assert_awaited_once_with(42)

    async def test_enqueue_rejects_non_positive_msg_id_before_redis_and_refunds(self) -> None:
        fake_redis = _FakeRedis()
        payload = {
            "chat_id": 10,
            "user_id": 30,
            "text": "cap",
            "msg_id": 0,
            "reservation_id": 42,
            "is_group": True,
            "is_channel_post": False,
            "entities": [],
        }
        with (
            patch.object(media, "consts") as consts_mock,
            patch.object(media, "refund_reservation_by_id", AsyncMock()) as refund_mock,
        ):
            consts_mock.redis_queue = fake_redis
            ok = await media._enqueue(payload)

        self.assertFalse(ok)
        self.assertEqual(fake_redis.queue, [])
        refund_mock.assert_awaited_once_with(42)

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
        send_error.assert_awaited_once_with(10, "не удалось ужать до 5MB", 20)

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


    async def test_preprocess_drops_oversized_image_for_moderation_queue(self) -> None:
        fake_redis = _FakeRedis()
        payload = {
            "chat_id": 10,
            "message_id": 20,
            "user_id": 30,
            "file_id": "abc",
            "caption": "cap",
            "caption_log": "cap",
            "trusted_repost": True,
        }
        with (
            patch.object(media, "consts") as consts_mock,
            patch.object(media, "_download_file_to_tmp", AsyncMock(return_value="/tmp/f.jpg")),
            patch.object(media, "strict_image_load", AsyncMock(return_value=object())),
            patch.object(media, "sanitize_and_compress", return_value=b"jpeg"),
            patch("app.tasks.moderation.MODERATION_MAX_IMAGE_BYTES", 3),
            patch("app.tasks.moderation.MODERATION_MAX_PAYLOAD_BYTES", 1024 * 1024),
            patch.object(media.passive_moderate, "delay") as passive_delay,
            patch("app.tasks.media.os.path.exists", return_value=False),
        ):
            consts_mock.redis_client = fake_redis
            consts_mock.redis_queue = fake_redis
            result = await media._preprocess(payload)

        self.assertEqual(result, "ok")
        moderation_payload = passive_delay.call_args.args[0]
        self.assertNotIn("image_b64", moderation_payload)
        self.assertNotIn("image_mime", moderation_payload)
        self.assertTrue(moderation_payload["trusted_repost"])



    async def test_preprocess_allows_input_larger_than_5mb_when_compression_succeeds(self) -> None:
        fake_redis = _FakeRedis()
        payload = {
            "chat_id": 10,
            "message_id": 21,
            "user_id": 30,
            "trigger": "mention",
            "file_id": "abc",
            "caption": "cap",
            "caption_log": "cap",
        }
        with (
            patch.object(media, "consts") as consts_mock,
            patch.object(media, "_download_file_to_tmp", AsyncMock(return_value="/tmp/f-big.jpg")),
            patch.object(media, "strict_image_load", AsyncMock(return_value=object())),
            patch.object(media, "sanitize_and_compress", return_value=b"jpeg"),
            patch.object(media, "_send_error", AsyncMock()) as send_error,
            patch.object(media.passive_moderate, "delay"),
            patch("app.tasks.media.os.path.exists", return_value=False),
        ):
            consts_mock.redis_client = fake_redis
            consts_mock.redis_queue = fake_redis
            result = await media._preprocess(payload)

        self.assertEqual(result, "ok")
        self.assertEqual(len(fake_redis.queue), 1)
        send_error.assert_not_awaited()

    async def test_preprocess_returns_specific_reason_when_input_exceeds_media_limit(self) -> None:
        payload = {
            "chat_id": 10,
            "message_id": 22,
            "user_id": 30,
            "file_id": "abc",
        }
        with (
            patch.object(
                media,
                "_download_file_to_tmp",
                AsyncMock(side_effect=ValueError("входной файл слишком большой для обработки")),
            ),
            patch.object(media, "_send_error", AsyncMock()) as send_error,
            patch("app.tasks.media.os.path.exists", return_value=False),
        ):
            result = await media._preprocess(payload)

        self.assertEqual(result, "skipped:validation")
        send_error.assert_awaited_once_with(10, "входной файл слишком большой для обработки", 22)

    async def test_download_file_to_tmp_rejects_when_input_exceeds_media_limit(self) -> None:
        fake_bot = types.SimpleNamespace(
            get_file=AsyncMock(return_value=object()),
            download=AsyncMock(return_value=None),
        )
        with (
            patch.object(media, "get_bot", return_value=fake_bot),
            patch("app.tasks.media.os.path.getsize", return_value=media.MEDIA_MAX_INPUT_BYTES + 1),
            patch("app.tasks.media.os.path.exists", return_value=True),
            patch("app.tasks.media.os.remove"),
        ):
            with self.assertRaisesRegex(ValueError, "входной файл слишком большой для обработки"):
                await media._download_file_to_tmp(file_id="abc", suffix=".jpg", timeout_s=1.0)

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

    def test_validate_bot_job_rejects_missing_msg_id(self) -> None:
        sample = {
            "chat_id": 10,
            "user_id": 30,
            "text": "cap",
            "is_group": True,
            "is_channel_post": False,
            "entities": [],
        }
        err = media.validate_bot_job(sample)
        self.assertIsNotNone(err)
        self.assertIn("msg_id", err or "")

    def test_validate_bot_job_rejects_non_positive_msg_id(self) -> None:
        sample = {
            "chat_id": 10,
            "user_id": 30,
            "text": "cap",
            "msg_id": 0,
            "is_group": True,
            "is_channel_post": False,
            "entities": [],
        }
        err = media.validate_bot_job(sample)
        self.assertEqual(err, "msg_id must be > 0")


if __name__ == "__main__":
    unittest.main()
