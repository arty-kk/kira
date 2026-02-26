import os
import unittest


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


class _RecordingPersona:
    def __init__(self, owner_id: int):
        self.owner_id = owner_id
        self.apply_calls: list[dict] = []
        self.reset_calls: int = 0
        self.last_prefs: dict | None = None

    def apply_overrides(self, prefs: dict | None, reset: bool = False):
        if reset:
            self.reset_calls += 1
            self.last_prefs = None
            return
        assert prefs is not None
        self.last_prefs = dict(prefs)
        self.apply_calls.append(dict(prefs))

    async def close(self):
        return None


class RegistryUpdateCachedPersonasOwnerFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await registry.shutdown_personas()
        async with registry._lock:
            registry._cache.clear()
            registry._inflight.clear()

    async def asyncTearDown(self) -> None:
        await registry.shutdown_personas()

    async def test_updates_personas_by_owner_id_for_legacy_and_direct_keys(self) -> None:
        owner_id = 321
        prefs = {"zodiac": "Leo"}

        persona_legacy = _RecordingPersona(owner_id=owner_id)
        persona_direct = _RecordingPersona(owner_id=owner_id)

        async with registry._lock:
            now = registry._now()
            registry._cache[(1001, 0, 0, "")] = (persona_legacy, now)
            registry._cache[(1002, owner_id, 0, "")] = (persona_direct, now)

        await registry.update_cached_personas_for_owner(owner_id, prefs)

        self.assertEqual(persona_legacy.apply_calls, [prefs])
        self.assertEqual(persona_direct.apply_calls, [prefs])

    async def test_empty_prefs_resets_overrides_for_all_cached_personas_of_owner(self) -> None:
        owner_id = 321

        persona_legacy = _RecordingPersona(owner_id=owner_id)
        persona_direct = _RecordingPersona(owner_id=owner_id)
        persona_legacy.apply_overrides({"tone": "friendly"})
        persona_direct.apply_overrides({"tone": "friendly"})

        async with registry._lock:
            now = registry._now()
            registry._cache[(1001, 0, 0, "")] = (persona_legacy, now)
            registry._cache[(1002, owner_id, 0, "")] = (persona_direct, now)

        await registry.update_cached_personas_for_owner(owner_id, {})

        self.assertEqual(persona_legacy.reset_calls, 1)
        self.assertEqual(persona_direct.reset_calls, 1)
        self.assertIsNone(persona_legacy.last_prefs)
        self.assertIsNone(persona_direct.last_prefs)


if __name__ == "__main__":
    unittest.main()
