import asyncio
import importlib.util
import json
import pathlib
import sys
import types
import unittest
from unittest.mock import patch


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
    )

    async def _noop_async(*_args, **_kwargs):
        return None

    fake_bot_debouncer.compute_typing_delay = lambda *_args, **_kwargs: 0.0
    fake_tg_client.get_bot = lambda: types.SimpleNamespace()
    fake_clients.openai_client = fake_openai_client
    fake_responder.respond_to_user = _noop_async
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
    spec = importlib.util.spec_from_file_location("queue_worker_sweeper_starvation", worker_path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, module_overrides):
        sys.modules["queue_worker_sweeper_starvation"] = module
        spec.loader.exec_module(module)
    return module


queue_worker = _load_queue_worker()


class _FakeRedis:
    def __init__(self, processing_key, queue_key, processing_items, job_values):
        self.processing_key = processing_key
        self.queue_key = queue_key
        self.lists = {
            processing_key: list(processing_items),
            queue_key: [],
        }
        self.kv = dict(job_values)
        self.set_calls = []

    async def llen(self, key):
        return len(self.lists.get(key, []))

    async def lrange(self, key, start, end):
        values = list(self.lists.get(key, []))
        if not values:
            return []
        norm_start = max(start, 0)
        norm_end = len(values) - 1 if end == -1 else min(end, len(values) - 1)
        if norm_start > norm_end:
            return []
        return values[norm_start : norm_end + 1]

    async def get(self, key):
        return self.kv.get(key)

    async def exists(self, key):
        return 1 if key in self.kv else 0

    async def set(self, key, value, ex=None, nx=False):
        self.set_calls.append((key, value, ex, nx))
        if nx and key in self.kv:
            return False
        self.kv[key] = value
        return True

    async def lrem(self, key, count, value):
        if count != 1:
            raise AssertionError("Only count=1 is supported")
        items = self.lists.get(key, [])
        for idx, raw in enumerate(items):
            if raw == value:
                del items[idx]
                return 1
        return 0

    async def rpush(self, key, *values):
        self.lists.setdefault(key, []).extend(values)
        return len(self.lists[key])

    async def scan_iter(self, match=None, count=None):
        if False:
            yield None


class QueueWorkerSweeperStarvationTests(unittest.IsolatedAsyncioTestCase):
    async def test_sweeper_eventually_reclaims_stale_head_with_tail_growth(self):
        queue_key = "q:in"
        processing_key = f"{queue_key}:processing"

        stale = json.dumps({"chat_id": 1, "msg_id": 1})
        fresh_1 = json.dumps({"chat_id": 2, "msg_id": 2})
        fresh_2 = json.dumps({"chat_id": 3, "msg_id": 3})
        fresh_3 = json.dumps({"chat_id": 4, "msg_id": 4})
        fresh_4 = json.dumps({"chat_id": 5, "msg_id": 5})

        fake_redis = _FakeRedis(
            processing_key=processing_key,
            queue_key=queue_key,
            processing_items=[stale, fresh_1, fresh_2],
            job_values={
                f"{queue_worker.JOB_KEY_PREFIX}2:2": "inflight:active",
                f"{queue_worker.JOB_KEY_PREFIX}3:3": "inflight:active",
            },
        )
        queue_worker.REDIS_QUEUE = fake_redis

        stop_evt = asyncio.Event()
        ticks = 0

        async def _sleep(_seconds):
            nonlocal ticks
            ticks += 1
            if ticks == 1:
                fake_redis.lists[processing_key].append(fresh_3)
                fake_redis.kv[f"{queue_worker.JOB_KEY_PREFIX}4:4"] = "inflight:active"
            elif ticks == 2:
                fake_redis.lists[processing_key].append(fresh_4)
                fake_redis.kv[f"{queue_worker.JOB_KEY_PREFIX}5:5"] = "inflight:active"
            elif ticks >= 4:
                stop_evt.set()

        with patch.object(queue_worker, "PROCESSING_SWEEP_BATCH", 2), patch.object(
            queue_worker, "PROCESSING_SWEEP_INTERVAL", 1
        ), patch.object(queue_worker.asyncio, "sleep", new=_sleep):
            await queue_worker._sweeper_loop(stop_evt, queue_key, processing_key)

        self.assertNotIn(stale, fake_redis.lists[processing_key])
        self.assertIn(stale, fake_redis.lists[queue_key])

        job_key = f"{queue_worker.JOB_KEY_PREFIX}1:1"
        reclaim_calls = [call for call in fake_redis.set_calls if call[0] == job_key]
        self.assertEqual(len(reclaim_calls), 1)
        self.assertTrue(reclaim_calls[0][1].startswith("inflight:reclaim:"))
        self.assertEqual(reclaim_calls[0][2], queue_worker.JOB_RECLAIM_TTL)
        self.assertTrue(reclaim_calls[0][3])


if __name__ == "__main__":
    unittest.main()
