import unittest

from app.services.responder.core import _group_coref_source_from_tail, _history_tail_for_coref


class GroupCorefSourceFilterTests(unittest.TestCase):
    def test_keeps_assistant_current_user_and_peer_context(self):
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
                {"role": "assistant", "content": "[peer_context] Чужой контекст"},
                {"role": "assistant", "content": "Ответ 2"},
            ],
        )


    def test_history_tail_is_limited_to_three_pairs(self):
        history = []
        for i in range(1, 13):
            role = "user" if i % 2 else "assistant"
            history.append({"role": role, "content": f"m{i}"})

        out = _history_tail_for_coref(history)

        self.assertEqual(len(out), 6)
        self.assertEqual(out[0]["content"], "m7")
        self.assertEqual(out[-1]["content"], "m12")

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
