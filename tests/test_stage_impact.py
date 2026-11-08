"""Stage ownership and cross-stage rerun policy tests."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from infrastructure.shared.project.stage_impact import (
    database_failure_owner,
    classify_changed_paths,
    owner_for_path,
    repair_request_for_owner,
    stage_write_allowed,
    validation_modules_for_paths,
)


class StageImpactTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / ".arc").mkdir()
        (self.root / ".arc/implementation_paths.json").write_text(json.dumps({
            "page": {"PAGE": "frontend/src/pages/Page.tsx"},
            "trigger": {"TRIGGER": "frontend/src/triggers/TRIGGER.ts"},
            "api": {"API": "backend/src/api/API.ts"},
        }), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_ownership_uses_generated_paths_and_shared_roots(self):
        self.assertEqual(owner_for_path(self.root, "backend/src/db/initialize.ts"), "05")
        self.assertEqual(owner_for_path(self.root, "backend/src/api/API.ts"), "08")
        self.assertEqual(owner_for_path(self.root, "frontend/src/pages/Page.tsx"), "10")
        self.assertEqual(owner_for_path(self.root, "frontend/src/triggers/TRIGGER.ts"), "12")

    def test_later_stage_cannot_write_earlier_stage_files(self):
        self.assertFalse(stage_write_allowed(self.root, "12", "backend/src/db/initialize.ts"))
        self.assertFalse(stage_write_allowed(self.root, "12", "frontend/src/pages/Page.tsx"))
        self.assertFalse(stage_write_allowed(self.root, "12", "frontend/src/ui-runtime.ts"))
        self.assertTrue(stage_write_allowed(self.root, "12", "frontend/src/triggers/TRIGGER.ts"))
        self.assertTrue(stage_write_allowed(self.root, "10", "frontend/src/pages/Page.tsx"))

    def test_implementation_stages_cannot_write_compiler_owned_test_infrastructure(self):
        protected = (
            "backend/scripts/test-api.mjs",
            "backend/tests/support/context.ts",
            "frontend/playwright.e2e.config.ts",
            "frontend/scripts/test-e2e.mjs",
            "frontend/tests/e2e/support/context.ts",
            "frontend/tests/e2e/support/observations.ts",
            "frontend/vite.e2e.config.ts",
        )
        for path in protected:
            self.assertFalse(stage_write_allowed(self.root, "08", path), path)
            self.assertFalse(stage_write_allowed(self.root, "12", path), path)

    def test_changed_paths_are_classified_without_model_inference(self):
        result = classify_changed_paths(self.root, "12", [
            "frontend/src/triggers/TRIGGER.ts",
            "backend/src/db/initialize.ts",
            "unknown.txt",
        ])
        self.assertEqual(result["owned"], ["frontend/src/triggers/TRIGGER.ts"])
        self.assertEqual(result["unknown"], ["unknown.txt"])
        self.assertEqual(result["upstream"][0]["owner_stage"], "05")

    def test_implementation_change_reuses_design_stages(self):
        self.assertEqual(
            validation_modules_for_paths(self.root, ["backend/src/db/initialize.ts"]),
            ["05_implement_db", "08_implement_backend_tdd", "12_implement_frontend_tdd"],
        )
        self.assertEqual(
            validation_modules_for_paths(self.root, ["backend/src/db/initialize.ts"], contract_changed=True)[0],
            "05_implement_db",
        )

    def test_repair_request_records_owner_and_evidence(self):
        request = repair_request_for_owner("12", "05", ["backend/src/db/initialize.ts"],
            "existing database startup failed", check="database")
        self.assertEqual(request["owner_module"], "05_implement_db")
        self.assertEqual(request["failed_check"], "database")
        self.assertFalse(request["contract_changed"])

    def test_database_failure_owner_requires_a_specific_phase(self):
        self.assertEqual(database_failure_owner({"phase": "fresh initialization"})[0], "05")
        self.assertEqual(database_failure_owner({"phase": "fresh server startup"})[0], "08")
        self.assertIsNone(database_failure_owner({"phase": "backend build"}))
        self.assertIsNone(database_failure_owner({"phase": "database initialization"}))


if __name__ == "__main__":
    unittest.main()
