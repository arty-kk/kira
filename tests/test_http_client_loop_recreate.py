import importlib
import os
import sys
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

_REQUIRED_IMPORT_ENV = {
    "OPENAI_API_KEY": "test-openai-key",
    "DATABASE_URL": "postgresql+psycopg://postgres:postgres@localhost/test",
    "REDIS_URL": "redis://localhost:6379/0",
    "REDIS_URL_QUEUE": "redis://localhost:6379/1",
    "REDIS_URL_VECTOR": "redis://localhost:6379/2",
    "TELEGRAM_BOT_TOKEN": "123456:TESTTOKEN",
    "CELERY_BROKER_URL": "redis://localhost:6379/3",
}


@contextmanager
def _required_app_import_env():
    saved = {k: os.environ.get(k) for k in _REQUIRED_IMPORT_ENV}
    try:
        for key, value in _REQUIRED_IMPORT_ENV.items():
            os.environ[key] = saved[key] or value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _import_module():
    sys.modules.pop("app.clients.http_client", None)
    with _required_app_import_env():
        return importlib.import_module("app.clients.http_client")


class _FakeSession:
    def __init__(self):
        self.closed = False
        self.close_calls = 0

    async def close(self):
        self.close_calls += 1
        self.closed = True


class HTTPClientLoopRecreateTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_session_recreates_when_loop_changes(self):
        module = _import_module()
        client = module.HTTPClient(timeout_sec=3)

        old_session = _FakeSession()
        client._session = old_session
        client._session_loop = object()

        new_session = _FakeSession()
        with (
            patch.object(module.aiohttp, "TCPConnector", MagicMock(return_value=object())),
            patch.object(module.aiohttp, "ClientSession", MagicMock(return_value=new_session)) as session_ctor,
        ):
            session = await client._get_session()

        self.assertIs(session, new_session)
        self.assertEqual(old_session.close_calls, 1)
        session_ctor.assert_called_once()

    async def test_get_session_reuses_when_loop_matches(self):
        module = _import_module()
        client = module.HTTPClient(timeout_sec=3)

        current_loop = module.asyncio.get_running_loop()
        existing_session = _FakeSession()
        client._session = existing_session
        client._session_loop = current_loop

        with patch.object(module.aiohttp, "ClientSession", MagicMock()) as session_ctor:
            session = await client._get_session()

        self.assertIs(session, existing_session)
        session_ctor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
