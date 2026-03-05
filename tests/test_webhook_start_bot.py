import asyncio
import importlib.util
import contextlib
import pathlib
import sys
import types
import unittest
import importlib.abc
import importlib.machinery
from unittest import mock


class _FakeAsyncFile:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def read(self):
        return "{}"


class _FakeAiofiles(types.ModuleType):
    def open(self, *_args, **_kwargs):
        return _FakeAsyncFile()


class _FakeRedis:
    def __init__(self):
        self.kv = {}

    async def exists(self, _key):
        return False

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.kv:
            return False
        self.kv[key] = value
        return True

    async def expire(self, *_args, **_kwargs):
        return True

    async def get(self, key):
        return self.kv.get(key)

    async def delete(self, key):
        self.kv.pop(key, None)
        return 1


class _FailingRedis(_FakeRedis):
    async def set(self, *_args, **_kwargs):
        raise RuntimeError("redis unavailable")


class _FakeRouter:
    def __init__(self):
        self.post_handlers = {}

    def add_get(self, *_args, **_kwargs):
        return None

    def add_post(self, path, handler, **_kwargs):
        self.post_handlers[path] = handler
        return handler

    def add_route(self, *_args, **_kwargs):
        return None


class _FakeApplication:
    last_instance = None

    def __init__(self):
        self.router = _FakeRouter()
        _FakeApplication.last_instance = self


class _FakeAppRunner:
    def __init__(self, app):
        self.app = app
        self.setup_called = False
        self.cleanup_called = False

    async def setup(self):
        self.setup_called = True

    async def cleanup(self):
        self.cleanup_called = True


class _FakeTCPSite:
    last_instance = None

    def __init__(self, runner, host, port, ssl_context=None):
        self.runner = runner
        self.host = host
        self.port = port
        self.ssl_context = ssl_context
        self.started = False
        _FakeTCPSite.last_instance = self

    async def start(self):
        self.started = True


class _FakeWeb(types.ModuleType):
    class Response:
        def __init__(self, status=200, text=""):
            self.status = status
            self.text = text

    class Request:
        def __init__(self, payload):
            self._payload = payload

        async def json(self):
            return self._payload

    AppRunner = _FakeAppRunner
    TCPSite = _FakeTCPSite
    Application = _FakeApplication


class _FakeSession:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class _FakeBot:
    last_instance = None
    set_webhook_side_effect = None

    def __init__(self):
        self.session = _FakeSession()
        self.last_set_webhook_kwargs = None
        self.set_webhook_calls = []
        _FakeBot.last_instance = self

    async def get_me(self):
        return types.SimpleNamespace(id=42, username="TestBot")

    async def set_webhook(self, **kwargs):
        self.last_set_webhook_kwargs = kwargs
        self.set_webhook_calls.append(kwargs)
        side_effect = _FakeBot.set_webhook_side_effect
        if isinstance(side_effect, Exception):
            raise side_effect
        if callable(side_effect):
            result = side_effect()
            if isinstance(result, Exception):
                raise result
        return True


class _FakeDispatcher:
    calls = 0
    should_fail = False
    start_handler_registered = False
    cmd_start_calls = 0

    async def feed_update(self, *_args, **_kwargs):
        _FakeDispatcher.calls += 1
        if _FakeDispatcher.should_fail:
            raise RuntimeError("dispatcher failed")

        if isinstance(_args[1], dict):
            message = _args[1].get("message")
            if isinstance(message, dict) and message.get("text") == "/start":
                if not _FakeDispatcher.start_handler_registered:
                    raise RuntimeError("start handler not registered")
                _FakeDispatcher.cmd_start_calls += 1
        return None


class _HandlersLoader(importlib.abc.Loader):
    def create_module(self, spec):
        return types.ModuleType(spec.name)

    def exec_module(self, module):
        _FakeDispatcher.start_handler_registered = True


class _HandlersFinder(importlib.abc.MetaPathFinder):
    def __init__(self, fullname):
        self.fullname = fullname
        self.loader = _HandlersLoader()

    def find_spec(self, fullname, path, target=None):
        if fullname == self.fullname:
            return importlib.machinery.ModuleSpec(fullname, self.loader)
        return None

HANDLERS_MODULE_NAME = "app.bot.handlers"
HANDLERS_FINDER = _HandlersFinder(HANDLERS_MODULE_NAME)


def _load_webhook_module():
    fake_app = types.ModuleType("app")
    fake_app.__path__ = []
    fake_config = types.ModuleType("app.config")
    fake_core = types.ModuleType("app.core")
    fake_core.__path__ = []
    fake_memory = types.ModuleType("app.core.memory")
    fake_tls = types.ModuleType("app.core.tls")
    fake_clients = types.ModuleType("app.clients")
    fake_telegram = types.ModuleType("app.clients.telegram_client")
    fake_bot_pkg = types.ModuleType("app.bot")
    fake_bot_pkg.__path__ = []
    fake_app.bot = fake_bot_pkg
    fake_bot_components = types.ModuleType("app.bot.components")
    fake_dispatcher = types.ModuleType("app.bot.components.dispatcher")
    fake_constants = types.ModuleType("app.bot.components.constants")

    settings = types.SimpleNamespace(
        USE_SELF_SIGNED_CERT=False,
        WEBHOOK_CERT="/tmp/missing-cert.pem",
        WEBHOOK_KEY="/tmp/missing-key.pem",
        WEBHOOK_URL="https://example.local",
        WEBHOOK_PATH="/webhook",
        WEBHOOK_HOST="127.0.0.1",
        WEBHOOK_PORT=8443,
        WEBHOOK_FEED_UPDATE_TIMEOUT_SEC=1,
        WEBHOOK_DROP_PENDING_UPDATES=True,
        WEBHOOK_ALLOW_START_WITHOUT_REGISTRATION=False,
        WEBHOOK_REGISTRATION_MAX_ATTEMPTS=3,
        WEBHOOK_REGISTRATION_RETRY_DELAY_SEC=0,
        ALLOWED_GROUP_IDS=[],
        MEMORY_TTL_DAYS=1,
    )

    fake_config.settings = settings
    fake_memory.get_redis = lambda: _FakeRedis()
    fake_memory.get_redis_queue = lambda: _FakeRedis()
    fake_tls.resolve_tls_server_files = lambda **kwargs: types.SimpleNamespace(
        certfile=kwargs.get("certfile") if kwargs.get("use_self_signed") else None,
        keyfile=kwargs.get("keyfile") if kwargs.get("use_self_signed") else None,
    )

    fake_telegram.get_bot = lambda: _FakeBot()

    fake_dispatcher.dp = _FakeDispatcher()

    fake_constants.redis_client = None
    fake_constants.redis_queue = None
    fake_constants.BOT_ID = None
    fake_constants.BOT_USERNAME = ""
    fake_constants.LANG_FILE = "lang.json"
    fake_constants.WELCOME_MESSAGES = {}

    fake_aiohttp = types.ModuleType("aiohttp")
    fake_aiohttp.web = _FakeWeb("aiohttp.web")
    fake_aiohttp.ContentTypeError = ValueError

    fake_aiogram = types.ModuleType("aiogram")
    fake_aiogram.types = types.SimpleNamespace(Update=dict)
    fake_aiogram_types = types.ModuleType("aiogram.types")

    class _FakeFSInputFile:
        def __init__(self, *_args, **_kwargs):
            pass

    fake_aiogram_types.FSInputFile = _FakeFSInputFile

    injected_modules = {
        "app": fake_app,
        "app.config": fake_config,
        "app.core": fake_core,
        "app.core.memory": fake_memory,
        "app.core.tls": fake_tls,
        "app.clients": fake_clients,
        "app.clients.telegram_client": fake_telegram,
        "app.bot": fake_bot_pkg,
        "app.bot.components": fake_bot_components,
        "app.bot.components.dispatcher": fake_dispatcher,
        "app.bot.components.constants": fake_constants,
        "aiofiles": _FakeAiofiles("aiofiles"),
        "aiohttp": fake_aiohttp,
        "aiogram": fake_aiogram,
        "aiogram.types": fake_aiogram_types,
    }
    module_name = "webhook_under_test"
    previous_modules = {name: sys.modules.get(name) for name in [*injected_modules, module_name, HANDLERS_MODULE_NAME]}
    try:
        sys.modules.update(injected_modules)

        webhook_path = pathlib.Path(__file__).resolve().parents[1] / "app" / "bot" / "components" / "webhook.py"
        spec = importlib.util.spec_from_file_location(module_name, webhook_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, previous in previous_modules.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


webhook = _load_webhook_module()


class WebhookStartBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _FakeDispatcher.calls = 0
        _FakeDispatcher.should_fail = False
        _FakeDispatcher.start_handler_registered = False
        _FakeDispatcher.cmd_start_calls = 0
        sys.modules.pop(HANDLERS_MODULE_NAME, None)
        if HANDLERS_FINDER not in sys.meta_path:
            sys.meta_path.insert(0, HANDLERS_FINDER)
        _FakeBot.set_webhook_side_effect = None
        webhook.settings.WEBHOOK_ALLOW_START_WITHOUT_REGISTRATION = False
        webhook.settings.USE_SELF_SIGNED_CERT = False
        webhook.settings.WEBHOOK_REGISTRATION_MAX_ATTEMPTS = 3
        webhook.settings.WEBHOOK_REGISTRATION_RETRY_DELAY_SEC = 0
        if _FakeBot.last_instance is not None:
            _FakeBot.last_instance.set_webhook_calls.clear()
            _FakeBot.last_instance.last_set_webhook_kwargs = None


    async def asyncTearDown(self):
        with contextlib.suppress(ValueError):
            sys.meta_path.remove(HANDLERS_FINDER)

    async def test_start_bot_without_self_signed_cert_uses_none_ssl_context(self):
        stop_event = asyncio.Event()
        stop_event.set()

        await webhook.start_bot(stop_event=stop_event)

        self.assertIsNotNone(_FakeTCPSite.last_instance)
        self.assertIsNone(_FakeTCPSite.last_instance.ssl_context)
        self.assertTrue(_FakeTCPSite.last_instance.started)


    async def test_start_bot_drops_pending_updates_on_start(self):
        stop_event = asyncio.Event()
        stop_event.set()

        await webhook.start_bot(stop_event=stop_event)

        self.assertIsNotNone(_FakeBot.last_instance)
        self.assertIsNotNone(_FakeBot.last_instance.last_set_webhook_kwargs)
        self.assertIn("drop_pending_updates", _FakeBot.last_instance.last_set_webhook_kwargs)
        self.assertTrue(_FakeBot.last_instance.last_set_webhook_kwargs["drop_pending_updates"])
        self.assertIn("allowed_updates", _FakeBot.last_instance.last_set_webhook_kwargs)
        self.assertIn("message_reaction", _FakeBot.last_instance.last_set_webhook_kwargs["allowed_updates"])


    async def test_start_bot_respects_drop_pending_updates_setting(self):
        stop_event = asyncio.Event()
        stop_event.set()
        webhook.settings.WEBHOOK_DROP_PENDING_UPDATES = False

        await webhook.start_bot(stop_event=stop_event)

        self.assertIsNotNone(_FakeBot.last_instance)
        self.assertIsNotNone(_FakeBot.last_instance.last_set_webhook_kwargs)
        self.assertIn("drop_pending_updates", _FakeBot.last_instance.last_set_webhook_kwargs)
        self.assertFalse(_FakeBot.last_instance.last_set_webhook_kwargs["drop_pending_updates"])

    async def test_start_bot_raises_runtime_error_when_webhook_registration_fails_by_default(self):
        stop_event = asyncio.Event()
        stop_event.set()
        _FakeBot.set_webhook_side_effect = Exception("boom")

        with self.assertRaisesRegex(RuntimeError, "webhook registration failed"):
            await webhook.start_bot(stop_event=stop_event)


    async def test_start_bot_raises_runtime_error_on_webhook_registration_timeout_by_default(self):
        stop_event = asyncio.Event()
        stop_event.set()
        _FakeBot.set_webhook_side_effect = asyncio.TimeoutError()

        with self.assertRaisesRegex(RuntimeError, "webhook registration failed"):
            await webhook.start_bot(stop_event=stop_event)

    async def test_start_bot_continues_when_registration_fails_and_allow_flag_enabled(self):
        stop_event = asyncio.Event()
        stop_event.set()
        webhook.settings.WEBHOOK_ALLOW_START_WITHOUT_REGISTRATION = True
        _FakeBot.set_webhook_side_effect = Exception("boom")

        with self.assertLogs(webhook.logger, level="WARNING") as logs:
            await webhook.start_bot(stop_event=stop_event)

        self.assertTrue(any("Starting bot without successful webhook registration" in message for message in logs.output))


    async def test_start_bot_retries_webhook_registration_until_success(self):
        stop_event = asyncio.Event()
        stop_event.set()

        calls = {"n": 0}

        def side_effect():
            calls["n"] += 1
            if calls["n"] < 3:
                return Exception("temporary error")
            return None

        _FakeBot.set_webhook_side_effect = side_effect

        await webhook.start_bot(stop_event=stop_event)

        self.assertIsNotNone(_FakeBot.last_instance)
        self.assertEqual(len(_FakeBot.last_instance.set_webhook_calls), 3)

    async def test_start_bot_fails_after_registration_retry_limit(self):
        stop_event = asyncio.Event()
        stop_event.set()
        webhook.settings.WEBHOOK_REGISTRATION_MAX_ATTEMPTS = 2
        _FakeBot.set_webhook_side_effect = Exception("boom")

        with self.assertRaisesRegex(RuntimeError, "webhook registration failed"):
            await webhook.start_bot(stop_event=stop_event)

        self.assertIsNotNone(_FakeBot.last_instance)
        self.assertEqual(len(_FakeBot.last_instance.set_webhook_calls), 2)


    async def test_start_command_fails_without_handler_side_effect_registration(self):
        stop_event = asyncio.Event()
        sys.modules.pop(HANDLERS_MODULE_NAME, None)

        with mock.patch.object(_HandlersLoader, "exec_module", lambda self, module: None):
            task = asyncio.create_task(webhook.start_bot(stop_event=stop_event))
            await asyncio.sleep(0)

            app = _FakeApplication.last_instance
            self.assertIsNotNone(app)
            handler = app.router.post_handlers[webhook.settings.WEBHOOK_PATH]

            payload = {
                "update_id": 990,
                "message": {
                    "text": "/start",
                    "chat": {"id": 1, "type": "private"},
                    "from": {"id": 2},
                },
            }
            response = await handler(_FakeWeb.Request(payload))

            self.assertEqual(response.status, 503)
            self.assertEqual(_FakeDispatcher.cmd_start_calls, 0)

            stop_event.set()
            await task


    async def test_start_command_is_routed_after_start_bot_imports_handlers(self):
        stop_event = asyncio.Event()

        task = asyncio.create_task(webhook.start_bot(stop_event=stop_event))
        await asyncio.sleep(0)

        app = _FakeApplication.last_instance
        self.assertIsNotNone(app)
        handler = app.router.post_handlers[webhook.settings.WEBHOOK_PATH]

        payload = {
            "update_id": 991,
            "message": {
                "text": "/start",
                "chat": {"id": 1, "type": "private"},
                "from": {"id": 2},
            },
        }
        response = await handler(_FakeWeb.Request(payload))

        self.assertEqual(response.status, 200)
        self.assertTrue(_FakeDispatcher.start_handler_registered)
        self.assertEqual(_FakeDispatcher.cmd_start_calls, 1)

        stop_event.set()
        await task

    async def test_webhook_update_dedup_uses_atomic_nx_set(self):
        stop_event = asyncio.Event()

        task = asyncio.create_task(webhook.start_bot(stop_event=stop_event))
        await asyncio.sleep(0)

        app = _FakeApplication.last_instance
        self.assertIsNotNone(app)
        handler = app.router.post_handlers[webhook.settings.WEBHOOK_PATH]

        payload = {"update_id": 12345}
        response_a = await handler(_FakeWeb.Request(payload))
        response_b = await handler(_FakeWeb.Request(payload))
        await asyncio.sleep(0)

        self.assertEqual(response_a.status, 200)
        self.assertEqual(response_b.status, 200)
        self.assertEqual(_FakeDispatcher.calls, 1)

        stop_event.set()
        await task

    async def test_webhook_returns_503_when_redis_dedup_fails(self):
        stop_event = asyncio.Event()

        task = asyncio.create_task(webhook.start_bot(stop_event=stop_event))
        await asyncio.sleep(0)

        webhook.redis_client = _FailingRedis()
        app = _FakeApplication.last_instance
        handler = app.router.post_handlers[webhook.settings.WEBHOOK_PATH]

        response = await handler(_FakeWeb.Request({"update_id": 777}))

        self.assertEqual(response.status, 503)

        stop_event.set()
        await task

    async def test_webhook_returns_400_for_non_object_payload(self):
        stop_event = asyncio.Event()

        task = asyncio.create_task(webhook.start_bot(stop_event=stop_event))
        await asyncio.sleep(0)

        app = _FakeApplication.last_instance
        handler = app.router.post_handlers[webhook.settings.WEBHOOK_PATH]

        response = await handler(_FakeWeb.Request([{"update_id": 778}]))

        self.assertEqual(response.status, 400)
        self.assertEqual(_FakeDispatcher.calls, 0)

        stop_event.set()
        await task

    async def test_webhook_returns_400_for_invalid_update_schema(self):
        stop_event = asyncio.Event()

        task = asyncio.create_task(webhook.start_bot(stop_event=stop_event))
        await asyncio.sleep(0)

        app = _FakeApplication.last_instance
        handler = app.router.post_handlers[webhook.settings.WEBHOOK_PATH]

        with mock.patch.object(webhook.types, "Update", side_effect=ValueError("bad schema")):
            response = await handler(_FakeWeb.Request({"update_id": 778}))

        self.assertEqual(response.status, 400)
        self.assertEqual(_FakeDispatcher.calls, 0)

        stop_event.set()
        await task

    async def test_webhook_dedup_happens_after_schema_validation(self):
        stop_event = asyncio.Event()

        task = asyncio.create_task(webhook.start_bot(stop_event=stop_event))
        await asyncio.sleep(0)

        app = _FakeApplication.last_instance
        handler = app.router.post_handlers[webhook.settings.WEBHOOK_PATH]

        payload = {"update_id": 880}
        with mock.patch.object(webhook.types, "Update", side_effect=[ValueError("bad schema"), {}]):
            response_a = await handler(_FakeWeb.Request(payload))
            response_b = await handler(_FakeWeb.Request(payload))

        await asyncio.sleep(0)

        self.assertEqual(response_a.status, 400)
        self.assertEqual(response_b.status, 200)
        self.assertEqual(_FakeDispatcher.calls, 1)

        stop_event.set()
        await task


    async def test_webhook_logs_compact_context_only(self):
        stop_event = asyncio.Event()

        task = asyncio.create_task(webhook.start_bot(stop_event=stop_event))
        await asyncio.sleep(0)

        app = _FakeApplication.last_instance
        handler = app.router.post_handlers[webhook.settings.WEBHOOK_PATH]

        payload = {
            "update_id": 901,
            "message": {
                "message_id": 10,
                "chat": {"id": 123456, "type": "private", "title": "ignored"},
                "from": {"id": 654321, "is_bot": False, "username": "tester"},
                "text": "A" * (webhook.N + 50),
                "photo": [{"file_id": "x" * 1000, "file_size": 999999}],
                "document": {"file_id": "doc", "file_name": "big.bin"},
                "entities": [{"type": "mention", "offset": 0, "length": 5}],
                "nested": {"very": ["large", {"payload": "x" * 1000}]},
            },
        }

        with mock.patch.object(webhook.logger, "info") as mock_info:
            response = await handler(_FakeWeb.Request(payload))

        self.assertEqual(response.status, 200)
        self.assertTrue(mock_info.called)

        incoming_call = next(
            call
            for call in mock_info.call_args_list
            if call.args and call.args[0] == "Incoming update"
        )

        self.assertEqual(len(incoming_call.args), 1)
        self.assertIn("extra", incoming_call.kwargs)
        self.assertNotIn(payload, incoming_call.args)
        self.assertEqual(set(incoming_call.kwargs["extra"].keys()), {
            "update_id",
            "update_type",
            "message_id",
            "chat_id",
            "user_id",
            "text_preview",
        })

        log_update = incoming_call.kwargs["extra"]
        self.assertEqual(log_update["update_id"], payload["update_id"])
        self.assertEqual(log_update["update_type"], "message")
        self.assertEqual(log_update["message_id"], payload["message"]["message_id"])
        self.assertEqual(log_update["chat_id"], payload["message"]["chat"]["id"])
        self.assertEqual(log_update["user_id"], payload["message"]["from"]["id"])
        self.assertIn("text_preview", log_update)
        self.assertTrue(log_update["text_preview"].endswith("...[truncated]"))
        self.assertLessEqual(
            len(log_update["text_preview"]),
            webhook.N + len("...[truncated]"),
        )

        self.assertNotIn("message", log_update)
        self.assertNotIn("photo", log_update)
        self.assertNotIn("document", log_update)
        self.assertNotIn("entities", log_update)
        self.assertNotIn("nested", log_update)

        stop_event.set()
        await task

    async def test_webhook_callback_query_uses_nested_message_location_context(self):
        stop_event = asyncio.Event()

        task = asyncio.create_task(webhook.start_bot(stop_event=stop_event))
        await asyncio.sleep(0)

        app = _FakeApplication.last_instance
        handler = app.router.post_handlers[webhook.settings.WEBHOOK_PATH]

        payload = {
            "update_id": 902,
            "callback_query": {
                "id": "cbq-id",
                "from": {"id": 777, "is_bot": False},
                "data": "open-thread",
                "message": {
                    "message_id": 11,
                    "message_thread_id": 991,
                    "is_topic_message": True,
                    "reply_to_message": {"message_id": 3},
                    "chat": {"id": -1001, "type": "supergroup", "linked_chat_id": -1002},
                },
            },
        }

        with mock.patch.object(webhook.logger, "info") as mock_info:
            response = await handler(_FakeWeb.Request(payload))

        self.assertEqual(response.status, 200)

        incoming_call = next(
            call
            for call in mock_info.call_args_list
            if call.args and call.args[0] == "Incoming update"
        )

        log_update = incoming_call.kwargs["extra"]
        self.assertEqual(log_update["update_id"], payload["update_id"])
        self.assertEqual(log_update["update_type"], "callback_query")
        self.assertEqual(log_update["message_id"], payload["callback_query"]["message"]["message_id"])
        self.assertEqual(log_update["message_thread_id"], payload["callback_query"]["message"]["message_thread_id"])
        self.assertEqual(log_update["reply_to_message_id"], payload["callback_query"]["message"]["reply_to_message"]["message_id"])
        self.assertEqual(log_update["linked_chat_id"], payload["callback_query"]["message"]["chat"]["linked_chat_id"])
        self.assertTrue(log_update["is_topic_message"])
        self.assertEqual(log_update["chat_id"], payload["callback_query"]["message"]["chat"]["id"])
        self.assertEqual(log_update["user_id"], payload["callback_query"]["from"]["id"])
        self.assertEqual(log_update["text_preview"], payload["callback_query"]["data"])

        stop_event.set()
        await task

    async def test_webhook_returns_503_and_releases_claim_when_feed_update_fails(self):
        stop_event = asyncio.Event()

        task = asyncio.create_task(webhook.start_bot(stop_event=stop_event))
        await asyncio.sleep(0)

        app = _FakeApplication.last_instance
        handler = app.router.post_handlers[webhook.settings.WEBHOOK_PATH]
        key = f"tg:{webhook.consts.BOT_ID}:update:779"

        _FakeDispatcher.should_fail = True
        response_a = await handler(_FakeWeb.Request({"update_id": 779}))

        self.assertEqual(response_a.status, 503)
        self.assertEqual(webhook.redis_client.kv.get(key), None)
        self.assertEqual(_FakeDispatcher.calls, 1)

        _FakeDispatcher.should_fail = False
        response_b = await handler(_FakeWeb.Request({"update_id": 779}))

        self.assertEqual(response_b.status, 200)
        self.assertEqual(_FakeDispatcher.calls, 2)

        stop_event.set()
        await task


if __name__ == "__main__":
    unittest.main()
