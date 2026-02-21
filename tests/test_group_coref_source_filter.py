import unittest

from app.services.responder.core import _group_coref_source_from_tail


class GroupCorefSourceFilterTests(unittest.TestCase):
    def test_keeps_assistant_and_current_user_only(self):
        tail = [
            "[1] (user) [u:101] Привет",
            "[2] (assistant) [u:999] Ответ 1",
            "[3] (user) [u:202] Чужой контекст",
            "[4] (assistant) [u:999] Ответ 2",
        ]

        out = _group_coref_source_from_tail(tail, user_id=101)

        self.assertEqual(
            out,
            [
                {"role": "user", "content": "Привет"},
                {"role": "assistant", "content": "Ответ 1"},
                {"role": "assistant", "content": "Ответ 2"},
            ],
        )

    def test_skips_broken_lines(self):
        tail = [
            "not a log line",
            "[5] (user) [u:101]   ",
            "[6] (user) [u:oops] Текст",
            "[7] (assistant) [u:999] Ок",
        ]

        out = _group_coref_source_from_tail(tail, user_id=101)

        self.assertEqual(out, [{"role": "assistant", "content": "Ок"}])


if __name__ == "__main__":
    unittest.main()
