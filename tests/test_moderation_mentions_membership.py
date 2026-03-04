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


class ModerationMentionsMembershipTests(unittest.IsolatedAsyncioTestCase):
    def _settings(self):
        return types.SimpleNamespace(
            MODERATION_ADMIN_EXEMPT=False,
            MODERATION_DELETE_EXTERNAL_CHANNEL_MSGS=True,
            MODERATION_DELETE_BOT_CHANNEL_CHAT_FORWARDS=True,
            MODERATION_EXTERNAL_REPLIES_DELETE=True,
            COMMENT_MODERATION_DELETE_EXTERNAL_REPLIES=False,
            COMMENT_MODERATION_LINK_POLICY="group_default",
            MODERATION_DELETE_BUTTON_MESSAGES=False,
            MODERATION_ALLOW_STICKERS=True,
            MODERATION_ALLOW_MENTIONS=True,
            MODERATION_DELETE_NON_MEMBER_MENTIONS=True,
            MODERATION_ALLOW_GAMES=True,
            MODERATION_ALLOW_DICE=True,
            MODERATION_ALLOW_CUSTOM_EMOJI=True,
            MODERATION_CUSTOM_EMOJI_SPAM_THRESHOLD=12,
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

    def _message(self, text: str, entities: list):
        return types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-1001, type=ChatType.SUPERGROUP, linked_chat_id=None),
            message_id=77,
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            sender_chat=None,
            forward_from=None,
            forward_from_chat=None,
            forward_sender_name=None,
            is_automatic_forward=False,
            external_reply=None,
            text=text,
            caption=None,
            entities=entities,
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

    async def test_member_mention_not_deleted(self):
        message = self._message("hi @member", [types.SimpleNamespace(type="mention", offset=3, length=7)])
        redis_stub = types.SimpleNamespace(hget=AsyncMock(return_value="100500"))
        bot_stub = types.SimpleNamespace(
            get_chat=AsyncMock(),
            get_chat_member=AsyncMock(return_value=types.SimpleNamespace(status="member")),
        )

        with (
            patch.object(moderation, "settings", self._settings()),
            patch.object(moderation, "redis_client", redis_stub),
            patch.object(moderation, "bot", bot_stub),
            patch.object(moderation, "_trusted_scope_ids", return_value=(set(), set(), set())),
            patch.object(moderation, "_is_fully_trusted_actor_or_action", AsyncMock(return_value=False)),
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
        bot_stub.get_chat.assert_not_awaited()
        bot_stub.get_chat_member.assert_awaited_once_with(message.chat.id, 100500)

    async def test_non_member_mention_deleted(self):
        message = self._message("hi @outsider", [types.SimpleNamespace(type="mention", offset=3, length=9)])
        redis_stub = types.SimpleNamespace(hget=AsyncMock(return_value=None))
        bot_stub = types.SimpleNamespace(
            get_chat=AsyncMock(return_value=types.SimpleNamespace(id=222, type=ChatType.PRIVATE, is_bot=False)),
            get_chat_member=AsyncMock(return_value=types.SimpleNamespace(status="left")),
        )

        with (
            patch.object(moderation, "settings", self._settings()),
            patch.object(moderation, "redis_client", redis_stub),
            patch.object(moderation, "bot", bot_stub),
            patch.object(moderation, "_trusted_scope_ids", return_value=(set(), set(), set())),
            patch.object(moderation, "_is_fully_trusted_actor_or_action", AsyncMock(return_value=False)),
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
        self.assertIn("mention_non_member", flag_mock.await_args.kwargs["reason"])

    async def test_non_member_bot_or_channel_mention_deleted(self):
        message = self._message("hi @externalbot", [types.SimpleNamespace(type="mention", offset=3, length=12)])
        redis_stub = types.SimpleNamespace(hget=AsyncMock(return_value=None))
        bot_stub = types.SimpleNamespace(
            get_chat=AsyncMock(return_value=types.SimpleNamespace(id=333, type=ChatType.CHANNEL, is_bot=False)),
            get_chat_member=AsyncMock(side_effect=RuntimeError("user not found")),
        )

        with (
            patch.object(moderation, "settings", self._settings()),
            patch.object(moderation, "redis_client", redis_stub),
            patch.object(moderation, "bot", bot_stub),
            patch.object(moderation, "_trusted_scope_ids", return_value=(set(), set(), set())),
            patch.object(moderation, "_is_fully_trusted_actor_or_action", AsyncMock(return_value=False)),
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
        self.assertIn("mention_non_member", flag_mock.await_args.kwargs["reason"])

    async def test_custom_emoji_spam_deleted_even_when_custom_emoji_allowed(self):
        entities = [types.SimpleNamespace(type="custom_emoji", offset=i, length=1) for i in range(12)]
        message = self._message("x" * 12, entities)
        redis_stub = types.SimpleNamespace(hget=AsyncMock(return_value=None))
        bot_stub = types.SimpleNamespace(get_chat=AsyncMock(), get_chat_member=AsyncMock())

        with (
            patch.object(moderation, "settings", self._settings()),
            patch.object(moderation, "redis_client", redis_stub),
            patch.object(moderation, "bot", bot_stub),
            patch.object(moderation, "_trusted_scope_ids", return_value=(set(), set(), set())),
            patch.object(moderation, "_is_fully_trusted_actor_or_action", AsyncMock(return_value=False)),
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
        self.assertIn("emoji_overlimit", flag_mock.await_args.kwargs["reason"])


if __name__ == "__main__":
    unittest.main()
