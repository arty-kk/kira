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

from app.bot.handlers import group


class GroupUnauthorizedLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_on_group_message_logs_chat_title_for_unauthorized_group(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-1001, username="testgroup", title="Test Group", type=group.ChatType.SUPERGROUP),
            message_id=77,
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            text="hello",
            caption=None,
            entities=[],
            reply_to_message=None,
            content_type=group.ContentType.TEXT,
        )

        with (
            patch.object(group, "_is_message_allowed_for_group_handlers", AsyncMock(return_value=False)),
            patch.object(group, "logger") as logger_mock,
        ):
            await group.on_group_message(message)

        logger_mock.info.assert_called_once_with(
            "Ignore unauthorized group chat=%s title=%r uname=%s",
            -1001,
            "Test Group",
            "testgroup",
        )


if __name__ == "__main__":
    unittest.main()
