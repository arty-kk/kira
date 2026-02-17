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

    async def test_comment_target_chat_is_allowed_independently(self) -> None:
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

    async def test_disabled_comment_scope_does_not_bypass_allowed_groups(self) -> None:
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
            self.assertFalse(await group._is_message_allowed_for_group_handlers(message))


if __name__ == "__main__":
    unittest.main()
