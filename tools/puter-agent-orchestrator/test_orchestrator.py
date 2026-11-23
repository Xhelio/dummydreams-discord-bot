import unittest

from orchestrator import ROLES, build_messages, select_roles


class AgentSelectionTests(unittest.TestCase):
    def test_seven_roles_are_defined(self):
        self.assertEqual(len(ROLES), 7)

    def test_default_selection_returns_all_roles(self):
        self.assertEqual([r.key for r in select_roles()], [
            "core", "tiktok", "database", "dashboard", "qa", "devops", "architect"
        ])

    def test_selection_preserves_requested_order(self):
        self.assertEqual([r.key for r in select_roles(["qa", "core"])], ["qa", "core"])

    def test_unknown_role_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown agent"):
            select_roles(["not-a-role"])

    def test_empty_selection_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            select_roles([])

    def test_duplicate_role_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unique"):
            select_roles(["qa", "qa"])


class PromptSafetyTests(unittest.TestCase):
    def test_prompt_declares_analysis_only_and_no_tool_claims(self):
        messages = build_messages(ROLES[0], "Audit command lifecycle.")
        system = messages[0]["content"]
        self.assertIn("analysis-only mode", system)
        self.assertIn("Never claim to have inspected files", system)

    def test_task_is_inserted_as_user_content(self):
        messages = build_messages(ROLES[0], "Check regression risk.")
        self.assertIn("Check regression risk.", messages[1]["content"])


if __name__ == "__main__":
    unittest.main()
