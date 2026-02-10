import importlib.util
import pathlib
import sys
import types
import unittest


_FAKE_ENV: dict[str, str | None] = {}


class _FakeConfig:
    last_kwargs = None

    def __init__(self, app, **kwargs):
        self.app = app
        self.kwargs = kwargs
        _FakeConfig.last_kwargs = kwargs


class _FakeServer:
    was_served = False

    def __init__(self, config):
        self.config = config
        self.install_signal_handlers = True

    async def serve(self):
        _FakeServer.was_served = True
        return None


class _FakeUvicorn(types.ModuleType):
    Config = _FakeConfig
    Server = _FakeServer


def _load_main_module():
    fake_app_pkg = types.ModuleType("app")
    fake_config = types.ModuleType("app.config")
    fake_emo_engine = types.ModuleType("app.emo_engine")
    fake_persona_memory_mod = types.ModuleType("app.emo_engine.persona.memory")
    fake_registry = types.ModuleType("app.emo_engine.registry")
    fake_clients = types.ModuleType("app.clients")
    fake_http_client_mod = types.ModuleType("app.clients.http_client")
    fake_bot = types.ModuleType("app.bot")
    fake_api = types.ModuleType("app.api")
    fake_api_app = types.ModuleType("app.api.app")

    fake_settings = types.SimpleNamespace(
        USE_SELF_SIGNED_CERT=True,
        WEBHOOK_CERT="/tmp/missing-cert.pem",
        WEBHOOK_KEY="/tmp/missing-key.pem",
    )

    fake_app_pkg.engine = types.SimpleNamespace(dispose=lambda: None)
    fake_app_pkg.close_redis_pools = lambda: None
    def _fake_get_env(name, default=None, **_kwargs):
        value = _FAKE_ENV.get(name, default)
        return default if value is None else value

    fake_app_pkg._get_env = _fake_get_env
    fake_app_pkg.setup_logging = lambda: None

    fake_config.settings = fake_settings
    fake_config._parse_bool = lambda value: str(value).strip().lower() in {"1", "true", "yes", "on"}

    class _FakePersonaMemory:
        def __init__(self, *args, **kwargs):
            pass

        async def ready(self):
            return None

    fake_persona_memory_mod.PersonaMemory = _FakePersonaMemory
    fake_registry.shutdown_personas = lambda: None

    fake_http_client_mod.http_client = types.SimpleNamespace(close=lambda: None)
    fake_bot.start_bot = lambda: None
    fake_api_app.create_app = lambda: object()

    injected = {
        "app": fake_app_pkg,
        "app.config": fake_config,
        "app.emo_engine": fake_emo_engine,
        "app.emo_engine.persona.memory": fake_persona_memory_mod,
        "app.emo_engine.registry": fake_registry,
        "app.clients": fake_clients,
        "app.clients.http_client": fake_http_client_mod,
        "app.bot": fake_bot,
        "app.api": fake_api,
        "app.api.app": fake_api_app,
        "uvicorn": _FakeUvicorn("uvicorn"),
    }

    module_name = "main_under_test"
    previous = {name: sys.modules.get(name) for name in [*injected, module_name]}

    try:
        sys.modules.update(injected)
        main_path = pathlib.Path(__file__).resolve().parents[1] / "main.py"
        spec = importlib.util.spec_from_file_location(module_name, main_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


main = _load_main_module()


class StartAPIServerTLSTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _FAKE_ENV.clear()

    async def test_self_signed_cert_missing_files_raises_runtime_error(self):
        main.settings.USE_SELF_SIGNED_CERT = True
        main.settings.WEBHOOK_CERT = "/tmp/definitely-missing-cert.pem"
        main.settings.WEBHOOK_KEY = "/tmp/definitely-missing-key.pem"

        with self.assertRaises(RuntimeError):
            await main.start_api_server()

    async def test_empty_api_cert_env_uses_webhook_fallback_and_reports_real_missing_paths(self):
        main.settings.USE_SELF_SIGNED_CERT = True
        main.settings.WEBHOOK_CERT = "/tmp/fallback-missing-cert.pem"
        main.settings.WEBHOOK_KEY = "/tmp/fallback-missing-key.pem"

        _FAKE_ENV["API_CERT"] = ""
        _FAKE_ENV["API_KEY"] = ""

        with self.assertRaisesRegex(RuntimeError, "fallback-missing-cert.pem"):
            await main.start_api_server()

        _FAKE_ENV.clear()

    async def test_tls_disabled_starts_without_ssl_context(self):
        main.settings.USE_SELF_SIGNED_CERT = False
        main.settings.WEBHOOK_CERT = "/tmp/definitely-missing-cert.pem"
        main.settings.WEBHOOK_KEY = "/tmp/definitely-missing-key.pem"

        _FakeConfig.last_kwargs = None
        _FakeServer.was_served = False

        await main.start_api_server()

        self.assertTrue(_FakeServer.was_served)
        self.assertIsNotNone(_FakeConfig.last_kwargs)
        self.assertIsNone(_FakeConfig.last_kwargs["ssl_certfile"])
        self.assertIsNone(_FakeConfig.last_kwargs["ssl_keyfile"])


if __name__ == "__main__":
    unittest.main()
