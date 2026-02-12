# ruff: noqa: E402
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


_seed_env()

from app.emo_engine.persona.brain import PersonaBrain
from app.emo_engine.persona.constants.emotions import (
    ALL_METRICS,
    FAT_CLAMP,
    SECONDARY_EMOTIONS,
    TERTIARY_EMOTIONS,
)
from app.emo_engine.persona.constants.tone_map import Tone
from app.emo_engine.persona.core import Persona


class PersonaBrainSnapshotRecomputeTests(unittest.TestCase):
    def test_set_state_from_snapshot_recomputes_secondary_tertiary_dyad_triad(self) -> None:
        brain = PersonaBrain()

        snapshot = {
            "joy": 2.0,
            "trust": 1.5,
            "fear": 0.4,
            "anticipation": 1.3,
            "valence": 1.7,
        }
        brain.set_state_from_snapshot(snapshot)

        self.assertEqual(brain.state["valence"], 1.0)

        expected_optimism_raw = SECONDARY_EMOTIONS["joy"]["optimism"](brain.state)
        expected_optimism = FAT_CLAMP(expected_optimism_raw)
        self.assertAlmostEqual(brain.state["optimism"], expected_optimism)

        expected_hope_raw = TERTIARY_EMOTIONS["optimism"]["hope"](brain.state)
        expected_hope = FAT_CLAMP(expected_hope_raw)
        self.assertAlmostEqual(brain.state["hope"], expected_hope)

        expected_dyad = FAT_CLAMP(0.5 * (brain.state["joy"] + brain.state["trust"]))
        self.assertAlmostEqual(brain.state["joy_trust"], expected_dyad)

        expected_triad = FAT_CLAMP(
            (brain.state["joy"] + brain.state["trust"] + brain.state["fear"]) / 3.0
        )
        self.assertAlmostEqual(brain.state["joy_trust_fear"], expected_triad)


class PersonaUpdateSelfPatternsTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_self_patterns_uses_derived_before_project_to_tones(self) -> None:
        persona = Persona(chat_id=321)
        persona.state.update({
            "joy": 0.92,
            "trust": 0.83,
            "fear": 0.41,
            "anticipation": 0.78,
            "valence": 0.27,
        })

        captured = {}

        def _project_with_assertions():
            optimism_expected = FAT_CLAMP(SECONDARY_EMOTIONS["joy"]["optimism"](persona.brain.state))
            hope_expected = FAT_CLAMP(TERTIARY_EMOTIONS["optimism"]["hope"](persona.brain.state))
            dyad_expected = FAT_CLAMP(
                0.5 * (persona.brain.state["joy"] + persona.brain.state["trust"])
            )
            triad_expected = FAT_CLAMP(
                (
                    persona.brain.state["joy"]
                    + persona.brain.state["trust"]
                    + persona.brain.state["fear"]
                ) / 3.0
            )

            self.assertAlmostEqual(persona.brain.state["optimism"], optimism_expected)
            self.assertAlmostEqual(persona.brain.state["hope"], hope_expected)
            self.assertAlmostEqual(persona.brain.state["joy_trust"], dyad_expected)
            self.assertAlmostEqual(persona.brain.state["joy_trust_fear"], triad_expected)

            captured["called"] = True
            return {Tone.Optimism: 0.91, Tone.Hope: 0.82, Tone.Joy: 0.73}

        persona.brain.project_to_tones = _project_with_assertions  # type: ignore[method-assign]

        await persona._update_self_patterns(uid=77, text="ping")

        self.assertTrue(captured.get("called", False))
        self.assertEqual(len(persona._brain_top_tones), 3)
        self.assertEqual(persona._brain_top_tones[0][0], Tone.Optimism)
        self.assertGreater(persona._brain_top_tones[0][1], 0.0)


class PersonaBrainLearnedMetricsIsolationTests(unittest.TestCase):
    def test_learned_metric_is_local_to_persona_and_recomputed(self) -> None:
        persona_a = Persona(chat_id=1001)
        persona_a.brain.config.activation_threshold = 0.1
        persona_a.brain.config.learn_threshold = 1

        persona_a.brain.update_state({"joy": 0.9, "trust": 0.8}, mode="set")

        self.assertTrue(persona_a.brain.learned_metrics)
        learned_name, parents = next(iter(persona_a.brain.learned_metrics.items()))
        self.assertEqual(parents, ("joy", "trust"))

        expected = FAT_CLAMP(0.5 * (persona_a.brain.state["joy"] + persona_a.brain.state["trust"]))
        self.assertAlmostEqual(persona_a.brain.state[learned_name], expected)

        self.assertNotIn(learned_name, ALL_METRICS)
        self.assertNotIn(learned_name, SECONDARY_EMOTIONS.get("derived", {}))

        persona_b = Persona(chat_id=1002)
        self.assertNotIn(learned_name, persona_b.brain.state)
        self.assertNotIn(learned_name, persona_b.state)


if __name__ == "__main__":
    unittest.main()
