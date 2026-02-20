import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

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

    async def test_group_image_common_skips_non_channel_bot_messages(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123),
            message_id=778,
            media_group_id=None,
            caption="hello",
            entities=[],
            caption_entities=[],
            reply_to_message=None,
            from_user=types.SimpleNamespace(id=42, is_bot=True),
            sender_chat=None,
        )

        delay_mock = Mock()
        with (
            patch.object(group, "apply_moderation_filters", AsyncMock(return_value=False)),
            patch.object(group, "_reply_gate_requires_mention", return_value=False),
            patch.object(group, "_is_channel_post", return_value=False),
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
            await group._handle_group_image_message_common(
                message,
                file_id=None,
                document_id="doc-file-id",
                mime_type="image/png",
                suffix=".png",
                content_type_for_analytics="document",
            )

        delay_mock.assert_not_called()

    async def test_group_image_common_allows_channel_bot_messages(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123),
            message_id=779,
            media_group_id=None,
            caption="hello",
            entities=[],
            caption_entities=[],
            reply_to_message=None,
            from_user=types.SimpleNamespace(id=42, is_bot=True),
            sender_chat=types.SimpleNamespace(id=-10011, type=group.ChatType.CHANNEL),
        )

        delay_mock = Mock()
        with (
            patch.object(group, "apply_moderation_filters", AsyncMock(return_value=False)),
            patch.object(group, "_reply_gate_requires_mention", return_value=False),
            patch.object(group, "_is_channel_post", return_value=True),
            patch.object(group, "is_from_linked_channel", AsyncMock(return_value=True)),
            patch.object(group, "_extract_entities", return_value=[]),
            patch.object(group, "split_context_text", return_value=("hello", "hello")),
            patch.object(group, "_is_mention", return_value=False),
            patch.object(group, "_mentions_other_user", return_value=False),
            patch.object(group, "_ensure_daily_limit", AsyncMock(return_value=True)),
            patch.object(group, "_user_id_val", return_value=42),
            patch.object(group, "_replied_to_our_bot", return_value=False),
            patch.object(group, "_channel_obj", return_value=types.SimpleNamespace(id=-10011, title="channel")),
            patch.object(group, "_resolve_group_comment_context", AsyncMock(return_value=False)),
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
        self.assertEqual(payload["trigger"], "channel_post")
        self.assertTrue(payload["is_channel_post"])


class GroupReplyMentionFallbackTests(unittest.IsolatedAsyncioTestCase):
    def test_reply_to_bot_username_works_without_bot_id(self) -> None:
        message = types.SimpleNamespace(
            reply_to_message=types.SimpleNamespace(
                from_user=types.SimpleNamespace(id=9001, username="MyBot", is_bot=True),
            ),
            text=None,
            caption=None,
            entities=[],
            caption_entities=[],
        )

        with (
            patch.object(group.consts, "BOT_ID", None),
            patch.object(group.consts, "BOT_USERNAME", "MyBot"),
        ):
            self.assertTrue(group._is_mention(message))
            self.assertTrue(group._replied_to_our_bot(message))
            self.assertFalse(group._reply_gate_requires_mention(message))



class GroupVoiceHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_on_group_voice_skips_non_channel_bot_messages(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123),
            message_id=880,
            from_user=types.SimpleNamespace(id=42, is_bot=True),
            voice=types.SimpleNamespace(file_id="voice-file-id"),
            reply_to_message=None,
            entities=[],
            caption_entities=[],
            text=None,
            caption=None,
            sender_chat=None,
            forward_from_chat=None,
            is_automatic_forward=False,
        )

        buffer_mock = Mock()
        linked_check = AsyncMock(return_value=True)
        with (
            patch.object(group, "_is_message_allowed_for_group_handlers", AsyncMock(return_value=True)),
            patch.object(group, "_first_delivery", AsyncMock(return_value=True)),
            patch.object(group, "apply_moderation_filters", AsyncMock(return_value=False)),
            patch.object(group, "_update_presence", AsyncMock()),
            patch.object(group, "_reply_gate_requires_mention", return_value=False),
            patch.object(group, "_is_channel_post", return_value=False),
            patch.object(group, "is_from_linked_channel", linked_check),
            patch.object(group, "buffer_message_for_response", buffer_mock),
            patch.object(group, "record_activity", AsyncMock()),
        ):
            await group.on_group_voice(message)

        buffer_mock.assert_not_called()
        linked_check.assert_not_called()

    async def test_on_group_voice_allows_channel_bot_messages(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123),
            message_id=881,
            from_user=types.SimpleNamespace(id=42, is_bot=True),
            voice=types.SimpleNamespace(file_id="voice-file-id"),
            reply_to_message=None,
            entities=[],
            caption_entities=[],
            text=None,
            caption=None,
            sender_chat=types.SimpleNamespace(id=-10011, type=group.ChatType.CHANNEL),
            forward_from_chat=None,
            is_automatic_forward=False,
        )

        buffer_mock = Mock()
        linked_check = AsyncMock(return_value=True)
        with (
            patch.object(group, "_is_message_allowed_for_group_handlers", AsyncMock(return_value=True)),
            patch.object(group, "_first_delivery", AsyncMock(return_value=True)),
            patch.object(group, "apply_moderation_filters", AsyncMock(return_value=False)),
            patch.object(group, "_update_presence", AsyncMock()),
            patch.object(group, "_reply_gate_requires_mention", return_value=False),
            patch.object(group, "_is_channel_post", return_value=True),
            patch.object(group, "is_from_linked_channel", linked_check),
            patch.object(group, "_is_mention", return_value=False),
            patch.object(group, "_ensure_daily_limit", AsyncMock(return_value=True)),
            patch.object(group, "inc_msg_count", AsyncMock()),
            patch.object(group, "_channel_obj", return_value=types.SimpleNamespace(id=-10011, title="channel")),
            patch.object(group, "_user_id_val", return_value=42),
            patch.object(group, "_replied_to_our_bot", return_value=False),
            patch.object(group, "_extract_entities", return_value=[]),
            patch.object(group, "_resolve_group_comment_context", AsyncMock(return_value=False)),
            patch.object(group, "_analytics_best_effort"),
            patch.object(group.redis_client, "sadd", AsyncMock()),
            patch.object(group, "_dispatch_passive_moderation"),
            patch.object(group, "buffer_message_for_response", buffer_mock),
        ):
            await group.on_group_voice(message)

        buffer_mock.assert_called_once()
        linked_check.assert_awaited_once()
        payload = buffer_mock.call_args.args[0]
        self.assertEqual(payload["trigger"], "channel_post")
        self.assertTrue(payload["is_channel_post"])

    async def test_on_group_voice_skips_non_channel_without_mention_even_with_on_topic(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123),
            message_id=882,
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            voice=types.SimpleNamespace(file_id="voice-file-id"),
            reply_to_message=None,
            entities=[],
            caption_entities=[],
            text=None,
            caption=None,
            sender_chat=None,
            forward_from_chat=None,
            is_automatic_forward=False,
        )

        buffer_mock = Mock()
        linked_check = AsyncMock(return_value=True)
        with (
            patch.object(group, "_is_message_allowed_for_group_handlers", AsyncMock(return_value=True)),
            patch.object(group, "_first_delivery", AsyncMock(return_value=True)),
            patch.object(group, "apply_moderation_filters", AsyncMock(return_value=False)),
            patch.object(group, "_update_presence", AsyncMock()),
            patch.object(group, "_reply_gate_requires_mention", return_value=False),
            patch.object(group, "_is_channel_post", return_value=False),
            patch.object(group, "_is_mention", return_value=False),
            patch.object(group, "is_from_linked_channel", linked_check),
            patch.object(group, "buffer_message_for_response", buffer_mock),
            patch.object(group, "record_activity", AsyncMock()),
        ):
            await group.on_group_voice(message)

        buffer_mock.assert_not_called()
        linked_check.assert_not_called()



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
                is_comment_context=False,
            )

        moderation_payload = delay_mock.call_args.args[0]
        self.assertNotIn("image_b64", moderation_payload)
        self.assertNotIn("image_mime", moderation_payload)
        self.assertNotIn("is_channel_post", moderation_payload)
        self.assertIn("is_comment_context", moderation_payload)
        self.assertFalse(moderation_payload["is_comment_context"])

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
                is_comment_context=True,
            )

        moderation_payload = delay_mock.call_args.args[0]
        self.assertEqual(moderation_payload["source"], "channel")
        self.assertNotIn("is_channel_post", moderation_payload)
        self.assertTrue(moderation_payload["is_comment_context"])

    async def test_localized_group_image_error_does_not_include_misleading_5mb_hint(self) -> None:
        send_mock = AsyncMock()
        with (
            patch.object(group, "t", AsyncMock(side_effect=Exception("no i18n"))),
            patch.object(group, "send_message_safe", send_mock),
        ):
            await group.localized_group_image_error(123, "unsupported image format", 777)

        sent_text = send_mock.await_args.args[2]
        self.assertIn("unsupported image format", sent_text)
        self.assertNotIn("≤ 5 MB", sent_text)
        self.assertNotIn("send exactly one image", sent_text.lower())


class GroupCommentContextTests(unittest.TestCase):
    def test_is_comment_context_detects_linked_chat_message(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123, linked_chat_id=-100500),
            sender_chat=None,
            forward_from_chat=None,
            is_automatic_forward=False,
        )

        self.assertTrue(group.resolve_message_moderation_context(message) == "comment")

    def test_is_comment_context_false_for_regular_group_message(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123, linked_chat_id=None),
            sender_chat=None,
            forward_from_chat=None,
            is_automatic_forward=False,
        )

        self.assertFalse(group.resolve_message_moderation_context(message) == "comment")



if __name__ == "__main__":
    unittest.main()
