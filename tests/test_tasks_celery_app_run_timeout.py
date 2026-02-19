import concurrent.futures
import importlib.util
import pathlib
import sys
import types
import unittest
from unittest.mock import Mock, patch


class _DummyFuture:
    def __init__(self):
        self.result_calls = []
        self.cancel_calls = 0

    def result(self, timeout=None):
        self.result_calls.append(timeout)
        raise concurrent.futures.TimeoutError("stuck")

    def cancel(self):
        self.cancel_calls += 1
        return True


class _DummyCelery:
    def __init__(self, *_args, **_kwargs):
        self.conf = types.SimpleNamespace(update=lambda **_kwargs: None)

    def task(self, *_args, **_kwargs):
        def _decorator(func):
            return func

        return _decorator


class _Signal:
    def connect(self, func):
        return func


def _load_celery_app_module():
    module_name = "tasks_celery_app_under_test"
    target_modules = {
        "app": types.ModuleType("app"),
        "app.config": types.ModuleType("app.config"),
        "app.core": types.ModuleType("app.core"),
        "app.core.logging_config": types.ModuleType("app.core.logging_config"),
        "app.services": types.ModuleType("app.services"),
        "app.services.responder": types.ModuleType("app.services.responder"),
        "app.services.responder.rag": types.ModuleType("app.services.responder.rag"),
        "app.services.responder.rag.knowledge_proc": types.ModuleType("app.services.responder.rag.knowledge_proc"),
        "app.tasks": types.ModuleType("app.tasks"),
        "app.tasks.utils": types.ModuleType("app.tasks.utils"),
        "app.tasks.utils.bg_loop": types.ModuleType("app.tasks.utils.bg_loop"),
        "celery": types.ModuleType("celery"),
        "celery.signals": types.ModuleType("celery.signals"),
    }

    target_modules["app.config"].settings = types.SimpleNamespace(
        CELERY_BROKER_URL="redis://localhost:6379/0",
        CELERY_CONCURRENCY=1,
        CELERY_DEFAULT_QUEUE="celery",
        CELERY_MEDIA_QUEUE="media",
        CELERY_MODERATION_QUEUE="moderation",
        CELERY_RUN_TIMEOUT_SEC=12.5,
    )
    target_modules["app.core.logging_config"].setup_logging = lambda: None

    async def _init_kb():
        return None

    target_modules["app.services.responder.rag.knowledge_proc"]._init_kb = _init_kb
    target_modules["app.tasks.utils.bg_loop"].get_bg_loop = lambda: object()
    target_modules["celery"].Celery = _DummyCelery
    target_modules["celery"].current_task = types.SimpleNamespace(name="payments.process_outbox", request=None)
    target_modules["celery.signals"].setup_logging = _Signal()
    target_modules["celery.signals"].worker_ready = _Signal()

    previous = {}
    names = set(target_modules) | {module_name}
    for name in names:
        previous[name] = sys.modules.get(name)
        sys.modules.pop(name, None)

    try:
        sys.modules.update(target_modules)
        module_path = pathlib.Path(__file__).resolve().parents[1] / "app" / "tasks" / "celery_app.py"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name in names:
            sys.modules.pop(name, None)
            if previous[name] is not None:
                sys.modules[name] = previous[name]


class CeleryRunTimeoutTests(unittest.TestCase):
    def test_run_timeout_cancels_future_logs_and_reraises(self):
        module = _load_celery_app_module()
        future = _DummyFuture()

        async def _never_finishes():
            return None

        coro = _never_finishes()
        logger_mock = Mock()
        try:
            with (
                patch.object(module.asyncio, "run_coroutine_threadsafe", return_value=future),
                patch.object(module, "logger", logger_mock),
            ):
                with self.assertRaises(concurrent.futures.TimeoutError):
                    module._run(coro)
        finally:
            coro.close()

        self.assertEqual(future.result_calls, [12.5])
        self.assertEqual(future.cancel_calls, 1)

        logger_mock.error.assert_called_once()
        _, kwargs = logger_mock.error.call_args
        self.assertEqual(kwargs["extra"]["celery_task_name"], "payments.process_outbox")
        self.assertEqual(kwargs["extra"]["coroutine_name"], "CeleryRunTimeoutTests.test_run_timeout_cancels_future_logs_and_reraises.<locals>._never_finishes")
        self.assertEqual(kwargs["extra"]["timeout_sec"], 12.5)
        self.assertTrue(kwargs["extra"]["future_cancelled"])


if __name__ == "__main__":
    unittest.main()
