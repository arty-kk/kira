import types
import unittest
from unittest.mock import AsyncMock, patch

from app.bot.handlers import group


class GroupImageEnqueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_image_common_enqueues_preprocess_without_inline_image(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123),
            message_id=777,
            media_group_id=None,
            caption="hello",
            entities=[],
            caption_entities=[],
            reply_to_message=None,
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            sender_chat=None,
        )

        from unittest.mock import Mock
        delay_mock = Mock()
        with (
            patch.object(group, "apply_moderation_filters", AsyncMock(return_value=False)),
            patch.object(group, "_reply_gate_requires_mention", return_value=False),
            patch.object(group, "_is_channel_post", return_value=False),
            patch.object(group, "_extract_entities", return_value=[]),
            patch.object(group, "split_context_text", return_value=("hello", "hello")),
            patch.object(group, "_is_mention", return_value=True),
            patch.object(group, "_mentions_other_user", return_value=False),
            patch.object(group, "_ensure_daily_limit", AsyncMock(return_value=True)),
            patch.object(group, "_user_id_val", return_value=42),
            patch.object(group, "_replied_to_our_bot", return_value=False),
            patch.object(group, "_channel_obj", return_value=None),
            patch.object(group, "_analytics_best_effort"),
            patch.object(group.redis_client, "sadd", AsyncMock()),
            patch.object(group.preprocess_group_image, "delay", delay_mock),
        ):
            await group._handle_group_image_message_common(
                message,
                file_id="photo-file-id",
                document_id=None,
                mime_type="image/jpeg",
                suffix=".jpg",
                content_type_for_analytics="photo",
            )

        delay_mock.assert_called_once()
        payload = delay_mock.call_args.args[0]
        self.assertEqual(payload["chat_id"], 123)
        self.assertEqual(payload["message_id"], 777)
        self.assertEqual(payload["file_id"], "photo-file-id")
        self.assertNotIn("image_b64", payload)


if __name__ == "__main__":
    unittest.main()
