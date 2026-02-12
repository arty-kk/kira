import unittest

from app.tasks.queue_schema import validate_bot_job


class QueueSchemaBotJobReservationIdsTests(unittest.TestCase):
    def test_validate_bot_job_accepts_positive_reservation_ids(self) -> None:
        payload = {
            "chat_id": 1,
            "user_id": 2,
            "text": "hello",
            "msg_id": 3,
            "reservation_ids": [10, 11],
            "entities": [],
        }
        self.assertIsNone(validate_bot_job(payload))

    def test_validate_bot_job_rejects_invalid_reservation_ids(self) -> None:
        payload = {
            "chat_id": 1,
            "user_id": 2,
            "text": "hello",
            "msg_id": 3,
            "reservation_ids": [10, 0, "bad"],
            "entities": [],
        }
        err = validate_bot_job(payload)
        self.assertIsNotNone(err)
        self.assertIn("reservation_ids", err or "")


if __name__ == "__main__":
    unittest.main()
