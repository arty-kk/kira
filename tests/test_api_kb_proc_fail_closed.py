import unittest
from unittest import mock

from app.services.responder.rag import api_kb_proc


class ApiKbCacheControlsTests(unittest.TestCase):
    def test_invalidate_api_kb_cache_logs(self):
        with mock.patch.object(api_kb_proc.logger, "info") as info_mock:
            api_kb_proc.invalidate_api_kb_cache()
            api_kb_proc.invalidate_api_kb_cache(10)

        self.assertEqual(info_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
