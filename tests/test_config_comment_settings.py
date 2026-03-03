import os
import types
import unittest
from unittest.mock import patch

from app import config


class CommentSettingsConfigTests(unittest.TestCase):
    def test_comment_csv_parsing_logs_invalid_tokens(self) -> None:
        env = {
            "OPENAI_API_KEY": "test-key",
            "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5432/db",
            "REDIS_URL": "redis://localhost:6379/0",
            "REDIS_URL_QUEUE": "redis://localhost:6379/1",
            "REDIS_URL_VECTOR": "redis://localhost:6379/2",
            "COMMENT_MODERATION_ENABLED": "true",
            "COMMENT_TARGET_CHAT_IDS": "101, foo, 202,bar",
            "COMMENT_SOURCE_CHANNEL_IDS": "-1001, nope",
            "COMMENT_MODERATION_LINK_POLICY": "group_default",
        }

        with patch.dict(os.environ, env, clear=True):
            with self.assertLogs("app.config", level="WARNING") as cm:
                cfg = config.Settings()

        self.assertEqual(cfg.COMMENT_TARGET_CHAT_IDS, [101, 202])
        self.assertEqual(cfg.COMMENT_SOURCE_CHANNEL_IDS, [-1001])
        self.assertEqual(cfg._COMMENT_TARGET_CHAT_IDS_INVALID_TOKENS, ["foo", "bar"])
        self.assertEqual(cfg._COMMENT_SOURCE_CHANNEL_IDS_INVALID_TOKENS, ["nope"])
        self.assertTrue(any("Invalid integer tokens in COMMENT_TARGET_CHAT_IDS" in msg for msg in cm.output))
        self.assertTrue(any("Invalid integer tokens in COMMENT_SOURCE_CHANNEL_IDS" in msg for msg in cm.output))

    def test_enabled_without_lists_logs_warning(self) -> None:
        env = {
            "OPENAI_API_KEY": "test-key",
            "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5432/db",
            "REDIS_URL": "redis://localhost:6379/0",
            "REDIS_URL_QUEUE": "redis://localhost:6379/1",
            "REDIS_URL_VECTOR": "redis://localhost:6379/2",
            "COMMENT_MODERATION_ENABLED": "true",
            "COMMENT_TARGET_CHAT_IDS": "",
            "COMMENT_SOURCE_CHANNEL_IDS": "",
            "COMMENT_MODERATION_LINK_POLICY": "group_default",
        }

        with patch.dict(os.environ, env, clear=True):
            with self.assertLogs("app.config", level="WARNING") as cm:
                config.Settings()

        self.assertTrue(
            any(
                "COMMENT_MODERATION_ENABLED=true but both COMMENT_TARGET_CHAT_IDS and COMMENT_SOURCE_CHANNEL_IDS are empty"
                in msg
                for msg in cm.output
            )
        )


    def test_invalid_comment_link_policy_falls_back_with_warning(self) -> None:
        env = {
            "OPENAI_API_KEY": "test-key",
            "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5432/db",
            "REDIS_URL": "redis://localhost:6379/0",
            "REDIS_URL_QUEUE": "redis://localhost:6379/1",
            "REDIS_URL_VECTOR": "redis://localhost:6379/2",
            "COMMENT_MODERATION_LINK_POLICY": "unexpected_mode",
        }

        with patch.dict(os.environ, env, clear=True):
            with self.assertLogs("app.config", level="WARNING") as cm:
                cfg = config.Settings()

        self.assertEqual(cfg.COMMENT_MODERATION_LINK_POLICY, "group_default")
        self.assertTrue(any("Invalid COMMENT_MODERATION_LINK_POLICY" in msg for msg in cm.output))

    def test_valid_comment_csv_parses_without_invalid_tokens_warning(self) -> None:
        env = {
            "OPENAI_API_KEY": "test-key",
            "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5432/db",
            "REDIS_URL": "redis://localhost:6379/0",
            "REDIS_URL_QUEUE": "redis://localhost:6379/1",
            "REDIS_URL_VECTOR": "redis://localhost:6379/2",
            "COMMENT_TARGET_CHAT_IDS": "101,202",
            "COMMENT_SOURCE_CHANNEL_IDS": "-1001,-1002",
            "COMMENT_MODERATION_LINK_POLICY": "group_default",
        }

        with patch.dict(os.environ, env, clear=True):
            cfg = config.Settings()

        self.assertEqual(cfg.COMMENT_TARGET_CHAT_IDS, [101, 202])
        self.assertEqual(cfg.COMMENT_SOURCE_CHANNEL_IDS, [-1001, -1002])
        self.assertEqual(cfg._COMMENT_TARGET_CHAT_IDS_INVALID_TOKENS, [])
        self.assertEqual(cfg._COMMENT_SOURCE_CHANNEL_IDS_INVALID_TOKENS, [])

    def test_allowed_group_ids_csv_parsing_logs_invalid_tokens(self) -> None:
        env = {
            "OPENAI_API_KEY": "test-key",
            "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5432/db",
            "REDIS_URL": "redis://localhost:6379/0",
            "REDIS_URL_QUEUE": "redis://localhost:6379/1",
            "REDIS_URL_VECTOR": "redis://localhost:6379/2",
            "ALLOWED_GROUP_IDS": "111, bad, 222, nope",
        }

        with patch.dict(os.environ, env, clear=True):
            with self.assertLogs("app.config", level="WARNING") as cm:
                cfg = config.Settings()

        self.assertEqual(cfg.ALLOWED_GROUP_IDS, [111, 222])
        self.assertEqual(cfg._ALLOWED_GROUP_IDS_INVALID_TOKENS, ["bad", "nope"])
        self.assertTrue(any("Invalid integer tokens in ALLOWED_GROUP_IDS" in msg for msg in cm.output))
        self.assertTrue(any("Group access config contains invalid CSV tokens" in msg for msg in cm.output))

    def test_moderator_ids_mixed_csv_parsing_logs_invalid_tokens(self) -> None:
        env = {
            "OPENAI_API_KEY": "test-key",
            "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5432/db",
            "REDIS_URL": "redis://localhost:6379/0",
            "REDIS_URL_QUEUE": "redis://localhost:6379/1",
            "REDIS_URL_VECTOR": "redis://localhost:6379/2",
            "MODERATOR_IDS": "123, abc, 456 ",
        }

        with patch.dict(os.environ, env, clear=True):
            with self.assertLogs("app.config", level="WARNING") as cm:
                cfg = config.Settings()

        self.assertEqual(cfg.MODERATOR_IDS, [123, 456])
        self.assertEqual(cfg._MODERATOR_IDS_INVALID_TOKENS, ["abc"])
        self.assertTrue(any("Moderator config contains invalid CSV tokens: MODERATOR_IDS" in msg for msg in cm.output))


class CommentSettingsMixedAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_allowed_groups_and_comment_scope_work_without_conflict(self) -> None:
        from aiogram.enums import ChatType
        from app.bot.handlers import group

        cfg = types.SimpleNamespace(
            ALLOWED_GROUP_IDS=[111],
            COMMENT_MODERATION_ENABLED=True,
            COMMENT_TARGET_CHAT_IDS=[222],
            COMMENT_SOURCE_CHANNEL_IDS=[-100555],
        )

        regular_message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=111, type=ChatType.SUPERGROUP, linked_chat_id=None),
            sender_chat=None,
            forward_from_chat=None,
            is_automatic_forward=False,
        )
        comment_target_message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=222, type=ChatType.SUPERGROUP, linked_chat_id=-100555),
            sender_chat=None,
            forward_from_chat=None,
            is_automatic_forward=False,
        )
        comment_source_message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=333, type=ChatType.SUPERGROUP, linked_chat_id=None),
            sender_chat=types.SimpleNamespace(id=-100555, type=ChatType.CHANNEL),
            forward_from_chat=None,
            is_automatic_forward=False,
        )

        with patch.object(group, "settings", cfg):
            self.assertTrue(await group._is_message_allowed_for_group_handlers(regular_message))
            self.assertTrue(await group._is_message_allowed_for_group_handlers(comment_target_message))
            self.assertFalse(await group._is_message_allowed_for_group_handlers(comment_source_message))

    async def test_comment_target_linked_source_message_is_allowed(self) -> None:
        from aiogram.enums import ChatType
        from app.bot.handlers import group

        cfg = types.SimpleNamespace(
            ALLOWED_GROUP_IDS=[],
            COMMENT_MODERATION_ENABLED=True,
            COMMENT_TARGET_CHAT_IDS=[222],
            COMMENT_SOURCE_CHANNEL_IDS=[-100555],
        )

        comment_message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=222, type=ChatType.SUPERGROUP, linked_chat_id=-100555),
            sender_chat=types.SimpleNamespace(id=-100555, type=ChatType.CHANNEL),
            forward_from_chat=None,
            is_automatic_forward=True,
        )

        with patch.object(group, "settings", cfg):
            self.assertTrue(await group._is_message_allowed_for_group_handlers(comment_message))



if __name__ == "__main__":
    unittest.main()
