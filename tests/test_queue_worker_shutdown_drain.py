import asyncio
import importlib.util
import pathlib
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


def _load_queue_worker():
    fake_app = types.ModuleType("app")
    fake_config = types.ModuleType("app.config")
    fake_bot = types.ModuleType("app.bot")
    fake_bot_utils = types.ModuleType("app.bot.utils")
    fake_bot_debouncer = types.ModuleType("app.bot.utils.debouncer")
    fake_bot_components = types.ModuleType("app.bot.components")
    fake_bot_constants = types.ModuleType("app.bot.components.constants")
    fake_clients = types.ModuleType("app.clients")
    fake_tg_client = types.ModuleType("app.clients.telegram_client")
    fake_openai_client = types.ModuleType("app.clients.openai_client")
    fake_services = types.ModuleType("app.services")
    fake_responder = types.ModuleType("app.services.responder")
    fake_addons = types.ModuleType("app.services.addons")
    fake_voice = types.ModuleType("app.services.addons.voice_generator")
    fake_mod = types.ModuleType("app.services.addons.passive_moderation")
    fake_analytics = types.ModuleType("app.services.addons.analytics")
    fake_user = types.ModuleType("app.services.user")
    fake_user_service = types.ModuleType("app.services.user.user_service")
    fake_core = types.ModuleType("app.core")
    fake_memory = types.ModuleType("app.core.memory")
    fake_queue_recovery = types.ModuleType("app.core.queue_recovery")

    fake_aiogram = types.ModuleType("aiogram")
    fake_aiogram_enums = types.ModuleType("aiogram.enums")
    fake_aiogram_types = types.ModuleType("aiogram.types")
    fake_aiogram_exceptions = types.ModuleType("aiogram.exceptions")

    class _ChatAction:
        TYPING = "typing"

    class _Message:
        pass

    class _Exc(Exception):
        pass

    fake_aiogram_enums.ChatAction = _ChatAction
    fake_aiogram_types.Message = _Message
    fake_aiogram_exceptions.TelegramBadRequest = _Exc
    fake_aiogram_exceptions.TelegramRetryAfter = _Exc
    fake_aiogram_exceptions.TelegramNetworkError = _Exc
    fake_aiogram_exceptions.TelegramForbiddenError = _Exc

    fake_config.settings = types.SimpleNamespace(
        CHATTY_MODE=True,
        TG_TEXT_LIMIT=4096,
        REDIS_URL="redis://local",
        REDIS_URL_QUEUE="redis://local",
        OPENAI_MAX_CONCURRENT_REQUESTS=4,
        QUEUE_KEY="q:in",
        RESPOND_TIMEOUT=5,
        MODERATION_STATUS_WAIT_SEC=1.2,
        MODERATION_STATUS_POLL_SEC=0.1,
        MODERATION_SIGNAL_REQUEUE_MAX_ATTEMPTS=3,
        MODERATION_SIGNAL_REQUEUE_MAX_WAIT_SEC=60,
        MODERATION_SIGNAL_INFLIGHT_REQUEUE_MAX_WAIT_SEC=60,
    )

    fake_bot_debouncer.compute_typing_delay = lambda *_args, **_kwargs: 0.0
    fake_tg_client.get_bot = lambda: types.SimpleNamespace(session=types.SimpleNamespace(close=AsyncMock()))
    fake_openai_client.get_openai = lambda: None
    fake_openai_client.transcribe_audio_with_retry = lambda **_kwargs: ""
    fake_openai_client.classify_openai_error = lambda _exc: "other"
    fake_clients.openai_client = fake_openai_client

    async def _respond_ok(*_args, **_kwargs):
        return "ok"

    async def _noop_async(*_args, **_kwargs):
        return None

    fake_responder.respond_to_user = _respond_ok
    fake_voice.maybe_tts_and_send = _noop_async
    fake_voice.shutdown_tts = _noop_async
    fake_voice.will_speak = lambda **_kwargs: False
    fake_voice.is_tts_eligible_short = lambda *_args, **_kwargs: False
    fake_mod.split_context_text = lambda text, entities, allow_web=False: (text, entities)
    fake_analytics.record_timeout = _noop_async
    fake_user_service.confirm_reservation_by_id = _noop_async
    fake_user_service.refund_reservation_by_id = _noop_async

    fake_memory.get_redis = lambda: None
    fake_memory.get_redis_queue = lambda: None
    fake_memory.close_redis_pools = _noop_async
    fake_memory.SafeRedis = object
    fake_memory.push_message = _noop_async

    async def _fake_requeue_processing_on_start(*_args, **_kwargs):
        return types.SimpleNamespace(moved_count=0, lock_acquired=True)

    fake_queue_recovery.requeue_processing_on_start = _fake_requeue_processing_on_start

    module_overrides = {
        "app": fake_app,
        "app.config": fake_config,
        "app.bot": fake_bot,
        "app.bot.utils": fake_bot_utils,
        "app.bot.utils.debouncer": fake_bot_debouncer,
        "app.bot.components": fake_bot_components,
        "app.bot.components.constants": fake_bot_constants,
        "app.clients": fake_clients,
        "app.clients.telegram_client": fake_tg_client,
        "app.clients.openai_client": fake_openai_client,
        "app.services": fake_services,
        "app.services.responder": fake_responder,
        "app.services.addons": fake_addons,
        "app.services.addons.voice_generator": fake_voice,
        "app.services.addons.passive_moderation": fake_mod,
        "app.services.addons.analytics": fake_analytics,
        "app.services.user": fake_user,
        "app.services.user.user_service": fake_user_service,
        "app.core": fake_core,
        "app.core.memory": fake_memory,
        "app.core.queue_recovery": fake_queue_recovery,
        "aiogram": fake_aiogram,
        "aiogram.enums": fake_aiogram_enums,
        "aiogram.types": fake_aiogram_types,
        "aiogram.exceptions": fake_aiogram_exceptions,
    }

    worker_path = pathlib.Path(__file__).resolve().parents[1] / "app" / "tasks" / "queue_worker.py"
    spec = importlib.util.spec_from_file_location("queue_worker_shutdown_test", worker_path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, module_overrides):
        sys.modules["queue_worker_shutdown_test"] = module
        spec.loader.exec_module(module)
    return module


queue_worker = _load_queue_worker()


class QueueWorkerShutdownDrainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._original_processing_tasks = queue_worker.PROCESSING_TASKS
        queue_worker.PROCESSING_TASKS = set()

    async def asyncTearDown(self):
        queue_worker.PROCESSING_TASKS = self._original_processing_tasks

    async def test_async_main_drains_pending_and_logs_task_failure_before_shutdown(self):
        events = []

        async def _fake_worker(stop_evt):
            stop_evt.set()
            await asyncio.sleep(3600)

        async def _fake_cleanup_loop(_stop_evt):
            await asyncio.sleep(3600)

        async def _raise_runtime_after_cancel():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                raise RuntimeError("shutdown drain boom")

        async def _cancel_cleanly():
            await asyncio.sleep(3600)

        fail_task = asyncio.create_task(_raise_runtime_after_cancel())
        cancel_task = asyncio.create_task(_cancel_cleanly())
        queue_worker.PROCESSING_TASKS = {fail_task, cancel_task}

        async def _fake_wait(tasks, timeout):
            self.assertEqual(timeout, 15)
            self.assertSetEqual(set(tasks), {fail_task, cancel_task})
            return set(), {fail_task, cancel_task}

        async def _close_session():
            events.append("bot_close")

        async def _shutdown_tts():
            self.assertTrue(fail_task.done())
            self.assertTrue(cancel_task.done())
            events.append("shutdown_tts")

        async def _close_redis():
            self.assertTrue(fail_task.done())
            self.assertTrue(cancel_task.done())
            events.append("close_redis")

        queue_worker.BOT = types.SimpleNamespace(session=types.SimpleNamespace(close=AsyncMock(side_effect=_close_session)))

        with (
            patch.object(queue_worker, "queue_worker", _fake_worker),
            patch.object(queue_worker, "_cleanup_chat_locks_loop", _fake_cleanup_loop),
            patch.object(queue_worker.asyncio, "wait", AsyncMock(side_effect=_fake_wait)),
            patch.object(queue_worker, "shutdown_tts", AsyncMock(side_effect=_shutdown_tts)),
            patch.object(queue_worker, "close_redis_pools", AsyncMock(side_effect=_close_redis)),
            patch.object(queue_worker.logger, "warning") as warning_log,
        ):
            await queue_worker._async_main()

        self.assertTrue(fail_task.done())
        self.assertTrue(cancel_task.done())
        self.assertEqual(events, ["bot_close", "shutdown_tts", "close_redis"])

        self.assertGreaterEqual(warning_log.call_count, 1)
        logged_task_reprs = [call.args[1] for call in warning_log.call_args_list if len(call.args) > 1]
        self.assertIn(repr(fail_task), logged_task_reprs)
