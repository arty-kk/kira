#app/tasks/celery_app.py
from __future__ import annotations

import os
import asyncio
import logging

from celery import Celery
from celery.signals import setup_logging as celery_setup_logging, worker_ready

from app.config import settings
from app.services.responder.rag.knowledge_proc import _init_kb
from app.core.logging_config import setup_logging
from app.tasks.utils.bg_loop import get_bg_loop

setup_logging()
logger = logging.getLogger(__name__)


@celery_setup_logging.connect
def _disable_celery_autologger(**_kwargs):
    pass


broker_url = settings.CELERY_BROKER_URL or os.getenv("CELERY_BROKER_URL")
if not broker_url:
    raise RuntimeError("Environment variable CELERY_BROKER_URL is required")
if not broker_url.startswith("redis://"):
    raise RuntimeError("CELERY_BROKER_URL must start with redis://")


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
        "app.tasks.kb"
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
)


def _run(coro):
    loop = get_bg_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result()


@worker_ready.connect
def _warm_up_worker(sender=None, **_kwargs) -> None:
    try:
        _run(_init_kb())
        logger.info(
            "Knowledge base initialized in Celery worker %s",
            getattr(sender, "hostname", "?"),
        )
    except Exception:
        logger.exception(
            "Failed to initialize knowledge base in worker process"
        )