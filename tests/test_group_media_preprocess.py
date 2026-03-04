import types
from contextlib import ExitStack
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.bot.handlers import group


class GroupImageEnqueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_image_common_dispatches_preprocess_for_caption_without_mention(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123),
            message_id=776,
            media_group_id="album-1",
            caption="just a caption",
            entities=[],
            caption_entities=[],
            reply_to_message=None,
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            sender_chat=None,
        )

        delay_mock = Mock()
        reject_mock = Mock()
        with (
            patch.object(group, "apply_moderation_filters", AsyncMock(return_value=False)),
            patch.object(group, "_reply_gate_requires_mention", return_value=False),
            patch.object(group, "_is_channel_post", return_value=False),
            patch.object(group, "_extract_entities", return_value=[]),
            patch.object(group, "split_context_text", return_value=("just a caption", "just a caption")),
            patch.object(group, "_is_mention", return_value=False),
            patch.object(group, "_mentions_other_user", return_value=False),
            patch.object(group, "_user_id_val", return_value=42),
            patch.object(group, "_resolve_group_comment_context", AsyncMock(return_value=False)),
            patch.object(group, "_channel_obj", return_value=None),
            patch.object(group.preprocess_group_image, "delay", delay_mock),
            patch.object(group, "reject_image_and_reply", reject_mock),
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
        self.assertTrue(payload["skip_responder_enqueue"])
        self.assertEqual(payload["file_id"], "photo-file-id")
        reject_mock.assert_not_called()

    async def test_group_document_image_common_dispatches_preprocess_without_mention(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123),
            message_id=775,
            media_group_id=None,
            caption="doc caption",
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
            patch.object(group, "split_context_text", return_value=("doc caption", "doc caption")),
            patch.object(group, "_is_mention", return_value=False),
            patch.object(group, "_mentions_other_user", return_value=False),
            patch.object(group, "_user_id_val", return_value=42),
            patch.object(group, "_resolve_group_comment_context", AsyncMock(return_value=False)),
            patch.object(group, "_channel_obj", return_value=None),
            patch.object(group.preprocess_group_image, "delay", delay_mock),
        ):
            await group._handle_group_image_message_common(
                message,
                file_id=None,
                document_id="doc-file-id",
                mime_type="image/png",
                suffix=".png",
                content_type_for_analytics="document",
            )

        delay_mock.assert_called_once()
        payload = delay_mock.call_args.args[0]
        self.assertTrue(payload["skip_responder_enqueue"])
        self.assertEqual(payload["document_id"], "doc-file-id")

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
            patch.object(group, "_resolve_group_comment_context", AsyncMock(return_value=True)),
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
        self.assertTrue(payload["is_comment_context"])
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
        self.assertFalse(payload["is_comment_context"])


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
        self.assertFalse(payload["is_comment_context"])

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
            patch.object(group, "_user_id_val", return_value=42),
            patch.object(group, "_resolve_group_comment_context", AsyncMock(return_value=False)),
            patch.object(group, "_dispatch_passive_moderation") as dispatch_mock,
            patch.object(group, "buffer_message_for_response", buffer_mock),
            patch.object(group, "record_activity", AsyncMock()),
        ):
            await group.on_group_voice(message)

        buffer_mock.assert_not_called()
        linked_check.assert_not_called()
        dispatch_mock.assert_called_once()



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
        self.assertIn("trusted_repost", moderation_payload)
        self.assertFalse(moderation_payload["trusted_repost"])

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
                trusted_repost=True,
            )

        moderation_payload = delay_mock.call_args.args[0]
        self.assertEqual(moderation_payload["source"], "channel")
        self.assertNotIn("is_channel_post", moderation_payload)
        self.assertTrue(moderation_payload["is_comment_context"])
        self.assertTrue(moderation_payload["trusted_repost"])

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
    def test_is_comment_context_false_for_general_linked_group_message(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123, linked_chat_id=-100500),
            sender_chat=None,
            forward_from_chat=None,
            reply_to_message=None,
            is_automatic_forward=False,
        )

        self.assertFalse(group.resolve_message_moderation_context(message) == "comment")

    def test_is_comment_context_true_with_channel_sender(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123, linked_chat_id=-100500),
            sender_chat=types.SimpleNamespace(id=-100500, type=group.ChatType.CHANNEL),
            forward_from_chat=None,
            reply_to_message=None,
            is_automatic_forward=False,
        )

        self.assertTrue(group.resolve_message_moderation_context(message) == "comment")

    def test_is_comment_context_false_for_regular_group_message(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123, linked_chat_id=None),
            sender_chat=None,
            forward_from_chat=None,
            reply_to_message=None,
            is_automatic_forward=False,
        )

        self.assertFalse(group.resolve_message_moderation_context(message) == "comment")

    def test_is_comment_context_true_for_reply_to_linked_channel_post(self) -> None:
        parent = types.SimpleNamespace(
            sender_chat=types.SimpleNamespace(id=-100500, type=group.ChatType.CHANNEL),
            forward_from_chat=None,
            is_automatic_forward=True,
        )
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123, linked_chat_id=-100500),
            sender_chat=None,
            forward_from_chat=None,
            reply_to_message=parent,
            is_automatic_forward=False,
        )

        self.assertTrue(group.resolve_message_moderation_context(message) == "comment")



class TrustedRepostIgnoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_on_group_message_trusted_chat_repost_logs_stm_without_passive(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123),
            message_id=990,
            text="trusted repost text",
            caption=None,
            entities=[],
            caption_entities=[],
            reply_to_message=None,
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            sender_chat=None,
            forward_from_chat=types.SimpleNamespace(id=-100201, type=group.ChatType.SUPERGROUP),
            is_automatic_forward=False,
        )

        with (
            patch.object(group, "settings", types.SimpleNamespace(ALLOWED_GROUP_IDS=[123, -100201], COMMENT_TARGET_CHAT_IDS=[], COMMENT_SOURCE_CHANNEL_IDS=[], GROUP_AUTOREPLY_ON_TOPIC=True)),
            patch.object(group, "_is_message_allowed_for_group_handlers", AsyncMock(return_value=True)),
            patch.object(group, "_first_delivery", AsyncMock(return_value=True)),
            patch.object(group, "_update_presence", AsyncMock()),
            patch.object(group, "record_activity", AsyncMock()),
            patch.object(group, "apply_moderation_filters", AsyncMock(return_value=False)),
            patch.object(group, "_is_channel_post", return_value=False),
            patch.object(group, "_reply_gate_requires_mention", return_value=False),
            patch.object(group, "_extract_entities", return_value=[]),
            patch.object(group, "split_context_text", return_value=("trusted repost text", "trusted repost text")),
            patch.object(group, "_store_context", AsyncMock()) as store_ctx,
            patch.object(group, "_push_group_stm_and_recent", AsyncMock()) as push_stm,
            patch.object(group, "_dispatch_passive_moderation") as dispatch_mock,
            patch.object(group, "buffer_message_for_response") as buffer_mock,
        ):
            await group.on_group_message(message)

        buffer_mock.assert_not_called()
        store_ctx.assert_awaited_once()
        push_stm.assert_awaited_once()
        dispatch_mock.assert_not_called()


    async def test_on_group_message_sender_chat_same_chat_is_not_treated_as_trusted_repost(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123),
            message_id=993,
            text="hello from anon admin",
            caption=None,
            entities=[],
            caption_entities=[],
            reply_to_message=None,
            from_user=None,
            sender_chat=types.SimpleNamespace(id=123, type=group.ChatType.SUPERGROUP),
            forward_from_chat=None,
            is_automatic_forward=False,
        )

        with ExitStack() as stack:
            stack.enter_context(patch.object(group, "settings", types.SimpleNamespace(ALLOWED_GROUP_IDS=[123], COMMENT_TARGET_CHAT_IDS=[], COMMENT_SOURCE_CHANNEL_IDS=[], GROUP_AUTOREPLY_ON_TOPIC=True)))
            stack.enter_context(patch.object(group, "_is_message_allowed_for_group_handlers", AsyncMock(return_value=True)))
            stack.enter_context(patch.object(group, "_first_delivery", AsyncMock(return_value=True)))
            stack.enter_context(patch.object(group, "_update_presence", AsyncMock()))
            stack.enter_context(patch.object(group, "apply_moderation_filters", AsyncMock(return_value=False)))
            stack.enter_context(patch.object(group, "_is_channel_post", return_value=False))
            stack.enter_context(patch.object(group, "_reply_gate_requires_mention", return_value=False))
            stack.enter_context(patch.object(group, "_extract_entities", return_value=[]))
            stack.enter_context(patch.object(group, "split_context_text", return_value=("hello from anon admin", "hello from anon admin")))
            stack.enter_context(patch.object(group, "_is_mention", return_value=False))
            stack.enter_context(patch.object(group, "_mentions_other_user", return_value=False))
            stack.enter_context(patch.object(group, "_is_bot_command_to_us", return_value=False))
            stack.enter_context(patch.object(group, "_is_cmd_addressed_to_other_bot", return_value=False))
            stack.enter_context(patch.object(group, "_is_clean_message_for_on_topic", return_value=True))
            stack.enter_context(patch.object(group, "_maybe_handle_battle", AsyncMock(return_value=False)))
            stack.enter_context(patch.object(group, "_ensure_daily_limit", AsyncMock(return_value=True)))
            stack.enter_context(patch.object(group, "_user_id_val", return_value=123))
            stack.enter_context(patch.object(group, "_replied_to_our_bot", return_value=False))
            stack.enter_context(patch.object(group, "_store_context", AsyncMock()))
            stack.enter_context(patch.object(group, "_channel_obj", return_value=None))
            stack.enter_context(patch.object(group, "_resolve_group_comment_context", AsyncMock(return_value=False)))
            stack.enter_context(patch.object(group, "_push_group_stm_and_recent", AsyncMock()))
            stack.enter_context(patch.object(group, "_analytics_best_effort"))
            stack.enter_context(patch.object(group, "_dispatch_passive_moderation"))
            stack.enter_context(patch.object(group.redis_client, "sadd", AsyncMock()))
            ignored_log = stack.enter_context(patch.object(group, "_log_ignored_repost_to_stm", AsyncMock()))
            buffer_mock = stack.enter_context(patch.object(group, "buffer_message_for_response"))
            await group.on_group_message(message)

        ignored_log.assert_not_awaited()
        buffer_mock.assert_called_once()

    async def test_on_group_message_trusted_channel_post_logs_stm_without_passive(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123),
            message_id=992,
            text="trusted channel post",
            caption=None,
            entities=[],
            caption_entities=[],
            reply_to_message=None,
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            sender_chat=types.SimpleNamespace(id=-10011, type=group.ChatType.CHANNEL),
            forward_from_chat=None,
            is_automatic_forward=False,
        )

        with (
            patch.object(group, "settings", types.SimpleNamespace(ALLOWED_GROUP_IDS=[123], COMMENT_TARGET_CHAT_IDS=[], COMMENT_SOURCE_CHANNEL_IDS=[-10011], GROUP_AUTOREPLY_ON_TOPIC=True)),
            patch.object(group, "_is_message_allowed_for_group_handlers", AsyncMock(return_value=True)),
            patch.object(group, "_first_delivery", AsyncMock(return_value=True)),
            patch.object(group, "_update_presence", AsyncMock()),
            patch.object(group, "record_activity", AsyncMock()),
            patch.object(group, "apply_moderation_filters", AsyncMock(return_value=False)),
            patch.object(group, "_is_channel_post", return_value=True),
            patch.object(group, "_reply_gate_requires_mention", return_value=False),
            patch.object(group, "_extract_entities", return_value=[]),
            patch.object(group, "split_context_text", return_value=("trusted channel post", "trusted channel post")),
            patch.object(group, "_maybe_log_channel_post", AsyncMock(return_value=True)) as channel_log,
            patch.object(group, "_log_ignored_repost_to_stm", AsyncMock()) as ignored_log,
            patch.object(group, "_dispatch_passive_moderation") as dispatch_mock,
            patch.object(group, "buffer_message_for_response") as buffer_mock,
        ):
            await group.on_group_message(message)

        buffer_mock.assert_not_called()
        channel_log.assert_not_awaited()
        ignored_log.assert_awaited_once()
        dispatch_mock.assert_not_called()

    async def test_on_group_voice_trusted_channel_repost_logs_stm_without_passive(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123),
            message_id=991,
            from_user=types.SimpleNamespace(id=42, is_bot=False),
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

        with (
            patch.object(group, "settings", types.SimpleNamespace(ALLOWED_GROUP_IDS=[123], COMMENT_TARGET_CHAT_IDS=[], COMMENT_SOURCE_CHANNEL_IDS=[-10011], GROUP_AUTOREPLY_ON_TOPIC=True)),
            patch.object(group, "_is_message_allowed_for_group_handlers", AsyncMock(return_value=True)),
            patch.object(group, "_first_delivery", AsyncMock(return_value=True)),
            patch.object(group, "apply_moderation_filters", AsyncMock(return_value=False)),
            patch.object(group, "_update_presence", AsyncMock()),
            patch.object(group, "record_activity", AsyncMock()),
            patch.object(group, "_reply_gate_requires_mention", return_value=False),
            patch.object(group, "_is_channel_post", return_value=True),
            patch.object(group, "is_from_linked_channel", AsyncMock(return_value=True)),
            patch.object(group, "_log_ignored_repost_to_stm", AsyncMock()) as ignored_log,
            patch.object(group, "_ensure_daily_limit", AsyncMock(return_value=True)),
            patch.object(group.redis_client, "sadd", AsyncMock()),
            patch.object(group, "_dispatch_passive_moderation") as dispatch_mock,
            patch.object(group, "buffer_message_for_response") as buffer_mock,
        ):
            await group.on_group_voice(message)

        buffer_mock.assert_not_called()
        ignored_log.assert_awaited_once()
        dispatch_mock.assert_not_called()


    async def test_on_group_image_trusted_repost_logs_stm_without_enqueue(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123, title="chat"),
            message_id=994,
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            caption="trusted image repost",
            entities=[],
            caption_entities=[],
            reply_to_message=None,
            sender_chat=types.SimpleNamespace(id=-10011, type=group.ChatType.CHANNEL),
            forward_from_chat=None,
            is_automatic_forward=False,
        )

        with (
            patch.object(group, "settings", types.SimpleNamespace(ALLOWED_GROUP_IDS=[123], COMMENT_TARGET_CHAT_IDS=[], COMMENT_SOURCE_CHANNEL_IDS=[-10011], GROUP_AUTOREPLY_ON_TOPIC=True)),
            patch.object(group, "apply_moderation_filters", AsyncMock(return_value=False)),
            patch.object(group, "_reply_gate_requires_mention", return_value=False),
            patch.object(group, "_is_channel_post", return_value=True),
            patch.object(group, "is_from_linked_channel", AsyncMock(return_value=True)),
            patch.object(group, "_extract_entities", return_value=[]),
            patch.object(group, "split_context_text", return_value=("trusted image repost", "trusted image repost")),
            patch.object(group, "_is_mention", return_value=False),
            patch.object(group, "_mentions_other_user", return_value=False),
            patch.object(group, "is_single_media", return_value=True),
            patch.object(group, "_ensure_daily_limit", AsyncMock(return_value=True)),
            patch.object(group, "_user_id_val", return_value=42),
            patch.object(group, "_replied_to_our_bot", return_value=False),
            patch.object(group, "_channel_obj", return_value=None),
            patch.object(group, "_resolve_group_comment_context", AsyncMock(return_value=True)),
            patch.object(group, "_analytics_best_effort"),
            patch.object(group.redis_client, "sadd", AsyncMock()),
            patch.object(group, "_store_context", AsyncMock()) as store_ctx,
            patch.object(group, "_push_group_stm_and_recent", AsyncMock()) as push_stm,
            patch.object(group.preprocess_group_image, "delay") as preprocess_mock,
        ):
            await group._handle_group_image_message_common(
                message,
                file_id="file-id",
                document_id=None,
                mime_type="image/jpeg",
                suffix=".jpg",
                content_type_for_analytics="photo",
            )

        store_ctx.assert_awaited_once()
        push_stm.assert_awaited_once()
        preprocess_mock.assert_not_called()


class GroupFallbackModerationTests(unittest.IsolatedAsyncioTestCase):
    def _make_message(self, *, content_type, document=None):
        return types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123, type=group.ChatType.GROUP),
            message_id=2001,
            content_type=content_type,
            document=document,
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            sender_chat=None,
            forward_from_chat=None,
            text=None,
            caption=None,
            entities=[],
            caption_entities=[],
        )

    async def test_fallback_calls_moderation_for_trusted_chat_video_only(self) -> None:
        message = self._make_message(content_type=group.ContentType.VIDEO)

        with (
            patch.object(group, "settings", types.SimpleNamespace(ALLOWED_GROUP_IDS=[123], COMMENT_TARGET_CHAT_IDS=[], COMMENT_SOURCE_CHANNEL_IDS=[])),
            patch.object(group, "_first_delivery", AsyncMock(return_value=True)),
            patch.object(group, "apply_moderation_filters", AsyncMock(return_value=False)) as moderation_mock,
        ):
            await group.on_group_fallback_moderation(message)
            moderation_mock.assert_awaited_once_with(123, message)

            for covered_type in (group.ContentType.TEXT, group.ContentType.VOICE, group.ContentType.PHOTO):
                await group.on_group_fallback_moderation(self._make_message(content_type=covered_type))

            image_doc = types.SimpleNamespace(mime_type="image/png")
            await group.on_group_fallback_moderation(
                self._make_message(content_type=group.ContentType.DOCUMENT, document=image_doc)
            )

        moderation_mock.assert_awaited_once()

    async def test_fallback_calls_moderation_for_comment_target_video_only(self) -> None:
        message = self._make_message(content_type=group.ContentType.VIDEO)

        with (
            patch.object(group, "settings", types.SimpleNamespace(ALLOWED_GROUP_IDS=[], COMMENT_TARGET_CHAT_IDS=[123], COMMENT_SOURCE_CHANNEL_IDS=[])),
            patch.object(group, "_first_delivery", AsyncMock(return_value=True)),
            patch.object(group, "apply_moderation_filters", AsyncMock(return_value=False)) as moderation_mock,
        ):
            await group.on_group_fallback_moderation(message)
            moderation_mock.assert_awaited_once_with(123, message)

            for covered_type in (group.ContentType.TEXT, group.ContentType.VOICE, group.ContentType.PHOTO):
                await group.on_group_fallback_moderation(self._make_message(content_type=covered_type))

            image_doc = types.SimpleNamespace(mime_type="image/jpeg")
            await group.on_group_fallback_moderation(
                self._make_message(content_type=group.ContentType.DOCUMENT, document=image_doc)
            )

        moderation_mock.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
