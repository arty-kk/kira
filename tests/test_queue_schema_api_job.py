import unittest

from app.tasks.queue_schema import validate_api_job


class ApiQueueSchemaTests(unittest.TestCase):
    def test_validate_api_job_requires_knowledge_owner_id(self) -> None:
        payload = {
            "request_id": "r1",
            "text": "hi",
            "chat_id": 10,
            "memory_uid": 10,
            "persona_owner_id": 1,
            "api_key_id": 2,
            "result_key": "api:resp:r1",
            "enqueued_at": 1.0,
        }

        err = validate_api_job(payload)

        self.assertIsNotNone(err)
        self.assertIn("knowledge_owner_id", err)


if __name__ == "__main__":
    unittest.main()
