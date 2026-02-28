import importlib.util
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
    fake_tg_client.get_bot = lambda: types.SimpleNamespace()
    fake_openai_client.get_openai = lambda: None
    fake_openai_client.transcribe_audio_with_retry = lambda **_kwargs: ""
    fake_openai_client.classify_openai_error = lambda _exc: "other"
    fake_clients.openai_client = fake_openai_client
    fake_responder.respond_to_user = lambda *_args, **_kwargs: "ok"

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
    spec = importlib.util.spec_from_file_location("queue_worker_under_test", worker_path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, module_overrides):
        sys.modules["queue_worker_under_test"] = module
        spec.loader.exec_module(module)
    return module


queue_worker = _load_queue_worker()


class _FakeRedis:
    def __init__(self, initial=None):
        self.store = dict(initial or {})
        self.eval_calls = []

    async def eval(self, script, numkeys, key, *args):
        self.eval_calls.append((script, numkeys, key, args))
        if "DEL" in script:
            expected = args[0]
            if self.store.get(key) == expected:
                self.store.pop(key, None)
                return 1
            return 0

        expected = args[0]
        return 1 if self.store.get(key) == expected else 0


class QueueWorkerChatBusyOwnerTests(unittest.IsolatedAsyncioTestCase):
    def test_source_contains_expected_anchors(self) -> None:
        source = pathlib.Path(queue_worker.__file__).read_text(encoding="utf-8")
        for anchor in (
            "async def handle_job",
            "async def _heartbeat_key",
            "async def _delete_if_chatbusy_owner",
        ):
            self.assertIn(anchor, source)

    async def test_release_does_not_delete_lock_of_new_owner(self) -> None:
        redis = _FakeRedis({"chatbusy:42": "busy:A"})
        redis.store["chatbusy:42"] = "busy:B"

        deleted = await queue_worker._delete_if_chatbusy_owner(redis, "chatbusy:42", "busy:A")

        self.assertEqual(deleted, 0)
        self.assertEqual(redis.store.get("chatbusy:42"), "busy:B")
        self.assertEqual(len(redis.eval_calls), 1)

    async def test_heartbeat_checks_owner_token(self) -> None:
        redis = _FakeRedis({"chatbusy:42": "busy:B"})

        await queue_worker._heartbeat_key(
            redis,
            "chatbusy:42",
            "busy:A",
            interval=0,
            ttl=5,
        )

        self.assertEqual(len(redis.eval_calls), 1)
        script, numkeys, key, args = redis.eval_calls[0]
        self.assertIn("GET", script)
        self.assertEqual(numkeys, 1)
        self.assertEqual(key, "chatbusy:42")
        self.assertEqual(args[0], "busy:A")
        self.assertEqual(args[1], 5000)

    def test_is_bullet_list_text_detects_only_bullet_lines(self) -> None:
        self.assertTrue(queue_worker._is_bullet_list_text("- one.\n- two;\n- three."))
        self.assertFalse(queue_worker._is_bullet_list_text("- one.\nplain line"))


    def test_is_emoji_multiline_text_detects_emoji_lists(self) -> None:
        self.assertTrue(
            queue_worker._is_emoji_multiline_text(
                "Если ты геймер, у нас много общего 🫶\nКатки, гринд, тиммейты — обсудим всё 🎮\nОтвечаю 24/7"
            )
        )

    async def test_send_chatty_reply_keeps_multiline_emoji_text_in_single_message(self) -> None:
        sent = []

        async def _fake_send_reply(**kwargs):
            sent.append(kwargs)

        with (
            patch.object(queue_worker, "_send_reply", _fake_send_reply),
            patch.object(queue_worker, "compute_typing_delay", lambda *_args, **_kwargs: 0.0),
        ):
            await queue_worker._send_chatty_reply(
                chat_id=42,
                text=(
                    "Если ты геймер, у нас много общего 🫶\n\n"
                    "Катки, гринд, тиммейты, стата, гайды — обсудим всё 🎮\n\n"
                    "Отвечаю 24/7 на текст, голос и картинки"
                ),
                reply_to=100,
                msg_id=200,
                merged_ids=[200],
                user_id=1,
                enable_typing=False,
            )

        self.assertEqual(len(sent), 1)
        self.assertIn("Если ты геймер", sent[0]["text"])
        self.assertIn("обсудим всё 🎮", sent[0]["text"])

    async def test_send_chatty_reply_keeps_text_over_250_chars_in_single_message(self) -> None:
        sent = []

        async def _fake_send_reply(**kwargs):
            sent.append(kwargs)

        long_text = (
            "Это длинный ответ для проверки порога разбиения. "
            "Здесь несколько предложений, чтобы обычная логика могла делить по точкам. "
            "Добавляю ещё одно предложение для уверенного превышения порога. "
            "И финальная фраза, чтобы длина была больше 250 символов и осталась одним сообщением."
        )
        self.assertGreater(len(long_text), 250)

        with (
            patch.object(queue_worker, "_send_reply", _fake_send_reply),
            patch.object(queue_worker, "compute_typing_delay", lambda *_args, **_kwargs: 0.0),
        ):
            await queue_worker._send_chatty_reply(
                chat_id=42,
                text=long_text,
                reply_to=100,
                msg_id=200,
                merged_ids=[200],
                user_id=1,
                enable_typing=False,
            )

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["text"], long_text)

    async def test_send_chatty_reply_keeps_bullet_list_in_single_message(self) -> None:
        sent = []

        async def _fake_send_reply(**kwargs):
            sent.append(kwargs)

        with (
            patch.object(queue_worker, "_send_reply", _fake_send_reply),
            patch.object(queue_worker, "compute_typing_delay", lambda *_args, **_kwargs: 0.0),
        ):
            await queue_worker._send_chatty_reply(
                chat_id=42,
                text="- Первый пункт.\n- Второй пункт;\n- Третий пункт.",
                reply_to=100,
                msg_id=200,
                merged_ids=[200],
                user_id=1,
                enable_typing=False,
            )

        self.assertEqual(len(sent), 1)
        self.assertIn("- Первый пункт.", sent[0]["text"])
        self.assertIn("- Второй пункт;", sent[0]["text"])
        self.assertIn("- Третий пункт.", sent[0]["text"])


if __name__ == "__main__":
    unittest.main()
