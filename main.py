cat > main.py << EOF
#main.py
from __future__ import annotations

import asyncio, logging, signal, sys, os

from app import (
    engine, Base, setup_logging,
    start_scheduler, sched, start_bot,
    close_redis_pools, _get_env
)

async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logging.info("✅ Database tables created or already exist")


async def main() -> None:
    setup_logging()
    logging.getLogger("app").setLevel(logging.INFO)
    logging.info("🚀 Starting application")

    logging.info("⏳ Initializing database")
    try:
        await init_db()
    except Exception:
        logging.exception("Database initialization failed")
        return

    scheduler_enabled = os.getenv("ENABLE_SCHEDULER", "false").lower() == "true"
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
        await close_redis_pools()

    logging.info("✅ Graceful shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
EOF