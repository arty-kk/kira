from __future__ import annotations

import asyncio
import logging

from celery import shared_task

from app.config import settings
from app.core.memory import cache_gender
from app.services.responder.gender import detect_gender
from app.tasks.celery_app import run_coro_sync

logger = logging.getLogger(__name__)

GENDER_DETECT_TIMEOUT = int(getattr(settings, "GENDER_DETECT_TIMEOUT", 8) or 8)
GENDER_DETECT_TEXT_LIMIT = int(getattr(settings, "GENDER_DETECT_TEXT_LIMIT", 1000) or 1000)


@shared_task(
    name="gender.detect",
    bind=True,
    acks_late=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": 2},
    soft_time_limit=GENDER_DETECT_TIMEOUT,
    time_limit=GENDER_DETECT_TIMEOUT + 3,
)
def detect_gender_task(self, *, user_id: int, name: str, text: str) -> str:
    if isinstance(user_id, bool):
        return "invalid_payload"

    try:
        parsed_user_id = int(user_id)
    except (TypeError, ValueError):
        return "invalid_payload"

    if parsed_user_id <= 0:
        return "invalid_payload"

    normalized_name = str(name or "").strip()
    if not normalized_name:
        return "skip"

    normalized_text = str(text or "").strip()
    if GENDER_DETECT_TEXT_LIMIT > 0:
        normalized_text = normalized_text[:GENDER_DETECT_TEXT_LIMIT]

    async def _detect() -> str:
        answer = await asyncio.wait_for(
            detect_gender(normalized_name, normalized_text),
            timeout=float(max(1, GENDER_DETECT_TIMEOUT)),
        )
        return str(answer or "unknown").strip().lower()

    try:
        gender = run_coro_sync(_detect(), timeout=float(max(1, GENDER_DETECT_TIMEOUT + 1)))
    except asyncio.TimeoutError:
        logger.warning(
            "gender.detect timeout user_id=%s",
            parsed_user_id,
            exc_info=True,
        )
        raise

    if gender in ("male", "female"):
        run_coro_sync(cache_gender(parsed_user_id, gender))
        return "cached"

    return "unknown"
