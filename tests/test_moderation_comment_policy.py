import os
import types
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "12345:abcde")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("REDIS_URL_QUEUE", "redis://localhost:6379/1")
os.environ.setdefault("REDIS_URL_VECTOR", "redis://localhost:6379/2")

from aiogram.enums import ChatType

from app.bot.handlers import moderation
from app.services.addons import passive_moderation


class ModerationCommentPolicyTests(unittest.IsolatedAsyncioTestCase):
    def _base_settings(self, **overrides):
        data = dict(
            MODERATION_ADMIN_EXEMPT=False,
            MODERATION_DELETE_EXTERNAL_CHANNEL_MSGS=True,
            MODERATION_DELETE_BOT_CHANNEL_CHAT_FORWARDS=True,
            MODERATION_EXTERNAL_REPLIES_DELETE=True,
            COMMENT_MODERATION_DELETE_EXTERNAL_REPLIES=False,
            COMMENT_MODERATION_LINK_POLICY="group_default",
            MODERATION_DELETE_BUTTON_MESSAGES=False,
            MODERATION_ALLOW_STICKERS=True,
            MODERATION_ALLOW_GAMES=True,
            MODERATION_ALLOW_DICE=True,
            MODERATION_INLINE_BOT_MSGS_DELETE=False,
            MODERATION_STORIES_DELETE=False,
            MODERATION_VOICE_DELETE=False,
            MODERATION_VIDEO_NOTE_DELETE=False,
            MODERATION_AUDIO_DELETE=False,
            MODERATION_IMAGES_DELETE=False,
            MODERATION_VIDEOS_DELETE=False,
            MODERATION_GIFS_DELETE=False,
            MODERATION_FILES_DELETE_ALL=False,
            MODERATION_NEW_DELETE_FORWARDS_24H=False,
            MODERATION_ALLOW_MENTIONS=True,
            MODERATION_ALLOW_CUSTOM_EMOJI=True,
            MODERATION_COMMANDS_DELETE_ALL=False,
            MODERATION_SPAM_BAN_FIRST_LINK_AFTER_JOIN=False,
            MODERATION_NEW_DELETE_LINKS_24H=False,
            MODERATION_LINKS_DELETE_ALL=False,
            MODERATION_DELETE_TELEGRAM_LINKS=False,
            MODERATION_ALLOWED_LINK_KEYWORDS=[],
        )
        data.update(overrides)
        return types.SimpleNamespace(**data)

    async def test_linked_discussion_message_without_origin_signals_uses_comment_policy(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-1001, type=ChatType.SUPERGROUP, linked_chat_id=-10055),
            message_id=10,
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            sender_chat=None,
            forward_from=None,
            forward_from_chat=None,
            forward_sender_name=None,
            is_automatic_forward=False,
            external_reply=object(),
            text="hi",
            caption=None,
            entities=[],
            caption_entities=[],
            reply_markup=None,
            sticker=None,
            game=None,
            dice=None,
            via_bot=None,
            story=None,
            voice=None,
            video_note=None,
            audio=None,
            photo=None,
            video=None,
            animation=None,
            document=None,
        )

        with (
            patch.object(moderation, "settings", self._base_settings()),
            patch.object(moderation, "is_from_linked_channel", AsyncMock(return_value=False)),
            patch.object(moderation, "_is_admin", AsyncMock(return_value=False)),
            patch.object(moderation, "_is_new_user", AsyncMock(return_value=False)),
            patch.object(moderation, "extract_urls", return_value=[]),
            patch.object(moderation, "contains_any_link_obfuscated", return_value=False),
            patch.object(moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(moderation, "_flag", AsyncMock()) as flag_mock,
            patch.object(moderation, "_delete_message_safe", AsyncMock(return_value=True)) as delete_mock,
        ):
            handled = await moderation.apply_moderation_filters(message.chat.id, message)

        self.assertFalse(handled)
        delete_mock.assert_not_awaited()
        flag_mock.assert_not_awaited()

    async def test_linked_discussion_message_with_origin_signal_uses_comment_policy(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-1001, type=ChatType.SUPERGROUP, linked_chat_id=-10055),
            message_id=13,
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            sender_chat=None,
            forward_from=None,
            forward_from_chat=None,
            forward_sender_name=None,
            is_automatic_forward=False,
            external_reply=object(),
            text="hi",
            caption=None,
            entities=[],
            caption_entities=[],
            reply_markup=None,
            sticker=None,
            game=None,
            dice=None,
            via_bot=None,
            story=None,
            voice=None,
            video_note=None,
            audio=None,
            photo=None,
            video=None,
            animation=None,
            document=None,
        )

        with (
            patch.object(moderation, "settings", self._base_settings()),
            patch.object(moderation, "is_from_linked_channel", AsyncMock(return_value=True)),
            patch.object(moderation, "_is_admin", AsyncMock(return_value=False)),
            patch.object(moderation, "_is_new_user", AsyncMock(return_value=False)),
            patch.object(moderation, "extract_urls", return_value=[]),
            patch.object(moderation, "contains_any_link_obfuscated", return_value=False),
            patch.object(moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(moderation, "_flag", AsyncMock()) as flag_mock,
            patch.object(moderation, "_delete_message_safe", AsyncMock(return_value=True)) as delete_mock,
        ):
            handled = await moderation.apply_moderation_filters(message.chat.id, message)

        self.assertFalse(handled)
        delete_mock.assert_not_awaited()
        flag_mock.assert_not_awaited()

    async def test_group_context_external_reply_keeps_default_behavior(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-1001, type=ChatType.SUPERGROUP, linked_chat_id=None),
            message_id=11,
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            sender_chat=None,
            forward_from=None,
            forward_from_chat=None,
            forward_sender_name=None,
            is_automatic_forward=False,
            external_reply=object(),
            text="hi",
            caption=None,
            entities=[],
            caption_entities=[],
            reply_markup=None,
            sticker=None,
            game=None,
            dice=None,
            via_bot=None,
            story=None,
            voice=None,
            video_note=None,
            audio=None,
            photo=None,
            video=None,
            animation=None,
            document=None,
        )

        with (
            patch.object(moderation, "settings", self._base_settings()),
            patch.object(moderation, "is_from_linked_channel", AsyncMock(return_value=False)),
            patch.object(moderation, "_is_admin", AsyncMock(return_value=False)),
            patch.object(moderation, "_is_new_user", AsyncMock(return_value=False)),
            patch.object(moderation, "extract_urls", return_value=[]),
            patch.object(moderation, "contains_any_link_obfuscated", return_value=False),
            patch.object(moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(moderation, "_flag", AsyncMock()) as flag_mock,
            patch.object(moderation, "_delete_message_safe", AsyncMock(return_value=True)) as delete_mock,
        ):
            handled = await moderation.apply_moderation_filters(message.chat.id, message)

        self.assertTrue(handled)
        delete_mock.assert_awaited_once()
        reason = flag_mock.await_args.kwargs["reason"]
        self.assertIn("external_reply", reason)
        self.assertIn("context=group", reason)

    async def test_group_context_external_reply_returns_handled_when_delete_fails(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-1001, type=ChatType.SUPERGROUP, linked_chat_id=None),
            message_id=12,
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            sender_chat=None,
            forward_from=None,
            forward_from_chat=None,
            forward_sender_name=None,
            is_automatic_forward=False,
            external_reply=object(),
            text="hi",
            caption=None,
            entities=[],
            caption_entities=[],
            reply_markup=None,
            sticker=None,
            game=None,
            dice=None,
            via_bot=None,
            story=None,
            voice=None,
            video_note=None,
            audio=None,
            photo=None,
            video=None,
            animation=None,
            document=None,
        )

        with (
            patch.object(moderation, "settings", self._base_settings()),
            patch.object(moderation, "is_from_linked_channel", AsyncMock(return_value=False)),
            patch.object(moderation, "_is_admin", AsyncMock(return_value=False)),
            patch.object(moderation, "_is_new_user", AsyncMock(return_value=False)),
            patch.object(moderation, "extract_urls", return_value=[]),
            patch.object(moderation, "contains_any_link_obfuscated", return_value=False),
            patch.object(moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(moderation, "_flag", AsyncMock()) as flag_mock,
            patch.object(moderation, "_delete_message_safe", AsyncMock(return_value=False)) as delete_mock,
            self.assertLogs("app.bot.handlers.moderation", level="WARNING") as logs,
        ):
            handled = await moderation.apply_moderation_filters(message.chat.id, message)

        self.assertTrue(handled)
        delete_mock.assert_awaited_once()
        flag_mock.assert_awaited_once()
        self.assertTrue(any("failed to delete (external_reply)" in record for record in logs.output))



    async def test_profile_nsfw_is_not_enforced_in_apply_filters(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-1001, type=ChatType.SUPERGROUP, linked_chat_id=None),
            message_id=22,
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            sender_chat=None,
            forward_from=None,
            forward_from_chat=None,
            forward_sender_name=None,
            is_automatic_forward=False,
            external_reply=None,
            text="hello",
            caption=None,
            entities=[],
            caption_entities=[],
            reply_markup=None,
            sticker=None,
            game=None,
            dice=None,
            via_bot=None,
            story=None,
            voice=None,
            video_note=None,
            audio=None,
            photo=None,
            video=None,
            animation=None,
            document=None,
        )

        with (
            patch.object(moderation, "settings", self._base_settings(MODERATION_PROFILE_NSFW_ENFORCE=True)),
            patch.object(moderation, "is_from_linked_channel", AsyncMock(return_value=False)),
            patch.object(moderation, "_is_admin", AsyncMock(return_value=False)),
            patch.object(moderation.redis_client, "exists", AsyncMock(return_value=True)),
            patch.object(moderation, "_is_profile_nsfw", AsyncMock(return_value=True)) as nsfw_mock,
        ):
            handled = await moderation.apply_moderation_filters(message.chat.id, message)

        self.assertFalse(handled)
        nsfw_mock.assert_not_awaited()



    async def test_auto_forward_bypass_and_regular_forward_respects_toggle(self) -> None:
        def make_message(message_id: int, *, is_automatic_forward: bool, forward_from_chat: object | None):
            return types.SimpleNamespace(
                chat=types.SimpleNamespace(id=-1001, type=ChatType.SUPERGROUP, linked_chat_id=None),
                message_id=message_id,
                from_user=types.SimpleNamespace(id=42, is_bot=False),
                sender_chat=None,
                forward_from=None,
                forward_from_chat=forward_from_chat,
                forward_sender_name=None,
                is_automatic_forward=is_automatic_forward,
                external_reply=None,
                text="forwarded",
                caption=None,
                entities=[],
                caption_entities=[],
                reply_markup=None,
                sticker=None,
                game=None,
                dice=None,
                via_bot=None,
                story=None,
                voice=None,
                video_note=None,
                audio=None,
                photo=None,
                video=None,
                animation=None,
                document=None,
            )

        auto_forward_message = make_message(
            30,
            is_automatic_forward=True,
            forward_from_chat=types.SimpleNamespace(id=-2001, type=ChatType.CHANNEL),
        )
        regular_forward_message = make_message(
            31,
            is_automatic_forward=False,
            forward_from_chat=types.SimpleNamespace(id=-2002, type=ChatType.CHANNEL),
        )

        with (
            patch.object(moderation, "settings", self._base_settings(MODERATION_DELETE_BOT_CHANNEL_CHAT_FORWARDS=True, MODERATION_DELETE_EXTERNAL_CHANNEL_MSGS=False)),
            patch.object(moderation, "is_from_linked_channel", AsyncMock(return_value=False)),
            patch.object(moderation, "_is_admin", AsyncMock(return_value=False)),
            patch.object(moderation, "_is_new_user", AsyncMock(return_value=False)),
            patch.object(moderation, "extract_urls", return_value=[]),
            patch.object(moderation, "contains_any_link_obfuscated", return_value=False),
            patch.object(moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(moderation, "_flag", AsyncMock()) as flag_mock,
            patch.object(moderation, "_delete_message_safe", AsyncMock(return_value=True)) as delete_mock,
        ):
            auto_handled = await moderation.apply_moderation_filters(auto_forward_message.chat.id, auto_forward_message)
            regular_handled = await moderation.apply_moderation_filters(regular_forward_message.chat.id, regular_forward_message)

        self.assertFalse(auto_handled)
        self.assertTrue(regular_handled)
        self.assertEqual(delete_mock.await_count, 1)
        self.assertEqual(flag_mock.await_count, 1)
        self.assertIn("forward_disallowed", flag_mock.await_args.kwargs["reason"])

        with (
            patch.object(moderation, "settings", self._base_settings(MODERATION_DELETE_BOT_CHANNEL_CHAT_FORWARDS=False, MODERATION_DELETE_EXTERNAL_CHANNEL_MSGS=False)),
            patch.object(moderation, "is_from_linked_channel", AsyncMock(return_value=False)),
            patch.object(moderation, "_is_admin", AsyncMock(return_value=False)),
            patch.object(moderation, "_is_new_user", AsyncMock(return_value=False)),
            patch.object(moderation, "extract_urls", return_value=[]),
            patch.object(moderation, "contains_any_link_obfuscated", return_value=False),
            patch.object(moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(moderation, "_flag", AsyncMock()) as flag_mock_off,
            patch.object(moderation, "_delete_message_safe", AsyncMock(return_value=True)) as delete_mock_off,
        ):
            regular_handled_with_flag_off = await moderation.apply_moderation_filters(
                regular_forward_message.chat.id,
                regular_forward_message,
            )

        self.assertFalse(regular_handled_with_flag_off)
        delete_mock_off.assert_not_awaited()
        flag_mock_off.assert_not_awaited()

    async def test_comment_source_channel_admin_is_trusted_even_without_group_admin(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-1001, type=ChatType.SUPERGROUP, linked_chat_id=-10077),
            message_id=40,
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            sender_chat=None,
            forward_from=None,
            forward_from_chat=types.SimpleNamespace(id=-10099, type=ChatType.CHANNEL),
            forward_sender_name=None,
            is_automatic_forward=False,
            external_reply=None,
            text="channel post",
            caption=None,
            entities=[],
            caption_entities=[],
            reply_markup=None,
            sticker=None,
            game=None,
            dice=None,
            via_bot=None,
            story=None,
            voice=None,
            video_note=None,
            audio=None,
            photo=None,
            video=None,
            animation=None,
            document=None,
        )

        async def _is_admin_side_effect(chat_id: int, user_id: int) -> bool:
            return (chat_id, user_id) == (-10077, 42)

        with (
            patch.object(
                moderation,
                "settings",
                self._base_settings(ALLOWED_GROUP_IDS=[-1001], COMMENT_TARGET_CHAT_IDS=[], COMMENT_SOURCE_CHANNEL_IDS=[-10077]),
            ),
            patch.object(moderation, "is_from_linked_channel", AsyncMock(return_value=False)),
            patch.object(moderation, "_is_admin", AsyncMock(side_effect=_is_admin_side_effect)) as is_admin_mock,
            patch.object(moderation, "_flag", AsyncMock()) as flag_mock,
            patch.object(moderation, "_delete_message_safe", AsyncMock(return_value=True)) as delete_mock,
        ):
            handled = await moderation.apply_moderation_filters(message.chat.id, message)

        self.assertFalse(handled)
        delete_mock.assert_not_awaited()
        flag_mock.assert_not_awaited()
        self.assertEqual(
            [call.args for call in is_admin_mock.await_args_list],
            [(-1001, 42), (-10077, 42)],
        )

    def test_resolve_policy_relaxed_comment_disables_external_channel_checks(self) -> None:
        cfg = self._base_settings(COMMENT_MODERATION_LINK_POLICY="relaxed")
        policy = moderation.resolve_moderation_policy("comment", cfg)
        self.assertFalse(policy["delete_external_channel_msgs"])
        self.assertFalse(policy["delete_channel_forwards"])

    async def test_check_light_comment_relaxed_keeps_allowed_link_clean(self) -> None:
        with (
            patch.object(
                passive_moderation,
                "settings",
                types.SimpleNamespace(ENABLE_MODERATION=True, ENABLE_AI_MODERATION=False, MODERATION_SPAM_LINK_THRESHOLD=5),
            ),
            patch.object(passive_moderation, "is_flooding", AsyncMock(return_value=False)),
            patch.object(passive_moderation, "extract_urls", return_value=["https://example.com/page"]),
            patch.object(passive_moderation, "extract_external_mentions", AsyncMock(return_value=(["external_channel"], False))) as ext_mentions_mock,
            patch.object(passive_moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(passive_moderation, "url_is_unwanted", return_value=True),
        ):
            status = await passive_moderation.check_light(
                1,
                2,
                "check https://example.com/page",
                [{"type": "url", "offset": 6, "length": 24}],
                source="user",
                policy={"link_policy": "relaxed"},
            )

        self.assertEqual(status, "clean")
        ext_mentions_mock.assert_not_awaited()

    async def test_check_light_group_default_still_blocks_same_link(self) -> None:
        with (
            patch.object(
                passive_moderation,
                "settings",
                types.SimpleNamespace(ENABLE_MODERATION=True, ENABLE_AI_MODERATION=False, MODERATION_SPAM_LINK_THRESHOLD=5),
            ),
            patch.object(passive_moderation, "is_flooding", AsyncMock(return_value=False)),
            patch.object(passive_moderation, "extract_urls", return_value=["https://example.com/page"]),
            patch.object(passive_moderation, "extract_external_mentions", AsyncMock(return_value=(["external_channel"], False))),
            patch.object(passive_moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(passive_moderation, "url_is_unwanted", return_value=True),
        ):
            status = await passive_moderation.check_light(
                1,
                2,
                "check https://example.com/page",
                [{"type": "url", "offset": 6, "length": 24}],
                source="user",
                policy={"link_policy": "group_default"},
            )

        self.assertEqual(status, "link_violation")


class ModerationReactionProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_reaction_triggers_profile_moderation_for_untrusted_user_in_trusted_chat(self) -> None:
        event = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-1001, type=ChatType.SUPERGROUP),
            user=types.SimpleNamespace(id=42, is_bot=False),
            actor_chat=None,
            message_id=55,
        )

        with (
            patch.object(
                moderation,
                "settings",
                types.SimpleNamespace(
                    MODERATION_PROFILE_NSFW_ENFORCE=True,
                    ALLOWED_GROUP_IDS=[-1001],
                    COMMENT_TARGET_CHAT_IDS=[],
                    COMMENT_SOURCE_CHANNEL_IDS=[],
                ),
            ),
            patch.object(moderation, "_is_fully_trusted_actor_or_action", AsyncMock(return_value=False)),
            patch.object(moderation.redis_client, "exists", AsyncMock(return_value=False)),
            patch.object(moderation, "_is_profile_nsfw", AsyncMock(return_value=True)),
            patch.object(moderation.redis_client, "set", AsyncMock()) as redis_set_mock,
            patch.object(moderation, "_flag_inline_without_message", AsyncMock()) as flag_mock,
            patch.object(moderation, "_cleanup_user_history_and_mute", AsyncMock()) as cleanup_mock,
        ):
            await moderation.moderation_on_reaction(event)

        redis_set_mock.assert_awaited_once_with("mod:profile_nsfw_blocked:-1001:42", 1)
        flag_mock.assert_awaited_once()
        cleanup_mock.assert_awaited_once_with(-1001, 42)

    async def test_reaction_ignored_for_untrusted_chat(self) -> None:
        event = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-2002, type=ChatType.SUPERGROUP),
            user=types.SimpleNamespace(id=42, is_bot=False),
            actor_chat=None,
            message_id=56,
        )

        with (
            patch.object(
                moderation,
                "settings",
                types.SimpleNamespace(
                    MODERATION_PROFILE_NSFW_ENFORCE=True,
                    ALLOWED_GROUP_IDS=[-1001],
                    COMMENT_TARGET_CHAT_IDS=[],
                    COMMENT_SOURCE_CHANNEL_IDS=[],
                ),
            ),
            patch.object(moderation, "_is_profile_nsfw", AsyncMock(return_value=True)) as nsfw_mock,
            patch.object(moderation, "_cleanup_user_history_and_mute", AsyncMock()) as cleanup_mock,
        ):
            await moderation.moderation_on_reaction(event)

        nsfw_mock.assert_not_awaited()
        cleanup_mock.assert_not_awaited()

    async def test_reaction_comment_linked_chat_is_trusted_when_comment_moderation_enabled(self) -> None:
        event = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-2002, type=ChatType.SUPERGROUP, linked_chat_id=-10077),
            user=types.SimpleNamespace(id=42, is_bot=False),
            actor_chat=None,
            message_id=57,
        )

        with (
            patch.object(
                moderation,
                "settings",
                types.SimpleNamespace(
                    MODERATION_PROFILE_NSFW_ENFORCE=True,
                    COMMENT_MODERATION_ENABLED=True,
                    ALLOWED_GROUP_IDS=[],
                    COMMENT_TARGET_CHAT_IDS=[],
                    COMMENT_SOURCE_CHANNEL_IDS=[-10077],
                ),
            ),
            patch.object(moderation, "_is_fully_trusted_actor_or_action", AsyncMock(return_value=False)),
            patch.object(moderation.redis_client, "exists", AsyncMock(return_value=False)),
            patch.object(moderation, "_is_profile_nsfw", AsyncMock(return_value=True)),
            patch.object(moderation, "_cleanup_user_history_and_mute", AsyncMock()) as cleanup_mock,
        ):
            await moderation.moderation_on_reaction(event)

        cleanup_mock.assert_awaited_once_with(-2002, 42)

    async def test_reaction_comment_target_ignored_when_comment_moderation_disabled(self) -> None:
        event = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-3003, type=ChatType.SUPERGROUP, linked_chat_id=-10088),
            user=types.SimpleNamespace(id=42, is_bot=False),
            actor_chat=None,
            message_id=58,
        )

        with (
            patch.object(
                moderation,
                "settings",
                types.SimpleNamespace(
                    MODERATION_PROFILE_NSFW_ENFORCE=True,
                    COMMENT_MODERATION_ENABLED=False,
                    ALLOWED_GROUP_IDS=[],
                    COMMENT_TARGET_CHAT_IDS=[-3003],
                    COMMENT_SOURCE_CHANNEL_IDS=[-10088],
                ),
            ),
            patch.object(moderation, "_is_profile_nsfw", AsyncMock(return_value=True)) as nsfw_mock,
            patch.object(moderation, "_cleanup_user_history_and_mute", AsyncMock()) as cleanup_mock,
        ):
            await moderation.moderation_on_reaction(event)

        nsfw_mock.assert_not_awaited()
        cleanup_mock.assert_not_awaited()

    def test_url_is_unwanted_allows_whitelisted_non_telegram_domain(self) -> None:
        with patch.object(
            passive_moderation,
            "settings",
            types.SimpleNamespace(MODERATION_ALLOWED_LINK_KEYWORDS=["example.com"]),
        ):
            self.assertFalse(
                passive_moderation.url_is_unwanted(
                    "https://sub.example.com/path",
                    policy={"link_policy": "group_default"},
                )
            )

    def test_url_is_unwanted_keeps_telegram_block_in_group_default_even_if_whitelisted(self) -> None:
        with patch.object(
            passive_moderation,
            "settings",
            types.SimpleNamespace(MODERATION_ALLOWED_LINK_KEYWORDS=["t.me"]),
        ):
            self.assertTrue(
                passive_moderation.url_is_unwanted(
                    "https://t.me/test",
                    policy={"link_policy": "group_default"},
                )
            )


if __name__ == "__main__":
    unittest.main()
