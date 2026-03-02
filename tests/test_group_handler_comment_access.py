import types
import unittest
from unittest.mock import patch

from aiogram.enums import ChatType

from app.bot.handlers import group


class GroupCommentAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_regular_allowed_group_keeps_existing_path(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=100, type=ChatType.SUPERGROUP),
            sender_chat=None,
            forward_from_chat=None,
            is_automatic_forward=False,
        )

        cfg = types.SimpleNamespace(
            ALLOWED_GROUP_IDS=[100],
            COMMENT_MODERATION_ENABLED=True,
            COMMENT_TARGET_CHAT_IDS=[],
            COMMENT_SOURCE_CHANNEL_IDS=[],
        )
        with patch.object(group, "settings", cfg):
            self.assertTrue(await group._is_message_allowed_for_group_handlers(message))

    async def test_comment_target_chat_is_allowed(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=200, type=ChatType.SUPERGROUP),
            sender_chat=None,
            forward_from_chat=None,
            is_automatic_forward=False,
        )

        cfg = types.SimpleNamespace(
            ALLOWED_GROUP_IDS=[],
            COMMENT_MODERATION_ENABLED=True,
            COMMENT_TARGET_CHAT_IDS=[200],
            COMMENT_SOURCE_CHANNEL_IDS=[],
        )
        with patch.object(group, "settings", cfg):
            self.assertTrue(await group._is_message_allowed_for_group_handlers(message))

    async def test_comment_source_channel_is_allowed(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=300, type=ChatType.SUPERGROUP),
            sender_chat=types.SimpleNamespace(id=-100555, type=ChatType.CHANNEL),
            forward_from_chat=None,
            is_automatic_forward=False,
        )

        cfg = types.SimpleNamespace(
            ALLOWED_GROUP_IDS=[],
            COMMENT_MODERATION_ENABLED=True,
            COMMENT_TARGET_CHAT_IDS=[],
            COMMENT_SOURCE_CHANNEL_IDS=[-100555],
        )
        with patch.object(group, "settings", cfg):
            self.assertTrue(await group._is_message_allowed_for_group_handlers(message))

    async def test_comment_source_channel_is_allowed_via_linked_chat_for_user_message(self) -> None:
        linked_channel_id = -100888
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=301, type=ChatType.SUPERGROUP, linked_chat_id=linked_channel_id),
            sender_chat=None,
            forward_from_chat=None,
            is_automatic_forward=False,
        )

        cfg = types.SimpleNamespace(
            ALLOWED_GROUP_IDS=[],
            COMMENT_MODERATION_ENABLED=True,
            COMMENT_TARGET_CHAT_IDS=[],
            COMMENT_SOURCE_CHANNEL_IDS=[linked_channel_id],
        )
        with patch.object(group, "settings", cfg):
            self.assertTrue(await group._is_message_allowed_for_group_handlers(message))

    async def test_disabled_comment_scope_still_allows_trusted_comment_scope(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=400, type=ChatType.SUPERGROUP),
            sender_chat=types.SimpleNamespace(id=-100777, type=ChatType.CHANNEL),
            forward_from_chat=None,
            is_automatic_forward=False,
        )

        cfg = types.SimpleNamespace(
            ALLOWED_GROUP_IDS=[],
            COMMENT_MODERATION_ENABLED=False,
            COMMENT_TARGET_CHAT_IDS=[400],
            COMMENT_SOURCE_CHANNEL_IDS=[-100777],
        )
        with patch.object(group, "settings", cfg):
            self.assertTrue(await group._is_message_allowed_for_group_handlers(message))

    async def test_untrusted_chat_still_blocked_when_not_in_allowed_or_comment_scope(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=401, type=ChatType.SUPERGROUP),
            sender_chat=None,
            forward_from_chat=None,
            is_automatic_forward=False,
        )

        cfg = types.SimpleNamespace(
            ALLOWED_GROUP_IDS=[],
            COMMENT_MODERATION_ENABLED=False,
            COMMENT_TARGET_CHAT_IDS=[],
            COMMENT_SOURCE_CHANNEL_IDS=[-100777],
        )
        with patch.object(group, "settings", cfg):
            self.assertFalse(await group._is_message_allowed_for_group_handlers(message))

    async def test_untrusted_chat_with_untrusted_forward_source_is_blocked(self) -> None:
        chat_id = 310
        trusted_source_channel_id = -100123
        untrusted_source_channel_id = -100124
        cfg = types.SimpleNamespace(
            ALLOWED_GROUP_IDS=[],
            COMMENT_MODERATION_ENABLED=True,
            COMMENT_TARGET_CHAT_IDS=[],
            COMMENT_SOURCE_CHANNEL_IDS=[trusted_source_channel_id],
        )

        forwarded_message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=chat_id, type=ChatType.SUPERGROUP),
            sender_chat=None,
            forward_from_chat=types.SimpleNamespace(id=untrusted_source_channel_id, type=ChatType.CHANNEL),
            is_automatic_forward=False,
        )

        with patch.object(group, "settings", cfg):
            self.assertFalse(await group._is_message_allowed_for_group_handlers(forwarded_message))


if __name__ == "__main__":
    unittest.main()
