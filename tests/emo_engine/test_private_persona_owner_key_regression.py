# ruff: noqa: E402
import asyncio
import os
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch


def _seed_env() -> None:
    os.environ.setdefault("OPENAI_API_KEY", "test")
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABCdef1234567890")
    os.environ.setdefault("TELEGRAM_BOT_USERNAME", "testbot")
    os.environ.setdefault("TELEGRAM_BOT_ID", "1")
    os.environ.setdefault("WEBHOOK_URL", "https://example.invalid")
    os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://user:pass@localhost/db")
    os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
    os.environ.setdefault("REDIS_URL_QUEUE", "redis://localhost:6379/1")
    os.environ.setdefault("REDIS_URL_VECTOR", "redis://localhost:6379/2")


_seed_env()

import importlib

personal_ping = importlib.import_module("app.services.addons.personal_ping")
welcome_manager = importlib.import_module("app.services.addons.welcome_manager")


class _StopAfterPrompt(RuntimeError):
    pass


class _FakePersona:
    def __init__(self, name: str, style: dict, guideline: str):
        self.name = name
        self._mods_cache = dict(style)
        self._guideline = guideline
        self.state = {"engagement": 0.5, "curiosity": 0.5, "arousal": 0.5}
        self._restored_evt = asyncio.Event()
        self._restored_evt.set()

    async def style_modifiers(self):
        return dict(self._mods_cache)

    async def style_guidelines(self, _uid: int):
        return self._guideline


class _FakeRedis:
    async def get(self, _key):
        return None


class _FakeDB:
    async def get(self, _model, _ident):
        return None


@asynccontextmanager
async def _fake_session_scope(*_args, **_kwargs):
    yield _FakeDB()


class PrivatePersonaOwnerKeyRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_welcome_and_ping_use_same_owner_key_instead_of_legacy_zero_scope(self):
        owner_id = 4242
        legacy_key = (owner_id, 0, "default")
        owner_key = (owner_id, owner_id, "default")

        legacy_persona = _FakePersona(
            name="legacy-private-chat-scope",
            style={"creativity_mod": 0.1},
            guideline="LEGACY_GUIDELINE",
        )
        owner_persona = _FakePersona(
            name="owner-private-scope",
            style={"creativity_mod": 0.9},
            guideline="OWNER_GUIDELINE",
        )

        persona_by_key = {
            legacy_key: legacy_persona,
            owner_key: owner_persona,
        }
        persona_calls: list[tuple[int, int, str]] = []
        prompt_calls: list[tuple[str, str]] = []

        async def fake_get_persona(chat_id: int, user_id: int = 0, profile_id: str = "default"):
            persona_calls.append((chat_id, user_id, profile_id))
            return persona_by_key[(chat_id, user_id, profile_id)]

        async def fake_build_system_prompt(persona, guidelines, user_gender=None):
            prompt_calls.append((persona.name, guidelines))
            raise _StopAfterPrompt

        with patch.object(welcome_manager, "get_persona", side_effect=fake_get_persona), patch.object(
            personal_ping, "get_persona", side_effect=fake_get_persona
        ), patch.object(welcome_manager, "session_scope", _fake_session_scope), patch.object(
            personal_ping, "session_scope", _fake_session_scope
        ), patch.object(welcome_manager, "get_cached_gender", return_value=None), patch.object(
            personal_ping, "get_cached_gender", return_value=None
        ), patch.object(welcome_manager, "get_redis", return_value=_FakeRedis()), patch.object(
            personal_ping, "get_redis", return_value=_FakeRedis()
        ), patch.object(personal_ping, "bandit_check_expire_or_success", return_value=None), patch.object(
            welcome_manager, "build_system_prompt", side_effect=fake_build_system_prompt
        ), patch.object(personal_ping, "build_system_prompt", side_effect=fake_build_system_prompt):
            with self.assertRaises(_StopAfterPrompt):
                await welcome_manager.generate_private_welcome(
                    chat_id=owner_id,
                    user=SimpleNamespace(id=owner_id, language_code="en"),
                )
            with self.assertRaises(_StopAfterPrompt):
                await personal_ping.send_contextual_ping(chat_id=owner_id, user_id=owner_id)

        self.assertEqual(
            persona_calls,
            [owner_key, owner_key],
            "Regression guard: private flows must use owner key (chat_id=user_id) instead of legacy chat-scope key (chat_id, 0, ...)",
        )
        self.assertNotEqual(owner_key, legacy_key)

        self.assertEqual(
            prompt_calls,
            [
                ("owner-private-scope", "OWNER_GUIDELINE"),
                ("owner-private-scope", "OWNER_GUIDELINE"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
