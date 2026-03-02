import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services import dialog_logger


class DialogLoggerFailSafeTests(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_dialogs_dir_warns_once_and_never_raises(self) -> None:
        warned_before = set(dialog_logger._WARNED_ISSUES)
        dialog_logger._WARNED_ISSUES.clear()
        fake_settings = SimpleNamespace(ENABLE_DIALOG_LOGGING=True, DIALOGS_DIR="/root/forbidden_dialogs")
        try:
            with patch.object(dialog_logger, "settings", fake_settings), patch.object(
                dialog_logger.Path, "mkdir", side_effect=PermissionError("mkdir denied")
            ), self.assertLogs("app.services.dialog_logger", level="WARNING") as logs:
                await dialog_logger.start_dialog_logger()
                await dialog_logger.log_user_message(1, "user", "hello")
                await dialog_logger.log_bot_message(1, "bot", "world")
                await dialog_logger.log_user_message(1, "user", "hello again")
                await dialog_logger.log_bot_message(1, "bot", "world again")
                await dialog_logger.shutdown_dialog_logger()

            self.assertEqual(len(logs.records), 1)
            self.assertIn("DIALOGS_DIR=/root/forbidden_dialogs", logs.output[0])
            self.assertIn("mkdir denied", logs.output[0])
        finally:
            dialog_logger._WARNED_ISSUES.clear()
            dialog_logger._WARNED_ISSUES.update(warned_before)


if __name__ == "__main__":
    unittest.main()
