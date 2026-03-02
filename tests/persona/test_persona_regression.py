# ruff: noqa: E402
import asyncio
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
    os.environ.setdefault("PERSONA_NAME", "Kira")
    os.environ.setdefault("PERSONA_AGE", "24")
    os.environ.setdefault("PERSONA_GENDER", "female")
    os.environ.setdefault("PERSONA_ZODIAC", "Libra")
    os.environ.setdefault(
        "PERSONA_TEMPERAMENT",
        '{"sanguine": 0.4, "choleric": 0.25, "phlegmatic": 0.2, "melancholic": 0.15}',
    )
    os.environ.setdefault("PERSONA_ARCHETYPES", '["Rebel", "Jester", "Sage"]')
    os.environ.setdefault("PERSONA_ROLE", "Playful companion persona")


_seed_env()

from app.config import settings
from app.emo_engine.persona.core import Persona


def _create_persona(chat_id: int) -> Persona:
    async def _build() -> Persona:
        return Persona(chat_id=chat_id)

    return asyncio.run(_build())


class PersonaRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = {
            "PERSONA_NAME": settings.PERSONA_NAME,
            "PERSONA_AGE": settings.PERSONA_AGE,
            "PERSONA_GENDER": settings.PERSONA_GENDER,
            "PERSONA_ZODIAC": settings.PERSONA_ZODIAC,
            "PERSONA_TEMPERAMENT": settings.PERSONA_TEMPERAMENT,
            "PERSONA_ARCHETYPES": settings.PERSONA_ARCHETYPES,
            "PERSONA_ROLE": settings.PERSONA_ROLE,
        }
        settings.PERSONA_NAME = "Kira"
        settings.PERSONA_AGE = 24
        settings.PERSONA_GENDER = "female"
        settings.PERSONA_ZODIAC = "Libra"
        settings.PERSONA_TEMPERAMENT = '{"sanguine": 0.4, "choleric": 0.25, "phlegmatic": 0.2, "melancholic": 0.15}'
        settings.PERSONA_ARCHETYPES = '["Rebel", "Jester", "Sage"]'
        settings.PERSONA_ROLE = "Playful companion persona"

    def tearDown(self) -> None:
        settings.PERSONA_NAME = self._prev["PERSONA_NAME"]
        settings.PERSONA_AGE = self._prev["PERSONA_AGE"]
        settings.PERSONA_GENDER = self._prev["PERSONA_GENDER"]
        settings.PERSONA_ZODIAC = self._prev["PERSONA_ZODIAC"]
        settings.PERSONA_TEMPERAMENT = self._prev["PERSONA_TEMPERAMENT"]
        settings.PERSONA_ARCHETYPES = self._prev["PERSONA_ARCHETYPES"]
        settings.PERSONA_ROLE = self._prev["PERSONA_ROLE"]

    def test_baseline_persona_fields(self) -> None:
        persona = _create_persona(chat_id=1)
        persona.apply_overrides(reset=True)
        self.assertEqual(persona.name, "Kira")
        self.assertEqual(persona.age, 24)
        self.assertEqual(persona.gender, "female")
        self.assertEqual(persona.zodiac, "Libra")
        self.assertEqual(persona.temperament["sanguine"], 0.4)

    def test_apply_overrides_updates_profile(self) -> None:
        persona = _create_persona(chat_id=2)
        persona.apply_overrides({
            "name": "Iris",
            "age": 30,
            "gender": "female",
            "zodiac": "Aries",
            "temperament": {
                "sanguine": 0.25,
                "choleric": 0.25,
                "phlegmatic": 0.25,
                "melancholic": 0.25,
            },
            "sociality": "introvert",
            "archetypes": ["Muse"],
            "role": "Calm analyst",
        })
        self.assertEqual(persona.name, "Iris")
        self.assertEqual(persona.age, 30)
        self.assertEqual(persona.zodiac, "Aries")
        self.assertEqual(persona.sociality, "introvert")
        self.assertIn("Muse", persona.archetypes)


if __name__ == "__main__":
    unittest.main()
