import unittest

from hybrid_orchestrator import route_roles, validate_cloud_budget
from orchestrator import ROLES, select_roles


class HybridRoutingTests(unittest.TestCase):
    def test_default_hybrid_routes_qa_and_architect_to_cloud(self):
        routes = dict((role.key, provider) for role, provider in route_roles(ROLES, "hybrid"))
        self.assertEqual(routes["qa"], "puter")
        self.assertEqual(routes["architect"], "puter")
        self.assertEqual(routes["core"], "ollama")
        self.assertEqual(routes["tiktok"], "ollama")

    def test_provider_override_routes_all_roles(self):
        selected = select_roles(["core", "qa"])
        self.assertEqual([p for _, p in route_roles(selected, "puter")], ["puter", "puter"])
        self.assertEqual([p for _, p in route_roles(selected, "ollama")], ["ollama", "ollama"])

    def test_unknown_cloud_role_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown cloud role"):
            route_roles(ROLES, "hybrid", ("not-a-role",))

    def test_invalid_provider_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "provider must be"):
            route_roles(ROLES, "unknown")

    def test_cloud_budget_accepts_exact_limit(self):
        assignments = route_roles(ROLES, "hybrid")
        validate_cloud_budget(assignments, 2)

    def test_cloud_budget_rejects_excess_requests(self):
        assignments = route_roles(ROLES, "hybrid")
        with self.assertRaisesRegex(ValueError, "Cloud budget exceeded"):
            validate_cloud_budget(assignments, 1)

    def test_zero_cloud_budget_rejects_cloud_routing(self):
        assignments = route_roles(ROLES, "hybrid")
        with self.assertRaisesRegex(ValueError, "Cloud budget exceeded"):
            validate_cloud_budget(assignments, 0)

    def test_zero_cloud_budget_allows_local_only(self):
        assignments = route_roles(ROLES, "ollama")
        validate_cloud_budget(assignments, 0)

    def test_negative_cloud_budget_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "zero or greater"):
            validate_cloud_budget([], -1)


if __name__ == "__main__":
    unittest.main()
