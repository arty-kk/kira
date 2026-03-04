import os
import asyncio
import unittest
from unittest.mock import patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "12345:abcde")

from app.clients import telegram_client


class TelegramClientLoopBindingTests(unittest.TestCase):
    def test_get_bot_reuses_preloop_instance_in_active_loop(self) -> None:
        telegram_client._bots_by_loop.clear()

        class _FakeSession:
            async def close(self):
                return None

        class _FakeBot:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs
                self.session = _FakeSession()

        async def _get_in_loop():
            return telegram_client.get_bot()

        with patch.object(telegram_client, "Bot", _FakeBot):
            preloop_bot = telegram_client.get_bot()
            inloop_bot = asyncio.run(_get_in_loop())

            self.assertIs(preloop_bot, inloop_bot)
            self.assertNotIn(0, telegram_client._bots_by_loop)
            self.assertEqual(len(telegram_client._bots_by_loop), 1)

        asyncio.run(telegram_client.close_all_bots())


if __name__ == "__main__":
    unittest.main()
