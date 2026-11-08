"""The compiler-owned browser fixture cannot change the application schema."""

import unittest

from infrastructure.stages.frontend.triggers.acceptance import INSTRUCTIONS
from infrastructure.stages.frontend.triggers.e2e_runtime import FIXTURE_SOURCE


class E2ERuntimeTests(unittest.TestCase):
    def test_fixture_snapshots_schema_and_rejects_schema_mutations(self):
        self.assertIn("const schemaBefore", FIXTURE_SOURCE)
        self.assertIn("const schemaAfter", FIXTURE_SOURCE)
        self.assertIn("E2E tests must not change the application database schema", FIXTURE_SOURCE)
        self.assertNotIn('CREATE VIEW IF NOT EXISTS "books"', FIXTURE_SOURCE)
        self.assertEqual(FIXTURE_SOURCE.count("initializeDatabase(connection);"), 1)
        self.assertIn("each acceptance test owns its Given state", FIXTURE_SOURCE)

    def test_acceptance_fixtures_use_exact_schema_names_without_compatibility_layers(self):
        self.assertIn("exact table and column names", INSTRUCTIONS)
        self.assertIn("Never invent plural or snake_case aliases", INSTRUCTIONS)
        self.assertIn("never create a view", INSTRUCTIONS)


if __name__ == "__main__":
    unittest.main()
