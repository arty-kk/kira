cat >app/tasks/utils/bg_loop.py<< 'EOF'
#app/tasks/utils/bg_loop.py
import asyncio
import threading
import logging
import atexit


logger = logging.getLogger(__name__)

_BG_LOOP_LOCK = threading.Lock()
_BG_LOOP: asyncio.AbstractEventLoop | None = None
_BG_THREAD : threading.Thread | None = None
_BG_STARTED = threading.Event()

def get_bg_loop() -> asyncio.AbstractEventLoop:
    global _BG_LOOP, _BG_THREAD


    if _BG_LOOP and _BG_LOOP.is_running():
        return _BG_LOOP

    with _BG_LOOP_LOCK:
        if _BG_LOOP and _BG_LOOP.is_running():
            return _BG_LOOP

        loop = asyncio.new_event_loop()

        def _bg_exception_handler(loop: asyncio.AbstractEventLoop, context: dict):
            logger.exception("Unhandled exception in bg loop: %s", context)

        loop.set_exception_handler(_bg_exception_handler)

        def _run_loop():
            asyncio.set_event_loop(loop)
            logger.debug("Background event-loop started")
            _BG_STARTED.set()
            loop.run_forever()

        _BG_THREAD = threading.Thread(
            target=_run_loop,
            daemon=True,
            name="bg_asyncio_loop"
        )

        _BG_THREAD.start()

        _BG_LOOP = loop
        _BG_STARTED.wait(timeout=1.0)
        atexit.register(stop_bg_loop)
        return _BG_LOOP   


def stop_bg_loop() -> None:

    global _BG_LOOP, _BG_THREAD

    if _BG_LOOP and _BG_LOOP.is_running():
        _BG_LOOP.call_soon_threadsafe(_BG_LOOP.stop)
        logger.debug("Requested background event-loop shutdown")

        if _BG_THREAD and _BG_THREAD.is_alive():
            _BG_THREAD.join(timeout=2.0)
            if _BG_THREAD.is_alive():
                logger.warning("Background loop thread did not exit within timeout")

        try:
            _BG_LOOP.close()
        except Exception:
            logger.exception("Failed to close bg loop cleanly")

    _BG_LOOP = None
    _BG_THREAD = None
    _BG_STARTED.clear()
EOF