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

    async def test_comment_context_external_reply_respects_comment_policy(self) -> None:
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



    async def test_ai_flagged_user_message_is_deleted(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-1001, type=ChatType.SUPERGROUP, linked_chat_id=None),
            message_id=21,
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
            patch.object(moderation, "settings", self._base_settings(MODERATION_AUTO_DELETE_AI_FLAGGED_USERS=True, MODERATION_PROFILE_NSFW_ENFORCE=False)),
            patch.object(moderation, "is_from_linked_channel", AsyncMock(return_value=False)),
            patch.object(moderation, "_is_admin", AsyncMock(return_value=False)),
            patch.object(moderation, "_is_ai_flagged_user", AsyncMock(return_value=True)),
            patch.object(moderation, "_delete_message_safe", AsyncMock(return_value=True)) as delete_mock,
            patch.object(moderation, "_flag", AsyncMock()) as flag_mock,
        ):
            handled = await moderation.apply_moderation_filters(message.chat.id, message)

        self.assertTrue(handled)
        delete_mock.assert_awaited_once()
        self.assertIn("ai_flagged_user", flag_mock.await_args.kwargs["reason"])

    async def test_profile_nsfw_user_is_deleted_and_restricted(self) -> None:
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
            patch.object(moderation, "settings", self._base_settings(MODERATION_AUTO_DELETE_AI_FLAGGED_USERS=False, MODERATION_PROFILE_NSFW_ENFORCE=True)),
            patch.object(moderation, "is_from_linked_channel", AsyncMock(return_value=False)),
            patch.object(moderation, "_is_admin", AsyncMock(return_value=False)),
            patch.object(moderation, "_is_profile_nsfw", AsyncMock(return_value=True)),
            patch.object(moderation, "_delete_message_safe", AsyncMock(return_value=True)) as delete_mock,
            patch.object(moderation, "_restrict_user_write_safe", AsyncMock(return_value=True)) as restrict_mock,
            patch.object(moderation, "_ban_user_safe", AsyncMock(return_value=False)) as ban_mock,
            patch.object(moderation, "_flag", AsyncMock()) as flag_mock,
        ):
            handled = await moderation.apply_moderation_filters(message.chat.id, message)

        self.assertTrue(handled)
        delete_mock.assert_awaited_once()
        restrict_mock.assert_awaited_once()
        ban_mock.assert_not_awaited()
        self.assertIn("profile_nsfw", flag_mock.await_args.kwargs["reason"])

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
            patch.object(passive_moderation, "extract_external_mentions", AsyncMock(return_value=["external_channel"])) as ext_mentions_mock,
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
            patch.object(passive_moderation, "extract_external_mentions", AsyncMock(return_value=["external_channel"])),
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
