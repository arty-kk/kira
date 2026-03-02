import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "12345:abcde")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("REDIS_URL_QUEUE", "redis://localhost:6379/1")
os.environ.setdefault("REDIS_URL_VECTOR", "redis://localhost:6379/2")
os.environ.setdefault("CELERY_BROKER_URL", "redis://localhost:6379/3")
os.environ.setdefault("TWITTER_API_KEY", "x")
os.environ.setdefault("TWITTER_API_SECRET", "x")
os.environ.setdefault("TWITTER_ACCESS_TOKEN", "x")
os.environ.setdefault("TWITTER_ACCESS_TOKEN_SECRET", "x")
os.environ.setdefault("TWITTER_BEARER_TOKEN", "x")

from app.tasks.battle import battle_move_timeout_check_task, battle_start_timeout_check_task


class BattleTimeoutTasksTests(unittest.TestCase):
    @staticmethod
    def _fake_run(coro):
        return asyncio.run(coro)

    def test_start_timeout_invalid_empty_gid_logs_metrics_and_skips_check(self) -> None:
        payload = {"gid": "   ", "expected_phase_version": "2"}

        with (
            patch("app.tasks.battle.run_coro_sync", side_effect=self._fake_run),
            patch("app.tasks.battle.check_battle_timeout", new_callable=AsyncMock) as check_mock,
            patch("app.bot.components.constants.redis_client.incr", new_callable=AsyncMock) as incr_mock,
            self.assertLogs("app.tasks.battle", level="WARNING") as logs,
        ):
            battle_start_timeout_check_task.run(payload)

        check_mock.assert_not_called()
        incr_mock.assert_awaited_once()
        self.assertTrue(any("invalid payload" in entry for entry in logs.output))

    def test_move_timeout_invalid_expected_phase_version_logs_metrics_and_skips_check(self) -> None:
        payload = {"gid": "gid-1", "expected_phase_version": "abc"}

        with (
            patch("app.tasks.battle.run_coro_sync", side_effect=self._fake_run),
            patch("app.tasks.battle.check_move_timeout", new_callable=AsyncMock) as check_mock,
            patch("app.bot.components.constants.redis_client.incr", new_callable=AsyncMock) as incr_mock,
            self.assertLogs("app.tasks.battle", level="WARNING") as logs,
        ):
            battle_move_timeout_check_task.run(payload)

        check_mock.assert_not_called()
        incr_mock.assert_awaited_once()
        self.assertTrue(any("invalid payload" in entry for entry in logs.output))

    def test_start_timeout_fallback_expected_version_still_supported(self) -> None:
        payload = {"gid": "gid-1", "expected_version": "7"}

        with (
            patch("app.tasks.battle.run_coro_sync", side_effect=self._fake_run),
            patch("app.tasks.battle.check_battle_timeout", new_callable=AsyncMock) as check_mock,
        ):
            battle_start_timeout_check_task.run(payload)

        check_mock.assert_awaited_once_with("gid-1", expected_phase_version=7)

    def test_move_timeout_fallback_expected_version_still_supported(self) -> None:
        payload = {"gid": "gid-1", "expected_version": "9"}

        with (
            patch("app.tasks.battle.run_coro_sync", side_effect=self._fake_run),
            patch("app.tasks.battle.check_move_timeout", new_callable=AsyncMock) as check_mock,
        ):
            battle_move_timeout_check_task.run(payload)

        check_mock.assert_awaited_once_with("gid-1", expected_phase_version=9)


if __name__ == "__main__":
    unittest.main()
