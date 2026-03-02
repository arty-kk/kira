# ruff: noqa: E402
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


class _PrefsTrackingDB:
    def __init__(self, colliding_id: int):
        self.colliding_id = colliding_id
        self.calls: list[tuple[str, int]] = []

    async def get(self, model, ident: int):
        self.calls.append((model.__name__, ident))
        if model.__name__ == "ApiKey" and ident == self.colliding_id:
            return SimpleNamespace(id=ident, user_id=777, persona_prefs={"source": "api_key"})
        if model.__name__ == "User" and ident == self.colliding_id:
            return SimpleNamespace(id=ident, persona_prefs={"source": "user"})
        return None


class _RecordingPersona:
    instances: list["_RecordingPersona"] = []

    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.apply_calls: list[dict] = []
        self.__class__.instances.append(self)

    def apply_overrides(self, prefs: dict):
        self.apply_calls.append(prefs)

    def _spawn(self, *_args, **_kwargs):
        return None

    async def _ensure_background_started(self):
        return None

    async def close(self):
        return None


class RegistryPersonaPrefsScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await registry.shutdown_personas()
        async with registry._lock:
            registry._cache.clear()
            registry._inflight.clear()

    async def asyncTearDown(self) -> None:
        await registry.shutdown_personas()

    async def test_user_scope_ignores_apikey_prefs_on_id_collision(self) -> None:
        colliding_id = 42
        db = _PrefsTrackingDB(colliding_id=colliding_id)
        _RecordingPersona.instances.clear()

        @asynccontextmanager
        async def fake_session_scope(*_args, **_kwargs):
            yield db

        with patch.object(registry, "session_scope", new=fake_session_scope), patch.object(
            registry,
            "Persona",
            new=_RecordingPersona,
        ), patch.object(registry, "settings", SimpleNamespace(API_PERSONA_PER_KEY=False)):
            await registry.get_persona(chat_id=100500, user_id=colliding_id)

        self.assertEqual(db.calls, [("User", colliding_id)])
        self.assertEqual(len(_RecordingPersona.instances), 1)
        self.assertEqual(_RecordingPersona.instances[0].apply_calls, [{"source": "user"}])


if __name__ == "__main__":
    unittest.main()
