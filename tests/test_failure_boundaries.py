"""Regress shared effects, failed prerequisites and local edits without model requests."""

from copy import deepcopy
from importlib import import_module
import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from infrastructure.shared.agent.tools import FileTools, FileToolError
from infrastructure.utils.files import save_json, save_text
from test_backend_contracts import prepare_artifacts, conditions

def call(tool, **arguments):
    return {"tool": tool, **arguments}


class FailureBoundaries(unittest.TestCase):
    def test_repeated_shared_conflict_can_be_repaired_on_third_proposal(self):
        stage = import_module("06_design_backend_apis")
        with TemporaryDirectory() as directory:
            arc, _, design = prepare_artifacts(Path(directory))
            calls = []
            def generate(_, request):
                rid = request["requirement"]["id"]
                calls.append(rid)
                ids = design["requirements"][rid]
                apis = {aid: {**deepcopy(design["apis"][aid]), **conditions(aid)} for aid in ids}
                if rid == "REQ-B" and calls.count(rid) < 3:
                    apis["API-SAVE"]["write"].append("Note.tag")
                return {"apis": apis, "requirement_apis": ids}
            with patch.object(stage, "TextModel", return_value=Mock(generate_json=generate)), \
                    patch.object(stage, "DemoConsole", return_value=Mock()):
                result = stage.design_backend_apis(arc)
            self.assertEqual(calls, ["REQ-A", "REQ-B", "REQ-B", "REQ-B", "REQ-C"])
            persisted = json.loads((arc / "backend_apis.json").read_text())
            self.assertEqual(persisted, result)
            self.assertEqual(persisted["requirements"]["REQ-B"], ["API-SAVE"])
            self.assertEqual(persisted["requirements"]["REQ-C"], ["API-COUNT"])

    def test_shared_contract_conflict_supplies_prior_evidence_and_rejects_added_writes(self):
        stage = import_module("06_design_backend_apis")
        with TemporaryDirectory() as directory:
            arc, _, design = prepare_artifacts(Path(directory))
            calls = []

            def generate(_, request):
                rid = request["requirement"]["id"]
                calls.append(rid)
                ids = design["requirements"][rid]
                proposal = {"apis": {aid: {**deepcopy(design["apis"][aid]), **conditions(aid)} for aid in ids},
                    "requirement_apis": ids}
                if rid == "REQ-B" and "correction" not in request:
                    proposal["apis"]["API-SAVE"]["write"].append("Note.tag")
                elif rid == "REQ-B":
                    conflict = request["correction"]["details"]["shared_contract_conflict"]
                    self.assertEqual(conflict["proposed_for"], rid)
                    self.assertEqual(conflict["consumers"]["REQ-A"]["requirement"]["id"], "REQ-A")
                    self.assertEqual(conflict["changed_apis"]["API-SAVE"]["added"]["write"], ["Note.tag"])
                    self.assertNotIn("Note.tag", conflict["consumers"]["REQ-A"]["obligations"]["write"])
                return proposal

            with patch.object(stage, "TextModel", return_value=Mock(generate_json=generate)), \
                    patch.object(stage, "DemoConsole", return_value=Mock()):
                result = stage.design_backend_apis(arc)
            self.assertEqual(calls, ["REQ-A", "REQ-B", "REQ-B", "REQ-C"])
            self.assertNotIn("Note.tag", result["apis"]["API-SAVE"]["write"])

    def test_failed_design_is_durable_and_blocks_only_its_consumers(self):
        from infrastructure.shared.agent.tasks import TaskFailures, require_design_task, UpstreamTaskError
        with TemporaryDirectory() as directory:
            arc = Path(directory)
            tasks = TaskFailures(arc, "06_design_backend_apis", Mock())
            with tasks.task("BROKEN"):
                raise ValueError("Cannot preserve the shared operation")
            with tasks.task("STATIC"):
                pass
            report = json.loads((arc / "stage_tasks/06_design_backend_apis.json").read_text())
            self.assertEqual(report["tasks"]["BROKEN"]["status"], "FAILED")
            with self.assertRaises(UpstreamTaskError) as failure:
                require_design_task(arc, "06_design_backend_apis", "BROKEN", "backend_apis.json", {})
            self.assertIn("Cannot preserve", str(failure.exception))
            require_design_task(arc, "06_design_backend_apis", "STATIC", "backend_apis.json", {"STATIC": []})
            with self.assertRaises(UpstreamTaskError):
                require_design_task(arc, "06_design_backend_apis", "MISSING", "backend_apis.json", {})

    def test_full_candidate_with_local_changes_preserves_other_code_and_rolls_back_atomically(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = "frontend/src/navigation.ts"
            before = "".join(f"export const value{i} = {i};\n" for i in range(12))
            after = before.replace("value4 = 4", "value4 = 40")
            save_text(root / path, before)
            files = FileTools(root, [path])
            with self.assertRaises(FileToolError):
                files.execute([call("edit_file", path=path, operation="replace", old_content=before, new_content=after),
                    call("edit_file", path="../escape.ts", operation="create", new_content="bad")])
            self.assertEqual((root / path).read_text(), before)
            files.execute([call("edit_file", path=path, operation="replace", old_content=before, new_content=after)])
            self.assertEqual((root / path).read_text(), after)
            with self.assertRaises(FileToolError) as failure:
                files.execute([call("edit_file", path=path, operation="replace", old_content=after, new_content="unrelated")])
            self.assertEqual(failure.exception.feedback["code"], "WHOLE_FILE_REPLACEMENT_NOT_ALLOWED")
            self.assertIn("example", failure.exception.feedback)

    def test_local_conversion_still_runs_real_http_gate_and_preserves_existing_route(self):
        from infrastructure.shared.agent.loop import run_file_agent
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = "backend/src/handler.mjs"
            before = "".join(f"export const value{i} = {i};\n" for i in range(12)) + """
export function handler(request, response) {
  response.end(request.url === "/health" ? "healthy" : "wrong");
}
"""
            after = before.replace('"wrong"', '"ready"')
            save_text(root / path, before)
            script = """
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { handler } from './backend/src/handler.mjs';
const server = createServer(handler).listen(0, '127.0.0.1');
try {
  await once(server, 'listening');
  const base = 'http://127.0.0.1:' + server.address().port;
  assert.equal(await (await fetch(base + '/health')).text(), 'healthy');
  assert.equal(await (await fetch(base + '/records')).text(), 'ready');
} finally { await new Promise(resolve => server.close(resolve)); }
"""
            def gate():
                result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=root,
                    capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return {"returncode": result.returncode}
            checked = Mock(side_effect=gate)
            model = Mock(generate_json=Mock(return_value={"tool_calls": [call("edit_file",
                path=path, operation="replace", old_content=before, new_content=after)]}))
            run_file_agent(model, "", {}, FileTools(root, [path]), checked, 1, Mock(), "Local repair")
            checked.assert_called_once()

    def test_all_failed_api_designs_do_not_become_successful_no_api_backend(self):
        stage = import_module("08_implement_backend_tdd")
        with TemporaryDirectory() as directory:
            arc, _, _ = prepare_artifacts(Path(directory))
            save_json(arc / "backend_apis.json", {"apis": {}, "requirements": {"ROOT": []}})
            with patch.object(stage, "install_dependencies"), patch.object(stage, "TextModel") as model, \
                    patch.object(stage, "DemoConsole", return_value=Mock()):
                report = stage.implement_backend_tdd(arc)
            model.assert_not_called()
            self.assertEqual(report["status"], "COMPLETED_WITH_FAILURES")
            self.assertEqual(set(report["requirements"]), {"REQ-A", "REQ-B", "REQ-C"})

    def test_failed_api_blocks_trigger_design_without_guessing_empty_effects(self):
        from infrastructure.stages.frontend.triggers.synthesis import synthesize_triggers
        from infrastructure.shared.agent.tasks import StageIncomplete
        from test_frontend_flow import frontend_fixture
        _, tree, ui, design, backend, obligations = frontend_fixture()
        obligations["steps"] = {sid: obligations["requirements"][rid]
            for rid in tree.leaves for sid in tree.own_steps[rid]}
        del backend["requirements"]["REQ-A"]
        calls = []
        def generate(_, request):
            rid = request["requirement"]["id"]
            calls.append(rid)
            ids = design["requirements"][rid]
            return {"triggers": {tid: design["triggers"][tid] for tid in ids},
                "requirement_triggers": ids, "step_triggers": {sid: ids for sid in tree.own_steps[rid]}}
        with TemporaryDirectory() as directory:
            arc = Path(directory)
            save_json(arc / "db_obligations.json", obligations)
            save_json(arc / "stage_tasks/06_design_backend_apis.json",
                {"tasks": {rid: {"status": "FAILED", "error": "Shared contract conflict"} if rid == "REQ-A"
                    else {"status": "GREEN"} for rid in tree.leaves}})
            with self.assertRaises(StageIncomplete):
                synthesize_triggers(arc, ui, tree, backend, tree.requirements, Mock(generate_json=generate), Mock())
            self.assertNotIn("REQ-A", calls)
            report = json.loads((arc / "stage_tasks/11_design_frontend_triggers.json").read_text())
            self.assertIn("REQ-C", calls, report)
            self.assertEqual(report["tasks"]["REQ-A"]["blocked_by"]["task"], "REQ-A")

    def test_blocked_acceptance_skips_model_and_continues_independent_requirements(self):
        import yaml
        from test_frontend_flow import frontend_fixture
        from infrastructure.stages.frontend.triggers.acceptance import run_independent_acceptance
        raw, _, ui, design, backend, _ = frontend_fixture()
        del backend["requirements"]["REQ-A"]
        calls = []
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input"
            source.mkdir()
            save_text(source / "requirements.yaml", yaml.safe_dump(raw))
            save_json(root / ".arc/stage_tasks/06_design_backend_apis.json", {"tasks": {
                rid: {"status": "FAILED", "error": "Contract unavailable"} if rid == "REQ-A"
                else {"status": "GREEN"} for rid in ("REQ-A", "REQ-B", "REQ-C")}})
            def generate(_, request):
                calls.append(request["task"]["original_requirement"]["id"])
                return {"tool_calls": [call("edit_file", path=request["task"]["test_path"],
                    operation="create", new_content="requirement assertions")]}
            checks = Mock()
            checks.typecheck.return_value = {"returncode": 0}
            checks.tests.return_value = {"returncode": 0, "passed": 1, "failed": 0}
            with patch("infrastructure.stages.frontend.triggers.acceptance.E2EChecks", return_value=checks), \
                    patch("infrastructure.stages.frontend.triggers.acceptance.BackendChecks"), \
                    patch("infrastructure.stages.frontend.triggers.acceptance.FrontendChecks"):
                report = run_independent_acceptance(source, root / ".arc", {"entities": {}},
                    backend, ui, design, Mock(generate_json=generate), Mock(),
                    process_report={"requirements": {}, "verification": {}})
            self.assertEqual(calls, ["REQ-B", "REQ-C"])
            self.assertEqual(report["requirements"]["REQ-A"]["blocked_by"]["stage"], "06_design_backend_apis")
            self.assertEqual(report["requirements"]["REQ-C"]["status"], "GREEN")
