import os
import types
import unittest
from unittest.mock import AsyncMock, patch

from aiogram.enums import ChatType

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "12345:abcde")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("REDIS_URL_QUEUE", "redis://localhost:6379/1")
os.environ.setdefault("REDIS_URL_VECTOR", "redis://localhost:6379/2")

from app.bot.handlers import group, moderation
from app.bot.utils.trusted_scope import (
    is_trusted_actor,
    is_trusted_destination,
    is_trusted_repost,
    trusted_scope_ids,
)


class TrustedScopeConsistencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_trusted_repost_verdict_consistent_for_group_and_helper(self) -> None:
        cfg = types.SimpleNamespace(
            ALLOWED_GROUP_IDS=[-10010],
            COMMENT_TARGET_CHAT_IDS=[-10020],
            COMMENT_SOURCE_CHANNEL_IDS=[-10030],
        )
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-10010, linked_chat_id=None),
            sender_chat=None,
            forward_from_chat=types.SimpleNamespace(id=-10030, type=ChatType.CHANNEL),
        )

        with patch.object(group, "settings", cfg):
            group_verdict = group._is_trusted_scope_repost(message)

        _, _, trusted_scope = trusted_scope_ids(cfg)
        helper_verdict = is_trusted_repost(message, trusted_scope, destination_trusted=is_trusted_destination(-10010, message.chat, cfg))
        self.assertEqual(group_verdict, helper_verdict)
        self.assertTrue(group_verdict)

    async def test_trusted_actor_verdict_consistent_for_moderation_and_helper(self) -> None:
        cfg = types.SimpleNamespace(
            ALLOWED_GROUP_IDS=[-10010],
            COMMENT_TARGET_CHAT_IDS=[],
            COMMENT_SOURCE_CHANNEL_IDS=[-10030],
        )
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-10010, linked_chat_id=None),
            from_user=types.SimpleNamespace(id=42),
            sender_chat=None,
            forward_from_chat=types.SimpleNamespace(id=-100999, type=ChatType.CHANNEL),
            is_automatic_forward=False,
        )

        _, trusted_source_ids, trusted_scope = trusted_scope_ids(cfg)

        with (
            patch.object(moderation, "settings", cfg),
            patch.object(moderation, "_is_admin", AsyncMock(return_value=False)),
        ):
            mod_verdict = await moderation._is_fully_trusted_actor_or_action(
                chat_id=-10010,
                message=message,
                source="channel",
                user_id=42,
                from_linked=False,
            )

        helper_verdict = await is_trusted_actor(
            message=message,
            user_id=42,
            chat_id=-10010,
            from_linked=False,
            trusted_scope_ids=trusted_scope,
            trusted_source_channel_ids=trusted_source_ids,
            is_admin_cb=AsyncMock(return_value=False),
        )

        self.assertEqual(mod_verdict, helper_verdict)
        self.assertFalse(mod_verdict)


if __name__ == "__main__":
    unittest.main()
