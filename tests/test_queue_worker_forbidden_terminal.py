import importlib.util
import json
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

    fake_aiogram = types.ModuleType("aiogram")
    fake_aiogram_enums = types.ModuleType("aiogram.enums")
    fake_aiogram_types = types.ModuleType("aiogram.types")
    fake_aiogram_exceptions = types.ModuleType("aiogram.exceptions")

    class _ChatAction:
        TYPING = "typing"

    class _Message:
        def __init__(self, message_id=1):
            self.message_id = message_id

    class _BadRequest(Exception):
        pass

    class _RetryAfter(Exception):
        pass

    class _NetworkError(Exception):
        pass

    class _Forbidden(Exception):
        pass

    fake_aiogram_enums.ChatAction = _ChatAction
    fake_aiogram_types.Message = _Message
    fake_aiogram_exceptions.TelegramBadRequest = _BadRequest
    fake_aiogram_exceptions.TelegramRetryAfter = _RetryAfter
    fake_aiogram_exceptions.TelegramNetworkError = _NetworkError
    fake_aiogram_exceptions.TelegramForbiddenError = _Forbidden

    fake_config.settings = types.SimpleNamespace(
        CHATTY_MODE=False,
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
        "aiogram": fake_aiogram,
        "aiogram.enums": fake_aiogram_enums,
        "aiogram.types": fake_aiogram_types,
        "aiogram.exceptions": fake_aiogram_exceptions,
    }

    worker_path = pathlib.Path(__file__).resolve().parents[1] / "app" / "tasks" / "queue_worker.py"
    spec = importlib.util.spec_from_file_location("queue_worker_forbidden_terminal", worker_path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, module_overrides):
        sys.modules["queue_worker_forbidden_terminal"] = module
        spec.loader.exec_module(module)
    return module


queue_worker = _load_queue_worker()


class _Pipeline:
    def __init__(self, owner):
        self.owner = owner
        self.ops = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def lrem(self, key, count, value):
        self.ops.append(("lrem", key, count, value))

    def lpush(self, key, value):
        self.ops.append(("lpush", key, value))

    async def execute(self):
        if self.owner.pipeline_execute_error is not None:
            raise self.owner.pipeline_execute_error
        for op in self.ops:
            if op[0] == "lrem":
                await self.owner.lrem(op[1], op[2], op[3])
            elif op[0] == "lpush":
                await self.owner.lpush(op[1], op[2])


class _FakeQueueRedis:
    def __init__(self, *, requeue_set_result=True, pipeline_execute_error=None):
        self.kv = {}
        self.lpush_calls = []
        self.lrem_calls = []
        self.requeue_set_result = requeue_set_result
        self.pipeline_execute_error = pipeline_execute_error

    async def set(self, key, value, ex=None, nx=False):
        if key.endswith(":requeued"):
            return self.requeue_set_result
        if nx and key in self.kv:
            return False
        self.kv[key] = value
        return True

    async def get(self, key):
        return self.kv.get(key)

    async def delete(self, key):
        self.kv.pop(key, None)
        return 1

    async def lrem(self, key, count, value):
        self.lrem_calls.append((key, count, value))
        return 1

    async def lpush(self, key, value):
        self.lpush_calls.append((key, value))
        return 1

    def pipeline(self):
        return _Pipeline(self)


class QueueWorkerForbiddenTerminalTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_pm_is_terminal_without_requeue(self):
        fake_queue = _FakeQueueRedis()
        queue_worker.REDIS_QUEUE = fake_queue
        queue_worker.get_redis = lambda: types.SimpleNamespace(set=AsyncMock())
        queue_worker.CHATTY_MODE = False
        queue_worker.TYPING_ENABLED = False

        async def _raise_forbidden(**_kwargs):
            raise queue_worker.TelegramForbiddenError("forbidden")

        queue_worker.BOT = types.SimpleNamespace(send_message=AsyncMock(side_effect=_raise_forbidden))

        mark_done = AsyncMock(return_value=1)
        delete_inflight = AsyncMock(return_value=0)
        delete_busy_owner = AsyncMock(return_value=1)
        refund_reservation = AsyncMock(return_value=None)
        confirm_reservation = AsyncMock(return_value=None)

        job = {
            "chat_id": 101,
            "user_id": 101,
            "text": "hello",
            "msg_id": 55,
            "reply_to": 55,
            "is_group": False,
            "is_channel_post": False,
            "reservation_id": 777,
        }

        with patch.object(queue_worker, "_mark_done_if_inflight", mark_done), \
             patch.object(queue_worker, "_delete_if_inflight", delete_inflight), \
             patch.object(queue_worker, "_delete_if_chatbusy_owner", delete_busy_owner), \
             patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()), \
             patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()), \
             patch.object(queue_worker, "_heartbeat_key", AsyncMock()), \
             patch.object(queue_worker, "_heartbeat_inflight", AsyncMock()), \
             patch.object(queue_worker, "respond_to_user", AsyncMock(return_value="reply")), \
             patch.object(queue_worker, "refund_reservation_by_id", refund_reservation), \
             patch.object(queue_worker, "confirm_reservation_by_id", confirm_reservation), \
             patch.object(queue_worker, "_get_backlog", AsyncMock(return_value=0)):
            await queue_worker.handle_job(json.dumps(job), "q:in:processing")

        self.assertEqual(fake_queue.lpush_calls, [], "job must not be requeued for terminal forbidden")
        self.assertEqual(len(fake_queue.lrem_calls), 1, "processing item must be removed exactly once")
        mark_done.assert_awaited()
        delete_inflight.assert_not_awaited()
        refund_reservation.assert_awaited_once_with(777)
        confirm_reservation.assert_not_awaited()


    async def test_send_failure_requeue_pipeline_error_drops_job_terminally(self):
        fake_queue = _FakeQueueRedis(requeue_set_result=True, pipeline_execute_error=Exception("pipeline broken"))
        queue_worker.REDIS_QUEUE = fake_queue
        queue_worker.get_redis = lambda: types.SimpleNamespace(set=AsyncMock())
        queue_worker.CHATTY_MODE = False
        queue_worker.TYPING_ENABLED = False

        mark_done = AsyncMock(return_value=1)
        delete_inflight = AsyncMock(return_value=0)
        delete_busy_owner = AsyncMock(return_value=1)
        refund_reservation = AsyncMock(return_value=None)
        confirm_reservation = AsyncMock(return_value=None)

        job = {
            "chat_id": 303,
            "user_id": 303,
            "text": "hello",
            "msg_id": 99,
            "reply_to": 99,
            "is_group": False,
            "is_channel_post": False,
            "reservation_id": 999,
        }

        with patch.object(queue_worker, "_mark_done_if_inflight", mark_done), \
             patch.object(queue_worker, "_delete_if_inflight", delete_inflight), \
             patch.object(queue_worker, "_delete_if_chatbusy_owner", delete_busy_owner), \
             patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()), \
             patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()), \
             patch.object(queue_worker, "_heartbeat_key", AsyncMock()), \
             patch.object(queue_worker, "_heartbeat_inflight", AsyncMock()), \
             patch.object(queue_worker, "respond_to_user", AsyncMock(return_value="reply")), \
             patch.object(queue_worker, "_send_reply", AsyncMock(side_effect=Exception("telegram down"))), \
             patch.object(queue_worker, "refund_reservation_by_id", refund_reservation), \
             patch.object(queue_worker, "confirm_reservation_by_id", confirm_reservation), \
             patch.object(queue_worker, "_get_backlog", AsyncMock(return_value=0)):
            await queue_worker.handle_job(json.dumps(job), "q:in:processing")

        self.assertEqual(fake_queue.lpush_calls, [], "job must not be requeued when pipeline execution fails")
        self.assertEqual(len(fake_queue.lrem_calls), 1, "processing item must be removed exactly once")
        mark_done.assert_awaited_once()
        refund_reservation.assert_awaited_once_with(999)
        confirm_reservation.assert_not_awaited()

    async def test_fallback_send_failure_with_requeue_guard_exhausted_marks_done_and_refunds(self):
        fake_queue = _FakeQueueRedis(requeue_set_result=False)
        queue_worker.REDIS_QUEUE = fake_queue
        queue_worker.get_redis = lambda: types.SimpleNamespace(set=AsyncMock())
        queue_worker.CHATTY_MODE = False
        queue_worker.TYPING_ENABLED = False

        mark_done = AsyncMock(return_value=1)
        delete_inflight = AsyncMock(return_value=0)
        delete_busy_owner = AsyncMock(return_value=1)
        refund_reservation = AsyncMock(return_value=None)
        confirm_reservation = AsyncMock(return_value=None)

        job = {
            "chat_id": 202,
            "user_id": 202,
            "text": "hello",
            "msg_id": 77,
            "reply_to": 77,
            "is_group": False,
            "is_channel_post": False,
            "reservation_id": 888,
        }

        with patch.object(queue_worker, "_mark_done_if_inflight", mark_done), \
             patch.object(queue_worker, "_delete_if_inflight", delete_inflight), \
             patch.object(queue_worker, "_delete_if_chatbusy_owner", delete_busy_owner), \
             patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()), \
             patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()), \
             patch.object(queue_worker, "_heartbeat_key", AsyncMock()), \
             patch.object(queue_worker, "_heartbeat_inflight", AsyncMock()), \
             patch.object(queue_worker, "respond_to_user", AsyncMock(side_effect=Exception("model timeout"))), \
             patch.object(queue_worker, "_send_reply", AsyncMock(side_effect=Exception("telegram down"))), \
             patch.object(queue_worker, "refund_reservation_by_id", refund_reservation), \
             patch.object(queue_worker, "confirm_reservation_by_id", confirm_reservation), \
             patch.object(queue_worker, "_get_backlog", AsyncMock(return_value=0)):
            await queue_worker.handle_job(json.dumps(job), "q:in:processing")

        self.assertEqual(fake_queue.lpush_calls, [], "job must not be requeued when requeue guard is exhausted")
        self.assertEqual(len(fake_queue.lrem_calls), 1, "processing item must be removed exactly once")
        mark_done.assert_awaited_once()
        refund_reservation.assert_awaited_once_with(888)
        confirm_reservation.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()


class VoiceFileTranscriptionRetryWrapperTests(unittest.IsolatedAsyncioTestCase):
    async def test_transcribe_voice_file_id_returns_empty_on_final_failure(self) -> None:
        async def _get_file(_file_id):
            return object()

        async def _download(_file, path):
            with open(path, "wb") as f:
                f.write(b"OggS\x00\x02")

        async def _boom(**_kwargs):
            raise RuntimeError("fail")

        with patch.object(queue_worker, "BOT", types.SimpleNamespace(get_file=_get_file, download=_download)), \
             patch.object(queue_worker.openai_client, "transcribe_audio_with_retry", side_effect=_boom):
            text = await queue_worker._transcribe_voice_file_id("file_1")

        self.assertEqual(text, "")

    async def test_transcribe_voice_file_id_preserves_model_selection(self) -> None:
        async def _get_file(_file_id):
            return object()

        async def _download(_file, path):
            with open(path, "wb") as f:
                f.write(b"OggS\x00\x02")

        captured = {}

        async def _ok(**kwargs):
            captured.update(kwargs)
            return " world "

        with patch.object(queue_worker, "BOT", types.SimpleNamespace(get_file=_get_file, download=_download)), \
             patch.object(queue_worker.openai_client, "transcribe_audio_with_retry", side_effect=_ok), \
             patch.object(queue_worker, "settings", types.SimpleNamespace(TRANSCRIPTION_MODEL="whisper-x")):
            text = await queue_worker._transcribe_voice_file_id("file_1")

        self.assertEqual(text, "world")
        self.assertEqual(captured.get("model"), "whisper-x")

