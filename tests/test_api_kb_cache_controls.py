import unittest
from unittest.mock import AsyncMock, patch

from app.services.responder.rag import api_kb_proc


class ApiKbCacheControlsTests(unittest.IsolatedAsyncioTestCase):
    async def test_ensure_state_does_not_cache_between_calls(self) -> None:
        state = {"_mtime": None, "E": [], "mean": [], "ids": [], "texts": []}

        with patch.object(api_kb_proc, "_has_ready_kb", AsyncMock(return_value=True)), \
             patch.object(api_kb_proc, "_load_state_from_npz", return_value=state) as load_mock:
            first = await api_kb_proc._ensure_state(10, "m1")
            second = await api_kb_proc._ensure_state(10, "m1")

        self.assertIs(first, state)
        self.assertIs(second, state)
        self.assertEqual(load_mock.call_count, 2)

    async def test_invalidate_is_safe_noop_without_runtime_cache(self) -> None:
        with patch.object(api_kb_proc.logger, "info") as info_mock:
            api_kb_proc.invalidate_api_kb_cache()
            api_kb_proc.invalidate_api_kb_cache(10)

        self.assertGreaterEqual(info_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
