# app/tasks/celery_app.py
from __future__ import annotations

import os

from celery import Celery

from app.config import settings

broker_url = os.getenv("CELERY_BROKER_URL")
if not broker_url:
    raise RuntimeError("Environment variable CELERY_BROKER_URL is required")
if not broker_url.startswith("redis://"):
    raise RuntimeError("CELERY_BROKER_URL must start with redis://")


celery = Celery(
    "galaxytap",
    broker=broker_url,
    backend=None,
    include=["app.tasks.message"],
)


celery.conf.update(
    task_ignore_result=True,
    result_expires=0,
    task_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    broker_transport_options={
        "visibility_timeout": 3600,
        "socket_timeout": 10,
    },
    task_acks_late=False,
    worker_pool_restarts=True,
    worker_prefetch_multiplier=1,
    worker_concurrency=settings.CELERY_CONCURRENCY,
)


@celery.on_after_finalize.connect
def _warm_up(_sender, **_kwargs) -> None:

    import asyncio
    from app.services.responder.rag.knowledge_proc import _init_kb

    try:
        asyncio.run(_init_kb())
    except Exception:
        pass


celery.autodiscover_tasks(["app"])
