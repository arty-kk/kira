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



    def test_dispatch_passive_moderation_strips_oversized_image(self) -> None:
        message = types.SimpleNamespace(chat=types.SimpleNamespace(id=123), message_id=778)
        payload = {"image_b64": "abcd", "image_mime": "image/jpeg"}
        prepare_globals = group.prepare_moderation_payload.__globals__

        with (
            patch.dict(
                prepare_globals,
                {
                    "MODERATION_MAX_IMAGE_BYTES": 1,
                    "MODERATION_MAX_PAYLOAD_BYTES": 1024,
                    "decode_base64_payload": lambda _value: b"abc",
                },
            ),
            patch.object(group.passive_moderate, "delay") as delay_mock,
        ):
            group._dispatch_passive_moderation(
                message,
                payload,
                text="hello",
                ents=[],
                is_channel=False,
                user_id_val=42,
            )

        moderation_payload = delay_mock.call_args.args[0]
        self.assertNotIn("image_b64", moderation_payload)
        self.assertNotIn("image_mime", moderation_payload)
        self.assertIn("is_channel_post", moderation_payload)
        self.assertIn("is_comment_context", moderation_payload)
        self.assertFalse(moderation_payload["is_channel_post"])

    def test_dispatch_passive_moderation_channel_source_kept(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123, linked_chat_id=None),
            message_id=779,
            sender_chat=types.SimpleNamespace(id=-10011, type=group.ChatType.CHANNEL),
            forward_from_chat=None,
            is_automatic_forward=False,
        )

        with patch.object(group.passive_moderate, "delay") as delay_mock:
            group._dispatch_passive_moderation(
                message,
                payload={},
                text="hello",
                ents=[],
                is_channel=True,
                user_id_val=99,
            )

        moderation_payload = delay_mock.call_args.args[0]
        self.assertEqual(moderation_payload["source"], "channel")
        self.assertTrue(moderation_payload["is_channel_post"])
        self.assertFalse(moderation_payload["is_comment_context"])

if __name__ == "__main__":
    unittest.main()
