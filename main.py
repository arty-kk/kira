cat > main.py << 'EOF'
#main.py
from __future__ import annotations

import asyncio
import logging
import signal
import sys

from app.core.logging_config import setup_logging
from app import engine, Base, close_redis_pools, _get_env
from app.tasks.scheduler import start_scheduler, get_scheduler
from app.emo_engine.persona.memory import PersonaMemory
from app.bot import start_bot


def _init_database() -> None:
    async def _inner():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logging.info("✅ Database tables ready")
    return asyncio.create_task(_inner())


async def _preinit_persona_memory() -> PersonaMemory:
    pm = PersonaMemory()
    await pm._ready.wait()
    logging.info("✅ PersonaMemory is ready")
    return pm


async def main() -> None:
    setup_logging()
    logging.info("🚀 Starting application")

    db_task = _init_database()

    scheduler_enabled = _get_env("ENABLE_SCHEDULER", "false").lower() == "true"
    if scheduler_enabled:
        logging.info("⏱️ Starting scheduler")
        start_scheduler()
    else:
        logging.info("⏱️ Scheduler disabled")

    try:
        logging.info("⏳ Pre-initializing PersonaMemory")
        pm = await _preinit_persona_memory()
    except Exception:
        logging.exception("⚠️ PersonaMemory initialization failed")

    try:
        await db_task
    except Exception:
        logging.exception("❌ Database init failed")
        return

    logging.info("🤖 Launching bot")
    bot_task = asyncio.create_task(start_bot())

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, bot_task.cancel)
        except NotImplementedError:
            logging.warning("Signal handlers unsupported")
            break

    try:
        await bot_task
    except asyncio.CancelledError:
        logging.info("🔌 Shutdown signal received, stopping")
    except Exception:
        logging.exception("❌ Bot crashed")
        sys.exit(1)
    finally:
        s = get_scheduler()
        if scheduler_enabled and s and s.running:
            logging.info("⏹️ Shutting down scheduler")
            s.shutdown(wait=True)
        logging.info("🗄️ Disposing engine")
        engine.dispose()
        logging.info("🔌 Closing Redis")
        await close_redis_pools()
        logging.info("✅ Shutdown complete")

if __name__ == "__main__":
    asyncio.run(main())
EOF