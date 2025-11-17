#main.py
from __future__ import annotations

import asyncio
import uvicorn
import logging
import signal
import sys
import os

from typing import Any

from app import engine, close_redis_pools, _get_env, setup_logging
from app.tasks.scheduler import start_scheduler, get_scheduler
from app.emo_engine.persona.memory import PersonaMemory
from app.emo_engine.registry import shutdown_personas
from app.clients.http_client import http_client
from app.bot import start_bot
from app.api.app import create_app


async def _preinit_persona_memory() -> PersonaMemory:
    pm = PersonaMemory(chat_id=0, start_maintenance=False)
    await pm.ready()
    logging.info("✅ PersonaMemory is ready")
    return pm


async def start_api_server() -> None:
    app = create_app()

    host = _get_env("API_HOST", _get_env("WEBHOOK_HOST", "0.0.0.0"))
    port = int(_get_env("API_PORT", _get_env("WEBHOOK_PORT", "8000")))

    ssl_certfile = None
    ssl_keyfile = None

    certs_dir = _get_env("CERTS_DIR", "")
    use_self_signed = _get_env("USE_SELF_SIGNED_CERT", "false").lower() == "true"

    if certs_dir and use_self_signed:
        fullchain = os.path.join(certs_dir, "fullchain.pem")
        privkey = os.path.join(certs_dir, "privkey.pem")
        if os.path.exists(fullchain) and os.path.exists(privkey):
            ssl_certfile = fullchain
            ssl_keyfile = privkey

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
        access_log=False,
        proxy_headers=True,
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = False
    await server.serve()

async def main() -> None:
    setup_logging()
    logging.info("🚀 Starting application")

    run_bot = _get_env("RUN_BOT", "true").lower() == "true"
    run_api = _get_env("RUN_API", "true").lower() == "true"
    
    scheduler_enabled = (
        _get_env("ENABLE_SCHEDULER", "true").lower() == "true"
        and (run_bot or run_api)
    )
    if scheduler_enabled:
        start_scheduler()
    else:
        logging.info("⏱️ Scheduler disabled")

    try:
        await _preinit_persona_memory()
    except Exception:
        logging.exception("⚠️ PersonaMemory initialization failed")

    tasks: list[asyncio.Task[Any]] = []

    if run_bot:
        logging.info("🤖 Launching bot")
        bot_task = asyncio.create_task(start_bot())
        tasks.append(bot_task)
    else:
        bot_task = None
        logging.info("🤖 Bot disabled via RUN_BOT=false")

    if run_api:
        logging.info("🌐 Launching API server")
        api_task = asyncio.create_task(start_api_server())
        tasks.append(api_task)
    else:
        api_task = None
        logging.info("🌐 API server disabled via RUN_API=false")

    if not tasks:
        logging.error("Nothing to run (both RUN_BOT and RUN_API are false); exiting.")
        return

    loop = asyncio.get_running_loop()

    if os.getenv("PYTHONASYNCIODEBUG", "0").lower() not in ("0", "false"):
        loop.set_debug(True)
        loop.slow_callback_duration = float(
            os.getenv("ASYNCIO_SLOW_THRESHOLD_SEC", "1.0")
        )

    logging.getLogger("asyncio").setLevel(
        os.getenv("ASYNCIO_LOG_LEVEL", "INFO").upper()
    )

    def _loop_ex_handler(loop, ctx):
        logging.error("UNHANDLED ASYNCIO EXCEPTION: %s", ctx.get("message"))
        if ctx.get("exception"):
            logging.exception(ctx["exception"])

    loop.set_exception_handler(_loop_ex_handler)

    def _cancel_all():
        for t in list(tasks):
            if not t.done():
                t.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _cancel_all)
        except NotImplementedError:
            logging.warning("Signal handlers unsupported")
            break

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logging.info("🔌 Shutdown signal received, stopping")
    except Exception:
        logging.exception("❌ Application crashed")
        sys.exit(1)
    finally:
        try:
            logging.info("🧹 Shutting down personas")
            await shutdown_personas()
        except Exception:
            logging.exception("Failed to shutdown personas cleanly")

        s = get_scheduler()
        if scheduler_enabled and s and s.running:
            logging.info("⏹️ Shutting down scheduler")
            s.shutdown(wait=True)

        logging.info("🗄️ Disposing engine")
        try:
            await engine.dispose()
        except Exception:
            logging.exception("Failed to dispose engine")

        logging.info("🌐 Closing HTTP client")
        await http_client.close()

        logging.info("🔌 Closing Redis")
        await close_redis_pools()
        logging.info("✅ Shutdown complete")

if __name__ == "__main__":
    asyncio.run(main())