import unittest
from unittest.mock import patch

from app.services.responder.rag import api_kb_proc


class ApiKbCacheControlsTests(unittest.TestCase):
    def test_invalidate_api_kb_cache_logs_expected_messages(self):
        with patch.object(api_kb_proc.logger, "info") as info_mock:
            api_kb_proc.invalidate_api_kb_cache()
            api_kb_proc.invalidate_api_kb_cache(10)

        self.assertEqual(info_mock.call_args_list[0].args[0], "API-KB cache invalidated: full")
        self.assertEqual(info_mock.call_args_list[1].args[0], "API-KB cache invalidated: owner_id=%s")


if __name__ == "__main__":
    unittest.main()
