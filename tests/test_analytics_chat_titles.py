import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "12345:abcde")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("REDIS_URL_QUEUE", "redis://localhost:6379/1")
os.environ.setdefault("REDIS_URL_VECTOR", "redis://localhost:6379/2")

from app.services.addons import analytics


class _FakeRedis:
    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str):
        return self._store.get(key)

    async def set(self, key: str, value, nx: bool = False, ex: int | None = None):
        if nx and key in self._store:
            return False
        self._store[key] = value
        return True


class AnalyticsChatTitleTests(unittest.IsolatedAsyncioTestCase):
    def test_render_html_includes_chat_title_when_present(self) -> None:
        snap = {"msg_total": 0, "active_users": 0, "assistant_total": 0, "new_users_unique": 0}
        html = analytics._render_html(-100123, "2026-01-01", snap, chat_title="Main Group")
        self.assertIn("<i>Chat:</i> <b>Main Group</b>", html)
        self.assertIn("<i>Chat ID:</i> <code>-100123</code>", html)

    async def test_generate_report_resolves_and_includes_chat_title(self) -> None:
        fake_redis = _FakeRedis()
        sent_messages: list[str] = []

        async def _capture_send(_chat_id: int, html_text: str) -> None:
            sent_messages.append(html_text)

        with (
            patch.object(analytics, "get_redis", return_value=fake_redis),
            patch.object(analytics, "_load_day_snapshot", AsyncMock(return_value={
                "date": "2026-01-01",
                "msg_total": 0,
                "active_users": 0,
                "assistant_total": 0,
                "new_users_unique": 0,
            })),
            patch.object(analytics, "_safe_send_dm", AsyncMock(side_effect=_capture_send)),
            patch.object(analytics, "_llm_insights", AsyncMock(return_value=None)),
            patch.object(analytics, "get_bot", return_value=type("B", (), {"get_chat": AsyncMock(return_value=type("C", (), {"title": "Ops Chat", "username": None})())})()),
        ):
            await analytics.generate_and_send_report_for_chat(-100555, analytics._yesterday_utc_date(), [42])

        self.assertEqual(len(sent_messages), 1)
        self.assertIn("Ops Chat", sent_messages[0])


if __name__ == "__main__":
    unittest.main()
