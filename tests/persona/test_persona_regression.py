import os
import unittest


def _seed_env() -> None:
    os.environ.setdefault("OPENAI_API_KEY", "test")
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABCdef1234567890")
    os.environ.setdefault("TELEGRAM_BOT_USERNAME", "testbot")
    os.environ.setdefault("TELEGRAM_BOT_ID", "1")
    os.environ.setdefault("WEBHOOK_URL", "https://example.invalid")
    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/db")
    os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
    os.environ.setdefault("REDIS_URL_QUEUE", "redis://localhost:6379/1")
    os.environ.setdefault("REDIS_URL_VECTOR", "redis://localhost:6379/2")
    os.environ.setdefault("TWITTER_API_KEY", "test")
    os.environ.setdefault("TWITTER_API_SECRET", "test")
    os.environ.setdefault("TWITTER_ACCESS_TOKEN", "test")
    os.environ.setdefault("TWITTER_ACCESS_TOKEN_SECRET", "test")
    os.environ.setdefault("TWITTER_BEARER_TOKEN", "test")
    os.environ.setdefault("PERSONA_NAME", "Bonnie")
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

from app.emo_engine.persona.core import Persona


class PersonaRegressionTests(unittest.TestCase):
    def test_baseline_persona_fields(self) -> None:
        persona = Persona(chat_id=1)
        self.assertEqual(persona.name, "Bonnie")
        self.assertEqual(persona.age, 24)
        self.assertEqual(persona.gender, "female")
        self.assertEqual(persona.zodiac, "Libra")
        self.assertEqual(persona.temperament["sanguine"], 0.4)

    def test_apply_overrides_updates_profile(self) -> None:
        persona = Persona(chat_id=2)
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
