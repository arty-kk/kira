import importlib.util
import pathlib
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch


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
    )

    fake_bot_debouncer.compute_typing_delay = lambda *_args, **_kwargs: 0.0
    fake_tg_client.get_bot = lambda: types.SimpleNamespace()
    fake_openai_client.get_openai = lambda: None
    fake_openai_client.transcribe_audio_with_retry = lambda **_kwargs: ""
    fake_openai_client.classify_openai_error = lambda _exc: "other"
    fake_clients.openai_client = fake_openai_client

    async def _respond_ok(*_args, **_kwargs):
        return "ok"

    fake_responder.respond_to_user = _respond_ok

    async def _noop_async(*_args, **_kwargs):
        return None

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
    fake_queue_recovery.QueueRecoveryResult = lambda moved_count, lock_acquired: types.SimpleNamespace(
        moved_count=moved_count,
        lock_acquired=lock_acquired,
    )

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
    spec = importlib.util.spec_from_file_location("queue_worker_recovery_test", worker_path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, module_overrides):
        sys.modules["queue_worker_recovery_test"] = module
        spec.loader.exec_module(module)
    return module


queue_worker = _load_queue_worker()


class _BootRedis:
    async def brpoplpush(self, *_args, **_kwargs):
        raise Exception("generic read failure")


class _RecoveredRedis(_BootRedis):
    def __init__(self):
        self.calls = 0

    async def brpoplpush(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return "payload"
        return None




class _QueueWithPayload(_BootRedis):
    def __init__(self):
        self.calls = 0

    async def brpoplpush(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return "payload"
        return None

class QueueWorkerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_generic_queue_read_error_triggers_recovery_and_loop_continues(self):
        stop_evt = queue_worker.asyncio.Event()
        boot_redis = _BootRedis()
        recovered_redis = _RecoveredRedis()

        queue_worker.REDIS_QUEUE = boot_redis
        close_redis_pools = AsyncMock(return_value=None)
        try_start = AsyncMock(side_effect=lambda *_args, **_kwargs: stop_evt.set())

        async def _fake_sweeper(stop_evt_arg, *_args, **_kwargs):
            await stop_evt_arg.wait()

        with (
            patch.object(queue_worker, "close_redis_pools", close_redis_pools),
            patch.object(queue_worker, "get_redis_queue", lambda: recovered_redis),
            patch.object(queue_worker, "_jitter", lambda *_args, **_kwargs: 0),
            patch.object(queue_worker, "_sweeper_loop", _fake_sweeper),
            patch.object(queue_worker, "_try_start_task_or_requeue", try_start),
            patch.object(
                queue_worker,
                "requeue_processing_on_start",
                AsyncMock(return_value=types.SimpleNamespace(moved_count=0, lock_acquired=True)),
            ),
        ):
            await queue_worker.queue_worker(stop_evt)

        close_redis_pools.assert_awaited_once()
        self.assertIs(queue_worker.REDIS_QUEUE, recovered_redis)
        self.assertGreaterEqual(recovered_redis.calls, 1)
        try_start.assert_awaited_once_with("payload", "q:in", "q:in:processing")

    async def test_non_redis_processing_error_keeps_generic_branch(self):
        stop_evt = queue_worker.asyncio.Event()
        queue_worker.REDIS_QUEUE = _QueueWithPayload()

        close_redis_pools = AsyncMock(return_value=None)
        get_redis_queue = Mock()

        async def _fake_sweeper(stop_evt_arg, *_args, **_kwargs):
            await stop_evt_arg.wait()

        async def _sleep_and_stop(*_args, **_kwargs):
            stop_evt.set()

        with (
            patch.object(queue_worker, "close_redis_pools", close_redis_pools),
            patch.object(queue_worker, "get_redis_queue", get_redis_queue),
            patch.object(queue_worker, "_jitter", lambda *_args, **_kwargs: 0),
            patch.object(queue_worker, "_sweeper_loop", _fake_sweeper),
            patch.object(queue_worker, "_try_start_task_or_requeue", AsyncMock(side_effect=ValueError("boom"))),
            patch.object(queue_worker.asyncio, "sleep", AsyncMock(side_effect=_sleep_and_stop)),
            patch.object(
                queue_worker,
                "requeue_processing_on_start",
                AsyncMock(return_value=types.SimpleNamespace(moved_count=0, lock_acquired=True)),
            ),
        ):
            await queue_worker.queue_worker(stop_evt)

        close_redis_pools.assert_not_awaited()
        get_redis_queue.assert_not_called()

    async def test_startup_requeue_uses_atomic_eval_without_losing_concurrent_processing_items(self):
        stop_evt = queue_worker.asyncio.Event()

        class _AtomicRequeueRedis(_BootRedis):
            async def brpoplpush(self, *_args, **_kwargs):
                stop_evt.set()
                return None

        redis = _AtomicRequeueRedis()
        queue_worker.REDIS_QUEUE = redis

        async def _fake_sweeper(stop_evt_arg, *_args, **_kwargs):
            await stop_evt_arg.wait()

        requeue_mock = AsyncMock(return_value=types.SimpleNamespace(moved_count=2, lock_acquired=True))
        with (
            patch.object(queue_worker, "_sweeper_loop", _fake_sweeper),
            patch.object(queue_worker, "requeue_processing_on_start", requeue_mock),
        ):
            await queue_worker.queue_worker(stop_evt)
        requeue_mock.assert_awaited_once_with(
            redis,
            queue_key="q:in",
            processing_key="q:in:processing",
            lock_ttl=queue_worker.REQUEUE_LOCK_TTL_SEC,
        )


if __name__ == "__main__":
    unittest.main()
