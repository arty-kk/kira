#app/tasks/celery_app.py
from __future__ import annotations

import os
import asyncio
import logging
import threading
import concurrent.futures

from celery import Celery, current_task
from celery.signals import setup_logging as celery_setup_logging, worker_ready, worker_shutdown

from app.clients.telegram_client import close_all_bots, close_bot_for_current_loop
from app.config import settings
from app.core.memory import close_redis_pools
from app.core.logging_config import setup_logging

setup_logging()
logger = logging.getLogger(__name__)


class _WorkerLoopRunner:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._state_lock = threading.Lock()

    def ensure_started(self) -> asyncio.AbstractEventLoop:
        with self._state_lock:
            loop = self._loop
            thread = self._thread
            if loop is not None and thread is not None and thread.is_alive() and loop.is_running():
                return loop

            self._started.clear()
            self._loop = None
            self._thread = threading.Thread(
                target=self._run_loop_thread,
                name="celery-worker-loop",
                daemon=True,
            )
            self._thread.start()

        self._started.wait(timeout=5)
        loop = self._loop
        if loop is None or not loop.is_running():
            raise RuntimeError("Failed to start celery worker event loop runner")
        return loop

    def _run_loop_thread(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._started.set()
        try:
            loop.run_forever()
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                logger.debug("Worker loop shutdown_asyncgens failed", exc_info=True)
            loop.close()

    def submit(self, coro):
        loop = self.ensure_started()
        return asyncio.run_coroutine_threadsafe(coro, loop)

    def is_running(self) -> bool:
        loop = self._loop
        thread = self._thread
        return loop is not None and thread is not None and thread.is_alive() and loop.is_running()

    def stop(self) -> None:
        with self._state_lock:
            loop = self._loop
            thread = self._thread
            self._loop = None
            self._thread = None

        if loop is None or thread is None:
            return

        if loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)


_worker_loop_runner: _WorkerLoopRunner | None = None
_worker_loop_runner_lock = threading.Lock()


def _get_existing_worker_loop_runner() -> _WorkerLoopRunner | None:
    with _worker_loop_runner_lock:
        return _worker_loop_runner


def _get_worker_loop_runner() -> _WorkerLoopRunner:
    global _worker_loop_runner

    with _worker_loop_runner_lock:
        runner = _worker_loop_runner
        if runner is None:
            runner = _WorkerLoopRunner()
            _worker_loop_runner = runner

    runner.ensure_started()
    return runner


@celery_setup_logging.connect
def _disable_celery_autologger(**_kwargs):
    pass


broker_url = settings.CELERY_BROKER_URL or os.getenv("CELERY_BROKER_URL")
if not broker_url:
    raise RuntimeError("Environment variable CELERY_BROKER_URL is required")
if not broker_url.startswith(("redis://", "rediss://")):
    raise RuntimeError("CELERY_BROKER_URL must start with redis:// or rediss://")


celery = Celery(
    "synchatica",
    broker=broker_url,
    backend=None,
    include=[
        "app.tasks.summarize",
        "app.tasks.gifts",
        "app.tasks.periodic",
        "app.tasks.welcome",
        "app.tasks.moderation",
        "app.tasks.api_cleanup",
        "app.tasks.kb",
        "app.tasks.payments",
        "app.tasks.refunds",
        "app.tasks.battle",
        "app.tasks.media",
    ],
)


celery.conf.update(
    task_ignore_result=True,
    result_expires=0,
    task_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_reject_on_worker_lost=True,
    broker_transport_options={
        "visibility_timeout": 3600,
        "socket_timeout": 60,
    },
    task_acks_late=True,
    worker_pool_restarts=True,
    worker_prefetch_multiplier=1,
    worker_concurrency=settings.CELERY_CONCURRENCY,
    worker_hijack_root_logger=False,
    broker_connection_retry_on_startup=True,
    task_default_queue=settings.CELERY_DEFAULT_QUEUE,
    task_routes={
        "media.preprocess_group_image": {"queue": settings.CELERY_MEDIA_QUEUE},
        "moderation.*": {"queue": settings.CELERY_MODERATION_QUEUE},
    },
)


def _resolve_run_context(coro) -> tuple[str | None, str | None, str | None, str | None]:
    coro_name = getattr(coro, "__qualname__", None)
    if not coro_name:
        coro_code = getattr(coro, "cr_code", None)
        coro_name = getattr(coro_code, "co_name", None)

    task_name = None
    task_id = None
    routing_key = None
    try:
        task_name = getattr(current_task, "name", None)
        request = getattr(current_task, "request", None)
        if not task_name:
            task_name = getattr(request, "task", None)
        task_id = getattr(request, "id", None)

        delivery_info = getattr(request, "delivery_info", None)
        if isinstance(delivery_info, dict):
            routing_key = delivery_info.get("routing_key") or delivery_info.get("queue")
        elif delivery_info is not None:
            routing_key = getattr(delivery_info, "routing_key", None) or getattr(delivery_info, "queue", None)
    except Exception:
        task_name = None
        task_id = None
        routing_key = None

    return coro_name, task_name, task_id, routing_key


def run_coro_sync(coro, timeout: float | None = None):
    effective_timeout = settings.CELERY_RUN_TIMEOUT_SEC if timeout is None else timeout

    wait_timeout = None
    if effective_timeout is not None and float(effective_timeout) > 0:
        wait_timeout = float(effective_timeout)

    runner = _get_worker_loop_runner()
    future = runner.submit(coro)

    try:
        return future.result(timeout=wait_timeout)
    except concurrent.futures.TimeoutError:
        future.cancel()
        coro_name, task_name, task_id, routing_key = _resolve_run_context(coro)
        logger.error(
            "Celery coroutine timed out",
            extra={
                "phase": "run_coro_sync_wait_for",
                "celery_task_name": task_name,
                "celery_task_id": task_id,
                "celery_queue": routing_key,
                "coroutine_name": coro_name,
                "timeout_sec": effective_timeout,
            },
        )
        raise asyncio.TimeoutError


async def _shutdown_async_resources() -> None:
    await close_bot_for_current_loop()
    await close_all_bots()
    await close_redis_pools()


@worker_ready.connect
def _warm_up_worker(sender=None, **_kwargs) -> None:
    logger.info(
        "Celery worker ready: %s",
        getattr(sender, "hostname", "?"),
    )


@worker_shutdown.connect
def _close_telegram_bot_sessions(**_kwargs) -> None:
    global _worker_loop_runner

    runner = _get_existing_worker_loop_runner()
    try:
        if runner is not None and runner.is_running():
            fut = runner.submit(_shutdown_async_resources())
            fut.result(timeout=10)
        else:
            asyncio.run(_shutdown_async_resources())
    except RuntimeError:
        logger.debug("Celery worker shutdown: no loop available for bot close", exc_info=True)
    except Exception:
        logger.exception("Celery worker shutdown: failed to close async resources")
    finally:
        if runner is not None:
            runner.stop()
        with _worker_loop_runner_lock:
            if _worker_loop_runner is runner:
                _worker_loop_runner = None
