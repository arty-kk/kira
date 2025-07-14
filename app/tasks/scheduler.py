cat > app/tasks/scheduler.py << EOF
#app/tasks/scheduler.py
from __future__ import annotations

import logging
import random

from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram.exceptions import TelegramBadRequest
from apscheduler.triggers.date import DateTrigger

from app.config import settings
from app.clients.telegram_client import get_bot   
from app.services.addons import (
    start_battle_job, price_fetcher, group_ping, 
    personal_ping, generate_and_post_tweet
)

logger = logging.getLogger(__name__)

bot = get_bot()

sched = AsyncIOScheduler(timezone=ZoneInfo("Europe/Riga"))

@sched.scheduled_job(
    "cron", 
    hour=0, 
    minute=0, 
    id="tweet_scheduler_job"
)
async def tweet_scheduler_job():

    for job in sched.get_jobs():
        if job.id.startswith("dynamic_tweet_"):
            sched.remove_job(job.id)

    count = random.randint(1, 3)
    now = datetime.now(timezone.utc)
    window_start = datetime(now.year, now.month, now.day, 11, 0, tzinfo=timezone.utc)
    window_end = datetime(now.year, now.month, now.day, 23, 0, tzinfo=timezone.utc)
    total_sec = (window_end - window_start).total_seconds()
    segment = total_sec / count

    for i in range(count):
        offset = random.uniform(0, segment)
        run_dt = window_start + timedelta(seconds=segment * i + offset)
        if run_dt <= now:
            run_dt += timedelta(days=1)

        job_id = f"dynamic_tweet_{run_dt.strftime('%Y%m%d%H%M')}_{i}"
        sched.add_job(
            generate_and_post_tweet,
            trigger=DateTrigger(run_date=run_dt),
            id=job_id,
            max_instances=1,
            coalesce=True,
        )
        logger.info("Scheduled tweet at %s UTC (job id=%s)", run_dt.isoformat(), job_id)



@sched.scheduled_job(
    "interval",
    hours=1,
    id="battle_job",
    max_instances=1,
    coalesce=True,
)
async def battle_job():
    try:
        await start_battle_job()
    except Exception:
        logger.exception("battle_job error")


@sched.scheduled_job(
    "interval", 
    hours=4, 
    id="prices_post", 
    max_instances=1, 
    coalesce=True
)
async def prices_post():
    try:
        for msg in await price_fetcher():
            try:
                await bot.send_message(
                    settings.ALLOWED_GROUP_ID,
                    msg,
                    disable_web_page_preview=True,
                    parse_mode="HTML",
                )
            except TelegramBadRequest as e:
                logger.warning("prices_post BadRequest: %s", e)
        logger.info("prices_post done")
    except Exception:
        logger.exception("prices_post error")

@sched.scheduled_job(
    "interval",
    minutes=settings.PING_INTERVAL_MINUTES,
    id="group_ping_job",
    max_instances=1,
    coalesce=True,
    jitter=int(settings.PING_INTERVAL_MINUTES * 60 * 0.3),
)
async def group_ping_job():
    try:
        await group_ping()
    except Exception:
        logger.exception("group_ping_job error")


@sched.scheduled_job(
    "interval",
    minutes=settings.PERSONAL_PING_INTERVAL_MIN,
    id="personal_ping_job",
    max_instances=1,
    coalesce=True,
    jitter=int(settings.PERSONAL_PING_INTERVAL_MIN * 60 * 0.3),
)
async def personal_ping_job():
    try:
        await personal_ping()
    except Exception:
        logger.exception("personal_ping_job error")


def start_scheduler() -> None:
    logger.info("Starting scheduler with jobs: %s", sched.get_jobs())
    sched.start()
EOF