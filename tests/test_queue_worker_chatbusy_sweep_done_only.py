import importlib.util
import json
import pathlib
import sys
import types
import unittest
import tempfile
from contextlib import asynccontextmanager
from fnmatch import fnmatch
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

    class _Exc(Exception):
        pass

    fake_aiogram_enums.ChatAction = _ChatAction
    fake_aiogram_types.Message = _Message
    fake_aiogram_exceptions.TelegramBadRequest = _Exc
    fake_aiogram_exceptions.TelegramRetryAfter = _Exc
    fake_aiogram_exceptions.TelegramNetworkError = _Exc
    fake_aiogram_exceptions.TelegramForbiddenError = _Exc

    fake_config.settings = types.SimpleNamespace(
        CHATTY_MODE=False,
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
    fake_tg_client.get_bot = lambda: types.SimpleNamespace()
    fake_openai_client.get_openai = lambda: None
    fake_openai_client.transcribe_audio_with_retry = lambda **_kwargs: ""
    fake_openai_client.classify_openai_error = lambda _exc: "other"
    fake_clients.openai_client = fake_openai_client

    async def _respond_ok(*_args, **_kwargs):
        return "ok"

    async def _noop_async(*_args, **_kwargs):
        return None

    fake_responder.respond_to_user = _respond_ok

    async def _no_hits(*_args, **_kwargs):
        return []

    async def _query_embedding(*_args, **_kwargs):
        return None

    fake_responder_keyword.find_tag_hits = _no_hits
    fake_responder_knowledge._get_query_embedding = _query_embedding
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
    fake_embedding_utils.get_rag_embedding_model = lambda: None

    fake_dialog_logger.start_dialog_logger = _noop_async
    fake_dialog_logger.shutdown_dialog_logger = _noop_async

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
        "app.core.queue_recovery": fake_queue_recovery,
        "app.core.temp_files": fake_temp_files,
        "aiogram": fake_aiogram,
        "aiogram.enums": fake_aiogram_enums,
        "aiogram.types": fake_aiogram_types,
        "aiogram.exceptions": fake_aiogram_exceptions,
    }

    worker_path = pathlib.Path(__file__).resolve().parents[1] / "app" / "tasks" / "queue_worker.py"
    spec = importlib.util.spec_from_file_location("queue_worker_sweep_done_only", worker_path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, module_overrides):
        sys.modules["queue_worker_sweep_done_only"] = module
        spec.loader.exec_module(module)
    return module


queue_worker = _load_queue_worker()


class _FakeRedis:
    def __init__(self):
        self.kv = {}
        self.ttl = {}
        self.lists = {}
        self.lpush_calls = []
        self.lrem_calls = []
        self.set_calls = []

    async def scan_iter(self, match=None, count=None):
        for key in sorted(self.kv.keys()):
            if match is None or fnmatch(key, match):
                yield key

    async def pttl(self, key):
        if key not in self.kv:
            return -2
        return self.ttl.get(key, -1)

    async def get(self, key):
        return self.kv.get(key)

    async def set(self, key, value, ex=None, nx=False):
        self.set_calls.append((key, value, ex, nx))
        if nx and key in self.kv:
            return False
        self.kv[key] = value
        if ex is not None:
            self.ttl[key] = int(ex) * 1000
        return True

    async def delete(self, key):
        existed = key in self.kv
        self.kv.pop(key, None)
        self.ttl.pop(key, None)
        return 1 if existed else 0

    async def lpush(self, key, value):
        self.lpush_calls.append((key, value))
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    async def lrem(self, key, count, value):
        self.lrem_calls.append((key, count, value))
        items = self.lists.get(key, [])
        removed = 0
        if count >= 0:
            idx = 0
            while idx < len(items) and (count == 0 or removed < count):
                if items[idx] == value:
                    items.pop(idx)
                    removed += 1
                else:
                    idx += 1
        else:
            target = -count
            idx = len(items) - 1
            while idx >= 0 and removed < target:
                if items[idx] == value:
                    items.pop(idx)
                    removed += 1
                idx -= 1
        self.lists[key] = items
        return removed

    async def exists(self, key):
        return 1 if key in self.kv else 0


class QueueWorkerChatBusySweepDoneOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def test_sweep_removes_stale_chatbusy_for_done_only_jobs_and_allows_next_job(self):
        redis = _FakeRedis()
        queue_worker.REDIS_QUEUE = redis
        queue_worker.get_redis = lambda: types.SimpleNamespace(set=AsyncMock())
        queue_worker.CHATTY_MODE = False
        queue_worker.TYPING_ENABLED = False

        chat_id = 777
        msg_id = 1001
        stale_busy_key = f"chatbusy:{chat_id}"
        done_job_key = f"q:job:{chat_id}:1000"

        redis.kv[stale_busy_key] = "busy:stale"
        redis.ttl[stale_busy_key] = 0
        redis.kv[done_job_key] = "done:prev"

        job = {
            "chat_id": chat_id,
            "user_id": chat_id,
            "text": "hello",
            "msg_id": msg_id,
            "reply_to": msg_id,
            "is_group": False,
            "is_channel_post": False,
        }
        raw = json.dumps(job)
        processing_key = "q:in:processing"
        redis.lists[processing_key] = [raw]

        self.assertEqual(await redis.exists(stale_busy_key), 1)

        await queue_worker._sweep_chatbusy(redis)

        self.assertEqual(await redis.exists(stale_busy_key), 0)

        queue_worker.BOT = types.SimpleNamespace(send_message=AsyncMock(return_value=types.SimpleNamespace(message_id=111)))

        with patch.object(queue_worker, "_mark_done_if_inflight", AsyncMock(return_value=1)), \
             patch.object(queue_worker, "_delete_if_inflight", AsyncMock(return_value=1)), \
             patch.object(queue_worker, "_delete_if_chatbusy_owner", AsyncMock(return_value=1)), \
             patch.object(queue_worker, "_tg_acquire_permit", AsyncMock()), \
             patch.object(queue_worker, "_tg_acquire_chat_permit", AsyncMock()), \
             patch.object(queue_worker, "_heartbeat_key", AsyncMock()), \
             patch.object(queue_worker, "_heartbeat_inflight", AsyncMock()), \
             patch.object(queue_worker, "respond_to_user", AsyncMock(return_value="reply")), \
             patch.object(queue_worker, "_get_backlog", AsyncMock(return_value=0)), \
             patch.object(queue_worker, "push_message", AsyncMock()), \
             patch.object(queue_worker, "_mark_sent_reply_keys", AsyncMock()):
            await queue_worker.handle_job(raw, processing_key)

        self.assertFalse(
            any(key == queue_worker.settings.QUEUE_KEY for key, _ in redis.lpush_calls),
            "job must not be requeued due to stale chatbusy lock",
        )
        self.assertTrue(
            any(key == stale_busy_key and nx for key, _value, _ex, nx in redis.set_calls),
            "worker must be able to acquire chatbusy for new job after sweep",
        )


if __name__ == "__main__":
    unittest.main()
