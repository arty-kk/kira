import asyncio
import threading
import unittest
from unittest import mock

import numpy as np

from app.services.responder.rag import knowledge_proc


class KnowledgeProcConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        knowledge_proc._KB_ENTRIES.clear()
        knowledge_proc._KB_STATE.clear()

    async def test_concurrent_get_relevant_builds_state_once_under_lock(self):
        model = "test-concurrency-model"
        base_entries = [
            {"id": "1", "text": "alpha", "emb": [1.0, 0.0, 0.0]},
            {"id": "2", "text": "beta", "emb": [0.9, 0.1, 0.0]},
            {"id": "3", "text": "gamma", "emb": [0.0, 1.0, 0.0]},
        ]

        # Cold-cache setup + precomputed source stub.
        knowledge_proc._KB_ENTRIES.clear()
        knowledge_proc._KB_STATE.clear()

        with mock.patch.object(knowledge_proc, "_load_precomputed", return_value=base_entries):
            entries = knowledge_proc._load_precomputed(model)
        knowledge_proc._KB_ENTRIES[model] = entries

        build_calls = 0
        calls_guard = threading.Lock()
        original_build_state = knowledge_proc._build_state

        def _slow_build_state(local_entries):
            nonlocal build_calls
            with calls_guard:
                build_calls += 1
            # Increase overlap window for concurrent callers.
            import time
            time.sleep(0.05)
            return original_build_state(local_entries)

        async def _fake_get_query_embedding(_api_model: str, _query: str):
            return np.array([1.0, 0.0, 0.0], dtype=np.float32)

        with (
            mock.patch.object(knowledge_proc, "_build_state", side_effect=_slow_build_state),
            mock.patch.object(knowledge_proc, "_get_query_embedding", side_effect=_fake_get_query_embedding),
        ):
            tasks = [
                knowledge_proc.get_relevant(f"query-{idx}", model_name=model)
                for idx in range(8)
            ]
            results = await asyncio.gather(*tasks)

        self.assertEqual(build_calls, 1)
        self.assertTrue(all(results), "all concurrent calls must produce non-empty hits")
        self.assertIn(model, knowledge_proc._KB_STATE)


if __name__ == "__main__":
    unittest.main()
