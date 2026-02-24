import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.emo_engine.persona.stylers import guidelines


class StyleGuidelinesEmbeddingContextTests(unittest.IsolatedAsyncioTestCase):
    def _persona_stub(self):
        return SimpleNamespace(
            state={"arousal": 0.4},
            chat_id=1,
            state_version=1,
            _mods_cache={"novelty_mod": 0.2},
            _style_mods_version=1,
            _decayed_weight=lambda _uid: 0.5,
            attachments={},
            _last_user_msg="tell me more details please",
            _mem_count_cache_ts=0.0,
            _mem_count_cache=1,
            _prev_intensity_pct=None,
            _prev_address_score=None,
            current_dominant=None,
            current_mode_id=None,
            current_mode_stats={},
            _tone_hist=[],
            enhanced_memory=SimpleNamespace(
                count_entries=AsyncMock(return_value=1),
                query=AsyncMock(return_value=[]),
            ),
            style_modifiers=AsyncMock(return_value={"novelty_mod": 0.2}),
            _last_msg_emb=None,
            _last_msg_emb_text=None,
            _emb_inflight=None,
            _emb_inflight_text=None,
        )

    async def test_with_precomputed_embedding_skips_get_embedding(self):
        persona = self._persona_stub()
        with patch.object(guidelines, "_compute_guidelines_sync", return_value={
            "chosen_tone": None,
            "intensity_pct": 50,
            "address_score": 50,
            "flags": ["Tone=Neutral"],
        }), patch.object(guidelines, "_gather_extras", return_value=[]), \
             patch.object(guidelines, "get_embedding", AsyncMock(return_value=[0.9, 0.1])) as get_embedding_mock, \
             patch.object(guidelines.logger, "info") as logger_info:
            await guidelines.style_guidelines(
                persona,
                uid=1,
                precomputed_embedding=[0.1, 0.2],
                embedding_source="reused",
            )

        get_embedding_mock.assert_not_awaited()
        persona.enhanced_memory.query.assert_awaited_once()
        self.assertEqual(persona.enhanced_memory.query.await_args.args[0], [0.1, 0.2])
        found = [c for c in logger_info.call_args_list if c.args and c.args[0] == "style_guidelines embedding context"]
        self.assertTrue(found)
        self.assertEqual(found[0].kwargs["extra"]["embedding_source"], "reused")

    async def test_without_precomputed_embedding_uses_get_embedding(self):
        persona = self._persona_stub()
        with patch.object(guidelines, "_compute_guidelines_sync", return_value={
            "chosen_tone": None,
            "intensity_pct": 50,
            "address_score": 50,
            "flags": ["Tone=Neutral"],
        }), patch.object(guidelines, "_gather_extras", return_value=[]), \
             patch.object(guidelines, "get_embedding", AsyncMock(return_value=[0.3, 0.4])) as get_embedding_mock, \
             patch.object(guidelines.logger, "info") as logger_info:
            await guidelines.style_guidelines(persona, uid=1)

        get_embedding_mock.assert_awaited_once()
        found = [c for c in logger_info.call_args_list if c.args and c.args[0] == "style_guidelines embedding context"]
        self.assertTrue(found)
        self.assertEqual(found[0].kwargs["extra"]["embedding_source"], "computed")

    async def test_invalid_precomputed_embedding_recomputes_and_logs_computed_source(self):
        persona = self._persona_stub()
        with patch.object(guidelines, "_compute_guidelines_sync", return_value={
            "chosen_tone": None,
            "intensity_pct": 50,
            "address_score": 50,
            "flags": ["Tone=Neutral"],
        }), patch.object(guidelines, "_gather_extras", return_value=[]), \
             patch.object(guidelines, "get_embedding", AsyncMock(return_value=[0.7, 0.8])) as get_embedding_mock, \
             patch.object(guidelines.logger, "info") as logger_info:
            await guidelines.style_guidelines(
                persona,
                uid=1,
                precomputed_embedding=[0.0, 0.0],
                embedding_source="reused",
            )

        get_embedding_mock.assert_awaited_once()
        found = [c for c in logger_info.call_args_list if c.args and c.args[0] == "style_guidelines embedding context"]
        self.assertTrue(found)
        self.assertEqual(found[0].kwargs["extra"]["embedding_source"], "computed")


if __name__ == "__main__":
    unittest.main()
