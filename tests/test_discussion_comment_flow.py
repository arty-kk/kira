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

from app.bot.handlers import group, moderation
from app.bot.handlers import moderation_context
from app.bot.handlers.moderation_context import resolve_message_moderation_context


class DiscussionCommentIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_automatic_forward_is_never_deleted_by_moderation_guard(self) -> None:
        cfg = types.SimpleNamespace(
            MODERATION_ADMIN_EXEMPT=False,
        )
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-100200, type=ChatType.SUPERGROUP),
            message_id=999,
            is_automatic_forward=True,
            sender_chat=types.SimpleNamespace(id=-100900, type=ChatType.CHANNEL),
        )

        with (
            patch.object(moderation, "settings", cfg),
            patch.object(moderation, "_delete_message_safe", AsyncMock(return_value=True)) as delete_mock,
        ):
            handled = await moderation.apply_moderation_filters(message.chat.id, message)

        self.assertFalse(handled)
        delete_mock.assert_not_awaited()

    async def test_trusted_chat_admin_is_exempt_even_when_global_admin_exempt_disabled(self) -> None:
        cfg = types.SimpleNamespace(
            ALLOWED_GROUP_IDS=[-100200],
            COMMENT_TARGET_CHAT_IDS=[],
            MODERATION_ADMIN_EXEMPT=False,
        )
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-100200, type=ChatType.SUPERGROUP),
            message_id=1002,
            from_user=types.SimpleNamespace(id=777, is_bot=False),
            is_automatic_forward=False,
            sender_chat=None,
            forward_from=None,
            forward_from_chat=None,
            forward_sender_name=None,
            text=None,
            caption=None,
            entities=[],
            caption_entities=[],
            reply_markup=None,
            sticker=object(),
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
            external_reply=None,
        )

        with (
            patch.object(moderation, "settings", cfg),
            patch.object(moderation, "_is_admin", AsyncMock(return_value=True)),
            patch.object(moderation, "_delete_message_safe", AsyncMock(return_value=True)) as delete_mock,
        ):
            handled = await moderation.apply_moderation_filters(message.chat.id, message)

        self.assertFalse(handled)
        delete_mock.assert_not_awaited()

    async def test_trusted_channel_repost_between_trusted_scopes_is_not_deleted(self) -> None:
        cfg = types.SimpleNamespace(
            ALLOWED_GROUP_IDS=[-100200],
            COMMENT_TARGET_CHAT_IDS=[],
            COMMENT_SOURCE_CHANNEL_IDS=[-100900],
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
            chat=types.SimpleNamespace(id=-100200, type=ChatType.SUPERGROUP, linked_chat_id=None),
            message_id=322,
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            sender_chat=None,
            forward_from=None,
            forward_from_chat=types.SimpleNamespace(id=-100900, type=ChatType.CHANNEL),
            forward_sender_name=None,
            is_automatic_forward=False,
            external_reply=None,
            text="trusted repost",
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

    async def test_trusted_sender_chat_repost_from_untrusted_source_is_not_deleted(self) -> None:
        cfg = types.SimpleNamespace(
            ALLOWED_GROUP_IDS=[-100200],
            COMMENT_TARGET_CHAT_IDS=[],
            COMMENT_SOURCE_CHANNEL_IDS=[-100900],
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
            chat=types.SimpleNamespace(id=-100200, type=ChatType.SUPERGROUP, linked_chat_id=None),
            message_id=333,
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            sender_chat=types.SimpleNamespace(id=-100900, type=ChatType.CHANNEL),
            forward_from=None,
            forward_from_chat=types.SimpleNamespace(id=-100990, type=ChatType.CHANNEL),
            forward_sender_name=None,
            is_automatic_forward=False,
            external_reply=None,
            text="trusted sender repost",
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

    async def test_trusted_chat_repost_between_trusted_chats_is_not_deleted(self) -> None:
        cfg = types.SimpleNamespace(
            ALLOWED_GROUP_IDS=[-100200, -100201],
            COMMENT_TARGET_CHAT_IDS=[],
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
            chat=types.SimpleNamespace(id=-100200, type=ChatType.SUPERGROUP, linked_chat_id=None),
            message_id=323,
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            sender_chat=None,
            forward_from=None,
            forward_from_chat=types.SimpleNamespace(id=-100201, type=ChatType.SUPERGROUP),
            forward_sender_name=None,
            is_automatic_forward=False,
            external_reply=None,
            text="trusted chat repost",
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

    async def test_linked_source_reply_to_linked_post_not_false_deleted(self) -> None:
        cfg = types.SimpleNamespace(
            ALLOWED_GROUP_IDS=[],
            COMMENT_MODERATION_ENABLED=True,
            COMMENT_TARGET_CHAT_IDS=[-100200],
            COMMENT_SOURCE_CHANNEL_IDS=[-100900],
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
        flag_mock.assert_not_awaited()
        delete_mock.assert_not_awaited()

    async def test_linked_comment_chat_admin_is_exempt_without_global_flag(self) -> None:
        cfg = types.SimpleNamespace(
            ALLOWED_GROUP_IDS=[],
            COMMENT_MODERATION_ENABLED=True,
            COMMENT_TARGET_CHAT_IDS=[],
            COMMENT_SOURCE_CHANNEL_IDS=[-100900],
            MODERATION_ADMIN_EXEMPT=False,
            MODERATION_DELETE_EXTERNAL_CHANNEL_MSGS=True,
            MODERATION_DELETE_BOT_CHANNEL_CHAT_FORWARDS=True,
            MODERATION_EXTERNAL_REPLIES_DELETE=True,
            COMMENT_MODERATION_DELETE_EXTERNAL_REPLIES=False,
            COMMENT_MODERATION_LINK_POLICY="group_default",
            MODERATION_DELETE_BUTTON_MESSAGES=False,
            MODERATION_ALLOW_STICKERS=False,
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
            message_id=334,
            from_user=types.SimpleNamespace(id=99, is_bot=False),
            sender_chat=None,
            forward_from=None,
            forward_from_chat=None,
            forward_sender_name=None,
            is_automatic_forward=False,
            external_reply=None,
            text="admin comment",
            caption=None,
            entities=[],
            caption_entities=[],
            reply_markup=None,
            sticker=object(),
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
            patch.object(moderation, "settings", cfg),
            patch.object(moderation, "is_from_linked_channel", AsyncMock(return_value=False)),
            patch.object(moderation, "_is_admin", AsyncMock(return_value=True)) as is_admin_mock,
            patch.object(moderation, "_delete_message_safe", AsyncMock(return_value=True)) as delete_mock,
        ):
            handled = await moderation.apply_moderation_filters(message.chat.id, message)

        self.assertFalse(handled)
        is_admin_mock.assert_awaited_once_with(message.chat.id, message.from_user.id)
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


class DiscussionCommentAsyncContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_resolver_marks_nested_reply_as_comment_via_cache(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-100200, linked_chat_id=-100900),
            message_id=703,
            sender_chat=None,
            forward_from_chat=None,
            is_automatic_forward=False,
            reply_to_message=types.SimpleNamespace(message_id=702, sender_chat=None, forward_from_chat=None),
        )

        with patch.object(moderation_context, "redis_client", types.SimpleNamespace(
            get=AsyncMock(return_value=b"701"),
            set=AsyncMock(),
        )) as redis_mock:
            ctx = await moderation_context.resolve_message_moderation_context_async(message, from_linked=False)

        self.assertEqual(ctx, "comment")
        redis_mock.get.assert_awaited_once_with("comment:root_of:-100200:702")
        redis_mock.set.assert_awaited_once_with(
            "comment:root_of:-100200:703",
            701,
            ex=86400,
        )

    async def test_group_context_falls_back_to_sync_when_async_resolver_fails(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-100200, linked_chat_id=-100900),
            message_id=704,
            sender_chat=types.SimpleNamespace(id=-100900, type=ChatType.CHANNEL),
            forward_from_chat=None,
            reply_to_message=None,
            is_automatic_forward=False,
        )

        with (
            patch.object(group, "is_from_linked_channel", AsyncMock(return_value=False)),
            patch.object(group, "resolve_message_moderation_context_async", AsyncMock(side_effect=RuntimeError("boom"))),
        ):
            is_comment = await group._resolve_group_comment_context(message)

        self.assertTrue(is_comment)


    async def test_async_resolver_marks_comment_by_trusted_source_thread_cache_without_linked_chat(self) -> None:
        root_message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-100200, linked_chat_id=None),
            message_id=900,
            message_thread_id=777,
            sender_chat=types.SimpleNamespace(id=-100900, type=ChatType.CHANNEL),
            forward_from_chat=None,
            is_automatic_forward=False,
            reply_to_message=None,
        )
        reply_message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-100200, linked_chat_id=None),
            message_id=901,
            message_thread_id=777,
            sender_chat=None,
            forward_from_chat=None,
            is_automatic_forward=False,
            reply_to_message=types.SimpleNamespace(message_id=900, sender_chat=None, forward_from_chat=None),
        )

        store = {}

        async def _get(key):
            return store.get(key)

        async def _set(key, value, ex=None):
            if isinstance(value, int):
                value = str(value).encode("utf-8")
            store[key] = value
            return True

        redis_mock = types.SimpleNamespace(get=AsyncMock(side_effect=_get), set=AsyncMock(side_effect=_set))
        cfg = types.SimpleNamespace(COMMENT_SOURCE_CHANNEL_IDS=[-100900], REPLY_CONTEXT_TTL_SEC=86400)

        with (
            patch.object(moderation_context, "redis_client", redis_mock),
            patch.object(moderation_context, "settings", cfg),
        ):
            root_ctx = await moderation_context.resolve_message_moderation_context_async(root_message, from_linked=False)
            reply_ctx = await moderation_context.resolve_message_moderation_context_async(reply_message, from_linked=False)

        self.assertEqual(root_ctx, "comment")
        self.assertEqual(reply_ctx, "comment")
        redis_mock.set.assert_any_await("comment:thread_root:-100200:777", 900, ex=86400)

    async def test_async_resolver_keeps_channel_origin_fallback_without_linked_chat_id(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-100200, linked_chat_id=None),
            message_id=705,
            sender_chat=types.SimpleNamespace(id=-100900, type=ChatType.CHANNEL),
            forward_from_chat=None,
            reply_to_message=None,
            is_automatic_forward=False,
        )

        with patch.object(moderation_context, "redis_client", types.SimpleNamespace(
            get=AsyncMock(return_value=None),
            set=AsyncMock(),
        )):
            ctx = await moderation_context.resolve_message_moderation_context_async(message, from_linked=False)

        self.assertEqual(ctx, "comment")


class GroupOnTopicTriggerCleanlinessTests(unittest.TestCase):
    def test_comment_reply_is_clean_for_on_topic(self) -> None:
        message = types.SimpleNamespace(reply_to_message=types.SimpleNamespace(message_id=1))

        is_clean = group._is_clean_message_for_on_topic(
            message,
            mentioned=False,
            mentions_other=False,
            is_comment_context=True,
        )

        self.assertTrue(is_clean)

    def test_non_comment_reply_is_not_clean_for_on_topic(self) -> None:
        message = types.SimpleNamespace(reply_to_message=types.SimpleNamespace(message_id=1))

        is_clean = group._is_clean_message_for_on_topic(
            message,
            mentioned=False,
            mentions_other=False,
            is_comment_context=False,
        )

        self.assertTrue(is_clean)


if __name__ == "__main__":
    unittest.main()
