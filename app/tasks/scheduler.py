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

from app.config import settings
from app.tasks.periodic import (
    cleanup_nonbuyers_task, analytics_daily_task, battle_job_task,
    prices_post_task, group_ping_job_task, personal_ping_job_task,
    tweet_once_task, tg_channel_post_task,
)
from app.tasks.kb import (
    gc_orphan_api_key_dirs,
)

logger = logging.getLogger(__name__)


cpu_count = os.cpu_count() or 1

try:
    tz_raw = getattr(settings, "DEFAULT_TZ", "UTC")

    try:
        offset_hours = int(tz_raw)
    except (TypeError, ValueError):
        offset_hours = None

    if offset_hours is not None:
        LOCAL_TZ = timezone(timedelta(hours=offset_hours))
    else:
        LOCAL_TZ = ZoneInfo(str(tz_raw))
except Exception:
    LOCAL_TZ = timezone.utc

_sched: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler | None:
    return _sched


def stop_scheduler() -> None:
    global _sched
    if _sched and _sched.running:
        try:
            _sched.shutdown(wait=False)
        except Exception:
            logger.exception("Failed to shutdown scheduler cleanly")

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

        _sched.add_job(
            lambda: tweet_once_task.delay(),
            trigger=DateTrigger(run_date=run_dt),
            id=job_id,
            replace_existing=True,
            max_instances=1,
        )
        created.append(run_dt.isoformat())

    logger.info("Scheduled %d tweet(s) UTC: %s", len(created), ", ".join(created))


def _tg_window_today_to_utc() -> tuple[datetime, datetime]:

    today_local = _now_local().date()

    start_local = datetime.combine(
        today_local,
        time(settings.SCHED_TG_START_HOUR, 0),
        tzinfo=LOCAL_TZ,
    )
    end_local = datetime.combine(
        today_local,
        time(settings.SCHED_TG_END_HOUR, 0),
        tzinfo=LOCAL_TZ,
    )

    if _now_local() >= end_local:
        tomorrow = today_local + timedelta(days=1)
        start_local = datetime.combine(
            tomorrow,
            time(settings.SCHED_TG_START_HOUR, 0),
            tzinfo=LOCAL_TZ,
        )
        end_local = datetime.combine(
            tomorrow,
            time(settings.SCHED_TG_END_HOUR, 0),
            tzinfo=LOCAL_TZ,
        )

    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _dynamic_tg_job_ids() -> list[str]:
    if _sched is None:
        return []
    return [j.id for j in _sched.get_jobs() if j.id.startswith("dynamic_tg_")]


def _schedule_dynamic_tg_posts(count_min: int | None = None, count_max: int | None = None) -> None:

    if _sched is None or not _sched.running:
        logger.warning("Scheduler is not running; skip dynamic tg scheduling")
        return

    for jid in _dynamic_tg_job_ids():
        try:
            _sched.remove_job(jid)
        except Exception:
            logger.warning("Failed to remove TG job %s", jid, exc_info=True)

    now = _now_utc()
    window_start, window_end = _tg_window_today_to_utc()
    total_sec = (window_end - window_start).total_seconds()
    if total_sec <= 0:
        logger.warning("TG window length <= 0; skip scheduling")
        return

    if count_min is None:
        count_min = settings.SCHED_TG_MIN_POSTS
    if count_max is None:
        count_max = settings.SCHED_TG_MAX_POSTS
    if count_max < count_min:
        count_max = count_min

    count = max(1, random.randint(count_min, count_max))
    segment = total_sec / count

    created = []
    for i in range(count):
        offset = random.uniform(0, segment)
        run_dt = window_start + timedelta(seconds=segment * i + offset)
        if run_dt <= now:
            run_dt += timedelta(days=1)

        job_id = f"dynamic_tg_{run_dt.strftime('%Y%m%d%H%M%S')}_{i}"

        _sched.add_job(
            lambda: tg_channel_post_task.delay(),
            trigger=DateTrigger(run_date=run_dt),
            id=job_id,
            replace_existing=True,
            max_instances=1,
        )
        created.append(run_dt.isoformat())

    logger.info("Scheduled %d TG post(s) UTC: %s", len(created), ", ".join(created))


def _listener(event):
    if event.code == EVENT_JOB_ERROR:
        exc = getattr(event, "exception", None)
        tb  = getattr(event, "traceback", "")
        logger.error("Job %s raised: %s\n%s", event.job_id, exc, tb)
    elif event.code == EVENT_JOB_MISSED:
        when = getattr(event, "scheduled_run_time", None)
        logger.warning("Job %s MISSED at %s", event.job_id, when)


def start_scheduler() -> None:
    global _sched

    if _sched is not None and _sched.running:
        logger.info("Scheduler already running; skip second start")
        return

    logger.info("Starting scheduler in timezone %s", LOCAL_TZ)
    try:
        import asyncio
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    _sched = AsyncIOScheduler(
        event_loop=loop,
        timezone=LOCAL_TZ,
        job_defaults={
            "misfire_grace_time": settings.SCHEDULER_MISFIRE_GRACE_TIME,
            "coalesce": True,
            "max_instances": cpu_count * 2,
        },
    )

    _sched.add_listener(_listener, EVENT_JOB_ERROR | EVENT_JOB_MISSED)
    eager = _now_local() + timedelta(seconds=5)

    _sched.add_job(
        lambda: cleanup_nonbuyers_task.delay(),
        "cron",
        hour=3, minute=30,
        id="cleanup_nonbuyers_job",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    if settings.SCHED_ENABLE_KB_GC:
        try:
            kb_gc_hour = int(getattr(settings, "SCHED_KB_GC_HOUR", 4))
        except Exception:
            kb_gc_hour = 4
        kb_gc_hour = max(0, min(23, kb_gc_hour))

        _sched.add_job(
            lambda: gc_orphan_api_key_dirs.delay(),
            "cron",
            hour=kb_gc_hour,
            minute=0,
            id="kb_gc_orphan_dirs",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        logger.info(
            "kb_gc_orphan_dirs job enabled at %02d:00 local time", kb_gc_hour
        )
    else:
        logger.info("kb_gc_orphan_dirs job disabled by SCHED_ENABLE_KB_GC=false")

    if settings.SCHED_ENABLE_ANALYTICS:
        _sched.add_job(
            lambda: analytics_daily_task.delay(),
            "cron",
            hour=0, minute=5,
            timezone=timezone.utc,
            id="analytics_daily_job",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
    else:
        logger.info("analytics_daily_job disabled by SCHED_ENABLE_ANALYTICS=false")

    twitter_enabled = (
        settings.SCHED_ENABLE_TWEETS
        and bool(getattr(settings, "TWITTER_API_KEY", ""))
        and bool(getattr(settings, "TWITTER_API_SECRET", ""))
        and bool(getattr(settings, "TWITTER_ACCESS_TOKEN", ""))
        and bool(getattr(settings, "TWITTER_ACCESS_TOKEN_SECRET", ""))
    )

    if twitter_enabled:
        _sched.add_job(
            _schedule_dynamic_tweets,
            "cron",
            hour=0, minute=0,
            id="tweet_scheduler_job",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
    elif settings.SCHED_ENABLE_TWEETS:
        logger.warning(
            "tweet_scheduler_job not started: SCHED_ENABLE_TWEETS=true but Twitter credentials are missing"
        )
    else:
        logger.info("tweet_scheduler_job disabled by SCHED_ENABLE_TWEETS=false")

    if settings.SCHED_ENABLE_TG_POSTS:
        _sched.add_job(
            _schedule_dynamic_tg_posts,
            "cron",
            hour=0, minute=1,
            id="tg_scheduler_job",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
    else:
        logger.info("tg_scheduler_job disabled by SCHED_ENABLE_TG_POSTS=false")

    if settings.SCHED_ENABLE_BATTLE:
        _sched.add_job(
            lambda: battle_job_task.delay(),
            "interval",
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
            lambda: prices_post_task.delay(),
            "interval",
            hours=8,
            id="prices_post",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
            next_run_time=eager,
        )
    else:
        logger.info("prices_post disabled by SCHED_ENABLE_PRICES=false")

    if settings.SCHED_ENABLE_GROUP_PING:
        _sched.add_job(
            lambda: group_ping_job_task.delay(),
            "interval",
            minutes=settings.GROUP_PING_INTERVAL_MINUTES,
            id="group_ping_job",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
            next_run_time=eager,
        )
    else:
        logger.info("group_ping_job disabled by SCHED_ENABLE_GROUP_PING=false")

    if settings.SCHED_ENABLE_PERSONAL_PING:
        interval_secs = settings.PERSONAL_PING_INTERVAL_SEC
        _sched.add_job(
            lambda: personal_ping_job_task.delay(),
            "interval",
            seconds=interval_secs,
            id="personal_ping_job",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
            next_run_time=eager,
        )
    else:
        logger.info("personal_ping_job disabled by SCHED_ENABLE_PERSONAL_PING=false")

    _sched.start()

    if twitter_enabled and not _dynamic_tweet_job_ids():
        logger.info("No dynamic tweet jobs at startup — scheduling for today.")
        _schedule_dynamic_tweets()
    elif not twitter_enabled:
        logger.info("Dynamic tweet jobs are disabled; skipping initial scheduling.")

    if settings.SCHED_ENABLE_TG_POSTS and not _dynamic_tg_job_ids():
        logger.info("No dynamic TG jobs at startup — scheduling for today.")
        _schedule_dynamic_tg_posts()
    elif not settings.SCHED_ENABLE_TG_POSTS:
        logger.info("Dynamic TG jobs are disabled; skipping initial TG scheduling.")

    for job in _sched.get_jobs():
        logger.info("Job %s → next run at %s", job.id, job.next_run_time)