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
    os.environ.setdefault("TWITTER_API_KEY", "test")
    os.environ.setdefault("TWITTER_API_SECRET", "test")
    os.environ.setdefault("TWITTER_ACCESS_TOKEN", "test")
    os.environ.setdefault("TWITTER_ACCESS_TOKEN_SECRET", "test")
    os.environ.setdefault("TWITTER_BEARER_TOKEN", "test")


_seed_env()

from app.emo_engine import registry


class _MutablePrefsDB:
    def __init__(self):
        self.user_prefs: dict[int, dict] = {}
        self.calls: list[tuple[str, int]] = []

    def write_prefs(self, user_id: int, prefs: dict) -> None:
        self.user_prefs[user_id] = dict(prefs)

    async def get(self, model, ident: int):
        self.calls.append((model.__name__, ident))
        if model.__name__ != "User":
            return None
        prefs = self.user_prefs.get(ident)
        return SimpleNamespace(id=ident, persona_prefs=prefs)


class _RecordingPersona:
    created: list["_RecordingPersona"] = []

    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.apply_calls: list[dict] = []
        self.current_overrides: dict = {}
        self.__class__.created.append(self)

    def apply_overrides(self, prefs: dict):
        self.apply_calls.append(dict(prefs))
        self.current_overrides = dict(prefs)

    def _spawn(self, *_args, **_kwargs):
        return None

    async def _ensure_background_started(self):
        return None

    async def close(self):
        return None


class RegistryWritePathCacheRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await registry.shutdown_personas()
        async with registry._lock:
            registry._cache.clear()
            registry._inflight.clear()

    async def asyncTearDown(self) -> None:
        await registry.shutdown_personas()

    async def test_write_path_and_responder_share_cache_key_when_user_id_provided(self) -> None:
        uid = 4242
        written_prefs = {"archetype": "Sage", "temp": 0.35}
        db = _MutablePrefsDB()
        _RecordingPersona.created.clear()

        @asynccontextmanager
        async def fake_session_scope(*_args, **_kwargs):
            yield db

        with patch.object(registry, "session_scope", new=fake_session_scope), patch.object(
            registry,
            "Persona",
            new=_RecordingPersona,
        ), patch.object(registry, "settings", SimpleNamespace(API_PERSONA_PER_KEY=False)):
            # Регрессия: раньше write-path вызывал get_persona без user_id,
            # из-за чего ключ уходил в (chat_id, 0, ...), а responder читал другой cache-key.
            db.write_prefs(uid, written_prefs)
            write_path_persona = await registry.get_persona(chat_id=uid, user_id=uid)
            write_path_persona.apply_overrides(written_prefs)
            responder_persona = await registry.get_persona(chat_id=uid, user_id=uid)

        self.assertEqual(db.calls, [("User", uid)])
        self.assertEqual(len(_RecordingPersona.created), 1)
        self.assertIs(write_path_persona, responder_persona)
        self.assertEqual(write_path_persona.apply_calls, [written_prefs, written_prefs])
        self.assertEqual(responder_persona.current_overrides, written_prefs)


if __name__ == "__main__":
    unittest.main()
