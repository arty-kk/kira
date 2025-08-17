cat > app/tasks/scheduler.py << 'EOF'
#app/tasks/scheduler.py
from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import datetime, timedelta, time, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from app.config import settings
from app.clients.telegram_client import get_bot
from app.services.addons import (
    start_battle_job,
    price_fetcher,
    group_ping,
    personal_ping,
    generate_and_post_tweet,
)

logger = logging.getLogger(__name__)


_TWITTER_SEMA = asyncio.Semaphore(1)
_PRICES_SEMA = asyncio.Semaphore(1)
_GROUP_SEMA = asyncio.Semaphore(1)
_PERSONAL_SEMA = asyncio.Semaphore(1)

cpu_count = os.cpu_count()
LOCAL_TZ = ZoneInfo(settings.SCHEDULER_TIMEZONE)

_sched: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler | None:
    return _sched


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def _tweet_window_today_to_utc() -> tuple[datetime, datetime]:
    today_local = _now_local().date()
    start_local = datetime.combine(today_local, time(11, 0), tzinfo=LOCAL_TZ)
    end_local   = datetime.combine(today_local, time(23, 0), tzinfo=LOCAL_TZ)
    if _now_local() >= end_local:
        tomorrow  = today_local + timedelta(days=1)
        start_local = datetime.combine(tomorrow, time(11, 0), tzinfo=LOCAL_TZ)
        end_local   = datetime.combine(tomorrow, time(23, 0), tzinfo=LOCAL_TZ)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _dynamic_tweet_job_ids() -> list[str]:
    if _sched is None:
        return []
    return [j.id for j in _sched.get_jobs() if j.id.startswith("dynamic_tweet_")]


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
            await asyncio.sleep(delay); attempt += 1
        except TelegramBadRequest as e:
            logger.warning("Telegram BadRequest: %s", e)
            return
        except Exception as e:
            if attempt >= 3:
                logger.exception("send_message failed after %d attempts: %s", attempt, e)
                return
            await asyncio.sleep(1.5 * attempt); attempt += 1


def _schedule_dynamic_tweets(count_min: int = 6, count_max: int = 10) -> None:
    if _sched is None or not _sched.running:
        logger.warning("Scheduler is not running; skip dynamic tweets scheduling")
        return

    ids = _dynamic_tweet_job_ids()
    for jid in ids:
        try:
            _sched.remove_job(jid)
        except Exception:
            logger.warning("Failed to remove job %s", jid, exc_info=True)

    now = _now_utc()
    window_start, window_end = _tweet_window_today_to_utc()
    total_sec = (window_end - window_start).total_seconds()
    if total_sec <= 0:
        logger.warning("Tweet window length <= 0; skip scheduling")
        return

    count = max(1, random.randint(count_min, count_max))
    segment = total_sec / count

    created = []
    for i in range(count):
        offset = random.uniform(0, segment)
        run_dt = window_start + timedelta(seconds=segment * i + offset)
        if run_dt <= now:
            run_dt += timedelta(days=1)

        job_id = f"dynamic_tweet_{run_dt.strftime('%Y%m%d%H%M%S')}_{i}"

        async def _tweet_wrapper():
            async with _TWITTER_SEMA:
                await generate_and_post_tweet()

        _sched.add_job(
            _tweet_wrapper,
            trigger=DateTrigger(run_date=run_dt),
            id=job_id,
            replace_existing=True,
            max_instances=1,
        )
        created.append(run_dt.isoformat())

    logger.info("Scheduled %d tweet(s) UTC: %s", len(created), ", ".join(created))


def _listener(event):
    if event.code == EVENT_JOB_ERROR:
        exc = getattr(event, "exception", None)
        tb  = getattr(event, "traceback", "")
        logger.error("Job %s raised: %s\n%s", event.job_id, exc, tb)
    elif event.code == EVENT_JOB_MISSED:
        when = getattr(event, "scheduled_run_time", None)
        logger.warning("Job %s MISSED at %s", event.job_id, when)


async def tweet_scheduler_job():
    logger.info("→ tweet_scheduler_job start")
    try:
        _schedule_dynamic_tweets()
    except Exception:
        logger.exception("tweet_scheduler_job error")
    logger.info("← tweet_scheduler_job done")

async def battle_job():
    logger.info("→ battle_job start")
    try:
        await start_battle_job()
    except Exception:
        logger.exception("battle_job error")
    logger.info("← battle_job done")

async def prices_post():
    logger.info("→ prices_post start")
    try:
        try:
            data = await asyncio.wait_for(price_fetcher(), timeout=60)
        except asyncio.TimeoutError:
            logger.warning("price_fetcher() timed out after 60s")
            return

        async with _PRICES_SEMA:
            for msg in data:
                await _send_telegram_with_retry(settings.ALLOWED_GROUP_ID, msg)

        logger.info("prices_post done (%d message(s))", len(data))
    except Exception:
        logger.exception("prices_post error")
    logger.info("← prices_post end")

async def group_ping_job():
    logger.info("→ group_ping_job start")
    try:
        async with _GROUP_SEMA:
            await group_ping()
    except Exception:
        logger.exception("group_ping_job error")
    logger.info("← group_ping_job done")

async def personal_ping_job():
    logger.info("→ personal_ping_job start")
    try:
        async with _PERSONAL_SEMA:
            await personal_ping()
    except Exception:
        logger.exception("personal_ping_job error")
    logger.info("← personal_ping_job done")


def start_scheduler() -> None:

    global _sched

    logger.info("Starting scheduler in timezone %s", LOCAL_TZ)
    loop = asyncio.get_running_loop()

    _sched = AsyncIOScheduler(
        event_loop=loop,
        timezone=LOCAL_TZ,
        job_defaults={
            "misfire_grace_time": getattr(settings, "SCHEDULER_MISFIRE_GRACE_TIME", 600),
            "coalesce": True,
            "max_instances": cpu_count * 2,
        },
    )

    _sched.add_listener(_listener, EVENT_JOB_ERROR | EVENT_JOB_MISSED)

    eager = _now_local() + timedelta(seconds=5)

    if settings.SCHED_ENABLE_TWEETS:
        _sched.add_job(
            tweet_scheduler_job, "cron",
            hour=0, minute=0,
            id="tweet_scheduler_job",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
    else:
        logger.info("tweet_scheduler_job disabled by SCHED_ENABLE_TWEETS=false")

    if settings.SCHED_ENABLE_BATTLE:
        _sched.add_job(
            battle_job, "interval",
            hours=1,
            id="battle_job",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
            next_run_time=eager,
        )
    else:
        logger.info("battle_job disabled by SCHED_ENABLE_BATTLE=false")

    if settings.SCHED_ENABLE_PRICES:
        _sched.add_job(
            prices_post, "interval",
            hours=8,
            id="prices_post",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
            jitter=int(4 * 60 * 60 * 0.05),
            next_run_time=eager,
        )
    else:
        logger.info("prices_post disabled by SCHED_ENABLE_PRICES=false")

    if settings.SCHED_ENABLE_GROUP_PING:
        _sched.add_job(
            group_ping_job, "interval",
            minutes=settings.GROUP_PING_INTERVAL_MINUTES,
            id="group_ping_job",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
            jitter=int(settings.GROUP_PING_INTERVAL_MINUTES * 60 * 0.1),
            next_run_time=eager,
        )
    else:
        logger.info("group_ping_job disabled by SCHED_ENABLE_GROUP_PING=false")

    if settings.SCHED_ENABLE_PERSONAL_PING:
        _sched.add_job(
            personal_ping_job, "interval",
            minutes=settings.PERSONAL_PING_INTERVAL_MIN,
            id="personal_ping_job",
            max_instances=20,
            coalesce=True,
            replace_existing=True,
            jitter=int(settings.PERSONAL_PING_INTERVAL_MIN * 60 * 0.1),
            next_run_time=eager,
        )
    else:
        logger.info("personal_ping_job disabled by SCHED_ENABLE_PERSONAL_PING=false")

    _sched.start()

    if settings.SCHED_ENABLE_TWEETS and not _dynamic_tweet_job_ids():
        logger.info("No dynamic tweet jobs at startup — scheduling for today.")
        _schedule_dynamic_tweets()
    elif not settings.SCHED_ENABLE_TWEETS:
        logger.info("Tweet jobs are disabled; skipping initial scheduling.")

    for job in _sched.get_jobs():
        logger.info("Job %s → next run at %s", job.id, job.next_run_time)
EOF