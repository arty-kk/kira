import unittest

from app.services.responder.prompt_builder import make_gender_policy


class PromptBuilderGenderPolicyTests(unittest.TestCase):
    def test_female_policy_has_strict_self_gender_lock(self) -> None:
        policy = make_gender_policy("female")

        self.assertIn("SelfGender: female", policy)
        self.assertIn("Never switch self-gender", policy)
        self.assertIn('If USER.Gender is unknown/missing, use polite second-person address in Russian ("Вы/Вам/Ваш")', policy)
        self.assertIn("For Russian", policy)


if __name__ == "__main__":
    unittest.main()
