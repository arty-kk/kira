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

    def write_prefs(self, user_id: int, prefs: dict) -> None:
        self.user_prefs[user_id] = dict(prefs)

    async def get(self, model, ident: int):
        if model.__name__ != "User":
            return None
        prefs = self.user_prefs.get(ident)
        return SimpleNamespace(id=ident, persona_prefs=prefs)


class _RecordingPersona:
    created: list["_RecordingPersona"] = []

    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.apply_calls: list[tuple[dict | None, bool]] = []
        self.current_overrides: dict = {}
        self.__class__.created.append(self)

    def apply_overrides(self, prefs: dict | None, reset: bool = False):
        if reset:
            self.apply_calls.append((None, True))
            self.current_overrides = {}
            return
        payload = dict(prefs or {})
        self.apply_calls.append((payload, False))
        self.current_overrides = payload

    def _spawn(self, *_args, **_kwargs):
        return None

    async def _ensure_background_started(self):
        return None

    async def close(self):
        return None


class RegistryOwnerScopePrivateFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await registry.shutdown_personas()
        async with registry._lock:
            registry._cache.clear()
            registry._inflight.clear()

    async def asyncTearDown(self) -> None:
        await registry.shutdown_personas()

    async def test_owner_scope_cache_updates_for_write_and_reset_fallback(self) -> None:
        uid = 777
        initial_prefs = {"zodiac": "Leo", "sociality": "ambivert"}
        merged_prefs = {"zodiac": "Cancer", "sociality": "introvert", "archetypes": ["Sage"]}
        defaults = {"zodiac": "Aries", "sociality": "ambivert", "archetypes": []}

        db = _MutablePrefsDB()
        db.write_prefs(uid, initial_prefs)
        _RecordingPersona.created.clear()

        @asynccontextmanager
        async def fake_session_scope(*_args, **_kwargs):
            yield db

        with patch.object(registry, "session_scope", new=fake_session_scope), patch.object(
            registry,
            "Persona",
            new=_RecordingPersona,
        ), patch.object(registry, "settings", SimpleNamespace(API_PERSONA_PER_KEY=False)):
            owner_main = await registry.get_persona(chat_id=uid, user_id=uid)
            owner_alt_key = await registry.get_persona(chat_id=uid + 101, user_id=uid, profile_id="alt")

            owner_keys = [k for k in registry._cache.keys() if k[1] == uid]
            self.assertGreaterEqual(len(owner_keys), 2)

            db.write_prefs(uid, merged_prefs)
            await registry.update_cached_personas_for_owner(uid, merged_prefs)

            owner_main_after_write = await registry.get_persona(chat_id=uid, user_id=uid)
            owner_alt_after_write = await registry.get_persona(chat_id=uid + 101, user_id=uid, profile_id="alt")

            self.assertIs(owner_main_after_write, owner_main)
            self.assertIs(owner_alt_after_write, owner_alt_key)
            self.assertEqual(owner_main_after_write.current_overrides, merged_prefs)
            self.assertEqual(owner_alt_after_write.current_overrides, merged_prefs)

            owner_main.apply_overrides(None, reset=True)
            await registry.update_cached_personas_for_owner(uid, defaults)

            owner_main_after_reset = await registry.get_persona(chat_id=uid, user_id=uid)
            owner_alt_after_reset = await registry.get_persona(chat_id=uid + 101, user_id=uid, profile_id="alt")

            self.assertEqual(owner_main_after_reset.current_overrides, defaults)
            self.assertEqual(owner_alt_after_reset.current_overrides, defaults)


if __name__ == "__main__":
    unittest.main()
