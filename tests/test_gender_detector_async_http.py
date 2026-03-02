import importlib
import os
import sys
import unittest
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

from aiohttp import ClientResponseError, RequestInfo
from multidict import CIMultiDictProxy, CIMultiDict
from yarl import URL

_REQUIRED_IMPORT_ENV = {
    "OPENAI_API_KEY": "test-openai-key",
    "DATABASE_URL": "postgresql+psycopg://postgres:postgres@localhost/test",
    "REDIS_URL": "redis://localhost:6379/0",
    "REDIS_URL_QUEUE": "redis://localhost:6379/1",
    "REDIS_URL_VECTOR": "redis://localhost:6379/2",
    "TELEGRAM_BOT_TOKEN": "123456:TESTTOKEN",
    "CELERY_BROKER_URL": "redis://localhost:6379/3",
}


def _drop_module(module_name: str) -> None:
    sys.modules.pop(module_name, None)


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


class GenderDetectorAsyncHTTPTests(unittest.IsolatedAsyncioTestCase):
    def _import_module(self):
        _drop_module("app.services.responder.gender.gender_detector")
        with _required_app_import_env():
            return importlib.import_module("app.services.responder.gender.gender_detector")

    async def test_genderize_query_uses_shared_http_client_with_retry_and_timeout(self) -> None:
        module = self._import_module()

        with patch.object(module.http_client, "get_json", AsyncMock(return_value={"gender": "female", "probability": 0.95})) as get_mock:
            gender, prob = await module._genderize_query("anna")

        self.assertEqual((gender, prob), ("female", 0.95))
        get_mock.assert_awaited_once_with(
            module.GENDERIZE_URL,
            params={"name": "anna"},
            timeout_sec=module.GENDERIZE_TIMEOUT,
            retries=module.GENDERIZE_RETRIES,
            retry_backoff_sec=module.GENDERIZE_RETRY_BACKOFF_SEC,
        )

    async def test_genderize_query_returns_fallback_on_timeout(self) -> None:
        module = self._import_module()
        with patch.object(module.http_client, "get_json", AsyncMock(side_effect=TimeoutError)):
            gender, prob = await module._genderize_query("timed")

        self.assertEqual((gender, prob), (None, 0.0))

    async def test_genderize_query_returns_fallback_on_http_status(self) -> None:
        module = self._import_module()
        req_info = RequestInfo(url=URL(module.GENDERIZE_URL), method="GET", headers=CIMultiDictProxy(CIMultiDict()), real_url=URL(module.GENDERIZE_URL))
        err = ClientResponseError(req_info, (), status=429, message="too many requests")

        with patch.object(module.http_client, "get_json", AsyncMock(side_effect=err)):
            gender, prob = await module._genderize_query("rate_limited")

        self.assertEqual((gender, prob), (None, 0.0))

    async def test_genderize_query_concurrency_limited(self) -> None:
        module = self._import_module()

        inflight = 0
        max_inflight = 0

        async def _slow_get(*args, **kwargs):
            nonlocal inflight, max_inflight
            inflight += 1
            max_inflight = max(max_inflight, inflight)
            await module.asyncio.sleep(0.01)
            inflight -= 1
            return {"gender": "male", "probability": 0.95}

        with (
            patch.object(module, "_genderize_semaphore", module.asyncio.Semaphore(2)),
            patch.object(module.http_client, "get_json", AsyncMock(side_effect=_slow_get)),
        ):
            await module.asyncio.gather(*(module._genderize_query(f"user_{i}") for i in range(8)))

        self.assertLessEqual(max_inflight, 2)


if __name__ == "__main__":
    unittest.main()
