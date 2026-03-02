import asyncio
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from app.services import dialog_logger


class DialogLoggerQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await dialog_logger.shutdown_dialog_logger()
        dialog_logger._WARNED_ISSUES.clear()

    async def asyncTearDown(self) -> None:
        await dialog_logger.shutdown_dialog_logger()
        dialog_logger._WARNED_ISSUES.clear()

    async def test_parallel_logging_and_shutdown_flush(self) -> None:
        with TemporaryDirectory() as tmp:
            fake_settings = SimpleNamespace(
                ENABLE_DIALOG_LOGGING=True,
                DIALOGS_DIR=tmp,
                DEFAULT_TZ="UTC",
                DIALOG_LOGGER_QUEUE_MAXSIZE=1000,
            )
            with patch.object(dialog_logger, "settings", fake_settings):
                await dialog_logger.start_dialog_logger()
                await asyncio.gather(
                    *(dialog_logger.log_user_message(42, "user", f"msg-{i}") for i in range(100))
                )
                await dialog_logger.shutdown_dialog_logger()

            log_path = Path(tmp) / "42.txt"
            self.assertTrue(log_path.exists())
            content = log_path.read_text(encoding="utf-8")
            self.assertIn("msg-0", content)
            self.assertIn("msg-99", content)

    async def test_queue_full_drops_new_and_warns_once(self) -> None:
        with TemporaryDirectory() as tmp:
            fake_settings = SimpleNamespace(
                ENABLE_DIALOG_LOGGING=True,
                DIALOGS_DIR=tmp,
                DEFAULT_TZ="UTC",
                DIALOG_LOGGER_QUEUE_MAXSIZE=1,
            )

            with patch.object(dialog_logger, "settings", fake_settings):
                await dialog_logger.start_dialog_logger()
                queue = dialog_logger._DIALOG_LOG_QUEUE
                self.assertIsNotNone(queue)
                assert queue is not None

                with self.assertLogs("app.services.dialog_logger", level="WARNING") as logs:
                    queue.put_nowait((1, "occupied"))
                    await dialog_logger.log_user_message(1, "u", "drop-me")
                    await dialog_logger.log_bot_message(1, "b", "drop-me-too")
                    await dialog_logger.shutdown_dialog_logger()

            self.assertEqual(len(logs.records), 1)
            self.assertIn("dialog logger queue is full", logs.output[0])

    async def test_shutdown_drains_queue(self) -> None:
        with TemporaryDirectory() as tmp:
            fake_settings = SimpleNamespace(
                ENABLE_DIALOG_LOGGING=True,
                DIALOGS_DIR=tmp,
                DEFAULT_TZ="UTC",
                DIALOG_LOGGER_QUEUE_MAXSIZE=100,
            )

            with patch.object(dialog_logger, "settings", fake_settings):
                await dialog_logger.start_dialog_logger()
                for idx in range(20):
                    await dialog_logger.log_bot_message(11, "BOT", f"line-{idx}")
                await dialog_logger.shutdown_dialog_logger()

            content = (Path(tmp) / "11.txt").read_text(encoding="utf-8")
            self.assertIn("line-0", content)
            self.assertIn("line-19", content)


if __name__ == "__main__":
    unittest.main()
