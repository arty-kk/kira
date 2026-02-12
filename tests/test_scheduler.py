import importlib.util
import pathlib
import sys
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import patch


def _load_scheduler():
    fake_app = types.ModuleType("app")
    fake_tasks = types.ModuleType("app.tasks")
    fake_periodic = types.ModuleType("app.tasks.periodic")
    fake_kb = types.ModuleType("app.tasks.kb")
    fake_config = types.ModuleType("app.config")
    fake_clients = types.ModuleType("app.clients")
    fake_twitter_client = types.ModuleType("app.clients.twitter_client")

    settings = types.SimpleNamespace(
        DEFAULT_TZ="UTC",
        SCHED_TG_START_HOUR=22,
        SCHED_TG_END_HOUR=2,
    )
    fake_config.settings = settings
    fake_twitter_client.is_twitter_configured = lambda: False

    dummy_task = types.SimpleNamespace(delay=lambda: None)
    fake_periodic.cleanup_nonbuyers_task = dummy_task
    fake_periodic.analytics_daily_task = dummy_task
    fake_periodic.battle_job_task = dummy_task
    fake_periodic.prices_post_task = dummy_task
    fake_periodic.group_ping_job_task = dummy_task
    fake_periodic.personal_ping_job_task = dummy_task
    fake_periodic.tweet_once_task = dummy_task
    fake_periodic.tg_channel_post_task = dummy_task
    fake_periodic.payments_requeue_pending_outbox_task = dummy_task
    fake_periodic.payments_requeue_applied_unnotified_outbox_task = dummy_task
    fake_periodic.refunds_requeue_pending_outbox_task = dummy_task
    fake_kb.gc_orphan_api_key_dirs = dummy_task

    patch_modules = {
        "app": fake_app,
        "app.tasks": fake_tasks,
        "app.tasks.periodic": fake_periodic,
        "app.tasks.kb": fake_kb,
        "app.config": fake_config,
        "app.clients": fake_clients,
        "app.clients.twitter_client": fake_twitter_client,
    }
    previous = {name: sys.modules.get(name) for name in patch_modules}

    try:
        sys.modules.update(patch_modules)
        scheduler_path = pathlib.Path(__file__).resolve().parents[1] / "app" / "tasks" / "scheduler.py"
        spec = importlib.util.spec_from_file_location("scheduler_under_test", scheduler_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["scheduler_under_test"] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, old in previous.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


scheduler = _load_scheduler()


class SchedulerWindowTests(unittest.TestCase):
    def test_tg_window_crosses_midnight(self) -> None:
        scheduler._init_scheduler_context()
        tz = scheduler._local_tz or timezone.utc
        original_now_local = scheduler._now_local
        scheduler._now_local = lambda: datetime(2024, 1, 1, 12, 0, tzinfo=tz)
        try:
            window_start, window_end = scheduler._tg_window_today_to_utc()
        finally:
            scheduler._now_local = original_now_local

        expected_start = datetime(2024, 1, 1, 22, 0, tzinfo=tz)
        expected_end = datetime(2024, 1, 2, 2, 0, tzinfo=tz)

        self.assertEqual(window_start, expected_start.astimezone(timezone.utc))
        self.assertEqual(window_end, expected_end.astimezone(timezone.utc))
        self.assertGreater((window_end - window_start).total_seconds(), 0)

    def test_tg_window_shifts_after_end(self) -> None:
        scheduler._init_scheduler_context()
        tz = scheduler._local_tz or timezone.utc
        original_now_local = scheduler._now_local
        scheduler._now_local = lambda: datetime(2024, 1, 2, 3, 0, tzinfo=tz)
        try:
            window_start, window_end = scheduler._tg_window_today_to_utc()
        finally:
            scheduler._now_local = original_now_local

        expected_start = datetime(2024, 1, 2, 22, 0, tzinfo=tz)
        expected_end = datetime(2024, 1, 3, 2, 0, tzinfo=tz)

        self.assertEqual(window_start, expected_start.astimezone(timezone.utc))
        self.assertEqual(window_end, expected_end.astimezone(timezone.utc))


class SchedulerTwitterConfigTests(unittest.TestCase):
    def test_tweet_scheduler_not_started_when_twitter_config_incomplete(self) -> None:
        scheduler._sched = None
        scheduler.settings.SCHED_ENABLE_TWEETS = True
        scheduler.settings.SCHED_ENABLE_KB_GC = False
        scheduler.settings.SCHED_ENABLE_ANALYTICS = False
        scheduler.settings.SCHED_ENABLE_TG_POSTS = False
        scheduler.settings.SCHED_ENABLE_BATTLE = False
        scheduler.settings.SCHED_ENABLE_PRICES = False
        scheduler.settings.SCHED_ENABLE_GROUP_PING = False
        scheduler.settings.SCHED_ENABLE_PERSONAL_PING = False
        scheduler.settings.SCHEDULER_MISFIRE_GRACE_TIME = 30

        class FakeScheduler:
            def __init__(self, *args, **kwargs):
                self.jobs = []
                self.running = False

            def add_listener(self, *args, **kwargs):
                return None

            def add_job(self, func, trigger=None, id=None, **kwargs):
                self.jobs.append(types.SimpleNamespace(id=id, next_run_time=None))

            def get_jobs(self):
                return list(self.jobs)

            def start(self):
                self.running = True

            def shutdown(self, wait=False):
                self.running = False

        with patch.object(scheduler, "AsyncIOScheduler", FakeScheduler), patch.object(
            scheduler, "is_twitter_configured", return_value=False
        ), patch.object(scheduler.logger, "warning") as warning_mock:
            scheduler.start_scheduler()

        self.assertIsNotNone(scheduler._sched)
        job_ids = {job.id for job in scheduler._sched.get_jobs()}
        self.assertNotIn("tweet_scheduler_job", job_ids)
        warning_mock.assert_any_call(
            "tweet_scheduler_job disabled: SCHED_ENABLE_TWEETS=true but scheduler is disabled due to incomplete Twitter config"
        )

        scheduler.stop_scheduler()
        scheduler._sched = None


if __name__ == "__main__":
    unittest.main()
