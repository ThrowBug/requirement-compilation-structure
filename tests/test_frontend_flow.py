"""Frontend representation, publication, lifecycle dependencies and acceptance regressions."""
from tool_fixtures import propose_files

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import Mock

from infrastructure.stages.requirements.tree import RequirementTree
from infrastructure.stages.frontend.context import load_ui_context
from infrastructure.stages.frontend.ui.design import apply_proposal, empty_design, page_context, normalize_design
from infrastructure.stages.frontend.ui.synthesis import synthesize_ui
from infrastructure.stages.frontend.ui.testing import presentation_run_status
from infrastructure.stages.frontend.triggers.design import empty_triggers, trigger_obligations
from infrastructure.stages.frontend.triggers.implementation import scenario_context, test_run_status
from infrastructure.utils.files import save_json, save_text


def node(kind, parent, data=(), mode=(), **props):
    return {"type": kind, "parent": parent, "data": list(data), "mode": list(mode), "prop": props}


def binding(dtype, initial, source):
    return {"dtype": dtype, "constraints": [], "initial": initial, "source": source}


def frontend_fixture():
    """A shared editor navigates to a later requirement's read-only summary page."""
    raw = {"id": "ROOT", "name": "Notes", "children": [
        {"id": rid, "name": rid, "description": description, "scenarios": [{"id": "SCN-" + rid,
            "steps": [{"id": "STEP-" + rid, "Given": "Existing notes in the database", "When": behavior, "Then": outcome}]}]}
        for rid, description, behavior, outcome in (
            ("REQ-A", "Save a note then navigate to /summary; empty text is rejected without writes.", "Enter text and submit the editor", "Show the updated count after navigation and refresh; show an error on rejection"),
            ("REQ-B", "Use the same editor and API to save another note.", "Enter another note and save", "Persist it and display the summary"),
            ("REQ-C", "The /summary page displays the count on every entry and refresh without writes.", "Open or refresh /summary", "Display the persisted count and allow returning to the editor"))]}
    tree = RequirementTree(raw)
    ui = {"pages": {
        "editor": {"name": "Editor", "route": "/", "root": "UI-EDITOR", "images": [], "fixtures": {"default": {}, "rejected": {"form.errorVisible": True, "form.error": "Text required"}}},
        "summary": {"name": "Summary", "route": "/summary", "root": "UI-SUMMARY", "images": [], "fixtures": {"default": {}, "loaded": {"summary.count": "2"}}}},
        "elements": {
            "UI-EDITOR": node("page", None), "UI-FORM": node("form", "UI-EDITOR"),
            "UI-TEXT": node("input", "UI-FORM", [{"var": "form.text", "dtype": "string", "constraints": []}], ["user_write"], label="Note", input_type="text"),
            "UI-SAVE": node("button", "UI-FORM", text="Save"),
            "UI-ERROR": node("alert", "UI-EDITOR", [{"var": "form.error", "dtype": "string", "constraints": []}], visible={"var": "form.errorVisible", "equals": True}),
            "UI-SUMMARY": node("page", None), "UI-COUNT": node("text", "UI-SUMMARY", [{"var": "summary.count", "dtype": "string", "constraints": []}]),
            "UI-BACK": node("link", "UI-SUMMARY", text="Back", href="/")},
        "bindings": {"form.text": binding("string", "", "Native note input"), "form.error": binding("string", "", "API rejection message"),
            "form.errorVisible": binding("boolean", False, "Whether to show rejection"), "summary.count": binding("string", "Loading", "Count API response")},
        "requirements": {"ROOT": [], "REQ-A": ["UI-EDITOR", "UI-FORM", "UI-TEXT", "UI-SAVE", "UI-ERROR"],
            "REQ-B": ["UI-EDITOR", "UI-FORM", "UI-TEXT", "UI-SAVE", "UI-ERROR"], "REQ-C": ["UI-SUMMARY", "UI-COUNT", "UI-BACK"]},
        "steps": {"STEP-REQ-A": ["UI-TEXT", "UI-SAVE", "UI-ERROR"], "STEP-REQ-B": ["UI-TEXT", "UI-SAVE"], "STEP-REQ-C": ["UI-COUNT", "UI-BACK"]}}
    interactions = {'triggers': {'TR-INPUT': {'source': 'UI-TEXT',
                               'event': 'change',
                               'guard': None,
                               'read': [],
                               'action': [{'kind': 'ui',
                                           'description': 'Write input.value into form.text',
                                           'ui': 'UI-TEXT'}],
                               'write': ['form.text']},
                  'TR-SAVE': {'source': 'UI-FORM',
                              'event': 'submit',
                              'guard': None,
                              'read': ['form.text'],
                              'action': [{'kind': 'api',
                                          'api': 'API-SAVE',
                                          'description': 'POST form.text as body.text'},
                                         {'kind': 'ui',
                                          'description': 'On SAVE status 400 show its error message and set '
                                                         'form.errorVisible=true',
                                          'ui': 'UI-FORM'},
                                         {'kind': 'ui',
                                          'description': 'On SAVE status 200 navigate to summary',
                                          'ui': 'UI-SUMMARY'}],
                              'write': ['form.error', 'form.errorVisible']},
                  'TR-COUNT': {'source': 'UI-SUMMARY',
                               'event': 'mount',
                               'guard': None,
                               'read': [],
                               'action': [{'kind': 'api',
                                           'api': 'API-COUNT',
                                           'description': 'GET the current count'},
                                          {'kind': 'ui',
                                           'description': 'Write String(COUNT response.body.count) to '
                                                          'summary.count',
                                           'ui': 'UI-SUMMARY'}],
                               'write': ['summary.count']}},
     'requirements': {'ROOT': [],
                      'REQ-A': ['TR-INPUT', 'TR-SAVE'],
                      'REQ-B': ['TR-INPUT', 'TR-SAVE'],
                      'REQ-C': ['TR-COUNT']},
     'steps': {'STEP-REQ-A': ['TR-INPUT', 'TR-SAVE'],
               'STEP-REQ-B': ['TR-INPUT', 'TR-SAVE'],
               'STEP-REQ-C': ['TR-COUNT']}}
    backend = {"requirements": {"ROOT": [], "REQ-A": ["API-SAVE"], "REQ-B": ["API-SAVE"], "REQ-C": ["API-COUNT"]},
        "apis": {"API-SAVE": {"read": [], "write": ["Note.id", "Note.text"]}, "API-COUNT": {"read": ["Note.id", "Note.text"], "write": []}}}
    obligations = {"requirements": {rid: {"read": [] if rid != "REQ-C" else ["Note.id", "Note.text"],
        "write": ["Note.id", "Note.text"] if rid in {"REQ-A", "REQ-B"} else []} for rid in tree.requirements}}
    return raw, tree, ui, interactions, backend, obligations


class FrontendFlow(unittest.TestCase):
    def setUp(self):
        self.raw, self.tree, self.ui, self.interactions, self.backend, self.obligations = frontend_fixture()
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)
        self.arc = self.output / ".arc"

    def test_ui_design_accepts_model_metadata_without_type_or_semantic_gates(self):
        ui = deepcopy(self.ui)
        ui["bindings"]["form.text"]["dtype"] = "custom"
        ui["bindings"]["form.text"]["source"] = 123
        ui["pages"]["summary"]["route"] = "/"
        normalize_design(ui, self.tree)
        self.assertEqual(ui["bindings"]["form.text"]["source"], 123)


    def test_requirement_creates_all_triggers_in_one_call_and_runs_complete_acceptance(self):
        """Create the current requirement's target handlers in one acceptance scope."""
        self.check_event_creation()

    def test_green_tests_do_not_skip_missing_or_deleted_event_files(self):
        """A registered but deleted trigger remains in the implementation task."""
        from infrastructure.shared.project.entrypoints import remember_path
        remember_path(self.output, "trigger", "TR-COUNT", "frontend/src/deleted.ts")
        self.check_event_creation(early_green=True)

    def test_requirement_repair_can_edit_every_created_handler(self):
        """The requirement agent can repair any handler after a failed batch check."""
        self.check_event_creation(needs_repair=True)

    def test_partial_trigger_batch_receives_missing_files_without_passing_acceptance(self):
        self.check_event_creation(partial_batch=True)

    def test_trigger_batch_reuses_existing_path_and_only_creates_missing_handlers(self):
        self.check_event_creation(preexisting=True)

    def test_integration_can_repair_related_backend_sources_and_regresses_api_tests(self):
        self.check_event_creation(backend_repair=True)

    def test_backend_regression_failure_returns_to_integration_repair(self):
        self.check_event_creation(backend_repair=True, backend_fails_once=True)

    def check_event_creation(self, early_green=False, needs_repair=False, partial_batch=False, preexisting=False, backend_repair=False, backend_fails_once=False):
        """Run the real orchestration with fake browser reports and model edits."""
        from unittest.mock import patch
        from infrastructure.shared.agent.tools import FileTools
        from infrastructure.shared.project.entrypoints import selected_path, remember_path
        from infrastructure.shared.project.initialize import initialize_project
        from infrastructure.stages.frontend.triggers import implementation
        initialize_project(self.output)
        existing_path = "frontend/src/custom/input.ts"
        if preexisting:
            remember_path(self.output, "trigger", "TR-INPUT", existing_path)
            save_text(self.output / existing_path, "export function handle() { /* existing */ }")
        api_paths = {}
        backend_suites = []
        for aid in self.backend["apis"]:
            api_paths[aid] = f"backend/src/handlers/{aid}.ts"
            remember_path(self.output, "api", aid, api_paths[aid])
            save_text(self.output / api_paths[aid], "export function handle() {}")
            suite = f"backend/tests/{aid}.test.ts"
            remember_path(self.output, "backend_test", aid, suite)
            save_text(self.output / suite, "frozen API assertions")
            backend_suites.append(suite)
        save_text(self.output / "frontend/src/features/other-page-helper.ts", "export const helper = 1;")
        save_text(self.output / "backend/src/features/other-api-helper.ts", "export const helper = 2;")
        for pid in self.ui["pages"]:
            workspace = FileTools.for_target(self.output, "page", pid)
            workspace.state()
            workspace.execute(propose_files(self.output, [{"path": f"frontend/src/screens/{pid}.tsx",
                "content": "export function Page() {return null;}"}])["tool_calls"])
        implementation_calls = []
        implementation_counts = {}
        current_paths = {}
        test_requirements = {}
        repaired = False
        def generate(instructions, payload):
            nonlocal repaired
            task = payload["task"]
            if "implementation_paths" in task and "file_target" not in task:
                self.assertIn("every listed path is a required", instructions)
                self.assertIn("wire them into the real interaction", instructions)
                implementation_calls.append(deepcopy(payload))
                rid = task["requirement"]["id"]
                implementation_counts[rid] = implementation_counts.get(rid, 0) + 1
                current_paths[rid] = set(task["implementation_paths"].values())
                self.assertEqual(set(task["triggers"]), set(task["target_trigger_ids"]))
                for eid in task["target_trigger_ids"]:
                    path = task["implementation_paths"][eid]
                    self.assertTrue(payload["files"].get(path, {}).get("writable", True), eid)
                self.assertNotIn("frontend/tests/ui/editor.spec.ts", payload["files"])
                for pid in ("editor", "summary"):
                    path = f"frontend/src/screens/{pid}.tsx"
                    if pid in task["page_subtrees"]:
                        self.assertTrue(payload["files"][path]["writable"])
                    elif path in payload["files"]:
                        self.assertFalse(payload["files"][path]["writable"])
                for aid, path in api_paths.items():
                    if aid in task["backend_apis"]:
                        self.assertTrue(payload["files"][path]["writable"])
                    elif path in payload["files"]:
                        self.assertFalse(payload["files"][path]["writable"])
                for path in ("frontend/src/features/other-page-helper.ts", "backend/src/features/other-api-helper.ts", "backend/src/server.ts"):
                    self.assertNotIn(path, payload["files"])
                self.assertTrue(payload["files"]["frontend/src/style.css"]["writable"])
                ids = sorted(tid for tid, path in task["implementation_paths"].items()
                    if not (self.output / path).is_file())
                if implementation_counts[rid] == 1:
                    self.assertEqual(set(ids), set(task["target_trigger_ids"]) - ({"TR-INPUT"} if preexisting else set()))
                    if partial_batch:
                        ids = ids[:-1]
                    edits = [{"path": task["implementation_paths"][tid],
                        "content": "export function handle() { /* needs-repair */ }" if needs_repair
                            else "export function handle() {}"} for tid in ids]
                    if backend_repair:
                        edits.append({"path": api_paths["API-SAVE"], "content": "export function handle() { /* fixed integration */ }"})
                    return propose_files(self.output, edits)
                if partial_batch:
                    self.assertIn("Create all supplied trigger files", payload["feedback"][0]["error"])
                    edits = [{"path": path, "content": "export function handle() {}"}
                        for path in task["implementation_paths"].values()
                        if not (self.output / path).is_file()]
                    return propose_files(self.output, edits)
                if backend_fails_once:
                    self.assertEqual(payload["feedback"][0]["checks"]["backend_tests"]["returncode"], 1)
                    self.assertFalse(payload["files"][backend_suites[0]]["writable"])
                    repaired = True
                    return propose_files(self.output, [{"path": api_paths["API-SAVE"], "content": "export function handle() { /* API regression repaired */ }"}])
                repaired = True
                if needs_repair:
                    return propose_files(self.output, [{"path": path, "old_content": "/* needs-repair */",
                        "content": "/* repaired */"} for path in task["implementation_paths"].values()
                        if "/* needs-repair */" in (self.output / path).read_text()])
                return propose_files(self.output, [])

            creation = task["file_target"]
            self.assertEqual(creation["kind"], "e2e_test")
            for pid in task["page_subtrees"]:
                self.assertTrue(payload["files"][f"frontend/src/screens/{pid}.tsx"]["writable"])
            self.assertEqual(task["max_tests"], 2)
            for aid in task["backend_apis"]:
                self.assertTrue(payload["files"][api_paths[aid]]["writable"])
            self.assertFalse(payload["files"]["frontend/src/ui-runtime.ts"]["writable"])
            utilities = payload["files"]["backend/tests/support/context.ts"]
            self.assertFalse(utilities["writable"])
            self.assertIn("export function fixturePasswordHash(password: string): string", utilities["content"])
            path = f"frontend/tests/e2e/{creation['id']}.spec.ts"
            test_requirements[path] = creation["id"]
            current_paths[creation["id"]] = {f"frontend/src/triggers/{tid}.ts" for tid in task["target_trigger_ids"]}
            return propose_files(self.output, [{"path": path, "content": "export function handle() {}"}])
        checks, api_checks = Mock(), Mock()
        checks.typecheck.return_value = {"returncode": 0, "output": "compiled"}
        checks.build.return_value = {"returncode": 0, "output": "built"}
        green = {"returncode": 0, "output": "passed", "passed": 1, "failed": 0,
            "skipped": 0, "flaky": 0, "errors": [], "failures": []}
        green_test_status = {"returncode": 0, "output": "passed", "passed": 1, "failed": 0,
            "skipped": 0, "flaky": 0, "errors": [], "failures": []}
        api_checks.typecheck.return_value = {"returncode": 0, "output": "compiled backend"}
        api_checks.tests.return_value = {"returncode": 0, "tests": 2, "pass": 2, "fail": 0, "output": "API regression passed"}
        database_checks = Mock()
        database_checks.run.return_value = {"returncode": 0, "status": "GREEN", "phase": "output database initialization"}
        if backend_fails_once:
            api_checks.tests.side_effect = [
                {"returncode": 1, "tests": 2, "pass": 1, "fail": 1, "output": "Assertion failed: " + backend_suites[0]},
                api_checks.tests.return_value, api_checks.tests.return_value]
        def test_events(files):
            if early_green:
                return green_test_status
            required = {selected_path(self.output, "trigger", eid) or f"frontend/src/triggers/{eid}.ts"
                for eid in self.interactions["triggers"]}
            if all((self.output / path).is_file() and "/* needs-repair */" not in
                    (self.output / path).read_text() for path in required):
                return green_test_status
            return {**green, "returncode": 1, "passed": 0, "failed": 1,
                "failures": ["expect(received).toBe(expected)"]}
        checks.tests.side_effect = test_events
        backend = deepcopy(self.backend)
        for aid, api in backend["apis"].items():
            api["signature"] = {"method": "POST", "path": "/api/" + aid, "name": aid}
        with patch.object(implementation, "install_dependencies"), \
                patch.object(implementation, "E2EChecks", return_value=checks), \
                patch.object(implementation, "BackendChecks", return_value=api_checks) as backend_checks, \
                patch.object(implementation, "DatabaseChecks", return_value=database_checks):
            report = implementation.run_trigger_tdd(self.arc, self.tree, {"entities": {}},
                backend, self.ui, self.interactions, {},
                {rid: {"id": rid} for rid in self.tree.leaves}, Mock(generate_json=generate),
                Mock(), {"requirements": {}}, max_turns=2)
        self.assertEqual(report["status"], "GREEN")
        self.assertEqual(set(implementation_calls[0]["task"]["implementation_paths"]),
            set(self.interactions["requirements"]["REQ-A"]) | {"TR-COUNT"})
        self.assertEqual(len(implementation_calls), 2 if needs_repair or partial_batch or backend_fails_once else 1)
        self.assertEqual(report["requirements"]["REQ-A"]["implementation_turns"],
            2 if needs_repair or partial_batch or backend_fails_once else 1)
        if early_green:
            self.assertEqual(selected_path(self.output, "trigger", "TR-COUNT"), "frontend/src/deleted.ts")
        if preexisting:
            self.assertEqual(selected_path(self.output, "trigger", "TR-INPUT"), existing_path)
            self.assertEqual((self.output / existing_path).read_text(), "export function handle() { /* existing */ }")
        self.assertEqual(report["requirements"]["REQ-B"]["implementation_turns"], 0)
        if len(implementation_calls) > 1:
            self.assertTrue(all((self.output / path).is_file() for path in implementation_calls[1]["task"]["implementation_paths"].values()))
        self.assertTrue(all((self.output / path).is_file() for path in implementation_calls[0]["task"]["implementation_paths"].values()))
        self.assertIn("REQ-A.spec.ts", report["requirements"]["REQ-A"]["test_path"])
        # Generate and accept each of three requirements, then run final regression.
        # Baseline compilation is separate; API failures stop before the E2E run.
        self.assertEqual(checks.tests.call_count, 7 + int(needs_repair or partial_batch or early_green))
        self.assertEqual(checks.typecheck.call_count,
            8 + int(needs_repair or partial_batch or backend_fails_once or early_green))
        if backend_repair:
            self.assertEqual(backend_checks.call_count, 3 if backend_fails_once else 2)
            self.assertTrue(all(call.args == (self.output.resolve(),) for call in backend_checks.call_args_list))
            self.assertTrue(all(call.args == (backend_suites,) for call in api_checks.tests.call_args_list))
            self.assertEqual((self.output / "frontend/src/features/other-page-helper.ts").read_text(), "export const helper = 1;")
            self.assertEqual((self.output / "backend/src/features/other-api-helper.ts").read_text(), "export const helper = 2;")
        else:
            backend_checks.assert_called_once_with(self.output.resolve())
        self.assertEqual(report["summary"]["ui_tests"], 0)
        self.assertEqual(checks.tests.call_args.args[0],
            [report["requirements"][rid]["test_path"] for rid in self.tree.leaves])

    def test_single_trigger_context_supplies_frontend_data_and_derived_db_effects(self):
        from infrastructure.stages.frontend.triggers.scope import model_context
        context = scenario_context(self.ui, self.interactions, "REQ-A")
        context.update({"backend_apis": self.backend["apis"], "requirement": {"id": "REQ-A"},
            "obligations": {tid: trigger_obligations(tr, self.backend) for tid, tr in context["triggers"].items()},
            "target_trigger_ids": sorted(context["triggers"])})
        selected = model_context(context)
        self.assertEqual(set(selected["triggers"]), set(context["triggers"]))
        self.assertEqual(selected["obligations"]["TR-SAVE"], {"read": [], "write": ["Note.id", "Note.text"]})
        self.assertEqual(set(selected["backend_apis"]), {"API-SAVE", "API-COUNT"})
        self.assertIn("UI-TEXT", selected["page_subtrees"]["editor"]["elements"])
        self.assertNotIn("events", selected)
        self.assertNotIn("contracts", selected)


    def test_repair_scope_keeps_handlers_and_direct_imports_then_reads_failure_dependencies(self):
        """Do not omit required handlers/helpers, including a missing indexed file."""
        from infrastructure.shared.project.entrypoints import remember_path
        from infrastructure.shared.agent.context import SourceFiles
        paths = {"TR-INPUT": "frontend/src/events/input.ts", "TR-SAVE": "frontend/src/events/save.ts",
            "TR-COUNT": "frontend/src/events/deleted.ts"}
        for eid, relative in paths.items():
            remember_path(self.output, "trigger", eid, relative)
        save_text(self.output / paths["TR-INPUT"], 'import { value } from "../shared/value.js";')
        save_text(self.output / paths["TR-SAVE"], "export function handle() {}")
        save_text(self.output / "frontend/src/shared/value.ts", 'export { value } from "./nested";')
        save_text(self.output / "frontend/src/shared/nested/index.ts", 'import "../value.js"; export const value = 1;')
        save_text(self.output / "frontend/src/unrelated.ts", "unrelated")
        selected = SourceFiles(self.output, self.ui)
        workspace = selected.workspace(triggers=set(paths), references=["frontend/src/ui-runtime.ts"])
        files = workspace.state()
        for relative in [*paths.values(), "frontend/src/shared/value.ts"]:
            self.assertTrue(files[relative]["writable"], relative)
        self.assertNotIn("frontend/src/shared/nested/index.ts", files)
        selected.feedback(workspace, {"error": "frontend/src/shared/nested/index.ts: invalid value"})
        self.assertIn("frontend/src/shared/nested/index.ts", workspace.state())
        selected.feedback(workspace, {"error": "another failure"})
        self.assertNotIn("frontend/src/shared/nested/index.ts", workspace.state())
        self.assertIsNone(files[paths["TR-COUNT"]]["content"])
        self.assertNotIn("frontend/src/unrelated.ts", files)
        self.assertFalse(files["frontend/src/ui-runtime.ts"]["writable"])

    def test_feedback_exposes_generated_tests_for_requirement_based_repairs(self):
        """A locator/compiler error exposes repairable sources and generated test suites."""
        from infrastructure.shared.project.entrypoints import remember_path
        from infrastructure.shared.agent.context import SourceFiles
        page, helper = "frontend/src/screens/summary.tsx", "frontend/src/shared/count.ts"
        remember_path(self.output, "page", "summary", page)
        save_text(self.output / page, 'import { count } from "../shared/count.js";')
        save_text(self.output / helper, "export const count = 0;")
        frozen = "frontend/tests/e2e/accepted.spec.ts"
        save_text(self.output / frozen, "accepted assertions")
        selected = SourceFiles(self.output, self.ui)
        generation = selected.workspace(target=("e2e_test", "REQ-A"))
        generation.state()
        generation.execute(propose_files(self.output, [{"path": "frontend/tests/e2e/new.spec.ts", "content": "new assertions"}])["tool_calls"])
        selected.feedback(generation, {"error": "generation error"})
        workspace = selected.workspace()
        result = selected.feedback(workspace, {"ui": {"returncode": 1,
            "failures": [f"UI-COUNT is not visible: {frozen}; frontend/tests/e2e/new.spec.ts"], "output": "duplicate reporter body"},
            "typecheck": {"returncode": 0, "output": "successful compiler output"}})
        files = workspace.state()
        self.assertTrue(files[page]["writable"])
        self.assertTrue(files[helper]["writable"])
        self.assertTrue(files[frozen]["writable"])
        self.assertTrue(files["frontend/tests/e2e/new.spec.ts"]["writable"])
        self.assertNotIn("output", result["ui"])
        self.assertEqual(result["typecheck"], {"returncode": 0})

    def test_agent_retry_receives_sources_identified_by_failed_checks(self):
        """A file omitted initially becomes writable before the next model repair request."""
        from infrastructure.shared.agent.loop import run_file_agent
        from infrastructure.shared.agent.context import SourceFiles
        page = "frontend/src/screens/broken.tsx"
        helper = "frontend/src/shared/value.ts"
        save_text(self.output / page, 'import { value } from "../shared/value.js";')
        save_text(self.output / helper, "export const value = false;")
        selection = SourceFiles(self.output)
        workspace = selection.workspace()
        calls = []
        def generate(instructions, payload):
            calls.append(payload)
            if len(calls) == 1:
                self.assertNotIn(page, payload["files"])
                return propose_files(self.output, [])
            self.assertTrue(payload["files"][page]["writable"])
            self.assertTrue(payload["files"][helper]["writable"])
            return propose_files(self.output, [{"path": helper, "content": "export const value = true;"}])
        gate = Mock(side_effect=[ValueError(f"{page}(1,1): Type error"), {"green": True}])
        result = run_file_agent(Mock(generate_json=generate), "Repair", {}, workspace,
            gate, 2, Mock(), "Repair", feedback_filter=lambda value: selection.feedback(workspace, value))
        self.assertEqual(result["checks"], {"green": True})

    def test_runtime_types_survive_returning_to_presentation_stage(self):
        from infrastructure.shared.project.initialize import initialize_project
        from infrastructure.stages.frontend.ui.runtime import prepare_frontend
        from infrastructure.utils.files import read_source
        initialize_project(self.output)
        prepare_frontend(self.output)
        before = read_source(self.output / "frontend/src/ui-runtime.ts")
        prepare_frontend(self.output)
        after = read_source(self.output / "frontend/src/ui-runtime.ts")
        self.assertEqual(before, after)
        self.assertIn("isPresentationTest", after)

    def test_synthesis_retries_without_publishing_rejected_state(self):
        from infrastructure.shared.agent.tasks import StageIncomplete
        requests = []
        def generate(instructions, payload):
            requests.append(deepcopy(payload))
            rid = payload["requirement"]["id"]
            page = "summary" if rid == "REQ-C" else "editor"
            context = page_context(self.ui, page)
            proposal = {"pages": {page: self.ui["pages"][page]} if rid != "REQ-B" else {},
                "elements": context["elements"] if rid != "REQ-B" else {},
                "requirement_ui": self.ui["requirements"][rid], "step_ui": {sid: self.ui["steps"][sid] for sid in self.tree.own_steps[rid]}}
            return proposal
        contexts = {rid: {**record, "steps": [self.tree.steps[sid] for sid in self.tree.own_steps[rid]], "images": {}} for rid, record in self.tree.requirements.items()}
        result = synthesize_ui(self.arc, self.tree, {}, contexts, Mock(generate_json=generate), Mock())
        self.assertEqual(len(requests), 3)
        self.assertNotIn("step_obligations", requests[0])
        self.assertTrue(all("all_pages" not in request for request in requests))
        self.assertEqual(requests[1]["retrieved_pages"]["editor"], page_context(self.ui, "editor"))

    def test_incomplete_ui_response_gets_all_missing_sections_in_one_repair(self):
        requests = []
        def generate(instructions, payload):
            requests.append(deepcopy(payload))
            rid = payload["requirement"]["id"]
            pid = "summary" if rid == "REQ-C" else "editor"
            context = page_context(self.ui, pid)
            if len(requests) == 1:
                return {"pages": {pid: self.ui["pages"][pid]}}
            if len(requests) == 2:
                details = payload["correction"]["details"]
                self.assertIn("Cannot read UI/data response", payload["correction"]["error"])
                self.assertIn("complete replacement JSON object", instructions)
                self.assertEqual(set(details["missing_sections"]),
                    {"elements", "requirement_ui", "step_ui"})
                self.assertEqual(set(details["response_template"]),
                    {"pages", "elements", "requirement_ui", "step_ui"})
                self.assertEqual(payload["retrieved_pages"], {})
                self.assertEqual(json.loads((self.arc / "ui_design.json").read_text())["pages"], {})
            return {"pages": {pid: self.ui["pages"][pid]} if rid != "REQ-B" else {},
                "elements": context["elements"] if rid != "REQ-B" else {},

                "requirement_ui": self.ui["requirements"][rid],
                "step_ui": {sid: self.ui["steps"][sid] for sid in self.tree.own_steps[rid]}}
        contexts = {rid: {**record, "images": {},
            "steps": [self.tree.steps[sid] for sid in self.tree.own_steps[rid]]}
            for rid, record in self.tree.requirements.items()}
        result = synthesize_ui(self.arc, self.tree, {}, contexts, Mock(generate_json=generate), Mock())
        self.assertIn("editor", result["pages"])
        self.assertEqual(len(requests), 4)

    def test_complete_ui_prompt_example_produces_page_context_and_display_tests(self):
        from infrastructure.stages.frontend.ui.proposals import EXAMPLE
        from infrastructure.stages.frontend.ui.testing import existence_suite
        design = apply_proposal(empty_design(), EXAMPLE, "REQ-A", self.tree)
        context = page_context(design, "home")
        self.assertEqual(set(context["elements"]), set(EXAMPLE["elements"]))
        self.assertEqual(set(context["elements"]), set(EXAMPLE["elements"]))
        suite = existence_suite(design, "home")
        self.assertIn("UI-search", suite)
        self.assertIn("search.query", suite)

    def test_presentation_loader_has_no_report_policy(self):
        save_json(self.arc / "requirements.json", self.raw)
        save_json(self.arc / "visual_analysis.json", {})
        tree, images, contexts = load_ui_context(self.arc, self.output)
        self.assertEqual(tree.leaves, self.tree.leaves)
        self.assertEqual(images, {})
        self.assertNotIn("concatenated_steps", contexts["REQ-A"])

    def test_trigger_context_does_not_load_requirement_obligations_or_check_coverage(self):
        from infrastructure.stages.frontend.triggers.context import load_e2e_context
        for name, value in (("requirements", self.raw), ("visual_analysis", {}),
                ("db_schema", {"entities": {}}), ("backend_apis", self.backend),
                ("ui_design", self.ui), ("frontend_triggers", empty_triggers(self.tree))):
            save_json(self.arc / (name + ".json"), value)
        self.assertFalse((self.arc / "db_obligations.json").exists())
        _, _, backend, _, triggers, _, _ = load_e2e_context(self.arc, self.output)
        self.assertEqual(backend, self.backend)
        self.assertEqual(triggers["triggers"], {})

    def test_trigger_db_effects_are_derived_without_frontend_coverage_validation(self):
        from infrastructure.stages.frontend.triggers.design import apply_decomposition
        trigger = deepcopy(self.interactions["triggers"]["TR-SAVE"])
        trigger["action"] = [{"kind": "api", "api": "API-COUNT", "description": "Read count"}]
        result = apply_decomposition(empty_triggers(self.tree),
            {"triggers": {"T": trigger}, "requirement_triggers": ["T"], "step_triggers": {}}, "REQ-A")
        self.assertEqual(trigger_obligations(result["triggers"]["T"], self.backend),
            {"read": ["Note.id", "Note.text"], "write": []})
        self.assertEqual(result["triggers"]["T"]["read"], ["form.text"])

    def test_trigger_synthesis_retries_json_and_checks_declared_obligations(self):
        from infrastructure.shared.agent.tasks import StageIncomplete
        from infrastructure.stages.frontend.triggers.synthesis import synthesize_triggers
        calls = []
        self.obligations["steps"] = {sid: self.obligations["requirements"][rid]
            for rid in self.tree.leaves for sid in self.tree.own_steps[rid]}
        save_json(self.arc / "db_obligations.json", self.obligations)

        def generate(instructions, payload):
            calls.append(deepcopy(payload))
            if len(calls) == 1:
                raise json.JSONDecodeError("Invalid JSON", "{broken", 1)
            rid = payload["requirement"]["id"]
            ids = self.interactions["requirements"][rid]
            if len(calls) == 2:
                return {"triggers": {"TR:save": self.interactions["triggers"][ids[0]]},
                    "requirement_triggers": ["TR:save"], "step_triggers": {
                        sid: ["TR:save"] for sid in self.tree.own_steps[rid]}}
            if len(calls) == 3:
                self.assertIn("Invalid trigger ID 'TR:save'", payload["correction"]["error"])
                self.assertFalse(json.loads((self.arc / "frontend_triggers.json").read_text())["triggers"])
            return {"triggers": {tid: self.interactions["triggers"][tid] for tid in ids},
                "requirement_triggers": ids, "step_triggers": {sid: ids for sid in self.tree.own_steps[rid]}}

        synthesize_triggers(self.arc, self.ui, self.tree, self.backend,
            self.tree.requirements, Mock(generate_json=generate), Mock())
        self.assertEqual(len(calls), 5)
        self.assertIn("correction", calls[1])
        self.assertTrue(all("required_obligations" in call for call in calls))
        self.assertTrue((self.arc / "frontend_triggers.json").is_file())

    def test_trigger_repair_locates_the_changed_trigger_of_an_accepted_requirement(self):
        from infrastructure.stages.frontend.triggers.synthesis import synthesize_triggers
        self.obligations['steps'] = {sid: self.obligations['requirements'][rid]
            for rid in self.tree.leaves for sid in self.tree.own_steps[rid]}
        save_json(self.arc / 'db_obligations.json', self.obligations)
        retries = []
        def generate(_, payload):
            rid = payload['requirement']['id']
            ids = self.interactions['requirements'][rid]
            definitions = {tid: deepcopy(self.interactions['triggers'][tid]) for tid in ids}
            if rid == 'REQ-B':
                if 'correction' not in payload:
                    definitions['TR-SAVE']['action'] = [{'kind': 'api', 'api': 'API-COUNT'}]
                else:
                    retries.append(payload['correction']['details']['accepted_regression'])
            return {'triggers': definitions, 'requirement_triggers': ids,
                'step_triggers': {sid: ids for sid in self.tree.own_steps[rid]}}
        result = synthesize_triggers(self.arc, self.ui, self.tree, self.backend, self.tree.requirements,
            Mock(generate_json=generate), Mock(), max_attempts=2)
        self.assertEqual(len(retries), 1)
        self.assertEqual(retries[0]['affected_requirement'], 'REQ-A')
        self.assertEqual(retries[0]['changed_effects']['TR-SAVE']['removed']['write'], ['Note.id', 'Note.text'])
        self.assertEqual(result['triggers']['TR-SAVE'], self.interactions['triggers']['TR-SAVE'])


    def test_navigation_context_includes_later_page_mount_and_keeps_ownership(self):
        context = scenario_context(self.ui, self.interactions, "REQ-A")
        self.assertEqual(set(context["page_subtrees"]), {"editor", "summary"})
        self.assertEqual(set(context["triggers"]), {"TR-INPUT", "TR-SAVE", "TR-COUNT"})
        self.assertEqual(self.interactions["requirements"]["REQ-A"], ["TR-INPUT", "TR-SAVE"])

    def test_shared_trigger_change_must_name_prior_consumers_even_if_effects_match(self):
        from infrastructure.stages.frontend.triggers.design import apply_decomposition, validate_shared_changes
        current = apply_decomposition(empty_triggers(self.tree),
            {"triggers": {"TR-SAVE": self.interactions["triggers"]["TR-SAVE"]},
                "requirement_triggers": ["TR-SAVE"], "step_triggers": {}}, "REQ-A")
        reuse = {"triggers": {}, "requirement_triggers": ["TR-SAVE"], "step_triggers": {}}
        validate_shared_changes(current, reuse, "REQ-B", ["REQ-A"])
        changed = deepcopy(current["triggers"]["TR-SAVE"])
        changed["action"][0]["description"] = "Updated implementation guidance"
        proposal = {**reuse, "triggers": {"TR-SAVE": changed}}
        with self.assertRaisesRegex(ValueError, "shared_changes"):
            validate_shared_changes(current, proposal, "REQ-B", ["REQ-A"])
        proposal["shared_changes"] = {"TR-SAVE": ["REQ-A"]}
        validate_shared_changes(current, proposal, "REQ-B", ["REQ-A"])
        updated = apply_decomposition(current, proposal, "REQ-B")
        self.assertEqual(updated["owners"]["TR-SAVE"], "REQ-A")
        self.assertEqual(updated["requirements"]["REQ-B"], ["TR-SAVE"])

    def test_original_dependency_supplies_the_page_without_assigning_its_triggers(self):
        from importlib import import_module
        from infrastructure.stages.frontend.triggers.synthesis import synthesize_triggers
        self.raw["children"][1]["dependencies"] = ["REQ-C"]
        save_json(self.output / "requirements.yaml", self.raw)
        dependencies = import_module("11_design_frontend_triggers").original_dependencies(self.output)
        self.obligations["steps"] = {sid: self.obligations["requirements"][rid]
            for rid in self.tree.leaves for sid in self.tree.own_steps[rid]}
        save_json(self.arc / "db_obligations.json", self.obligations)
        seen = {}

        def generate(_, payload):
            rid = payload["requirement"]["id"]
            seen[rid] = set(payload["ui"]["pages"])
            ids = self.interactions["requirements"][rid]
            return {"triggers": {tid: self.interactions["triggers"][tid] for tid in ids},
                "requirement_triggers": ids, "step_triggers": {sid: ids for sid in self.tree.own_steps[rid]}}

        result = synthesize_triggers(self.arc, self.ui, self.tree, self.backend,
            self.tree.requirements, Mock(generate_json=generate), Mock(), dependencies=dependencies)
        self.assertEqual(seen["REQ-B"], {"editor", "summary"})
        self.assertNotIn("TR-COUNT", result["requirements"]["REQ-B"])

    def test_compatible_new_api_branch_does_not_overwrite_the_prior_trigger(self):
        from infrastructure.stages.frontend.triggers.synthesis import synthesize_triggers
        self.backend["requirements"]["REQ-B"] = ["API-SAVE-TAGGED"]
        self.backend["apis"]["API-SAVE-TAGGED"] = {"read": [], "write": ["Note.id", "Note.text", "Note.tag"]}
        self.obligations["requirements"]["REQ-B"] = {"read": [], "write": ["Note.id", "Note.text", "Note.tag"]}
        self.obligations["steps"] = {sid: self.obligations["requirements"][rid]
            for rid in self.tree.leaves for sid in self.tree.own_steps[rid]}
        save_json(self.arc / "db_obligations.json", self.obligations)
        calls = []

        def generate(_, payload):
            rid = payload["requirement"]["id"]
            calls.append(rid)
            ids = self.interactions["requirements"][rid]
            definitions = {tid: deepcopy(self.interactions["triggers"][tid]) for tid in ids}
            if rid == "REQ-B":
                definitions["TR-SAVE"]["action"][0]["api"] = "API-SAVE-TAGGED"
                self.assertIn("API-SAVE-TAGGED", payload["unique_api_evidence"])
            return {"triggers": definitions, "requirement_triggers": ids,
                "step_triggers": {sid: ids for sid in self.tree.own_steps[rid]}}

        result = synthesize_triggers(self.arc, self.ui, self.tree, self.backend,
            self.tree.requirements, Mock(generate_json=generate), Mock())
        self.assertEqual(calls, self.tree.leaves)
        self.assertEqual(result["triggers"]["TR-SAVE"]["action"][0]["api"], "API-SAVE")
        new_id = next(tid for tid in result["requirements"]["REQ-B"] if tid.startswith("TR-SAVE-"))
        self.assertEqual(result["owners"][new_id], "REQ-B")
        self.assertEqual(result["triggers"][new_id]["action"][0]["api"], "API-SAVE-TAGGED")

    def test_destination_page_does_not_pull_its_other_business_triggers_into_requirement(self):
        triggers = deepcopy(self.interactions)
        triggers["triggers"]["TR-FUTURE"] = {"source": "UI-BACK", "event": "click", "read": [], "write": [], "action": []}
        triggers["requirements"]["REQ-C"].append("TR-FUTURE")
        context = scenario_context(self.ui, triggers, "REQ-A")
        self.assertIn("TR-COUNT", context["triggers"])
        self.assertNotIn("TR-FUTURE", context["triggers"])

    def test_backend_only_requirements_need_no_frontend_triggers(self):
        ui = normalize_design(empty_design(), self.tree)
        triggers = empty_triggers(self.tree)
        self.assertEqual(scenario_context(ui, triggers, "REQ-A")["triggers"], {})


    def test_frontend_runtime_failure_is_repair_not_test_regeneration(self):
        report = {"returncode": 1, "failed": 1, "errors": [], "failures": ["E2E_RUNTIME_ERROR: Broken handler"]}
        self.assertEqual(test_run_status(report), "REPAIR_REQUIRED")
        page_report = {**report, "failures": ["UI_RUNTIME_ERROR: Broken page"]}
        self.assertEqual(presentation_run_status(page_report), "REPAIR_REQUIRED")
        report["failures"] = ["E2E_BACKEND_ERROR: undeclared response"]
        self.assertEqual(test_run_status(report), "REPAIR_REQUIRED")


if __name__ == "__main__":
    unittest.main()
