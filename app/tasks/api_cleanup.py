#app/tasks/api_cleanup.py
from __future__ import annotations

import logging

from app.tasks.celery_app import celery, run_coro_sync
from app.core.memory import cleanup_api_key_memory

logger = logging.getLogger(__name__)


@celery.task(name="api.cleanup_memory_for_key", ignore_result=True)
def cleanup_memory_for_key(api_key_id: int) -> None:
    try:
        deleted = run_coro_sync(cleanup_api_key_memory(int(api_key_id)))
        logger.info(
            "api.cleanup_memory_for_key: api_key_id=%s deleted_keys=%s",
            api_key_id,
            deleted,
        )
    except Exception:
        logger.exception(
            "api.cleanup_memory_for_key failed api_key_id=%s",
            api_key_id,
        )