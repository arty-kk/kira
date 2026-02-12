import unittest
from datetime import datetime, timezone
import importlib.util
import pathlib
import sys
import types


def _load_scheduler():
    fake_app = types.ModuleType("app")
    fake_tasks = types.ModuleType("app.tasks")
    fake_periodic = types.ModuleType("app.tasks.periodic")
    fake_kb = types.ModuleType("app.tasks.kb")
    fake_config = types.ModuleType("app.config")

    settings = types.SimpleNamespace(
        DEFAULT_TZ="UTC",
        SCHED_TG_START_HOUR=22,
        SCHED_TG_END_HOUR=2,
    )
    fake_config.settings = settings

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


if __name__ == "__main__":
    unittest.main()
