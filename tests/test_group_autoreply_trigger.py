import types
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

from app.bot.handlers import group


class ResolveAutoreplyTriggerTests(unittest.TestCase):
    def test_resolver_matrix(self) -> None:
        cases = [
            (
                "channel_post_priority",
                dict(
                    is_channel=True,
                    mentioned=False,
                    mentions_other=True,
                    has_content_signal=False,
                    is_battle_cmd_to_us=False,
                    autoreply_on_topic=False,
                ),
                "channel_post",
            ),
            (
                "mentioned",
                dict(
                    is_channel=False,
                    mentioned=True,
                    mentions_other=False,
                    has_content_signal=False,
                    is_battle_cmd_to_us=False,
                    autoreply_on_topic=True,
                ),
                "mention",
            ),
            (
                "mentioned_and_mentions_other",
                dict(
                    is_channel=False,
                    mentioned=True,
                    mentions_other=True,
                    has_content_signal=True,
                    is_battle_cmd_to_us=False,
                    autoreply_on_topic=True,
                ),
                "mention",
            ),
            (
                "battle_to_us",
                dict(
                    is_channel=False,
                    mentioned=False,
                    mentions_other=False,
                    has_content_signal=False,
                    is_battle_cmd_to_us=True,
                    autoreply_on_topic=True,
                ),
                "mention",
            ),
            (
                "battle_to_us_with_mentions_other",
                dict(
                    is_channel=False,
                    mentioned=False,
                    mentions_other=True,
                    has_content_signal=True,
                    is_battle_cmd_to_us=True,
                    autoreply_on_topic=True,
                ),
                "mention",
            ),
            (
                "mentions_other_blocks",
                dict(
                    is_channel=False,
                    mentioned=False,
                    mentions_other=True,
                    has_content_signal=True,
                    is_battle_cmd_to_us=False,
                    autoreply_on_topic=True,
                ),
                None,
            ),
            (
                "content_signal_required",
                dict(
                    is_channel=False,
                    mentioned=False,
                    mentions_other=False,
                    has_content_signal=False,
                    is_battle_cmd_to_us=False,
                    autoreply_on_topic=True,
                ),
                None,
            ),
            (
                "on_topic_disabled",
                dict(
                    is_channel=False,
                    mentioned=False,
                    mentions_other=False,
                    has_content_signal=True,
                    is_battle_cmd_to_us=False,
                    autoreply_on_topic=False,
                ),
                None,
            ),
            (
                "on_topic_enabled",
                dict(
                    is_channel=False,
                    mentioned=False,
                    mentions_other=False,
                    has_content_signal=True,
                    is_battle_cmd_to_us=False,
                    autoreply_on_topic=True,
                ),
                "check_on_topic",
            ),
        ]

        for name, kwargs, expected in cases:
            with self.subTest(case=name):
                self.assertEqual(group._resolve_autoreply_trigger(**kwargs), expected)


class CleanOnTopicMessageTests(unittest.TestCase):
    def test_requires_message_without_reply_or_mentions(self) -> None:
        clean_message = types.SimpleNamespace(reply_to_message=None)
        self.assertTrue(
            group._is_clean_message_for_on_topic(
                clean_message,
                mentioned=False,
                mentions_other=False,
            )
        )

        reply_message = types.SimpleNamespace(reply_to_message=types.SimpleNamespace(message_id=1))
        self.assertFalse(
            group._is_clean_message_for_on_topic(
                reply_message,
                mentioned=False,
                mentions_other=False,
            )
        )

        self.assertFalse(
            group._is_clean_message_for_on_topic(
                clean_message,
                mentioned=True,
                mentions_other=False,
            )
        )
        self.assertFalse(
            group._is_clean_message_for_on_topic(
                clean_message,
                mentioned=False,
                mentions_other=True,
            )
        )


class GroupHandlerTriggerContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_reply_gate_message_still_dispatches_passive_moderation(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123),
            message_id=88,
            text="reply text",
            caption=None,
            entities=[],
            caption_entities=[],
            reply_to_message=types.SimpleNamespace(message_id=77),
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            sender_chat=None,
            forward_from_chat=None,
            is_automatic_forward=False,
        )

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    group,
                    "settings",
                    types.SimpleNamespace(GROUP_AUTOREPLY_ON_TOPIC=True),
                )
            )
            stack.enter_context(
                patch.object(
                    group,
                    "_is_message_allowed_for_group_handlers",
                    AsyncMock(return_value=True),
                )
            )
            stack.enter_context(
                patch.object(group, "_first_delivery", AsyncMock(return_value=True))
            )
            stack.enter_context(patch.object(group, "_update_presence", AsyncMock()))
            stack.enter_context(patch.object(group, "record_activity", AsyncMock()))
            stack.enter_context(
                patch.object(
                    group, "apply_moderation_filters", AsyncMock(return_value=False)
                )
            )
            stack.enter_context(patch.object(group, "_is_channel_post", return_value=False))
            stack.enter_context(patch.object(group, "_reply_gate_requires_mention", return_value=True))
            stack.enter_context(patch.object(group, "_extract_entities", return_value=[]))
            stack.enter_context(
                patch.object(group, "split_context_text", return_value=("reply text", "reply text"))
            )
            stack.enter_context(
                patch.object(
                    group,
                    "_resolve_group_comment_context",
                    AsyncMock(return_value=True),
                )
            )
            stack.enter_context(patch.object(group, "_user_id_val", return_value=42))
            buffer_mock = stack.enter_context(
                patch.object(group, "buffer_message_for_response")
            )
            dispatch_mock = stack.enter_context(
                patch.object(group, "_dispatch_passive_moderation")
            )

            await group.on_group_message(message)

        buffer_mock.assert_not_called()
        dispatch_mock.assert_called_once()

    async def test_reply_gate_voice_still_dispatches_passive_moderation(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123),
            message_id=89,
            voice=types.SimpleNamespace(file_id="voice-file-id"),
            entities=[],
            caption_entities=[],
            reply_to_message=types.SimpleNamespace(message_id=77),
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            sender_chat=None,
            forward_from_chat=None,
            is_automatic_forward=False,
            text=None,
            caption=None,
        )

        with ExitStack() as stack:
            stack.enter_context(patch.object(group, "_is_message_allowed_for_group_handlers", AsyncMock(return_value=True)))
            stack.enter_context(patch.object(group, "_first_delivery", AsyncMock(return_value=True)))
            stack.enter_context(patch.object(group, "apply_moderation_filters", AsyncMock(return_value=False)))
            stack.enter_context(patch.object(group, "_update_presence", AsyncMock()))
            stack.enter_context(patch.object(group, "record_activity", AsyncMock()))
            stack.enter_context(patch.object(group, "_is_channel_post", return_value=False))
            stack.enter_context(patch.object(group, "_reply_gate_requires_mention", return_value=True))
            stack.enter_context(patch.object(group, "_user_id_val", return_value=42))
            stack.enter_context(patch.object(group, "_resolve_group_comment_context", AsyncMock(return_value=True)))
            buffer_mock = stack.enter_context(patch.object(group, "buffer_message_for_response"))
            dispatch_mock = stack.enter_context(patch.object(group, "_dispatch_passive_moderation"))

            await group.on_group_voice(message)

        buffer_mock.assert_not_called()
        dispatch_mock.assert_called_once()

    async def test_reply_gate_image_still_dispatches_passive_moderation(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123),
            message_id=90,
            media_group_id=None,
            caption="caption",
            entities=[],
            caption_entities=[],
            reply_to_message=types.SimpleNamespace(message_id=77),
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            sender_chat=None,
            forward_from_chat=None,
            is_automatic_forward=False,
        )

        with ExitStack() as stack:
            stack.enter_context(patch.object(group, "apply_moderation_filters", AsyncMock(return_value=False)))
            stack.enter_context(patch.object(group, "_is_channel_post", return_value=False))
            stack.enter_context(patch.object(group, "_extract_entities", return_value=[]))
            stack.enter_context(patch.object(group, "split_context_text", return_value=("caption", "caption")))
            stack.enter_context(patch.object(group, "_reply_gate_requires_mention", return_value=True))
            stack.enter_context(patch.object(group, "_user_id_val", return_value=42))
            stack.enter_context(patch.object(group, "_resolve_group_comment_context", AsyncMock(return_value=True)))
            stack.enter_context(patch.object(group.preprocess_group_image, "delay"))
            dispatch_mock = stack.enter_context(patch.object(group, "_dispatch_passive_moderation"))

            await group._handle_group_image_message_common(
                message,
                file_id="photo-file-id",
                document_id=None,
                mime_type="image/jpeg",
                suffix=".jpg",
                content_type_for_analytics="photo",
            )

        dispatch_mock.assert_called_once()

    async def test_mentions_other_still_dispatches_passive_moderation(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123),
            message_id=77,
            text="@someone hello",
            caption=None,
            entities=[],
            caption_entities=[],
            reply_to_message=None,
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            sender_chat=None,
            forward_from_chat=None,
            is_automatic_forward=False,
        )

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    group,
                    "settings",
                    types.SimpleNamespace(GROUP_AUTOREPLY_ON_TOPIC=True),
                )
            )
            stack.enter_context(
                patch.object(
                    group,
                    "_is_message_allowed_for_group_handlers",
                    AsyncMock(return_value=True),
                )
            )
            stack.enter_context(
                patch.object(group, "_first_delivery", AsyncMock(return_value=True))
            )
            stack.enter_context(patch.object(group, "_update_presence", AsyncMock()))
            stack.enter_context(patch.object(group, "record_activity", AsyncMock()))
            stack.enter_context(
                patch.object(
                    group, "apply_moderation_filters", AsyncMock(return_value=False)
                )
            )
            stack.enter_context(
                patch.object(group, "_is_channel_post", return_value=False)
            )
            stack.enter_context(
                patch.object(group, "_reply_gate_requires_mention", return_value=False)
            )
            stack.enter_context(patch.object(group, "_extract_entities", return_value=[]))
            stack.enter_context(
                patch.object(group, "split_context_text", return_value=("hello", "hello"))
            )
            stack.enter_context(patch.object(group, "_is_mention", return_value=False))
            stack.enter_context(
                patch.object(group, "_mentions_other_user", return_value=True)
            )
            stack.enter_context(
                patch.object(group, "_is_bot_command_to_us", return_value=False)
            )
            stack.enter_context(
                patch.object(
                    group,
                    "_resolve_group_comment_context",
                    AsyncMock(return_value=False),
                )
            )
            stack.enter_context(patch.object(group, "_user_id_val", return_value=42))
            buffer_mock = stack.enter_context(
                patch.object(group, "buffer_message_for_response")
            )
            dispatch_mock = stack.enter_context(
                patch.object(group, "_dispatch_passive_moderation")
            )

            await group.on_group_message(message)

        buffer_mock.assert_not_called()
        dispatch_mock.assert_called_once()

    async def _trigger_from_text(
        self,
        *,
        is_channel: bool,
        mentioned: bool,
        mentions_other: bool,
        has_content_signal: bool,
        is_battle_cmd_to_us: bool,
        autoreply_on_topic: bool,
        is_comment_context: bool = False,
    ) -> str | None:
        text_value = "hello" if has_content_signal else ""
        channel_chat = (
            types.SimpleNamespace(
                id=-10011, type=group.ChatType.CHANNEL, title="channel"
            )
            if is_channel
            else None
        )
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123),
            message_id=11,
            text=text_value,
            caption=None,
            entities=[],
            caption_entities=[],
            reply_to_message=None,
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            sender_chat=channel_chat,
            forward_from_chat=None,
            is_automatic_forward=False,
        )

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    group,
                    "settings",
                    types.SimpleNamespace(GROUP_AUTOREPLY_ON_TOPIC=autoreply_on_topic),
                )
            )
            stack.enter_context(
                patch.object(
                    group,
                    "_is_message_allowed_for_group_handlers",
                    AsyncMock(return_value=True),
                )
            )
            stack.enter_context(
                patch.object(group, "_first_delivery", AsyncMock(return_value=True))
            )
            stack.enter_context(patch.object(group, "_update_presence", AsyncMock()))
            stack.enter_context(patch.object(group, "record_activity", AsyncMock()))
            stack.enter_context(
                patch.object(
                    group, "apply_moderation_filters", AsyncMock(return_value=False)
                )
            )
            stack.enter_context(
                patch.object(group, "_is_channel_post", return_value=is_channel)
            )
            stack.enter_context(
                patch.object(group, "_reply_gate_requires_mention", return_value=False)
            )
            stack.enter_context(
                patch.object(group, "_extract_entities", return_value=[])
            )
            stack.enter_context(
                patch.object(
                    group, "split_context_text", return_value=(text_value, text_value)
                )
            )
            stack.enter_context(
                patch.object(group, "_is_mention", return_value=mentioned)
            )
            stack.enter_context(
                patch.object(group, "_mentions_other_user", return_value=mentions_other)
            )
            stack.enter_context(
                patch.object(
                    group, "_is_bot_command_to_us", return_value=is_battle_cmd_to_us
                )
            )
            stack.enter_context(
                patch.object(
                    group, "_maybe_handle_battle", AsyncMock(return_value=False)
                )
            )
            stack.enter_context(
                patch.object(group, "_ensure_daily_limit", AsyncMock(return_value=True))
            )
            stack.enter_context(patch.object(group, "_user_id_val", return_value=42))
            stack.enter_context(
                patch.object(group, "_replied_to_our_bot", return_value=False)
            )
            stack.enter_context(
                patch.object(group, "_channel_obj", return_value=channel_chat)
            )
            stack.enter_context(
                patch.object(
                    group,
                    "_resolve_group_comment_context",
                    AsyncMock(return_value=is_comment_context),
                )
            )
            stack.enter_context(patch.object(group, "_store_context", AsyncMock()))
            stack.enter_context(
                patch.object(group, "_push_group_stm_and_recent", AsyncMock())
            )
            stack.enter_context(patch.object(group, "_analytics_best_effort"))
            stack.enter_context(patch.object(group, "_dispatch_passive_moderation"))
            stack.enter_context(patch.object(group.redis_client, "sadd", AsyncMock()))
            if is_channel:
                stack.enter_context(
                    patch.object(
                        group, "_maybe_log_channel_post", AsyncMock(return_value=True)
                    )
                )
            buffer_mock = stack.enter_context(
                patch.object(group, "buffer_message_for_response")
            )
            await group.on_group_message(message)

        if not buffer_mock.call_args:
            return None
        return buffer_mock.call_args.args[0]["trigger"]

    async def _trigger_from_voice(
        self,
        *,
        is_channel: bool,
        mentioned: bool,
        mentions_other: bool,
        autoreply_on_topic: bool,
        is_comment_context: bool = False,
    ) -> str | None:
        channel_chat = (
            types.SimpleNamespace(
                id=-10011, type=group.ChatType.CHANNEL, title="channel"
            )
            if is_channel
            else None
        )
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123),
            message_id=12,
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            voice=types.SimpleNamespace(file_id="voice-file-id"),
            reply_to_message=None,
            entities=[],
            caption_entities=[],
            text=None,
            caption=None,
            sender_chat=channel_chat,
            forward_from_chat=None,
            is_automatic_forward=False,
        )

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    group,
                    "settings",
                    types.SimpleNamespace(GROUP_AUTOREPLY_ON_TOPIC=autoreply_on_topic),
                )
            )
            stack.enter_context(
                patch.object(
                    group,
                    "_is_message_allowed_for_group_handlers",
                    AsyncMock(return_value=True),
                )
            )
            stack.enter_context(
                patch.object(group, "_first_delivery", AsyncMock(return_value=True))
            )
            stack.enter_context(
                patch.object(
                    group, "apply_moderation_filters", AsyncMock(return_value=False)
                )
            )
            stack.enter_context(patch.object(group, "_update_presence", AsyncMock()))
            stack.enter_context(patch.object(group, "record_activity", AsyncMock()))
            stack.enter_context(
                patch.object(group, "_reply_gate_requires_mention", return_value=False)
            )
            stack.enter_context(
                patch.object(group, "_is_channel_post", return_value=is_channel)
            )
            stack.enter_context(
                patch.object(group, "_is_mention", return_value=mentioned)
            )
            stack.enter_context(
                patch.object(group, "_mentions_other_user", return_value=mentions_other)
            )
            stack.enter_context(
                patch.object(group, "_ensure_daily_limit", AsyncMock(return_value=True))
            )
            stack.enter_context(patch.object(group, "inc_msg_count", AsyncMock()))
            stack.enter_context(
                patch.object(group, "_channel_obj", return_value=channel_chat)
            )
            stack.enter_context(patch.object(group, "_user_id_val", return_value=42))
            stack.enter_context(
                patch.object(group, "_replied_to_our_bot", return_value=False)
            )
            stack.enter_context(
                patch.object(group, "_extract_entities", return_value=[])
            )
            stack.enter_context(
                patch.object(
                    group,
                    "_resolve_group_comment_context",
                    AsyncMock(return_value=is_comment_context),
                )
            )
            stack.enter_context(patch.object(group, "_analytics_best_effort"))
            stack.enter_context(patch.object(group.redis_client, "sadd", AsyncMock()))
            stack.enter_context(patch.object(group, "_dispatch_passive_moderation"))
            if is_channel:
                stack.enter_context(
                    patch.object(
                        group, "is_from_linked_channel", AsyncMock(return_value=True)
                    )
                )
            buffer_mock = stack.enter_context(
                patch.object(group, "buffer_message_for_response")
            )
            await group.on_group_voice(message)

        if not buffer_mock.call_args:
            return None
        return buffer_mock.call_args.args[0]["trigger"]

    async def _trigger_from_image(
        self,
        *,
        is_channel: bool,
        mentioned: bool,
        mentions_other: bool,
        has_content_signal: bool,
        autoreply_on_topic: bool,
        is_comment_context: bool = False,
    ) -> str | None:
        caption_value = "hello" if has_content_signal else ""
        channel_chat = (
            types.SimpleNamespace(
                id=-10011, type=group.ChatType.CHANNEL, title="channel"
            )
            if is_channel
            else None
        )
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123),
            message_id=13,
            media_group_id=None,
            caption=caption_value,
            entities=[],
            caption_entities=[],
            reply_to_message=None,
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            sender_chat=channel_chat,
            forward_from_chat=None,
            is_automatic_forward=False,
        )

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    group,
                    "settings",
                    types.SimpleNamespace(GROUP_AUTOREPLY_ON_TOPIC=autoreply_on_topic),
                )
            )
            stack.enter_context(
                patch.object(
                    group, "apply_moderation_filters", AsyncMock(return_value=False)
                )
            )
            stack.enter_context(
                patch.object(group, "_reply_gate_requires_mention", return_value=False)
            )
            stack.enter_context(
                patch.object(group, "_is_channel_post", return_value=is_channel)
            )
            stack.enter_context(
                patch.object(group, "_extract_entities", return_value=[])
            )
            stack.enter_context(
                patch.object(
                    group,
                    "split_context_text",
                    return_value=(caption_value, caption_value),
                )
            )
            stack.enter_context(
                patch.object(group, "_is_mention", return_value=mentioned)
            )
            stack.enter_context(
                patch.object(group, "_mentions_other_user", return_value=mentions_other)
            )
            stack.enter_context(
                patch.object(group, "_ensure_daily_limit", AsyncMock(return_value=True))
            )
            stack.enter_context(patch.object(group, "_user_id_val", return_value=42))
            stack.enter_context(
                patch.object(group, "_replied_to_our_bot", return_value=False)
            )
            stack.enter_context(
                patch.object(group, "_channel_obj", return_value=channel_chat)
            )
            stack.enter_context(
                patch.object(
                    group,
                    "_resolve_group_comment_context",
                    AsyncMock(return_value=is_comment_context),
                )
            )
            stack.enter_context(patch.object(group, "_analytics_best_effort"))
            stack.enter_context(patch.object(group.redis_client, "sadd", AsyncMock()))
            if is_channel:
                stack.enter_context(
                    patch.object(
                        group, "is_from_linked_channel", AsyncMock(return_value=True)
                    )
                )
            dispatch_mock = stack.enter_context(
                patch.object(group, "_dispatch_passive_moderation")
            )
            delay_mock = stack.enter_context(
                patch.object(group.preprocess_group_image, "delay")
            )
            await group._handle_group_image_message_common(
                message,
                file_id="photo-file-id",
                document_id=None,
                mime_type="image/jpeg",
                suffix=".jpg",
                content_type_for_analytics="photo",
            )

        if delay_mock.call_args:
            return delay_mock.call_args.args[0]["trigger"]
        if dispatch_mock.call_args:
            return dispatch_mock.call_args.args[1].get("trigger")
        return None


    async def test_check_on_topic_is_skipped_while_chatbusy(self) -> None:
        with patch.object(
            group,
            "_chat_has_active_generation",
            AsyncMock(return_value=True),
        ):
            trigger = await self._trigger_from_text(
                is_channel=False,
                mentioned=False,
                mentions_other=False,
                has_content_signal=True,
                is_battle_cmd_to_us=False,
                autoreply_on_topic=True,
            )

        self.assertIsNone(trigger)

    async def test_mention_still_enqueues_while_chatbusy(self) -> None:
        with patch.object(
            group,
            "_chat_has_active_generation",
            AsyncMock(return_value=True),
        ):
            trigger = await self._trigger_from_text(
                is_channel=False,
                mentioned=True,
                mentions_other=False,
                has_content_signal=True,
                is_battle_cmd_to_us=False,
                autoreply_on_topic=True,
            )

        self.assertEqual(trigger, "mention")

    async def test_check_on_topic_is_skipped_for_comment_context_while_chatbusy(self) -> None:
        with patch.object(
            group,
            "_chat_has_active_generation",
            AsyncMock(return_value=True),
        ):
            trigger = await self._trigger_from_text(
                is_channel=False,
                mentioned=False,
                mentions_other=False,
                has_content_signal=True,
                is_battle_cmd_to_us=False,
                autoreply_on_topic=True,
                is_comment_context=True,
            )

        self.assertIsNone(trigger)

    async def test_check_on_topic_chatbusy_still_dispatches_passive_moderation(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123),
            message_id=11,
            text="hello",
            caption=None,
            entities=[],
            caption_entities=[],
            reply_to_message=None,
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            sender_chat=None,
            forward_from_chat=None,
            is_automatic_forward=False,
        )

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    group,
                    "settings",
                    types.SimpleNamespace(GROUP_AUTOREPLY_ON_TOPIC=True),
                )
            )
            stack.enter_context(
                patch.object(group, "_is_message_allowed_for_group_handlers", AsyncMock(return_value=True))
            )
            stack.enter_context(patch.object(group, "_first_delivery", AsyncMock(return_value=True)))
            stack.enter_context(patch.object(group, "_update_presence", AsyncMock()))
            stack.enter_context(patch.object(group, "record_activity", AsyncMock()))
            stack.enter_context(patch.object(group, "apply_moderation_filters", AsyncMock(return_value=False)))
            stack.enter_context(patch.object(group, "_is_channel_post", return_value=False))
            stack.enter_context(patch.object(group, "_reply_gate_requires_mention", return_value=False))
            stack.enter_context(patch.object(group, "_extract_entities", return_value=[]))
            stack.enter_context(patch.object(group, "split_context_text", return_value=("hello", "hello")))
            stack.enter_context(patch.object(group, "_is_mention", return_value=False))
            stack.enter_context(patch.object(group, "_mentions_other_user", return_value=False))
            stack.enter_context(patch.object(group, "_is_bot_command_to_us", return_value=False))
            stack.enter_context(patch.object(group, "_maybe_handle_battle", AsyncMock(return_value=False)))
            stack.enter_context(patch.object(group, "_ensure_daily_limit", AsyncMock(return_value=True)))
            stack.enter_context(patch.object(group, "_user_id_val", return_value=42))
            stack.enter_context(patch.object(group, "_replied_to_our_bot", return_value=False))
            stack.enter_context(patch.object(group, "_channel_obj", return_value=None))
            stack.enter_context(patch.object(group, "_resolve_group_comment_context", AsyncMock(return_value=True)))
            stack.enter_context(patch.object(group, "_chat_has_active_generation", AsyncMock(return_value=True)))
            stack.enter_context(patch.object(group, "_store_context", AsyncMock()))
            stack.enter_context(patch.object(group, "_push_group_stm_and_recent", AsyncMock()))
            stack.enter_context(patch.object(group, "_analytics_best_effort"))
            dispatch_mock = stack.enter_context(patch.object(group, "_dispatch_passive_moderation"))
            stack.enter_context(patch.object(group.redis_client, "sadd", AsyncMock()))
            buffer_mock = stack.enter_context(patch.object(group, "buffer_message_for_response"))

            await group.on_group_message(message)

        dispatch_mock.assert_called_once()
        buffer_mock.assert_not_called()

    async def test_handlers_match_resolver_for_shared_inputs(self) -> None:
        cases = [
            dict(
                name="mentioned",
                is_channel=False,
                mentioned=True,
                mentions_other=False,
                has_content_signal=True,
                is_battle_cmd_to_us=False,
                autoreply_on_topic=True,
            ),
            dict(
                name="mentions_other",
                is_channel=False,
                mentioned=False,
                mentions_other=True,
                has_content_signal=True,
                is_battle_cmd_to_us=False,
                autoreply_on_topic=True,
            ),
            dict(
                name="mentioned_and_mentions_other",
                is_channel=False,
                mentioned=True,
                mentions_other=True,
                has_content_signal=True,
                is_battle_cmd_to_us=False,
                autoreply_on_topic=True,
            ),
            dict(
                name="on_topic_enabled_with_content",
                is_channel=False,
                mentioned=False,
                mentions_other=False,
                has_content_signal=True,
                is_battle_cmd_to_us=False,
                autoreply_on_topic=True,
            ),
            dict(
                name="on_topic_disabled",
                is_channel=False,
                mentioned=False,
                mentions_other=False,
                has_content_signal=True,
                is_battle_cmd_to_us=False,
                autoreply_on_topic=False,
            ),
            dict(
                name="channel_post",
                is_channel=True,
                mentioned=False,
                mentions_other=True,
                has_content_signal=False,
                is_battle_cmd_to_us=False,
                autoreply_on_topic=True,
            ),
        ]

        for case in cases:
            with self.subTest(case=case["name"]):
                expected_text = group._resolve_autoreply_trigger(
                    is_channel=case["is_channel"],
                    mentioned=case["mentioned"],
                    mentions_other=case["mentions_other"],
                    has_content_signal=case["has_content_signal"],
                    is_battle_cmd_to_us=case["is_battle_cmd_to_us"],
                    autoreply_on_topic=case["autoreply_on_topic"],
                )
                if case["is_channel"]:
                    expected_text = None
                expected_voice = group._resolve_autoreply_trigger(
                    is_channel=case["is_channel"],
                    mentioned=case["mentioned"],
                    mentions_other=case["mentions_other"],
                    has_content_signal=False,
                    is_battle_cmd_to_us=False,
                    autoreply_on_topic=case["autoreply_on_topic"],
                )
                expected_image = group._resolve_autoreply_trigger(
                    is_channel=case["is_channel"],
                    mentioned=case["mentioned"],
                    mentions_other=case["mentions_other"],
                    has_content_signal=False,
                    is_battle_cmd_to_us=False,
                    autoreply_on_topic=case["autoreply_on_topic"],
                )

                text_trigger = await self._trigger_from_text(
                    is_channel=case["is_channel"],
                    mentioned=case["mentioned"],
                    mentions_other=case["mentions_other"],
                    has_content_signal=case["has_content_signal"],
                    is_battle_cmd_to_us=case["is_battle_cmd_to_us"],
                    autoreply_on_topic=case["autoreply_on_topic"],
                )
                voice_trigger = await self._trigger_from_voice(
                    is_channel=case["is_channel"],
                    mentioned=case["mentioned"],
                    mentions_other=case["mentions_other"],
                    autoreply_on_topic=case["autoreply_on_topic"],
                )
                image_trigger = await self._trigger_from_image(
                    is_channel=case["is_channel"],
                    mentioned=case["mentioned"],
                    mentions_other=case["mentions_other"],
                    has_content_signal=case["has_content_signal"],
                    autoreply_on_topic=case["autoreply_on_topic"],
                )

                self.assertEqual(text_trigger, expected_text)
                self.assertEqual(voice_trigger, expected_voice)
                self.assertEqual(image_trigger, expected_image)

    async def test_album_without_trigger_skips_reject_and_enqueue(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123),
            message_id=17,
            media_group_id="album-1",
            caption="",
            entities=[],
            caption_entities=[],
            reply_to_message=None,
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            sender_chat=None,
            forward_from_chat=None,
            is_automatic_forward=False,
        )

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    group,
                    "settings",
                    types.SimpleNamespace(GROUP_AUTOREPLY_ON_TOPIC=True),
                )
            )
            stack.enter_context(
                patch.object(
                    group, "apply_moderation_filters", AsyncMock(return_value=False)
                )
            )
            stack.enter_context(
                patch.object(group, "_reply_gate_requires_mention", return_value=False)
            )
            stack.enter_context(
                patch.object(group, "_is_channel_post", return_value=False)
            )
            stack.enter_context(
                patch.object(group, "_extract_entities", return_value=[])
            )
            stack.enter_context(
                patch.object(group, "split_context_text", return_value=("", ""))
            )
            stack.enter_context(
                patch.object(group, "_is_mention", return_value=False)
            )
            stack.enter_context(
                patch.object(group, "_mentions_other_user", return_value=False)
            )
            reject_mock = stack.enter_context(patch.object(group, "reject_image_and_reply"))
            dispatch_mock = stack.enter_context(
                patch.object(group, "_dispatch_passive_moderation")
            )
            delay_mock = stack.enter_context(
                patch.object(group.preprocess_group_image, "delay")
            )

            await group._handle_group_image_message_common(
                message,
                file_id="photo-file-id",
                document_id=None,
                mime_type="image/jpeg",
                suffix=".jpg",
                content_type_for_analytics="photo",
            )

        reject_mock.assert_not_called()
        delay_mock.assert_not_called()
        dispatch_mock.assert_called_once()

    async def test_album_with_mention_rejects_and_skips_enqueue(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123),
            message_id=18,
            media_group_id="album-2",
            caption="",
            entities=[],
            caption_entities=[],
            reply_to_message=None,
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            sender_chat=None,
            forward_from_chat=None,
            is_automatic_forward=False,
        )

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    group,
                    "settings",
                    types.SimpleNamespace(GROUP_AUTOREPLY_ON_TOPIC=False),
                )
            )
            stack.enter_context(
                patch.object(
                    group, "apply_moderation_filters", AsyncMock(return_value=False)
                )
            )
            stack.enter_context(
                patch.object(group, "_reply_gate_requires_mention", return_value=False)
            )
            stack.enter_context(
                patch.object(group, "_is_channel_post", return_value=False)
            )
            stack.enter_context(
                patch.object(group, "_extract_entities", return_value=[])
            )
            stack.enter_context(
                patch.object(group, "split_context_text", return_value=("", ""))
            )
            stack.enter_context(
                patch.object(group, "_is_mention", return_value=True)
            )
            stack.enter_context(
                patch.object(group, "_mentions_other_user", return_value=False)
            )
            reject_mock = stack.enter_context(patch.object(group, "reject_image_and_reply"))
            delay_mock = stack.enter_context(
                patch.object(group.preprocess_group_image, "delay")
            )

            await group._handle_group_image_message_common(
                message,
                file_id="photo-file-id",
                document_id=None,
                mime_type="image/jpeg",
                suffix=".jpg",
                content_type_for_analytics="photo",
            )

        reject_mock.assert_called_once_with(
            123, "albums are not supported", reply_to=18
        )
        delay_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
