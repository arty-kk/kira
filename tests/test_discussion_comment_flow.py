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

from app.bot.handlers import group, moderation
from app.bot.handlers.moderation_context import resolve_message_moderation_context


class DiscussionCommentIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_comment_target_reply_to_linked_post_not_false_deleted(self) -> None:
        cfg = types.SimpleNamespace(
            ALLOWED_GROUP_IDS=[],
            COMMENT_MODERATION_ENABLED=True,
            COMMENT_TARGET_CHAT_IDS=[-100200],
            COMMENT_SOURCE_CHANNEL_IDS=[],
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
            MODERATION_COMMAND_WHITELIST=[],
            MODERATION_SPAM_BAN_FIRST_LINK_AFTER_JOIN=False,
            MODERATION_NEW_DELETE_LINKS_24H=False,
            MODERATION_LINKS_DELETE_ALL=False,
            MODERATION_DELETE_TELEGRAM_LINKS=False,
            MODERATION_ALLOWED_LINK_KEYWORDS=[],
            MODERATION_EXTERNAL_LINKS_DELETE=False,
            MODERATION_EXTERNAL_LINKS_ALLOW_DOMAINS=[],
            MODERATION_OBFUSCATED_LINKS_DELETE=False,
            MODERATION_NO_TEXT_URL_DELETE=False,
            MODERATION_PHONES_DELETE=False,
            MODERATION_EMAILS_DELETE=False,
            MODERATION_HASHTAGS_DELETE=False,
            MODERATION_CASHTAGS_DELETE=False,
            MODERATION_DELETE_SERVICE=False,
        )
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-100200, type=ChatType.SUPERGROUP, linked_chat_id=-100900),
            message_id=321,
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            sender_chat=None,
            forward_from=None,
            forward_from_chat=None,
            forward_sender_name=None,
            is_automatic_forward=False,
            external_reply=object(),
            text="comment reply",
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

        with patch.object(group, "settings", cfg):
            allowed = await group._is_message_allowed_for_group_handlers(message)
        self.assertTrue(allowed)

        with (
            patch.object(moderation, "settings", cfg),
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
        flag_mock.assert_not_awaited()
        delete_mock.assert_not_awaited()

    def test_sync_and_async_receive_same_comment_context(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-100200, type=ChatType.SUPERGROUP, linked_chat_id=None),
            message_id=654,
            sender_chat=None,
            forward_from_chat=None,
            is_automatic_forward=False,
        )

        sync_context = resolve_message_moderation_context(message, from_linked=True)

        with patch.object(group.passive_moderate, "delay") as delay_mock:
            group._dispatch_passive_moderation(
                message,
                payload={},
                text="discussion comment",
                ents=[],
                is_channel=False,
                user_id_val=42,
                is_comment_context=(sync_context == "comment"),
            )

        moderation_payload = delay_mock.call_args.args[0]
        self.assertEqual(sync_context, "comment")
        self.assertEqual(moderation_payload["is_comment_context"], sync_context == "comment")


if __name__ == "__main__":
    unittest.main()
