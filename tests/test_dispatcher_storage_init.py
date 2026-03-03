import contextlib
import importlib.util
import pathlib
import sys
import types
import unittest


class DispatcherStorageInitTests(unittest.TestCase):
    def test_import_uses_redis_storage_even_without_running_loop(self) -> None:
        fake_app = types.ModuleType("app")
        fake_app.__path__ = []
        fake_config = types.ModuleType("app.config")
        fake_core = types.ModuleType("app.core")
        fake_core.__path__ = []
        fake_memory = types.ModuleType("app.core.memory")
        fake_clients = types.ModuleType("app.clients")
        fake_clients.__path__ = []
        fake_telegram = types.ModuleType("app.clients.telegram_client")

        fake_aiogram = types.ModuleType("aiogram")
        fake_fsm = types.ModuleType("aiogram.fsm")
        fake_fsm.__path__ = []
        fake_storage = types.ModuleType("aiogram.fsm.storage")
        fake_storage.__path__ = []
        fake_memory_storage = types.ModuleType("aiogram.fsm.storage.memory")
        fake_redis_storage = types.ModuleType("aiogram.fsm.storage.redis")

        class _FakeDispatcher:
            def __init__(self, storage, bot):
                self.storage = storage
                self.bot = bot

        class _FakeMemoryStorage:
            pass

        class _FakeRedisStorage:
            def __init__(self, redis):
                self.redis = redis

        fake_aiogram.Dispatcher = _FakeDispatcher
        fake_memory_storage.MemoryStorage = _FakeMemoryStorage
        fake_redis_storage.RedisStorage = _FakeRedisStorage

        fake_config.settings = types.SimpleNamespace(DP_USE_REDIS_STORAGE=True)

        sentinel_client = object()

        def _get_redis():
            raise RuntimeError("get_redis() requires an active asyncio event loop; call it from async context")

        def _create_client(name: str):
            self.assertEqual(name, "default")
            return sentinel_client

        fake_memory.get_redis = _get_redis
        fake_memory._create_client = _create_client
        fake_telegram.get_bot = lambda: "fake-bot"

        module_path = pathlib.Path(__file__).resolve().parents[1] / "app" / "bot" / "components" / "dispatcher.py"
        module_name = "_test_dispatcher_storage_import"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        module = importlib.util.module_from_spec(spec)

        injected_modules = {
            "app": fake_app,
            "app.config": fake_config,
            "app.core": fake_core,
            "app.core.memory": fake_memory,
            "app.clients": fake_clients,
            "app.clients.telegram_client": fake_telegram,
            "aiogram": fake_aiogram,
            "aiogram.fsm": fake_fsm,
            "aiogram.fsm.storage": fake_storage,
            "aiogram.fsm.storage.memory": fake_memory_storage,
            "aiogram.fsm.storage.redis": fake_redis_storage,
            module_name: module,
        }

        previous = {name: sys.modules.get(name) for name in injected_modules}
        try:
            sys.modules.update(injected_modules)
            assert spec.loader is not None
            spec.loader.exec_module(module)

            self.assertIsInstance(module.dp.storage, _FakeRedisStorage)
            self.assertIs(module.dp.storage.redis, sentinel_client)
            self.assertEqual(module.dp.bot, "fake-bot")
        finally:
            for name, old_value in previous.items():
                if old_value is None:
                    with contextlib.suppress(KeyError):
                        del sys.modules[name]
                else:
                    sys.modules[name] = old_value


if __name__ == "__main__":
    unittest.main()
