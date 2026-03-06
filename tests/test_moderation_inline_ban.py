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

from app.bot.handlers import moderation


class _FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict] = {}
        self.expire_calls: list[tuple[str, int]] = []

    async def hset(self, key: str, mapping: dict) -> None:
        self.hashes[key] = dict(mapping)

    async def expire(self, key: str, ttl: int) -> None:
        self.expire_calls.append((key, ttl))


class ModerationInlineBanTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_ban_without_trigger_message_creates_unique_audit_entries(self) -> None:
        fake_redis = _FakeRedis()
        callback = types.SimpleNamespace(
            data="mod:ban:-100500:777",
            from_user=types.SimpleNamespace(id=42),
            message=None,
            answer=AsyncMock(),
        )

        with (
            patch.object(moderation, "redis_client", fake_redis),
            patch.object(moderation, "_is_admin", AsyncMock(return_value=True)),
            patch.object(moderation, "_ban_user_safe", AsyncMock(return_value=True)) as ban_mock,
            patch.object(moderation, "settings", types.SimpleNamespace(MODERATION_BAN_REVOKE_MESSAGES=False, NEW_USER_TTL_SECONDS=120)),
        ):
            await moderation.moderation_inline_ban(callback)
            await moderation.moderation_inline_ban(callback)

        self.assertEqual(2, ban_mock.await_count)
        ban_mock.assert_any_await(-100500, 777, revoke=True)

        keys = list(fake_redis.hashes.keys())
        self.assertEqual(2, len(keys))
        self.assertTrue(all(key.startswith("mod:combot:inline:-100500:") for key in keys))
        self.assertEqual(len(keys), len(set(keys)))
        self.assertNotIn("mod:combot:-100500:0", keys)

        for key in keys:
            payload = fake_redis.hashes[key]
            self.assertEqual("ban", payload["action"])
            self.assertEqual("inline_button", payload["reason"])
            self.assertEqual(777, payload["user_id"])
            self.assertIsInstance(payload["ts"], int)

        self.assertEqual(sorted(keys), sorted(k for k, _ in fake_redis.expire_calls))

    async def test_inline_unban_success_edits_message_and_answers(self) -> None:
        callback_message = types.SimpleNamespace(
            html_text="Alert",
            text="Alert",
            caption=None,
            edit_text=AsyncMock(),
            edit_reply_markup=AsyncMock(),
        )
        callback = types.SimpleNamespace(
            data="mod:unban:-100500:777",
            from_user=types.SimpleNamespace(id=42),
            message=callback_message,
            answer=AsyncMock(),
        )

        with (
            patch.object(moderation, "_is_admin", AsyncMock(return_value=True)),
            patch.object(moderation, "_unban_user_safe", AsyncMock(return_value=True)) as unban_mock,
        ):
            await moderation.moderation_inline_unban(callback)

        unban_mock.assert_awaited_once_with(-100500, 777)
        callback_message.edit_text.assert_awaited_once()
        callback.answer.assert_awaited_with("User unbanned.", show_alert=False)

    async def test_auto_ban_notification_formats_reason_without_context_suffix(self) -> None:
        send_mock = AsyncMock(return_value=types.SimpleNamespace(message=object()))

        with (
            patch.object(moderation, "send_message_safe_with_reason", send_mock),
            patch.object(moderation, "get_bot", return_value=object()),
        ):
            await moderation._notify_auto_ban_with_actions(
                [1001],
                chat_id=-100500,
                offender_id=777,
                reason_text="first_link_after_join|context=comment",
                msg_id=42,
                chat_title="Main Chat",
            )

        sent_text = send_mock.await_args.args[2]
        self.assertIn("User banned by bot (Main Chat | chat ID: <code>-100500</code>)", sent_text)
        self.assertIn("Message ID: <code>42</code>", sent_text)
        self.assertIn('https://t.me/c/500/42', sent_text)
        self.assertIn("Reason: <b>first_link_after_join</b>.", sent_text)
        self.assertNotIn("|context=comment", sent_text)

        reply_markup = send_mock.await_args.kwargs["reply_markup"]
        button = reply_markup.inline_keyboard[0][0]
        self.assertEqual("mod:unban:-100500:777", button.callback_data)


if __name__ == "__main__":
    unittest.main()
