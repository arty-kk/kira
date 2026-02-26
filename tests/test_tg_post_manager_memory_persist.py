import asyncio
import importlib
import os
import sys
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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


class TgPostManagerMemoryPersistTests(unittest.IsolatedAsyncioTestCase):
    def _import_module(self):
        _drop_module("app.services.addons.tg_post_manager")
        with _required_app_import_env():
            return importlib.import_module("app.services.addons.tg_post_manager")

    async def test_persist_memory_partial_failure_does_not_cancel_other_steps(self) -> None:
        module = self._import_module()
        persona = SimpleNamespace(process_interaction=AsyncMock(return_value=None))

        push_calls: list[str] = []
        assistant_attempts = 0

        async def _push_side_effect(_chat_id, role, _content, *, user_id):
            nonlocal assistant_attempts
            self.assertEqual(user_id, 777)
            push_calls.append(role)
            if role == "assistant":
                assistant_attempts += 1
                raise asyncio.TimeoutError("assistant push timeout")
            return None

        with (
            patch.object(module, "push_message", AsyncMock(side_effect=_push_side_effect)),
            self.assertLogs(module.logger, level="INFO") as logs,
        ):
            memory_persist_ok = await module._persist_post_to_memory(
                persona=persona,
                persona_chat_id=777,
                post_text="test post",
                meta_obj={"rubric": "news_explainer"},
            )

        self.assertFalse(memory_persist_ok)
        persona.process_interaction.assert_awaited_once_with(777, "test post")
        self.assertEqual(assistant_attempts, module.MEMORY_PUSH_RETRY_ATTEMPTS)
        self.assertIn("system", push_calls)
        self.assertIn("assistant", push_calls)

        joined_logs = "\n".join(logs.output)
        self.assertIn("memory persist retries exhausted step=push_message assistant", joined_logs)
        self.assertIn("memory_persist_ok=False", joined_logs)

    async def test_push_message_non_retryable_error_has_single_attempt(self) -> None:
        module = self._import_module()

        with patch.object(module, "push_message", AsyncMock(side_effect=ValueError("boom"))) as push_mock:
            with self.assertRaises(ValueError):
                await module._push_message_with_retry(
                    persona_chat_id=777,
                    role="assistant",
                    content="content",
                    step_name="push_message assistant",
                )

        self.assertEqual(push_mock.await_count, 1)



if __name__ == "__main__":
    unittest.main()
