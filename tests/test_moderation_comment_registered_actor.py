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


class ModerationCommentRegisteredActorTests(unittest.IsolatedAsyncioTestCase):
    def _settings(self, **overrides):
        data = dict(
            MODERATION_ADMIN_EXEMPT=False,
            MODERATION_DELETE_EXTERNAL_CHANNEL_MSGS=False,
            MODERATION_DELETE_BOT_CHANNEL_CHAT_FORWARDS=False,
            MODERATION_EXTERNAL_REPLIES_DELETE=False,
            COMMENT_MODERATION_DELETE_EXTERNAL_REPLIES=False,
            COMMENT_MODERATION_LINK_POLICY="group_default",
            COMMENT_MODERATION_REQUIRE_REGISTERED_ACTOR=True,
            COMMENT_MODERATION_REGISTERED_IDS=[],
            COMMENT_MODERATION_REGISTERED_USERNAMES=[],
            COMMENT_SOURCE_CHANNEL_IDS=[],
            MODERATION_DELETE_BUTTON_MESSAGES=False,
            MODERATION_ALLOW_STICKERS=True,
            MODERATION_ALLOW_MENTIONS=True,
            MODERATION_DELETE_NON_MEMBER_MENTIONS=False,
            MODERATION_ALLOW_GAMES=True,
            MODERATION_ALLOW_DICE=True,
            MODERATION_ALLOW_CUSTOM_EMOJI=True,
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
            MODERATION_COMMANDS_DELETE_ALL=False,
            MODERATION_SPAM_BAN_FIRST_LINK_AFTER_JOIN=False,
            MODERATION_NEW_DELETE_LINKS_24H=False,
            MODERATION_LINKS_DELETE_ALL=False,
            MODERATION_DELETE_TELEGRAM_LINKS=False,
            MODERATION_ALLOWED_LINK_KEYWORDS=[],
        )
        data.update(overrides)
        return types.SimpleNamespace(**data)

    def _message(self, *, user_id=42, username="alice", sender_chat=None):
        return types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-1001, type=ChatType.SUPERGROUP, linked_chat_id=None),
            message_id=500,
            from_user=types.SimpleNamespace(id=user_id, username=username, is_bot=False),
            sender_chat=sender_chat,
            forward_from=None,
            forward_from_chat=None,
            forward_sender_name=None,
            is_automatic_forward=False,
            external_reply=None,
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

    async def test_registered_id_or_username_passes(self):
        message = self._message(user_id=42, username="RegUser", sender_chat=types.SimpleNamespace(id=-100900, type=ChatType.CHANNEL))
        with (
            patch.object(moderation, "settings", self._settings(COMMENT_MODERATION_REGISTERED_IDS=[42])),
            patch.object(moderation, "_trusted_scope_ids", return_value=(set(), set(), set())),
            patch.object(moderation, "_is_fully_trusted_actor_or_action", AsyncMock(return_value=False)),
            patch.object(moderation, "is_from_linked_channel", AsyncMock(return_value=False)),
            patch.object(moderation, "_is_admin", AsyncMock(return_value=False)),
            patch.object(moderation, "extract_urls", return_value=[]),
            patch.object(moderation, "contains_any_link_obfuscated", return_value=False),
            patch.object(moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(moderation, "_flag", AsyncMock()) as flag_mock,
            patch.object(moderation, "_delete_message_safe", AsyncMock(return_value=True)) as delete_mock,
        ):
            handled = await moderation.apply_moderation_filters(message.chat.id, message)

        self.assertFalse(handled)
        flag_mock.assert_not_awaited()
        delete_mock.assert_not_awaited()

    async def test_registered_username_passes(self):
        message = self._message(user_id=404, username="RegisteredName", sender_chat=types.SimpleNamespace(id=-100900, type=ChatType.CHANNEL))
        with (
            patch.object(moderation, "settings", self._settings(COMMENT_MODERATION_REGISTERED_USERNAMES=["registeredname"])),
            patch.object(moderation, "_trusted_scope_ids", return_value=(set(), set(), set())),
            patch.object(moderation, "_is_fully_trusted_actor_or_action", AsyncMock(return_value=False)),
            patch.object(moderation, "is_from_linked_channel", AsyncMock(return_value=False)),
            patch.object(moderation, "_is_admin", AsyncMock(return_value=False)),
            patch.object(moderation, "extract_urls", return_value=[]),
            patch.object(moderation, "contains_any_link_obfuscated", return_value=False),
            patch.object(moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(moderation, "_flag", AsyncMock()) as flag_mock,
            patch.object(moderation, "_delete_message_safe", AsyncMock(return_value=True)) as delete_mock,
        ):
            handled = await moderation.apply_moderation_filters(message.chat.id, message)

        self.assertFalse(handled)
        flag_mock.assert_not_awaited()
        delete_mock.assert_not_awaited()

    async def test_unregistered_actor_deleted(self):
        message = self._message(user_id=99, username="ghost", sender_chat=types.SimpleNamespace(id=-100901, type=ChatType.CHANNEL))
        with (
            patch.object(moderation, "settings", self._settings()),
            patch.object(moderation, "_trusted_scope_ids", return_value=(set(), set(), set())),
            patch.object(moderation, "_is_fully_trusted_actor_or_action", AsyncMock(return_value=False)),
            patch.object(moderation, "is_from_linked_channel", AsyncMock(return_value=False)),
            patch.object(moderation, "_is_admin", AsyncMock(return_value=False)),
            patch.object(moderation, "extract_urls", return_value=[]),
            patch.object(moderation, "contains_any_link_obfuscated", return_value=False),
            patch.object(moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(moderation, "_flag", AsyncMock()) as flag_mock,
            patch.object(moderation, "_delete_message_safe", AsyncMock(return_value=True)) as delete_mock,
        ):
            handled = await moderation.apply_moderation_filters(message.chat.id, message)

        self.assertTrue(handled)
        delete_mock.assert_awaited_once()
        self.assertIn("comment_unregistered_actor", flag_mock.await_args.kwargs["reason"])

    async def test_sender_chat_in_comment_sources_passes(self):
        sender_chat = types.SimpleNamespace(id=-100555, type=ChatType.CHANNEL)
        message = self._message(user_id=99, username="ghost", sender_chat=sender_chat)
        with (
            patch.object(moderation, "settings", self._settings(COMMENT_SOURCE_CHANNEL_IDS=[-100555])),
            patch.object(moderation, "_trusted_scope_ids", return_value=(set(), set(), set())),
            patch.object(moderation, "_is_fully_trusted_actor_or_action", AsyncMock(return_value=False)),
            patch.object(moderation, "is_from_linked_channel", AsyncMock(return_value=False)),
            patch.object(moderation, "_is_admin", AsyncMock(return_value=False)),
            patch.object(moderation, "extract_urls", return_value=[]),
            patch.object(moderation, "contains_any_link_obfuscated", return_value=False),
            patch.object(moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(moderation, "_flag", AsyncMock()) as flag_mock,
            patch.object(moderation, "_delete_message_safe", AsyncMock(return_value=True)) as delete_mock,
        ):
            handled = await moderation.apply_moderation_filters(message.chat.id, message)

        self.assertFalse(handled)
        flag_mock.assert_not_awaited()
        delete_mock.assert_not_awaited()

    async def test_admin_exempt_bypass_kept_when_actor_not_registered(self):
        message = self._message(user_id=99, username="ghost", sender_chat=types.SimpleNamespace(id=-100901, type=ChatType.CHANNEL))
        with (
            patch.object(moderation, "settings", self._settings(MODERATION_ADMIN_EXEMPT=True)),
            patch.object(moderation, "_trusted_scope_ids", return_value=(set(), set(), set())),
            patch.object(moderation, "_is_fully_trusted_actor_or_action", AsyncMock(return_value=False)),
            patch.object(moderation, "is_from_linked_channel", AsyncMock(return_value=False)),
            patch.object(moderation, "_is_admin", AsyncMock(return_value=True)),
            patch.object(moderation, "_flag", AsyncMock()) as flag_mock,
            patch.object(moderation, "_delete_message_safe", AsyncMock(return_value=True)) as delete_mock,
        ):
            handled = await moderation.apply_moderation_filters(message.chat.id, message)

        self.assertFalse(handled)
        flag_mock.assert_not_awaited()
        delete_mock.assert_not_awaited()

    async def test_trusted_or_admin_bypass_kept(self):
        message = self._message(user_id=99, username="ghost", sender_chat=types.SimpleNamespace(id=-100901, type=ChatType.CHANNEL))
        with (
            patch.object(moderation, "settings", self._settings()),
            patch.object(moderation, "_trusted_scope_ids", return_value=(set(), set(), set())),
            patch.object(moderation, "_is_fully_trusted_actor_or_action", AsyncMock(return_value=True)),
            patch.object(moderation, "is_from_linked_channel", AsyncMock(return_value=False)),
            patch.object(moderation, "_is_admin", AsyncMock(return_value=True)),
            patch.object(moderation, "_flag", AsyncMock()) as flag_mock,
            patch.object(moderation, "_delete_message_safe", AsyncMock(return_value=True)) as delete_mock,
        ):
            handled = await moderation.apply_moderation_filters(message.chat.id, message)

        self.assertFalse(handled)
        flag_mock.assert_not_awaited()
        delete_mock.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
