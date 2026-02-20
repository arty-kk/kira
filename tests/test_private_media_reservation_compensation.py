import os
import types
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "12345:abcde")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("REDIS_URL_QUEUE", "redis://localhost:6379/1")
os.environ.setdefault("REDIS_URL_VECTOR", "redis://localhost:6379/2")

from app.bot.handlers import private


class PrivateMediaReservationCompensationTests(unittest.IsolatedAsyncioTestCase):
    async def test_photo_refund_when_not_enqueued(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=100),
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            message_id=77,
            media_group_id=None,
            caption="hello",
            caption_entities=[],
            photo=[types.SimpleNamespace(file_id="p1")],
            reply_to_message=None,
        )
        refund_mock = AsyncMock()

        with (
            patch.object(private, "is_spam", AsyncMock(return_value=False)),
            patch.object(private, "_first_delivery", AsyncMock(return_value=True)),
            patch.object(private, "_analytics_best_effort"),
            patch.object(private, "_ensure_access_and_increment", AsyncMock(return_value=(object(), True, "free", 901))),
            patch.object(private, "download_to_tmp", AsyncMock(return_value=None)),
            patch.object(private, "reject_multi_or_oversize_and_reply"),
            patch.object(private, "refund_reservation_by_id", refund_mock),
        ):
            await private.on_private_photo(message)

        refund_mock.assert_awaited_once_with(901)

    async def test_photo_without_caption_is_enqueued(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=101),
            from_user=types.SimpleNamespace(id=45, is_bot=False),
            message_id=78,
            media_group_id=None,
            caption=None,
            caption_entities=[],
            photo=[types.SimpleNamespace(file_id="p2")],
            reply_to_message=None,
        )

        with (
            patch.object(private, "is_spam", AsyncMock(return_value=False)),
            patch.object(private, "_first_delivery", AsyncMock(return_value=True)),
            patch.object(private, "_analytics_best_effort"),
            patch.object(private, "_ensure_access_and_increment", AsyncMock(return_value=(object(), True, "free", 904))),
            patch.object(private, "download_to_tmp", AsyncMock(return_value="/tmp/test.jpg")),
            patch.object(private, "strict_image_load", AsyncMock(return_value=object())),
            patch.object(private, "sanitize_and_compress", return_value=b"jpeg-bytes"),
            patch.object(private, "_handle_image_payload", AsyncMock()) as payload_mock,
            patch.object(private.os.path, "exists", return_value=False),
        ):
            await private.on_private_photo(message)

        payload_mock.assert_awaited_once()
        self.assertEqual(payload_mock.await_args.args[1], "")


    async def test_document_refund_when_not_enqueued(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=200),
            from_user=types.SimpleNamespace(id=43, is_bot=False),
            message_id=88,
            media_group_id=None,
            caption="doc",
            caption_entities=[],
            reply_to_message=None,
            document=types.SimpleNamespace(file_name="img.png", mime_type="image/png", file_size=1024),
        )
        refund_mock = AsyncMock()

        with (
            patch.object(private, "is_spam", AsyncMock(return_value=False)),
            patch.object(private, "_first_delivery", AsyncMock(return_value=True)),
            patch.object(private, "_analytics_best_effort"),
            patch.object(private, "_ensure_access_and_increment", AsyncMock(return_value=(object(), True, "free", 902))),
            patch.object(private, "download_to_tmp", AsyncMock(return_value=None)),
            patch.object(private, "reject_multi_or_oversize_and_reply"),
            patch.object(private, "refund_reservation_by_id", refund_mock),
        ):
            await private.on_private_document(message)

        refund_mock.assert_awaited_once_with(902)

    async def test_voice_refund_invariant_when_not_enqueued(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=300),
            from_user=types.SimpleNamespace(id=44, is_bot=False),
            message_id=99,
            reply_to_message=None,
            voice=types.SimpleNamespace(file_size=1024, duration=3, mime_type="audio/ogg", file_id="v1"),
        )
        refund_mock = AsyncMock()

        with (
            patch.object(private, "is_spam", AsyncMock(return_value=False)),
            patch.object(private, "_first_delivery", AsyncMock(return_value=True)),
            patch.object(private, "_analytics_best_effort"),
            patch.object(private, "_ensure_access_and_increment", AsyncMock(return_value=(object(), True, "free", 903))),
            patch.object(private, "_store_reply_target_best_effort", AsyncMock(side_effect=RuntimeError("boom"))),
            patch.object(private, "refund_reservation_by_id", refund_mock),
        ):
            with self.assertRaises(RuntimeError):
                await private.on_private_voice(message)

        self.assertGreaterEqual(refund_mock.await_count, 1)
        self.assertTrue(any(call.args == (903,) for call in refund_mock.await_args_list))


if __name__ == "__main__":
    unittest.main()
