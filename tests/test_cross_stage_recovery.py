"""Verify that upstream repair requests schedule only their affected stages."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from infrastructure.shared.artifacts.dependency_graph import DependencyGraph
from infrastructure.shared.project.resume import ResumeManager
from infrastructure.utils.files import save_json


STAGES = [
    "00_project_initialization",
    "01_explore_requirement",
    "02_build_db_schema",
    "03_infer_db_obligations",
    "04_check_db_obligations",
    "05_implement_db",
    "06_design_backend_apis",
    "07_check_backend_apis",
    "08_implement_backend_tdd",
    "09_design_ui",
    "10_implement_ui",
    "11_design_frontend_triggers",
    "12_implement_frontend_tdd",
]


class CrossStageRecoveryTests(unittest.TestCase):
    def seed(self, root):
        output = root / "output"
        input_dir = root / "input"
        (output / ".arc").mkdir(parents=True)
        input_dir.mkdir()
        graph = DependencyGraph(output)
        for module in STAGES:
            graph.register("stage:" + module, stage=module, complete=True, valid=True)
        graph.flush()
        manager = ResumeManager(output, input_dir, graph)
        for module in STAGES:
            manager.mark_task_done(module, "stage", {"status": "GREEN"})
        return output, input_dir, graph, manager

    def resumed_for(self, request):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            output, input_dir, graph, manager = self.seed(root)
            state = json.loads(manager.path.read_text(encoding="utf-8"))
            state["stages"]["12_implement_frontend_tdd"]["result"]["upstream_repair_request"] = request
            save_json(manager.path, state)
            resumed = ResumeManager(output, input_dir, graph, enabled=True)
            return resumed.rerun["stages"]

    def test_database_implementation_repair_skips_unrelated_design_stages(self):
        stages = self.resumed_for({
            "status": "PENDING",
            "owner_stage": "05",
            "owner_module": "05_implement_db",
            "changed_paths": ["backend/src/db/initialize.ts"],
            "contract_changed": False,
        })
        self.assertEqual(stages, [
            "05_implement_db", "08_implement_backend_tdd", "12_implement_frontend_tdd",
        ])

    def test_backend_and_ui_repairs_recheck_their_consumers(self):
        backend = self.resumed_for({
            "status": "PENDING",
            "owner_stage": "08",
            "owner_module": "08_implement_backend_tdd",
            "changed_paths": ["backend/src/api/register.ts"],
            "contract_changed": False,
        })
        ui = self.resumed_for({
            "status": "PENDING",
            "owner_stage": "10",
            "owner_module": "10_implement_ui",
            "changed_paths": ["frontend/src/pages/Home.tsx"],
            "contract_changed": False,
        })
        self.assertEqual(backend, ["08_implement_backend_tdd", "12_implement_frontend_tdd"])
        self.assertEqual(ui, ["10_implement_ui", "12_implement_frontend_tdd"])

    def test_contract_change_restarts_the_full_suffix(self):
        stages = self.resumed_for({
            "status": "PENDING",
            "owner_stage": "05",
            "owner_module": "05_implement_db",
            "changed_paths": [".arc/db_schema.json"],
            "contract_changed": True,
        })
        self.assertEqual(stages, STAGES[5:])


if __name__ == "__main__":
    unittest.main()
