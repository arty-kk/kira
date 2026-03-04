#app/tasks/periodic.py
from __future__ import annotations

import asyncio
import logging

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from app.clients.telegram_client import get_bot
from app.config import settings
from app.tasks.celery_app import celery, run_coro_sync
from app.tasks.cleanup import cleanup_nonbuyers
from app.tasks.payments import requeue_pending_outbox, requeue_applied_unnotified_outbox
from app.tasks.refunds import requeue_pending_refund_outbox
from app.services.addons.analytics import generate_and_send_daily_reports
from app.services.addons import (
    start_battle_job, price_fetcher,
    group_ping, personal_ping,
    generate_and_post_tweet,
    generate_and_post_tg
)


logger = logging.getLogger(__name__)
PRICES_POST_TIME_LIMIT_SEC = 180
PRICES_POST_RUN_TIMEOUT_SEC = 170
REFUNDS_REQUEUE_RUN_TIMEOUT_SEC = 170


@celery.task(name="cleanup_nonbuyers")
def cleanup_nonbuyers_task():
    logger.info("cleanup_nonbuyers_task start")
    run_coro_sync(cleanup_nonbuyers())
    logger.info("cleanup_nonbuyers_task done")


@celery.task(name="tg_channel_post")
def tg_channel_post_task():
    logger.info("tg_channel_post_task start")
    run_coro_sync(generate_and_post_tg())
    logger.info("tg_channel_post_task done")


@celery.task(name="analytics_daily")
def analytics_daily_task():
    logger.info("analytics_daily_task start")
    run_coro_sync(generate_and_send_daily_reports())
    logger.info("analytics_daily_task done")


@celery.task(name="battle_job")
def battle_job_task():
    logger.info("battle_job_task start")
    run_coro_sync(start_battle_job())
    logger.info("battle_job_task done")


@celery.task(name="prices_post", time_limit=PRICES_POST_TIME_LIMIT_SEC)
def prices_post_task():
    logger.info("prices_post_task start")

    async def _send_telegram_with_retry(chat_id: int, html: str) -> None:
        bot = get_bot()
        attempt = 1
        while True:
            try:
                await bot.send_message(
                    chat_id,
                    html,
                    disable_web_page_preview=True,
                    parse_mode="HTML",
                )
                return
            except TelegramRetryAfter as e:
                delay = max(1, int(getattr(e, "retry_after", 5)))
                logger.warning("TelegramRetryAfter: sleeping %ss (attempt %d)", delay, attempt)
                await asyncio.sleep(delay)
                attempt += 1
            except TelegramBadRequest as e:
                logger.warning("Telegram BadRequest: %s", e)
                return
            except Exception as e:
                if attempt >= 3:
                    logger.exception("send_message failed after %d attempts: %s", attempt, e)
                    return
                await asyncio.sleep(1.5 * attempt)
                attempt += 1

    async def _inner():
        try:
            data = await asyncio.wait_for(price_fetcher(), timeout=60)
        except asyncio.TimeoutError:
            logger.warning("price_fetcher() timed out after 60s")
            return

        targets = {int(x) for x in (getattr(settings, "ALLOWED_GROUP_IDS", []) or [])}
        if not targets:
            logger.info("prices_post: no targets; skip")
            return
        if not data:
            logger.info("prices_post: no data to send; skip")
            return

        for msg in data:
            for chat_id in targets:
                await _send_telegram_with_retry(chat_id, msg)

        logger.info("prices_post: sent %d message(s)", len(data))

    run_coro_sync(_inner(), timeout=PRICES_POST_RUN_TIMEOUT_SEC)
    logger.info("prices_post_task done")


@celery.task(name="group_ping_job")
def group_ping_job_task():
    logger.info("group_ping_job_task start")
    run_coro_sync(group_ping())
    logger.info("group_ping_job_task done")


@celery.task(name="personal_ping_job")
def personal_ping_job_task():
    logger.info("personal_ping_job_task start")
    run_coro_sync(personal_ping())
    logger.info("personal_ping_job_task done")


@celery.task(name="tweet_once")
def tweet_once_task():
    logger.info("tweet_once_task start")
    run_coro_sync(generate_and_post_tweet())
    logger.info("tweet_once_task done")


@celery.task(name="payments_requeue_pending_outbox")
def payments_requeue_pending_outbox_task():
    logger.info("payments_requeue_pending_outbox_task start")
    run_coro_sync(requeue_pending_outbox())
    logger.info("payments_requeue_pending_outbox_task done")


@celery.task(name="payments_requeue_applied_unnotified_outbox")
def payments_requeue_applied_unnotified_outbox_task():
    logger.info("payments_requeue_applied_unnotified_outbox_task start")
    run_coro_sync(requeue_applied_unnotified_outbox())
    logger.info("payments_requeue_applied_unnotified_outbox_task done")


@celery.task(name="refunds_requeue_pending_outbox")
def refunds_requeue_pending_outbox_task():
    logger.info("refunds_requeue_pending_outbox_task start")
    run_coro_sync(requeue_pending_refund_outbox(), timeout=REFUNDS_REQUEUE_RUN_TIMEOUT_SEC)
    logger.info("refunds_requeue_pending_outbox_task done")
