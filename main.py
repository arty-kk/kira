# main.py
from __future__ import annotations

import asyncio
import logging
import signal
import sys

from app.core import setup_logging, engine, Base
from app.tasks import start_scheduler, sched
from app.bot.components.webhook import start_bot
from app.config import _get_env


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logging.info("✅ Database tables created or already exist")


async def main() -> None:
    setup_logging()
    logging.getLogger("app").setLevel(logging.INFO)
    logging.info("🚀 Starting application")

    logging.info("⏳ Initialising database")
    try:
        await init_db()
    except Exception:
        logging.exception("Database initialisation failed")
        return

    scheduler_enabled = str(_get_env("ENABLE_SCHEDULER", "false")).lower() == "true"
    if scheduler_enabled:
        logging.info("⏱️  Starting scheduler")
        start_scheduler()
    else:
        logging.info("⏱️  Scheduler disabled by ENABLE_SCHEDULER flag")

    logging.info("🤖 Starting bot")
    bot_task = asyncio.create_task(start_bot())

    loop = asyncio.get_running_loop()
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, bot_task.cancel)
    except NotImplementedError:
        logging.warning("Signal handlers are not supported on this platform")

    try:
        await bot_task
    except asyncio.CancelledError:
        logging.info("🔌 Shutdown signal received, stopping bot")
    except Exception:
        logging.exception("❌ start_bot crashed unexpectedly")
        sys.exit(1)
    finally:
        if scheduler_enabled:
            logging.info("⏹️  Shutting down scheduler")
            sched.shutdown(wait=True)
        logging.info("🗄️  Disposing database engine")
        await engine.dispose()

        logging.info("🔌 Closing Redis pools")
        from app.core.memory import close_redis_pools
        await close_redis_pools()

    logging.info("✅ Graceful shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
