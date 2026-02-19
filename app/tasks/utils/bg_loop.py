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

_BG_START_TIMEOUT_SEC = 1.0
_BG_START_MAX_ATTEMPTS = 3


def _cleanup_failed_start(loop: asyncio.AbstractEventLoop, thread: threading.Thread | None) -> None:
    global _BG_LOOP, _BG_THREAD

    if loop.is_running():
        loop.call_soon_threadsafe(loop.stop)

    if thread and thread.is_alive():
        thread.join(timeout=2.0)
        if thread.is_alive():
            logger.warning("Background loop thread did not exit within timeout during start cleanup")

    try:
        loop.close()
    except Exception:
        logger.exception("Failed to close bg loop after failed start")

    _BG_LOOP = None
    _BG_THREAD = None
    _BG_STARTED.clear()

def get_bg_loop() -> asyncio.AbstractEventLoop:
    global _BG_LOOP, _BG_THREAD


    if _BG_LOOP and _BG_LOOP.is_running():
        return _BG_LOOP

    with _BG_LOOP_LOCK:
        if _BG_LOOP and _BG_LOOP.is_running():
            return _BG_LOOP

        last_error = "unknown"
        for attempt in range(1, _BG_START_MAX_ATTEMPTS + 1):
            _BG_STARTED.clear()
            loop = asyncio.new_event_loop()

            def _bg_exception_handler(_loop: asyncio.AbstractEventLoop, context: dict):
                logger.exception("Unhandled exception in bg loop: %s", context)

            loop.set_exception_handler(_bg_exception_handler)

            def _run_loop(local_loop: asyncio.AbstractEventLoop = loop):
                asyncio.set_event_loop(local_loop)
                logger.debug("Background event-loop started")
                _BG_STARTED.set()
                local_loop.run_forever()

            thread = threading.Thread(
                target=_run_loop,
                daemon=True,
                name="bg_asyncio_loop"
            )

            _BG_LOOP = loop
            _BG_THREAD = thread
            thread.start()

            started = _BG_STARTED.wait(timeout=_BG_START_TIMEOUT_SEC)
            if started and loop.is_running():
                atexit.register(stop_bg_loop)
                return loop

            if not started:
                reason = f"timeout waiting for bg loop start after {_BG_START_TIMEOUT_SEC:.1f}s"
            else:
                reason = "bg loop start event set, but loop is not running"

            last_error = reason
            logger.error(
                "Failed to initialize background event loop on attempt %d/%d: %s",
                attempt,
                _BG_START_MAX_ATTEMPTS,
                reason,
            )
            _cleanup_failed_start(loop, thread)

        raise RuntimeError(
            f"Failed to initialize background event loop after {_BG_START_MAX_ATTEMPTS} attempts: {last_error}"
        )


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
