# app/bot/core.py
from __future__ import annotations

import asyncio
import logging

from app.bot.components.webhook import start_bot
from app.core import setup_logging


def main() -> None:

    setup_logging()
    logging.info("Starting bot…")
    asyncio.run(start_bot())


if __name__ == "__main__":
    main()
