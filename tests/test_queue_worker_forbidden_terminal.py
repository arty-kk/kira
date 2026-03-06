import importlib.util
import json
import pathlib
import sys
import types
import unittest
import tempfile
from contextlib import asynccontextmanager
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
    fake_dialog_logger = types.ModuleType("app.services.dialog_logger")
    fake_responder_rag = types.ModuleType("app.services.responder.rag")
    fake_responder_keyword = types.ModuleType("app.services.responder.rag.keyword_filter")
    fake_responder_knowledge = types.ModuleType("app.services.responder.rag.knowledge_proc")
    fake_addons = types.ModuleType("app.services.addons")
    fake_voice = types.ModuleType("app.services.addons.voice_generator")
    fake_mod = types.ModuleType("app.services.addons.passive_moderation")
    fake_analytics = types.ModuleType("app.services.addons.analytics")
    fake_user = types.ModuleType("app.services.user")
    fake_user_service = types.ModuleType("app.services.user.user_service")
    fake_core = types.ModuleType("app.core")
    fake_memory = types.ModuleType("app.core.memory")
    fake_embedding_utils = types.ModuleType("app.core.embedding_utils")
    fake_core_models = types.ModuleType("app.core.models")
    fake_queue_recovery = types.ModuleType("app.core.queue_recovery")
    fake_temp_files = types.ModuleType("app.core.temp_files")

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
        EMBEDDING_MODEL="text-embedding-3-large",
        MODERATION_STATUS_WAIT_SEC=8,
        MODERATION_STATUS_POLL_SEC=0.5,
        MODERATION_SIGNAL_REQUEUE_MAX_ATTEMPTS=3,
        MODERATION_SIGNAL_REQUEUE_MAX_WAIT_SEC=60,
        MODERATION_SIGNAL_INFLIGHT_REQUEUE_MAX_WAIT_SEC=120,
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

    async def _no_hits(*_args, **_kwargs):
        return []

    async def _query_embedding(*_args, **_kwargs):
        return None

    fake_responder_keyword.find_tag_hits = _no_hits
    fake_responder_knowledge._get_query_embedding = _query_embedding

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
    fake_dialog_logger.start_dialog_logger = _noop_async
    fake_dialog_logger.shutdown_dialog_logger = _noop_async
    fake_embedding_utils.get_rag_embedding_model = lambda: "text-embedding-3-large"
    class _RagTagVector:
        pass
    fake_core_models.RagTagVector = _RagTagVector

    async def _fake_requeue_processing_on_start(*_args, **_kwargs):
        return types.SimpleNamespace(moved_count=0, lock_acquired=True)


    @asynccontextmanager
    async def _fake_managed_temp_file(*, data: bytes | None = None, suffix: str = ""):
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            if data is not None:
                tmp.write(data)
            path = tmp.name
        try:
            yield path
        finally:
            pathlib.Path(path).unlink(missing_ok=True)

    async def _fake_open_binary_read(path: str):
        return open(path, "rb")

    fake_temp_files.managed_temp_file = _fake_managed_temp_file
    fake_temp_files.open_binary_read = _fake_open_binary_read
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
        "app.services.dialog_logger": fake_dialog_logger,
        "app.services.responder.rag": fake_responder_rag,
        "app.services.responder.rag.keyword_filter": fake_responder_keyword,
        "app.services.responder.rag.knowledge_proc": fake_responder_knowledge,
        "app.services.addons": fake_addons,
        "app.services.addons.voice_generator": fake_voice,
        "app.services.addons.passive_moderation": fake_mod,
        "app.services.addons.analytics": fake_analytics,
        "app.services.user": fake_user,
        "app.services.user.user_service": fake_user_service,
        "app.core": fake_core,
        "app.core.memory": fake_memory,
        "app.core.embedding_utils": fake_embedding_utils,
        "app.core.models": fake_core_models,
        "app.core.queue_recovery": fake_queue_recovery,
        "app.core.temp_files": fake_temp_files,
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

    def set(self, key, value, ex=None, nx=False):
        self.ops.append(("set", key, value, ex, nx))

    async def execute(self):
        if self.owner.pipeline_execute_error is not None:
            raise self.owner.pipeline_execute_error
        for op in self.ops:
            if op[0] == "lrem":
                await self.owner.lrem(op[1], op[2], op[3])
            elif op[0] == "lpush":
                await self.owner.lpush(op[1], op[2])
            elif op[0] == "set":
                await self.owner.set(op[1], op[2], ex=op[3], nx=op[4])


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

    async def delete(self, *keys):
        removed = 0
        for key in keys:
            if key in self.kv:
                removed += 1
            self.kv.pop(key, None)
        return removed

    async def incr(self, key):
        current = int(self.kv.get(key, 0))
        updated = current + 1
        self.kv[key] = updated
        return updated

    async def expire(self, key, _ttl):
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
    async def test_group_job_uses_chat_scoped_persona_owner(self):
        fake_queue = _FakeQueueRedis()
        queue_worker.REDIS_QUEUE = fake_queue
        queue_worker.get_redis = lambda: types.SimpleNamespace(set=AsyncMock())
        queue_worker.CHATTY_MODE = False
        queue_worker.TYPING_ENABLED = False

        mark_done = AsyncMock(return_value=1)
        delete_inflight = AsyncMock(return_value=0)
        delete_busy_owner = AsyncMock(return_value=1)
        refund_reservation = AsyncMock(return_value=None)
        confirm_reservation = AsyncMock(return_value=None)
        respond_mock = AsyncMock(return_value="reply")

        job = {
            "chat_id": -100123,
            "user_id": 777,
            "text": "hello",
            "msg_id": 55,
            "reply_to": 55,
            "is_group": True,
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
             patch.object(queue_worker, "respond_to_user", respond_mock), \
             patch.object(queue_worker, "_send_reply", AsyncMock(return_value=types.SimpleNamespace(message_id=999))), \
             patch.object(queue_worker, "refund_reservation_by_id", refund_reservation), \
             patch.object(queue_worker, "confirm_reservation_by_id", confirm_reservation), \
             patch.object(queue_worker, "_get_backlog", AsyncMock(return_value=0)):
            await queue_worker.handle_job(json.dumps(job), "q:in:processing")

        respond_mock.assert_awaited_once()
        self.assertEqual(respond_mock.await_args.kwargs.get("persona_owner_id"), -100123)

    async def test_channel_job_uses_chat_scoped_persona_owner(self):
        fake_queue = _FakeQueueRedis()
        queue_worker.REDIS_QUEUE = fake_queue
        queue_worker.get_redis = lambda: types.SimpleNamespace(set=AsyncMock())
        queue_worker.CHATTY_MODE = False
        queue_worker.TYPING_ENABLED = False

        respond_mock = AsyncMock(return_value="reply")
        job = {
            "chat_id": -100555,
            "user_id": 777,
            "text": "hello",
            "msg_id": 56,
            "reply_to": 56,
            "is_group": True,
            "is_channel_post": True,
            "reservation_id": 778,
        }

        with patch.object(queue_worker, "_mark_done_if_inflight", AsyncMock(return_value=1)), \
             patch.object(queue_worker, "_delete_if_inflight", AsyncMock(return_value=1)) as delete_inflight, \
             patch.object(queue_worker, "_delete_if_chatbusy_owner", AsyncMock(return_value=1)), \
             patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()), \
             patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()), \
             patch.object(queue_worker, "_heartbeat_key", AsyncMock()), \
             patch.object(queue_worker, "_heartbeat_inflight", AsyncMock()), \
             patch.object(queue_worker, "respond_to_user", respond_mock), \
             patch.object(queue_worker, "_send_reply", AsyncMock(return_value=types.SimpleNamespace(message_id=1000))), \
             patch.object(queue_worker, "refund_reservation_by_id", AsyncMock(return_value=None)), \
             patch.object(queue_worker, "confirm_reservation_by_id", AsyncMock(return_value=None)), \
             patch.object(queue_worker, "_get_backlog", AsyncMock(return_value=0)):
            await queue_worker.handle_job(json.dumps(job), "q:in:processing")

        respond_mock.assert_awaited_once()
        self.assertEqual(respond_mock.await_args.kwargs.get("persona_owner_id"), -100555)

    async def test_private_job_keeps_default_persona_owner_none(self):
        fake_queue = _FakeQueueRedis()
        queue_worker.REDIS_QUEUE = fake_queue
        queue_worker.get_redis = lambda: types.SimpleNamespace(set=AsyncMock())
        queue_worker.CHATTY_MODE = False
        queue_worker.TYPING_ENABLED = False

        respond_mock = AsyncMock(return_value="reply")
        job = {
            "chat_id": 123,
            "user_id": 123,
            "text": "hello",
            "msg_id": 57,
            "reply_to": 57,
            "is_group": False,
            "is_channel_post": False,
            "reservation_id": 779,
        }

        with patch.object(queue_worker, "_mark_done_if_inflight", AsyncMock(return_value=1)), \
             patch.object(queue_worker, "_delete_if_inflight", AsyncMock(return_value=1)) as delete_inflight, \
             patch.object(queue_worker, "_delete_if_chatbusy_owner", AsyncMock(return_value=1)), \
             patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()), \
             patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()), \
             patch.object(queue_worker, "_heartbeat_key", AsyncMock()), \
             patch.object(queue_worker, "_heartbeat_inflight", AsyncMock()), \
             patch.object(queue_worker, "respond_to_user", respond_mock), \
             patch.object(queue_worker, "_send_reply", AsyncMock(return_value=types.SimpleNamespace(message_id=1001))), \
             patch.object(queue_worker, "refund_reservation_by_id", AsyncMock(return_value=None)), \
             patch.object(queue_worker, "confirm_reservation_by_id", AsyncMock(return_value=None)), \
             patch.object(queue_worker, "_get_backlog", AsyncMock(return_value=0)):
            await queue_worker.handle_job(json.dumps(job), "q:in:processing")

        respond_mock.assert_awaited_once()
        self.assertIsNone(respond_mock.await_args.kwargs.get("persona_owner_id"))

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

    async def test_success_path_confirms_single_billable_and_refunds_extra_merged_reservation_ids(self):
        fake_queue = _FakeQueueRedis()
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
            "chat_id": 404,
            "user_id": 404,
            "text": "hello",
            "msg_id": 100,
            "reply_to": 100,
            "is_group": False,
            "is_channel_post": False,
            "reservation_ids": [11, 12, 11, "13", -1, 0],
            "reservation_id": 99,
        }

        with patch.object(queue_worker, "_mark_done_if_inflight", mark_done),              patch.object(queue_worker, "_delete_if_inflight", delete_inflight),              patch.object(queue_worker, "_delete_if_chatbusy_owner", delete_busy_owner),              patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()),              patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()),              patch.object(queue_worker, "_heartbeat_key", AsyncMock()),              patch.object(queue_worker, "_heartbeat_inflight", AsyncMock()),              patch.object(queue_worker, "respond_to_user", AsyncMock(return_value="reply")),              patch.object(queue_worker, "_send_reply", AsyncMock(return_value=None)),              patch.object(queue_worker, "refund_reservation_by_id", refund_reservation),              patch.object(queue_worker, "confirm_reservation_by_id", confirm_reservation),              patch.object(queue_worker, "_get_backlog", AsyncMock(return_value=0)):
            await queue_worker.handle_job(json.dumps(job), "q:in:processing")

        self.assertEqual(
            [call.args[0] for call in confirm_reservation.await_args_list],
            [99],
            "only billable reservation must be confirmed for a merged generation",
        )
        self.assertEqual(
            [call.args[0] for call in refund_reservation.await_args_list],
            [11, 12, 13],
            "extra merged reservations must be refunded on successful completion",
        )

    async def test_terminal_failure_refunds_all_ids_and_fallbacks_to_legacy_single(self):
        fake_queue = _FakeQueueRedis()
        queue_worker.REDIS_QUEUE = fake_queue
        queue_worker.get_redis = lambda: types.SimpleNamespace(set=AsyncMock())
        queue_worker.CHATTY_MODE = False
        queue_worker.TYPING_ENABLED = False

        mark_done = AsyncMock(return_value=1)
        delete_inflight = AsyncMock(return_value=0)
        delete_busy_owner = AsyncMock(return_value=1)
        refund_reservation = AsyncMock(side_effect=[Exception("boom"), None, None])
        confirm_reservation = AsyncMock(return_value=None)

        job = {
            "chat_id": 505,
            "user_id": 505,
            "text": "hello",
            "msg_id": 101,
            "reply_to": 101,
            "is_group": False,
            "is_channel_post": False,
            "reservation_ids": [21, 22, 23],
        }

        with patch.object(queue_worker, "_mark_done_if_inflight", mark_done),              patch.object(queue_worker, "_delete_if_inflight", delete_inflight),              patch.object(queue_worker, "_delete_if_chatbusy_owner", delete_busy_owner),              patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()),              patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()),              patch.object(queue_worker, "_heartbeat_key", AsyncMock()),              patch.object(queue_worker, "_heartbeat_inflight", AsyncMock()),              patch.object(queue_worker, "respond_to_user", AsyncMock(return_value="reply")),              patch.object(queue_worker, "_send_reply", AsyncMock(side_effect=queue_worker.ReplyTerminalError("terminal"))),              patch.object(queue_worker, "refund_reservation_by_id", refund_reservation),              patch.object(queue_worker, "confirm_reservation_by_id", confirm_reservation),              patch.object(queue_worker, "_get_backlog", AsyncMock(return_value=0)):
            await queue_worker.handle_job(json.dumps(job), "q:in:processing")

        self.assertEqual(
            [call.args[0] for call in refund_reservation.await_args_list],
            [21, 22, 23],
            "terminal failure must refund every reservation id even with partial errors",
        )
        confirm_reservation.assert_not_awaited()

        refund_reservation.reset_mock(side_effect=True)
        refund_reservation.side_effect = None

        legacy_job = {
            "chat_id": 606,
            "user_id": 606,
            "text": "hello",
            "msg_id": 102,
            "reply_to": 102,
            "is_group": False,
            "is_channel_post": False,
            "reservation_id": 77,
        }

        with patch.object(queue_worker, "_mark_done_if_inflight", AsyncMock(return_value=1)),              patch.object(queue_worker, "_delete_if_inflight", AsyncMock(return_value=1)) as delete_inflight,              patch.object(queue_worker, "_delete_if_chatbusy_owner", AsyncMock(return_value=1)),              patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()),              patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()),              patch.object(queue_worker, "_heartbeat_key", AsyncMock()),              patch.object(queue_worker, "_heartbeat_inflight", AsyncMock()),              patch.object(queue_worker, "respond_to_user", AsyncMock(return_value="reply")),              patch.object(queue_worker, "_send_reply", AsyncMock(side_effect=queue_worker.ReplyTerminalError("terminal"))),              patch.object(queue_worker, "refund_reservation_by_id", refund_reservation),              patch.object(queue_worker, "confirm_reservation_by_id", AsyncMock(return_value=None)),              patch.object(queue_worker, "_get_backlog", AsyncMock(return_value=0)):
            await queue_worker.handle_job(json.dumps(legacy_job), "q:in:processing")

        refund_reservation.assert_awaited_once_with(77)


    async def test_group_reply_badrequest_is_terminal_without_reply_fallback(self):
        queue_worker.REDIS_QUEUE = _FakeQueueRedis()

        send_calls = []

        async def _send_message(**kwargs):
            send_calls.append(kwargs)
            if len(send_calls) == 1:
                raise queue_worker.TelegramBadRequest("Bad Request: reply_to_message_id is invalid")
            return types.SimpleNamespace(message_id=9001)

        queue_worker.BOT = types.SimpleNamespace(send_message=AsyncMock(side_effect=_send_message))

        with patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()),              patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()):
            with self.assertLogs(queue_worker.logger, level="WARNING") as logs:
                with self.assertRaises(queue_worker.ReplyTerminalError):
                    await queue_worker._send_reply(
                        chat_id=-100777,
                        text="hello",
                        reply_to=42,
                        msg_id=None,
                        user_id=123,
                        is_group=True,
                    )

        self.assertEqual(len(send_calls), 1, "group reply error must not retry without reply_to_message_id")
        self.assertIn("reply_to_message_id", send_calls[0])
        self.assertTrue(
            any("REPLY_TERMINAL_GROUP_REPLY_TARGET" in line for line in logs.output),
            "terminal group reply branch must log stable marker",
        )

    async def test_private_reply_badrequest_uses_reply_fallback(self):
        queue_worker.REDIS_QUEUE = _FakeQueueRedis()

        send_calls = []

        async def _send_message(**kwargs):
            send_calls.append(kwargs)
            if len(send_calls) == 1:
                raise queue_worker.TelegramBadRequest("Bad Request: reply_to_message_id is invalid")
            return types.SimpleNamespace(message_id=9002)

        queue_worker.BOT = types.SimpleNamespace(send_message=AsyncMock(side_effect=_send_message))

        with patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()),              patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()):
            await queue_worker._send_reply(
                chat_id=321,
                text="hello",
                reply_to=42,
                msg_id=None,
                user_id=321,
                is_group=False,
            )

        self.assertEqual(len(send_calls), 2, "private chat should retry after dropping reply_to_message_id")
        self.assertIn("reply_to_message_id", send_calls[0])
        self.assertNotIn("reply_to_message_id", send_calls[1])



    async def test_send_reply_preserves_message_thread_id(self):
        queue_worker.REDIS_QUEUE = _FakeQueueRedis()

        send_calls = []

        async def _send_message(**kwargs):
            send_calls.append(kwargs)
            return types.SimpleNamespace(message_id=9005)

        queue_worker.BOT = types.SimpleNamespace(send_message=AsyncMock(side_effect=_send_message))

        with patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()),              patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()):
            await queue_worker._send_reply(
                chat_id=-100777,
                text="hello",
                reply_to=42,
                msg_id=None,
                user_id=123,
                is_group=True,
                message_thread_id=777001,
            )

        self.assertEqual(len(send_calls), 1)
        self.assertEqual(send_calls[0].get("message_thread_id"), 777001)

    async def test_send_reply_marks_merged_ids_and_returns_sent_message(self):
        fake_queue = _FakeQueueRedis()
        queue_worker.REDIS_QUEUE = fake_queue

        sent = types.SimpleNamespace(message_id=9010)
        queue_worker.BOT = types.SimpleNamespace(send_message=AsyncMock(return_value=sent))

        with patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()),              patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()):
            result = await queue_worker._send_reply(
                chat_id=-100777,
                text="hello",
                reply_to=42,
                msg_id=700,
                merged_ids=[700, 701, 702],
                user_id=123,
                is_group=True,
                message_thread_id=777001,
            )

        self.assertIs(result, sent)
        self.assertEqual(fake_queue.kv.get("sent_reply:-100777:700"), 1)
        self.assertEqual(fake_queue.kv.get("sent_reply:-100777:701"), 1)
        self.assertEqual(fake_queue.kv.get("sent_reply:-100777:702"), 1)

    async def test_send_chatty_reply_keeps_message_thread_id_for_all_chunks(self):
        send_reply_mock = AsyncMock(return_value=None)

        with (
            patch.object(queue_worker, "_send_reply", send_reply_mock),
            patch.object(queue_worker, "compute_typing_delay", return_value=0.0),
            patch.object(queue_worker, "_split_reply_into_messages", return_value=["Первая фраза.", "Вторая фраза."]),
            patch.object(queue_worker, "_group_chatty_chunks", side_effect=lambda chunks: chunks),
        ):
            await queue_worker._send_chatty_reply(
                chat_id=-100777,
                text="Первая фраза. Вторая фраза.",
                reply_to=42,
                msg_id=300,
                user_id=123,
                is_group=True,
                enable_typing=False,
                message_thread_id=777002,
            )

        self.assertGreaterEqual(send_reply_mock.await_count, 1)
        self.assertEqual(send_reply_mock.await_args_list[0].kwargs.get("reply_to"), 42)
        for call_item in send_reply_mock.await_args_list:
            self.assertEqual(call_item.kwargs.get("message_thread_id"), 777002)
        if send_reply_mock.await_count >= 2:
            self.assertEqual(send_reply_mock.await_args_list[1].kwargs.get("reply_to"), 777002)

    async def test_send_chatty_reply_infers_thread_id_from_first_chunk_when_missing(self):
        sent_first = types.SimpleNamespace(message_id=5001, message_thread_id=888003)
        send_reply_mock = AsyncMock(side_effect=[sent_first, None])

        with (
            patch.object(queue_worker, "_send_reply", send_reply_mock),
            patch.object(queue_worker, "compute_typing_delay", return_value=0.0),
            patch.object(queue_worker, "_split_reply_into_messages", return_value=["Первая фраза.", "Вторая фраза."]),
            patch.object(queue_worker, "_group_chatty_chunks", side_effect=lambda chunks: chunks),
        ):
            await queue_worker._send_chatty_reply(
                chat_id=-100777,
                text="Первая фраза. Вторая фраза.",
                reply_to=42,
                msg_id=301,
                user_id=123,
                is_group=True,
                enable_typing=False,
                message_thread_id=None,
            )

        self.assertGreaterEqual(send_reply_mock.await_count, 2)
        self.assertEqual(send_reply_mock.await_args_list[0].kwargs.get("reply_to"), 42)
        self.assertIsNone(send_reply_mock.await_args_list[0].kwargs.get("message_thread_id"))
        self.assertEqual(send_reply_mock.await_args_list[1].kwargs.get("message_thread_id"), 888003)
        self.assertEqual(send_reply_mock.await_args_list[1].kwargs.get("reply_to"), 888003)

    async def test_send_chatty_reply_stops_when_first_chunk_is_deduped(self):
        send_reply_mock = AsyncMock(side_effect=[None, types.SimpleNamespace(message_id=5002)])

        with (
            patch.object(queue_worker, "_send_reply", send_reply_mock),
            patch.object(queue_worker, "compute_typing_delay", return_value=0.0),
            patch.object(queue_worker, "_split_reply_into_messages", return_value=["Первая фраза.", "Вторая фраза."]),
            patch.object(queue_worker, "_group_chatty_chunks", side_effect=lambda chunks: chunks),
        ):
            await queue_worker._send_chatty_reply(
                chat_id=-100777,
                text="Первая фраза. Вторая фраза.",
                reply_to=42,
                msg_id=302,
                user_id=123,
                is_group=True,
                enable_typing=False,
                message_thread_id=None,
            )

        self.assertEqual(send_reply_mock.await_count, 1)

    async def test_blocked_moderation_status_skips_response_and_refunds(self):
        fake_queue = _FakeQueueRedis()
        queue_worker.REDIS_QUEUE = fake_queue
        queue_worker.CHATTY_MODE = False
        queue_worker.TYPING_ENABLED = True
        queue_worker.TYPING_SKIP_GROUPS = False
        queue_worker.BOT = types.SimpleNamespace(send_chat_action=AsyncMock(), send_message=AsyncMock())

        redis_stub = types.SimpleNamespace(hget=AsyncMock(return_value="blocked"), set=AsyncMock())
        queue_worker.get_redis = lambda: redis_stub

        mark_done = AsyncMock(return_value=1)
        refund_reservation = AsyncMock(return_value=None)
        respond_mock = AsyncMock(return_value="reply")
        typing_mock = AsyncMock(return_value=None)
        send_reply_mock = AsyncMock(return_value=types.SimpleNamespace(message_id=1002))

        job = {
            "chat_id": -100909,
            "user_id": 909,
            "text": "hello",
            "msg_id": 200,
            "reply_to": 200,
            "is_group": True,
            "is_channel_post": False,
            "trigger": "mention",
            "reservation_id": 333,
        }

        with patch.object(queue_worker, "_mark_done_if_inflight", mark_done),              patch.object(queue_worker, "_delete_if_inflight", AsyncMock(return_value=1)) as delete_inflight,              patch.object(queue_worker, "_delete_if_chatbusy_owner", AsyncMock(return_value=1)),              patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()),              patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()),              patch.object(queue_worker, "_heartbeat_key", AsyncMock()),              patch.object(queue_worker, "_heartbeat_inflight", AsyncMock()),              patch.object(queue_worker, "respond_to_user", respond_mock),              patch.object(queue_worker, "_typing_during_generation", typing_mock),              patch.object(queue_worker, "_send_reply", send_reply_mock),              patch.object(queue_worker, "refund_reservation_by_id", refund_reservation),              patch.object(queue_worker, "confirm_reservation_by_id", AsyncMock(return_value=None)),              patch.object(queue_worker, "_get_backlog", AsyncMock(return_value=0)):
            await queue_worker.handle_job(json.dumps(job), "q:in:processing")

        redis_stub.hget.assert_awaited_once_with("mod:msg:-100909:200", "status")
        respond_mock.assert_not_awaited()
        typing_mock.assert_not_called()
        queue_worker.BOT.send_chat_action.assert_not_awaited()
        send_reply_mock.assert_not_awaited()
        mark_done.assert_awaited()
        refund_reservation.assert_awaited_once_with(333)

    async def test_flagged_moderation_status_skips_response_without_typing(self):
        fake_queue = _FakeQueueRedis()
        queue_worker.REDIS_QUEUE = fake_queue
        queue_worker.CHATTY_MODE = False
        queue_worker.TYPING_ENABLED = True
        queue_worker.TYPING_SKIP_GROUPS = False
        queue_worker.BOT = types.SimpleNamespace(send_chat_action=AsyncMock(), send_message=AsyncMock())

        redis_stub = types.SimpleNamespace(hget=AsyncMock(return_value="flagged"), set=AsyncMock())
        queue_worker.get_redis = lambda: redis_stub

        refund_reservation = AsyncMock(return_value=None)
        respond_mock = AsyncMock(return_value="reply")
        typing_mock = AsyncMock(return_value=None)

        job = {
            "chat_id": -100910,
            "user_id": 910,
            "text": "hello",
            "msg_id": 201,
            "reply_to": 201,
            "is_group": True,
            "is_channel_post": False,
            "trigger": "mention",
            "reservation_id": 334,
        }

        with patch.object(queue_worker, "_mark_done_if_inflight", AsyncMock(return_value=1)),              patch.object(queue_worker, "_delete_if_inflight", AsyncMock(return_value=1)) as delete_inflight,              patch.object(queue_worker, "_delete_if_chatbusy_owner", AsyncMock(return_value=1)),              patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()),              patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()),              patch.object(queue_worker, "_heartbeat_key", AsyncMock()),              patch.object(queue_worker, "_heartbeat_inflight", AsyncMock()),              patch.object(queue_worker, "respond_to_user", respond_mock),              patch.object(queue_worker, "_typing_during_generation", typing_mock),              patch.object(queue_worker, "_send_reply", AsyncMock(return_value=types.SimpleNamespace(message_id=1003))),              patch.object(queue_worker, "refund_reservation_by_id", refund_reservation),              patch.object(queue_worker, "confirm_reservation_by_id", AsyncMock(return_value=None)),              patch.object(queue_worker, "_get_backlog", AsyncMock(return_value=0)):
            await queue_worker.handle_job(json.dumps(job), "q:in:processing")

        respond_mock.assert_not_awaited()
        typing_mock.assert_not_called()
        queue_worker.BOT.send_chat_action.assert_not_awaited()
        refund_reservation.assert_awaited_once_with(334)

    async def test_group_trigger_skips_when_moderation_signal_missing_for_untrusted_chat(self):
        fake_queue = _FakeQueueRedis()
        queue_worker.REDIS_QUEUE = fake_queue
        queue_worker.CHATTY_MODE = False
        queue_worker.TYPING_ENABLED = True
        queue_worker.TYPING_SKIP_GROUPS = False
        queue_worker.BOT = types.SimpleNamespace(send_chat_action=AsyncMock(), send_message=AsyncMock())

        redis_stub = types.SimpleNamespace(hget=AsyncMock(return_value=None), set=AsyncMock())
        queue_worker.get_redis = lambda: redis_stub

        mark_done = AsyncMock(return_value=1)
        refund_reservation = AsyncMock(return_value=None)
        respond_mock = AsyncMock(return_value="reply")
        typing_mock = AsyncMock(return_value=None)

        job = {
            "chat_id": -100911,
            "user_id": 911,
            "text": "hello",
            "msg_id": 211,
            "reply_to": 211,
            "is_group": True,
            "is_channel_post": False,
            "trigger": "mention",
            "reservation_id": 335,
        }

        with patch.object(queue_worker, "MODERATION_STATUS_WAIT_SEC", 0.0),              patch.object(queue_worker, "_mark_done_if_inflight", mark_done),              patch.object(queue_worker, "_delete_if_inflight", AsyncMock(return_value=1)) as delete_inflight,              patch.object(queue_worker, "_delete_if_chatbusy_owner", AsyncMock(return_value=1)),              patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()),              patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()),              patch.object(queue_worker, "_heartbeat_key", AsyncMock()),              patch.object(queue_worker, "_heartbeat_inflight", AsyncMock()),              patch.object(queue_worker, "respond_to_user", respond_mock),              patch.object(queue_worker, "_typing_during_generation", typing_mock),              patch.object(queue_worker, "_send_reply", AsyncMock(return_value=types.SimpleNamespace(message_id=1004))),              patch.object(queue_worker, "refund_reservation_by_id", refund_reservation),              patch.object(queue_worker, "confirm_reservation_by_id", AsyncMock(return_value=None)),              patch.object(queue_worker, "_get_backlog", AsyncMock(return_value=0)):
            await queue_worker.handle_job(json.dumps(job), "q:in:processing")

        respond_mock.assert_not_awaited()
        typing_mock.assert_not_called()
        queue_worker.BOT.send_chat_action.assert_not_awaited()
        delete_inflight.assert_awaited_once()
        refund_reservation.assert_not_awaited()
        self.assertEqual(len(fake_queue.lpush_calls), 1)

    async def test_group_trigger_missing_moderation_signal_requeues_with_attempt_increment(self):
        fake_queue = _FakeQueueRedis()
        queue_worker.REDIS_QUEUE = fake_queue
        queue_worker.CHATTY_MODE = False
        queue_worker.TYPING_ENABLED = True
        queue_worker.TYPING_SKIP_GROUPS = False
        queue_worker.BOT = types.SimpleNamespace(send_chat_action=AsyncMock(), send_message=AsyncMock())

        redis_stub = types.SimpleNamespace(hget=AsyncMock(return_value=None), set=AsyncMock())
        queue_worker.get_redis = lambda: redis_stub

        mark_done = AsyncMock(return_value=1)
        refund_reservation = AsyncMock(return_value=None)
        respond_mock = AsyncMock(return_value="reply")

        job = {
            "chat_id": -100912,
            "user_id": 912,
            "text": "hello",
            "msg_id": 212,
            "reply_to": 212,
            "is_group": True,
            "is_channel_post": False,
            "trigger": "mention",
            "reservation_id": 336,
        }

        with patch.object(queue_worker, "MODERATION_STATUS_WAIT_SEC", 0.0),              patch.object(queue_worker, "MAX_MODERATION_WAIT_RETRIES", 3),              patch.object(queue_worker, "_mark_done_if_inflight", mark_done),              patch.object(queue_worker, "_delete_if_inflight", AsyncMock(return_value=1)) as delete_inflight,              patch.object(queue_worker, "_delete_if_chatbusy_owner", AsyncMock(return_value=1)),              patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()),              patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()),              patch.object(queue_worker, "_heartbeat_key", AsyncMock()),              patch.object(queue_worker, "_heartbeat_inflight", AsyncMock()),              patch.object(queue_worker, "respond_to_user", respond_mock),              patch.object(queue_worker, "_typing_during_generation", AsyncMock(return_value=None)),              patch.object(queue_worker, "_send_reply", AsyncMock(return_value=types.SimpleNamespace(message_id=1005))),              patch.object(queue_worker, "refund_reservation_by_id", refund_reservation),              patch.object(queue_worker, "confirm_reservation_by_id", AsyncMock(return_value=None)),              patch.object(queue_worker, "_get_backlog", AsyncMock(return_value=0)):
            await queue_worker.handle_job(json.dumps(job), "q:in:processing")

        respond_mock.assert_not_awaited()
        delete_inflight.assert_awaited_once()
        mark_done.assert_not_awaited()
        refund_reservation.assert_not_awaited()
        self.assertEqual(len(fake_queue.lpush_calls), 1)
        queued_payload = json.loads(fake_queue.lpush_calls[0][1])
        self.assertEqual(queued_payload.get("moderation_wait_attempt"), 1)

    async def test_group_trigger_missing_moderation_signal_becomes_terminal_after_retry_limit(self):
        fake_queue = _FakeQueueRedis()
        queue_worker.REDIS_QUEUE = fake_queue
        queue_worker.CHATTY_MODE = False
        queue_worker.TYPING_ENABLED = False

        redis_stub = types.SimpleNamespace(hget=AsyncMock(return_value=None), set=AsyncMock())
        queue_worker.get_redis = lambda: redis_stub

        mark_done = AsyncMock(return_value=1)
        refund_reservation = AsyncMock(return_value=None)

        job = {
            "chat_id": -100912,
            "user_id": 912,
            "text": "hello",
            "msg_id": 212,
            "reply_to": 212,
            "is_group": True,
            "is_channel_post": False,
            "trigger": "mention",
            "reservation_id": 336,
            "moderation_wait_attempt": 2,
        }

        with patch.object(queue_worker, "MODERATION_STATUS_WAIT_SEC", 0.0),              patch.object(queue_worker, "MAX_MODERATION_WAIT_RETRIES", 2),              patch.object(queue_worker, "_mark_done_if_inflight", mark_done),              patch.object(queue_worker, "_delete_if_inflight", AsyncMock(return_value=1)) as delete_inflight,              patch.object(queue_worker, "_delete_if_chatbusy_owner", AsyncMock(return_value=1)),              patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()),              patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()),              patch.object(queue_worker, "_heartbeat_key", AsyncMock()),              patch.object(queue_worker, "_heartbeat_inflight", AsyncMock()),              patch.object(queue_worker, "respond_to_user", AsyncMock(return_value="reply")),              patch.object(queue_worker, "_send_reply", AsyncMock(return_value=types.SimpleNamespace(message_id=1005))),              patch.object(queue_worker, "refund_reservation_by_id", refund_reservation),              patch.object(queue_worker, "confirm_reservation_by_id", AsyncMock(return_value=None)),              patch.object(queue_worker, "_get_backlog", AsyncMock(return_value=0)):
            await queue_worker.handle_job(json.dumps(job), "q:in:processing")

        delete_inflight.assert_not_awaited()
        mark_done.assert_awaited_once()
        refund_reservation.assert_awaited_once_with(336)
        self.assertEqual(fake_queue.lpush_calls, [])


    async def test_group_trigger_missing_moderation_signal_requeue_failure_sets_done_marker(self):
        fake_queue = _FakeQueueRedis(pipeline_execute_error=Exception("pipeline broken"))
        queue_worker.REDIS_QUEUE = fake_queue
        queue_worker.CHATTY_MODE = False
        queue_worker.TYPING_ENABLED = False

        redis_stub = types.SimpleNamespace(hget=AsyncMock(return_value=None), set=AsyncMock())
        queue_worker.get_redis = lambda: redis_stub

        mark_done = AsyncMock(return_value=0)
        refund_reservation = AsyncMock(return_value=None)

        job = {
            "chat_id": -100912,
            "user_id": 912,
            "text": "hello",
            "msg_id": 212,
            "reply_to": 212,
            "is_group": True,
            "is_channel_post": False,
            "trigger": "mention",
            "reservation_id": 336,
            "moderation_wait_attempt": 0,
        }

        with patch.object(queue_worker, "MODERATION_STATUS_WAIT_SEC", 0.0),              patch.object(queue_worker, "MAX_MODERATION_WAIT_RETRIES", 2),              patch.object(queue_worker, "_mark_done_if_inflight", mark_done),              patch.object(queue_worker, "_delete_if_inflight", AsyncMock(side_effect=lambda *_args, **_kwargs: fake_queue.kv.pop("q:job:-100912:212", None) or 1)),              patch.object(queue_worker, "_delete_if_chatbusy_owner", AsyncMock(return_value=1)),              patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()),              patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()),              patch.object(queue_worker, "_heartbeat_key", AsyncMock()),              patch.object(queue_worker, "_heartbeat_inflight", AsyncMock()),              patch.object(queue_worker, "respond_to_user", AsyncMock(return_value="reply")),              patch.object(queue_worker, "_send_reply", AsyncMock(return_value=types.SimpleNamespace(message_id=1005))),              patch.object(queue_worker, "refund_reservation_by_id", refund_reservation),              patch.object(queue_worker, "confirm_reservation_by_id", AsyncMock(return_value=None)),              patch.object(queue_worker, "_get_backlog", AsyncMock(return_value=0)):
            await queue_worker.handle_job(json.dumps(job), "q:in:processing")

        mark_done.assert_awaited_once()
        refund_reservation.assert_awaited_once_with(336)
        self.assertEqual(fake_queue.kv.get("q:job:-100912:212"), "done")
        self.assertEqual(fake_queue.lpush_calls, [])

    async def test_group_trigger_missing_moderation_signal_requeue_failure_does_not_override_foreign_inflight(self):
        fake_queue = _FakeQueueRedis(pipeline_execute_error=Exception("pipeline broken"))
        queue_worker.REDIS_QUEUE = fake_queue
        queue_worker.CHATTY_MODE = False
        queue_worker.TYPING_ENABLED = False

        redis_stub = types.SimpleNamespace(hget=AsyncMock(return_value=None), set=AsyncMock())
        queue_worker.get_redis = lambda: redis_stub

        async def _mark_done_side_effect(*_args, **_kwargs):
            fake_queue.kv["q:job:-100913:213"] = "inflight:other-owner"
            return 0

        mark_done = AsyncMock(side_effect=_mark_done_side_effect)
        refund_reservation = AsyncMock(return_value=None)

        job = {
            "chat_id": -100913,
            "user_id": 913,
            "text": "hello",
            "msg_id": 213,
            "reply_to": 213,
            "is_group": True,
            "is_channel_post": False,
            "trigger": "mention",
            "reservation_id": 337,
            "moderation_wait_attempt": 0,
        }

        with patch.object(queue_worker, "MODERATION_STATUS_WAIT_SEC", 0.0),              patch.object(queue_worker, "MAX_MODERATION_WAIT_RETRIES", 2),              patch.object(queue_worker, "_mark_done_if_inflight", mark_done),              patch.object(queue_worker, "_delete_if_inflight", AsyncMock(side_effect=lambda *_args, **_kwargs: fake_queue.kv.pop("q:job:-100913:213", None) or 1)),              patch.object(queue_worker, "_delete_if_chatbusy_owner", AsyncMock(return_value=1)),              patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()),              patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()),              patch.object(queue_worker, "_heartbeat_key", AsyncMock()),              patch.object(queue_worker, "_heartbeat_inflight", AsyncMock()),              patch.object(queue_worker, "respond_to_user", AsyncMock(return_value="reply")),              patch.object(queue_worker, "_send_reply", AsyncMock(return_value=types.SimpleNamespace(message_id=1005))),              patch.object(queue_worker, "refund_reservation_by_id", refund_reservation),              patch.object(queue_worker, "confirm_reservation_by_id", AsyncMock(return_value=None)),              patch.object(queue_worker, "_get_backlog", AsyncMock(return_value=0)):
            await queue_worker.handle_job(json.dumps(job), "q:in:processing")

        mark_done.assert_awaited_once()
        refund_reservation.assert_awaited_once_with(337)
        self.assertEqual(fake_queue.kv.get("q:job:-100913:213"), "inflight:other-owner")
    async def test_group_trigger_missing_moderation_signal_requeues_for_comment_context(self):
        fake_queue = _FakeQueueRedis()
        queue_worker.REDIS_QUEUE = fake_queue
        queue_worker.CHATTY_MODE = False
        queue_worker.TYPING_ENABLED = True
        queue_worker.TYPING_SKIP_GROUPS = False
        queue_worker.BOT = types.SimpleNamespace(send_chat_action=AsyncMock(), send_message=AsyncMock())
        setattr(queue_worker.settings, "ALLOWED_GROUP_IDS", [])
        setattr(queue_worker.settings, "COMMENT_TARGET_CHAT_IDS", [])
        setattr(queue_worker.settings, "COMMENT_SOURCE_CHANNEL_IDS", [-100991])
        setattr(queue_worker.settings, "COMMENT_MODERATION_ENABLED", True)

        redis_stub = types.SimpleNamespace(hget=AsyncMock(return_value=None), set=AsyncMock())
        queue_worker.get_redis = lambda: redis_stub

        mark_done = AsyncMock(return_value=1)
        refund_reservation = AsyncMock(return_value=None)
        respond_mock = AsyncMock(return_value="reply")

        job = {
            "chat_id": -100992,
            "user_id": 992,
            "text": "hello",
            "msg_id": 214,
            "reply_to": 214,
            "is_group": True,
            "is_channel_post": False,
            "trigger": "mention",
            "reservation_id": 338,
            "is_comment_context": True,
        }

        with patch.object(queue_worker, "MODERATION_STATUS_WAIT_SEC", 0.0),              patch.object(queue_worker, "_mark_done_if_inflight", mark_done),              patch.object(queue_worker, "_delete_if_inflight", AsyncMock(return_value=1)) as delete_inflight,              patch.object(queue_worker, "_delete_if_chatbusy_owner", AsyncMock(return_value=1)),              patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()),              patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()),              patch.object(queue_worker, "_heartbeat_key", AsyncMock()),              patch.object(queue_worker, "_heartbeat_inflight", AsyncMock()),              patch.object(queue_worker, "respond_to_user", respond_mock),              patch.object(queue_worker, "_typing_during_generation", AsyncMock(return_value=None)),              patch.object(queue_worker, "_send_reply", AsyncMock(return_value=types.SimpleNamespace(message_id=1007))),              patch.object(queue_worker, "refund_reservation_by_id", refund_reservation),              patch.object(queue_worker, "confirm_reservation_by_id", AsyncMock(return_value=None)),              patch.object(queue_worker, "_get_backlog", AsyncMock(return_value=0)):
            await queue_worker.handle_job(json.dumps(job), "q:in:processing")

        respond_mock.assert_not_awaited()
        delete_inflight.assert_awaited_once()
        mark_done.assert_not_awaited()
        refund_reservation.assert_not_awaited()
        self.assertEqual(len(fake_queue.lpush_calls), 1)
        queued_payload = json.loads(fake_queue.lpush_calls[0][1])
        self.assertEqual(queued_payload.get("moderation_wait_attempt"), 1)
    async def test_group_trigger_clean_status_continues_for_trusted_chat(self):
        fake_queue = _FakeQueueRedis()
        queue_worker.REDIS_QUEUE = fake_queue
        queue_worker.CHATTY_MODE = False
        queue_worker.TYPING_ENABLED = True
        queue_worker.TYPING_SKIP_GROUPS = False
        queue_worker.BOT = types.SimpleNamespace(send_chat_action=AsyncMock(), send_message=AsyncMock())
        setattr(queue_worker.settings, "ALLOWED_GROUP_IDS", [-100913])
        setattr(queue_worker.settings, "COMMENT_TARGET_CHAT_IDS", [])
        setattr(queue_worker.settings, "COMMENT_SOURCE_CHANNEL_IDS", [])

        redis_stub = types.SimpleNamespace(hget=AsyncMock(return_value="clean"), set=AsyncMock())
        queue_worker.get_redis = lambda: redis_stub

        refund_reservation = AsyncMock(return_value=None)
        respond_mock = AsyncMock(return_value="reply")

        job = {
            "chat_id": -100913,
            "user_id": 913,
            "text": "hello",
            "msg_id": 213,
            "reply_to": 213,
            "is_group": True,
            "is_channel_post": False,
            "trigger": "mention",
            "reservation_id": 337,
        }

        with patch.object(queue_worker, "_mark_done_if_inflight", AsyncMock(return_value=1)),              patch.object(queue_worker, "_delete_if_inflight", AsyncMock(return_value=1)) as delete_inflight,              patch.object(queue_worker, "_delete_if_chatbusy_owner", AsyncMock(return_value=1)),              patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()),              patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()),              patch.object(queue_worker, "_heartbeat_key", AsyncMock()),              patch.object(queue_worker, "_heartbeat_inflight", AsyncMock()),              patch.object(queue_worker, "respond_to_user", respond_mock),              patch.object(queue_worker, "_typing_during_generation", AsyncMock(return_value=None)),              patch.object(queue_worker, "_send_reply", AsyncMock(return_value=types.SimpleNamespace(message_id=1006))),              patch.object(queue_worker, "refund_reservation_by_id", refund_reservation),              patch.object(queue_worker, "confirm_reservation_by_id", AsyncMock(return_value=None)),              patch.object(queue_worker, "_get_backlog", AsyncMock(return_value=0)):
            await queue_worker.handle_job(json.dumps(job), "q:in:processing")

        respond_mock.assert_awaited_once()
        refund_reservation.assert_not_awaited()

    async def test_channel_post_trigger_requeues_when_moderation_signal_missing(self):
        fake_queue = _FakeQueueRedis()
        queue_worker.REDIS_QUEUE = fake_queue
        queue_worker.CHATTY_MODE = False
        queue_worker.TYPING_ENABLED = False

        redis_stub = types.SimpleNamespace(hget=AsyncMock(return_value=None), set=AsyncMock())
        queue_worker.get_redis = lambda: redis_stub

        mark_done = AsyncMock(return_value=1)
        refund_reservation = AsyncMock(return_value=None)
        respond_mock = AsyncMock(return_value="reply")

        job = {
            "chat_id": -100913,
            "user_id": 913,
            "text": "hello",
            "msg_id": 213,
            "reply_to": 213,
            "is_group": True,
            "is_channel_post": True,
            "trigger": "channel_post",
            "reservation_id": 337,
        }

        with patch.object(queue_worker, "MODERATION_STATUS_WAIT_SEC", 0.0),              patch.object(queue_worker, "_mark_done_if_inflight", mark_done),              patch.object(queue_worker, "_delete_if_inflight", AsyncMock(return_value=1)) as delete_inflight,              patch.object(queue_worker, "_delete_if_chatbusy_owner", AsyncMock(return_value=1)),              patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()),              patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()),              patch.object(queue_worker, "_heartbeat_key", AsyncMock()),              patch.object(queue_worker, "_heartbeat_inflight", AsyncMock()),              patch.object(queue_worker, "respond_to_user", respond_mock),              patch.object(queue_worker, "_send_reply", AsyncMock(return_value=types.SimpleNamespace(message_id=1006))),              patch.object(queue_worker, "refund_reservation_by_id", refund_reservation),              patch.object(queue_worker, "confirm_reservation_by_id", AsyncMock(return_value=None)),              patch.object(queue_worker, "_get_backlog", AsyncMock(return_value=0)):
            await queue_worker.handle_job(json.dumps(job), "q:in:processing")

        respond_mock.assert_not_awaited()
        delete_inflight.assert_awaited_once()
        mark_done.assert_not_awaited()
        refund_reservation.assert_not_awaited()
        self.assertEqual(len(fake_queue.lpush_calls), 1)
    async def test_group_trigger_continues_when_moderation_status_clean(self):
        fake_queue = _FakeQueueRedis()
        queue_worker.REDIS_QUEUE = fake_queue
        queue_worker.CHATTY_MODE = False
        queue_worker.TYPING_ENABLED = False

        redis_stub = types.SimpleNamespace(hget=AsyncMock(return_value="clean"), set=AsyncMock())
        queue_worker.get_redis = lambda: redis_stub

        respond_mock = AsyncMock(return_value="reply")

        job = {
            "chat_id": -100912,
            "user_id": 912,
            "text": "hello",
            "msg_id": 212,
            "reply_to": 212,
            "is_group": True,
            "is_channel_post": False,
            "trigger": "mention",
            "reservation_id": 336,
        }

        with patch.object(queue_worker, "_mark_done_if_inflight", AsyncMock(return_value=1)),              patch.object(queue_worker, "_delete_if_inflight", AsyncMock(return_value=1)) as delete_inflight,              patch.object(queue_worker, "_delete_if_chatbusy_owner", AsyncMock(return_value=1)),              patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()),              patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()),              patch.object(queue_worker, "_heartbeat_key", AsyncMock()),              patch.object(queue_worker, "_heartbeat_inflight", AsyncMock()),              patch.object(queue_worker, "respond_to_user", respond_mock),              patch.object(queue_worker, "_send_reply", AsyncMock(return_value=types.SimpleNamespace(message_id=1005))),              patch.object(queue_worker, "refund_reservation_by_id", AsyncMock(return_value=None)),              patch.object(queue_worker, "confirm_reservation_by_id", AsyncMock(return_value=None)),              patch.object(queue_worker, "_get_backlog", AsyncMock(return_value=0)):
            await queue_worker.handle_job(json.dumps(job), "q:in:processing")

        respond_mock.assert_awaited_once()

    async def test_group_trigger_continues_when_moderation_disabled_for_trusted_destination(self):
        fake_queue = _FakeQueueRedis()
        queue_worker.REDIS_QUEUE = fake_queue
        queue_worker.CHATTY_MODE = False
        queue_worker.TYPING_ENABLED = False

        redis_stub = types.SimpleNamespace(hget=AsyncMock(return_value=None), set=AsyncMock())
        queue_worker.get_redis = lambda: redis_stub

        respond_mock = AsyncMock(return_value="reply")

        prev_enable = getattr(queue_worker.settings, "ENABLE_MODERATION", True)
        prev_allowed = getattr(queue_worker.settings, "ALLOWED_GROUP_IDS", [])
        prev_targets = getattr(queue_worker.settings, "COMMENT_TARGET_CHAT_IDS", [])
        prev_sources = getattr(queue_worker.settings, "COMMENT_SOURCE_CHANNEL_IDS", [])
        setattr(queue_worker.settings, "ENABLE_MODERATION", False)
        setattr(queue_worker.settings, "ALLOWED_GROUP_IDS", [-100912])
        setattr(queue_worker.settings, "COMMENT_TARGET_CHAT_IDS", [])
        setattr(queue_worker.settings, "COMMENT_SOURCE_CHANNEL_IDS", [])

        job = {
            "chat_id": -100912,
            "user_id": 912,
            "text": "hello",
            "msg_id": 312,
            "reply_to": 312,
            "is_group": True,
            "is_channel_post": False,
            "trigger": "mention",
            "reservation_id": 1336,
        }

        try:
            with patch.object(queue_worker, "_mark_done_if_inflight", AsyncMock(return_value=1)),                  patch.object(queue_worker, "_delete_if_inflight", AsyncMock(return_value=1)) as delete_inflight,                  patch.object(queue_worker, "_delete_if_chatbusy_owner", AsyncMock(return_value=1)),                  patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()),                  patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()),                  patch.object(queue_worker, "_heartbeat_key", AsyncMock()),                  patch.object(queue_worker, "_heartbeat_inflight", AsyncMock()),                  patch.object(queue_worker, "respond_to_user", respond_mock),                  patch.object(queue_worker, "_send_reply", AsyncMock(return_value=types.SimpleNamespace(message_id=1105))),                  patch.object(queue_worker, "refund_reservation_by_id", AsyncMock(return_value=None)),                  patch.object(queue_worker, "confirm_reservation_by_id", AsyncMock(return_value=None)),                  patch.object(queue_worker, "_get_backlog", AsyncMock(return_value=0)):
                await queue_worker.handle_job(json.dumps(job), "q:in:processing")

            redis_stub.hget.assert_not_awaited()
            respond_mock.assert_awaited_once()
        finally:
            setattr(queue_worker.settings, "ENABLE_MODERATION", prev_enable)
            setattr(queue_worker.settings, "ALLOWED_GROUP_IDS", prev_allowed)
            setattr(queue_worker.settings, "COMMENT_TARGET_CHAT_IDS", prev_targets)
            setattr(queue_worker.settings, "COMMENT_SOURCE_CHANNEL_IDS", prev_sources)

    async def test_group_trigger_continues_when_moderation_disabled_for_comment_source_only_context(self):
        fake_queue = _FakeQueueRedis()
        queue_worker.REDIS_QUEUE = fake_queue
        queue_worker.CHATTY_MODE = False
        queue_worker.TYPING_ENABLED = False

        redis_stub = types.SimpleNamespace(hget=AsyncMock(return_value=None), set=AsyncMock())
        queue_worker.get_redis = lambda: redis_stub

        respond_mock = AsyncMock(return_value="reply")

        prev_enable = getattr(queue_worker.settings, "ENABLE_MODERATION", True)
        prev_allowed = getattr(queue_worker.settings, "ALLOWED_GROUP_IDS", [])
        prev_targets = getattr(queue_worker.settings, "COMMENT_TARGET_CHAT_IDS", [])
        prev_sources = getattr(queue_worker.settings, "COMMENT_SOURCE_CHANNEL_IDS", [])
        setattr(queue_worker.settings, "ENABLE_MODERATION", False)
        setattr(queue_worker.settings, "ALLOWED_GROUP_IDS", [])
        setattr(queue_worker.settings, "COMMENT_TARGET_CHAT_IDS", [])
        setattr(queue_worker.settings, "COMMENT_SOURCE_CHANNEL_IDS", [-100777000])

        job = {
            "chat_id": -100999,
            "user_id": 999,
            "text": "hello from comments",
            "msg_id": 399,
            "reply_to": 399,
            "is_group": True,
            "is_channel_post": False,
            "is_comment_context": True,
            "channel_id": None,
            "linked_chat_id": -100777000,
            "trigger": "mention",
            "reservation_id": 1399,
        }

        try:
            with patch.object(queue_worker, "_mark_done_if_inflight", AsyncMock(return_value=1)),                  patch.object(queue_worker, "_delete_if_inflight", AsyncMock(return_value=1)) as delete_inflight,                  patch.object(queue_worker, "_delete_if_chatbusy_owner", AsyncMock(return_value=1)),                  patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()),                  patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()),                  patch.object(queue_worker, "_heartbeat_key", AsyncMock()),                  patch.object(queue_worker, "_heartbeat_inflight", AsyncMock()),                  patch.object(queue_worker, "respond_to_user", respond_mock),                  patch.object(queue_worker, "_send_reply", AsyncMock(return_value=types.SimpleNamespace(message_id=1199))),                  patch.object(queue_worker, "refund_reservation_by_id", AsyncMock(return_value=None)),                  patch.object(queue_worker, "confirm_reservation_by_id", AsyncMock(return_value=None)),                  patch.object(queue_worker, "_get_backlog", AsyncMock(return_value=0)):
                await queue_worker.handle_job(json.dumps(job), "q:in:processing")

            redis_stub.hget.assert_not_awaited()
            respond_mock.assert_awaited_once()
        finally:
            setattr(queue_worker.settings, "ENABLE_MODERATION", prev_enable)
            setattr(queue_worker.settings, "ALLOWED_GROUP_IDS", prev_allowed)
            setattr(queue_worker.settings, "COMMENT_TARGET_CHAT_IDS", prev_targets)
            setattr(queue_worker.settings, "COMMENT_SOURCE_CHANNEL_IDS", prev_sources)

    async def test_group_trigger_skips_when_moderation_disabled_without_matching_comment_source_channel(self):
        fake_queue = _FakeQueueRedis()
        queue_worker.REDIS_QUEUE = fake_queue
        queue_worker.CHATTY_MODE = False
        queue_worker.TYPING_ENABLED = False

        redis_stub = types.SimpleNamespace(hget=AsyncMock(return_value=None), set=AsyncMock())
        queue_worker.get_redis = lambda: redis_stub

        respond_mock = AsyncMock(return_value="reply")
        refund_reservation = AsyncMock(return_value=None)

        prev_enable = getattr(queue_worker.settings, "ENABLE_MODERATION", True)
        prev_allowed = getattr(queue_worker.settings, "ALLOWED_GROUP_IDS", [])
        prev_targets = getattr(queue_worker.settings, "COMMENT_TARGET_CHAT_IDS", [])
        prev_sources = getattr(queue_worker.settings, "COMMENT_SOURCE_CHANNEL_IDS", [])
        setattr(queue_worker.settings, "ENABLE_MODERATION", False)
        setattr(queue_worker.settings, "ALLOWED_GROUP_IDS", [])
        setattr(queue_worker.settings, "COMMENT_TARGET_CHAT_IDS", [])
        setattr(queue_worker.settings, "COMMENT_SOURCE_CHANNEL_IDS", [-100777000])

        job = {
            "chat_id": -100999,
            "user_id": 999,
            "text": "hello from comments",
            "msg_id": 499,
            "reply_to": 499,
            "is_group": True,
            "is_channel_post": False,
            "is_comment_context": True,
            "channel_id": None,
            "linked_chat_id": -100123456,
            "trigger": "mention",
            "reservation_id": 1499,
        }

        mark_done = AsyncMock(return_value=1)
        try:
            with patch.object(queue_worker, "_mark_done_if_inflight", mark_done),                  patch.object(queue_worker, "_delete_if_inflight", AsyncMock(return_value=1)) as delete_inflight,                  patch.object(queue_worker, "_delete_if_chatbusy_owner", AsyncMock(return_value=1)),                  patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()),                  patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()),                  patch.object(queue_worker, "_heartbeat_key", AsyncMock()),                  patch.object(queue_worker, "_heartbeat_inflight", AsyncMock()),                  patch.object(queue_worker, "respond_to_user", respond_mock),                  patch.object(queue_worker, "_send_reply", AsyncMock(return_value=types.SimpleNamespace(message_id=1299))),                  patch.object(queue_worker, "refund_reservation_by_id", refund_reservation),                  patch.object(queue_worker, "confirm_reservation_by_id", AsyncMock(return_value=None)),                  patch.object(queue_worker, "_get_backlog", AsyncMock(return_value=0)):
                await queue_worker.handle_job(json.dumps(job), "q:in:processing")

            redis_stub.hget.assert_not_awaited()
            respond_mock.assert_not_awaited()
            refund_reservation.assert_awaited_once_with(1499)
            mark_done.assert_awaited()
        finally:
            setattr(queue_worker.settings, "ENABLE_MODERATION", prev_enable)
            setattr(queue_worker.settings, "ALLOWED_GROUP_IDS", prev_allowed)
            setattr(queue_worker.settings, "COMMENT_TARGET_CHAT_IDS", prev_targets)
            setattr(queue_worker.settings, "COMMENT_SOURCE_CHANNEL_IDS", prev_sources)

    async def test_group_trigger_skips_when_moderation_disabled_comment_context_without_source_config(self):
        fake_queue = _FakeQueueRedis()
        queue_worker.REDIS_QUEUE = fake_queue
        queue_worker.CHATTY_MODE = False
        queue_worker.TYPING_ENABLED = False

        redis_stub = types.SimpleNamespace(hget=AsyncMock(return_value=None), set=AsyncMock())
        queue_worker.get_redis = lambda: redis_stub

        respond_mock = AsyncMock(return_value="reply")
        refund_reservation = AsyncMock(return_value=None)

        prev_enable = getattr(queue_worker.settings, "ENABLE_MODERATION", True)
        prev_allowed = getattr(queue_worker.settings, "ALLOWED_GROUP_IDS", [])
        prev_targets = getattr(queue_worker.settings, "COMMENT_TARGET_CHAT_IDS", [])
        prev_sources = getattr(queue_worker.settings, "COMMENT_SOURCE_CHANNEL_IDS", [])
        setattr(queue_worker.settings, "ENABLE_MODERATION", False)
        setattr(queue_worker.settings, "ALLOWED_GROUP_IDS", [])
        setattr(queue_worker.settings, "COMMENT_TARGET_CHAT_IDS", [])
        setattr(queue_worker.settings, "COMMENT_SOURCE_CHANNEL_IDS", [])

        job = {
            "chat_id": -100999,
            "user_id": 999,
            "text": "hello from comments",
            "msg_id": 599,
            "reply_to": 599,
            "is_group": True,
            "is_channel_post": False,
            "is_comment_context": True,
            "linked_chat_id": -100777000,
            "trigger": "mention",
            "reservation_id": 1599,
        }

        mark_done = AsyncMock(return_value=1)
        try:
            with patch.object(queue_worker, "_mark_done_if_inflight", mark_done),                  patch.object(queue_worker, "_delete_if_inflight", AsyncMock(return_value=1)) as delete_inflight,                  patch.object(queue_worker, "_delete_if_chatbusy_owner", AsyncMock(return_value=1)),                  patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()),                  patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()),                  patch.object(queue_worker, "_heartbeat_key", AsyncMock()),                  patch.object(queue_worker, "_heartbeat_inflight", AsyncMock()),                  patch.object(queue_worker, "respond_to_user", respond_mock),                  patch.object(queue_worker, "_send_reply", AsyncMock(return_value=types.SimpleNamespace(message_id=1399))),                  patch.object(queue_worker, "refund_reservation_by_id", refund_reservation),                  patch.object(queue_worker, "confirm_reservation_by_id", AsyncMock(return_value=None)),                  patch.object(queue_worker, "_get_backlog", AsyncMock(return_value=0)):
                await queue_worker.handle_job(json.dumps(job), "q:in:processing")

            redis_stub.hget.assert_not_awaited()
            respond_mock.assert_not_awaited()
            refund_reservation.assert_awaited_once_with(1599)
            mark_done.assert_awaited()
        finally:
            setattr(queue_worker.settings, "ENABLE_MODERATION", prev_enable)
            setattr(queue_worker.settings, "ALLOWED_GROUP_IDS", prev_allowed)
            setattr(queue_worker.settings, "COMMENT_TARGET_CHAT_IDS", prev_targets)
            setattr(queue_worker.settings, "COMMENT_SOURCE_CHANNEL_IDS", prev_sources)

    async def test_handle_job_ignores_knowledge_owner_id_for_bot_worker(self):
        fake_queue = _FakeQueueRedis()
        queue_worker.REDIS_QUEUE = fake_queue
        queue_worker.CHATTY_MODE = False
        queue_worker.TYPING_ENABLED = False

        redis_stub = types.SimpleNamespace(set=AsyncMock())
        queue_worker.get_redis = lambda: redis_stub

        respond_mock = AsyncMock(return_value="reply")

        job = {
            "chat_id": 12345,
            "user_id": 555,
            "text": "hello",
            "msg_id": 42,
            "reply_to": 42,
            "is_group": False,
            "is_channel_post": False,
            "reservation_id": 777,
            "knowledge_owner_id": 9988,
        }

        with patch.object(queue_worker, "_mark_done_if_inflight", AsyncMock(return_value=1)),              patch.object(queue_worker, "_delete_if_inflight", AsyncMock(return_value=1)) as delete_inflight,              patch.object(queue_worker, "_delete_if_chatbusy_owner", AsyncMock(return_value=1)),              patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()),              patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()),              patch.object(queue_worker, "_heartbeat_key", AsyncMock()),              patch.object(queue_worker, "_heartbeat_inflight", AsyncMock()),              patch.object(queue_worker, "respond_to_user", respond_mock),              patch.object(queue_worker, "_send_reply", AsyncMock(return_value=types.SimpleNamespace(message_id=1006))),              patch.object(queue_worker, "refund_reservation_by_id", AsyncMock(return_value=None)),              patch.object(queue_worker, "confirm_reservation_by_id", AsyncMock(return_value=None)),              patch.object(queue_worker, "_get_backlog", AsyncMock(return_value=0)):
            await queue_worker.handle_job(json.dumps(job), "q:in:processing")

        respond_mock.assert_awaited_once()
        self.assertIsNone(respond_mock.await_args.kwargs.get("knowledge_owner_id"))

    async def test_moderation_status_read_error_is_fail_open(self):
        fake_queue = _FakeQueueRedis()
        queue_worker.REDIS_QUEUE = fake_queue
        queue_worker.CHATTY_MODE = False
        queue_worker.TYPING_ENABLED = False

        redis_stub = types.SimpleNamespace(hget=AsyncMock(side_effect=RuntimeError("redis down")), set=AsyncMock())
        queue_worker.get_redis = lambda: redis_stub

        respond_mock = AsyncMock(return_value="reply")

        job = {
            "chat_id": 910,
            "user_id": 910,
            "text": "hello",
            "msg_id": 201,
            "reply_to": 201,
            "is_group": False,
            "is_channel_post": False,
            "reservation_id": 334,
        }

        with patch.object(queue_worker, "_mark_done_if_inflight", AsyncMock(return_value=1)),              patch.object(queue_worker, "_delete_if_inflight", AsyncMock(return_value=1)) as delete_inflight,              patch.object(queue_worker, "_delete_if_chatbusy_owner", AsyncMock(return_value=1)),              patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()),              patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()),              patch.object(queue_worker, "_heartbeat_key", AsyncMock()),              patch.object(queue_worker, "_heartbeat_inflight", AsyncMock()),              patch.object(queue_worker, "respond_to_user", respond_mock),              patch.object(queue_worker, "_send_reply", AsyncMock(return_value=types.SimpleNamespace(message_id=1003))),              patch.object(queue_worker, "refund_reservation_by_id", AsyncMock(return_value=None)),              patch.object(queue_worker, "confirm_reservation_by_id", AsyncMock(return_value=None)),              patch.object(queue_worker, "_get_backlog", AsyncMock(return_value=0)):
            await queue_worker.handle_job(json.dumps(job), "q:in:processing")

        respond_mock.assert_awaited_once()

    async def test_check_on_topic_without_tag_hits_finishes_without_responder(self):
        queue_worker.REDIS_QUEUE = _FakeQueueRedis()
        queue_worker.CHATTY_MODE = False
        queue_worker.TYPING_ENABLED = False

        redis_stub = types.SimpleNamespace(hget=AsyncMock(return_value="clean"), set=AsyncMock())
        queue_worker.get_redis = lambda: redis_stub

        mark_done = AsyncMock(return_value=1)
        refund_reservation = AsyncMock(return_value=None)
        respond_mock = AsyncMock(return_value="reply")

        job = {
            "chat_id": -10077,
            "user_id": 77,
            "text": "hello team",
            "msg_id": 301,
            "reply_to": 301,
            "is_group": True,
            "is_channel_post": False,
            "trigger": "check_on_topic",
            "reservation_id": 700,
            "knowledge_owner_id": 900,
        }

        with patch.object(queue_worker, "_mark_done_if_inflight", mark_done),              patch.object(queue_worker, "_delete_if_inflight", AsyncMock(return_value=1)) as delete_inflight,              patch.object(queue_worker, "_delete_if_chatbusy_owner", AsyncMock(return_value=1)),              patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()),              patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()),              patch.object(queue_worker, "_heartbeat_key", AsyncMock()),              patch.object(queue_worker, "_heartbeat_inflight", AsyncMock()),              patch.object(queue_worker, "is_relevant", AsyncMock(return_value=(False, None))),              patch.object(queue_worker, "_get_query_embedding", AsyncMock(return_value=[0.1, 0.2])),              patch.object(queue_worker, "respond_to_user", respond_mock),              patch.object(queue_worker, "refund_reservation_by_id", refund_reservation),              patch.object(queue_worker, "confirm_reservation_by_id", AsyncMock(return_value=None)),              patch.object(queue_worker, "_get_backlog", AsyncMock(return_value=0)):
            await queue_worker.handle_job(json.dumps(job), "q:in:processing")

        respond_mock.assert_not_awaited()
        mark_done.assert_awaited()
        refund_reservation.assert_awaited_once_with(700)

    async def test_check_on_topic_with_tag_hits_passes_precomputed_payload(self):
        queue_worker.REDIS_QUEUE = _FakeQueueRedis()
        queue_worker.CHATTY_MODE = False
        queue_worker.TYPING_ENABLED = False

        redis_stub = types.SimpleNamespace(hget=AsyncMock(return_value="clean"), set=AsyncMock())
        queue_worker.get_redis = lambda: redis_stub

        respond_mock = AsyncMock(return_value="reply")
        tag_hits = [(0.88, "id1", "chunk")]
        query_embedding = [0.11, 0.22]

        job = {
            "chat_id": -10078,
            "user_id": 78,
            "text": "hello team",
            "msg_id": 302,
            "reply_to": 302,
            "is_group": True,
            "is_channel_post": False,
            "trigger": "check_on_topic",
            "reservation_id": 701,
            "knowledge_owner_id": 901,
        }

        with patch.object(queue_worker, "_mark_done_if_inflight", AsyncMock(return_value=1)),              patch.object(queue_worker, "_delete_if_inflight", AsyncMock(return_value=1)) as delete_inflight,              patch.object(queue_worker, "_delete_if_chatbusy_owner", AsyncMock(return_value=1)),              patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()),              patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()),              patch.object(queue_worker, "_heartbeat_key", AsyncMock()),              patch.object(queue_worker, "_heartbeat_inflight", AsyncMock()),              patch.object(queue_worker, "is_relevant", AsyncMock(return_value=(True, None))),              patch.object(queue_worker, "_get_query_embedding", AsyncMock(return_value=query_embedding)),              patch.object(queue_worker, "respond_to_user", respond_mock),              patch.object(queue_worker, "_send_reply", AsyncMock(return_value=types.SimpleNamespace(message_id=9003))),              patch.object(queue_worker, "refund_reservation_by_id", AsyncMock(return_value=None)),              patch.object(queue_worker, "confirm_reservation_by_id", AsyncMock(return_value=None)),              patch.object(queue_worker, "_get_backlog", AsyncMock(return_value=0)):
            await queue_worker.handle_job(json.dumps(job), "q:in:processing")

        respond_mock.assert_awaited_once()
        self.assertIsNone(respond_mock.await_args.kwargs.get("precomputed_rag_hits"))
        self.assertEqual(respond_mock.await_args.kwargs.get("query_embedding"), query_embedding)
        self.assertEqual(respond_mock.await_args.kwargs.get("embedding_model"), queue_worker.settings.EMBEDDING_MODEL)
        self.assertTrue(respond_mock.await_args.kwargs.get("skip_autoreply_strict_gate"))

    async def test_check_on_topic_requeues_when_moderation_signal_missing(self):
        fake_queue = _FakeQueueRedis()
        queue_worker.REDIS_QUEUE = fake_queue
        queue_worker.CHATTY_MODE = False
        queue_worker.TYPING_ENABLED = False

        redis_stub = types.SimpleNamespace(hget=AsyncMock(return_value=None), set=AsyncMock())
        queue_worker.get_redis = lambda: redis_stub

        mark_done = AsyncMock(return_value=1)
        refund_reservation = AsyncMock(return_value=None)
        respond_mock = AsyncMock(return_value="reply")

        job = {
            "chat_id": -10079,
            "user_id": 79,
            "text": "hello team",
            "msg_id": 303,
            "reply_to": 303,
            "is_group": True,
            "is_channel_post": False,
            "trigger": "check_on_topic",
            "reservation_id": 702,
        }

        with patch.object(queue_worker, "_mark_done_if_inflight", mark_done),              patch.object(queue_worker, "_delete_if_inflight", AsyncMock(return_value=1)) as delete_inflight,              patch.object(queue_worker, "_delete_if_chatbusy_owner", AsyncMock(return_value=1)),              patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()),              patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()),              patch.object(queue_worker, "_heartbeat_key", AsyncMock()),              patch.object(queue_worker, "_heartbeat_inflight", AsyncMock()),              patch.object(queue_worker, "is_relevant", AsyncMock(return_value=(True, None))),              patch.object(queue_worker, "_get_query_embedding", AsyncMock(return_value=[0.1, 0.2])),              patch.object(queue_worker, "respond_to_user", respond_mock),              patch.object(queue_worker, "refund_reservation_by_id", refund_reservation),              patch.object(queue_worker, "confirm_reservation_by_id", AsyncMock(return_value=None)),              patch.object(queue_worker, "_get_backlog", AsyncMock(return_value=0)),              patch.object(queue_worker, "MODERATION_STATUS_WAIT_SEC", 0.0):
            await queue_worker.handle_job(json.dumps(job), "q:in:processing")

        respond_mock.assert_not_awaited()
        delete_inflight.assert_awaited_once()
        mark_done.assert_not_awaited()
        refund_reservation.assert_not_awaited()
        self.assertEqual(len(fake_queue.lpush_calls), 1)
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
