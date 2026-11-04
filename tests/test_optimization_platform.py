"""Cross-cutting optimization contracts: evidence, provenance, telemetry and reuse."""

from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from infrastructure.compiler import Compiler
from infrastructure.shared.agent.context_broker import (
    ContextBroker, TaskCard, compact_json, expand_context, intern_context, merge_excerpts)
from infrastructure.shared.agent.discovery import ContextIndex
from infrastructure.shared.agent.experiments import (
    OptimizationOptions, ResponseCache, consume, deterministic_proposal, optimization_scope, task_budget)
from infrastructure.shared.agent.failures import FailureClassifier
from infrastructure.shared.agent.loop import run_file_agent, AgentExhausted
from infrastructure.shared.agent.proposals import propose_json
from infrastructure.shared.agent.tools import FileTools
from infrastructure.shared.artifacts.dependency_graph import DependencyGraph
from infrastructure.shared.project.resume import ResumeManager, snapshot
from infrastructure.shared.usage.recorder import UsageRecorder, usage_scope
from infrastructure.utils.files import read_artifact, save_json


class ContextContracts(unittest.TestCase):
    def test_interning_round_trip_preserves_whitespace_permissions_and_error_values(self):
        record = {"description": "需求原文\r\n" * 40, "writable": False, "version": "v1"}
        data = {"a/b~c": deepcopy(record), "task": {"api": deepcopy(record)},
            "feedback": {"expected": [1, 2], "actual": [3]}, "source": " \tcode\r\n" * 60}
        data["another_source"] = data["source"]
        frozen = deepcopy(data)
        packed = intern_context(data)
        self.assertEqual(expand_context(packed), data)
        self.assertEqual(data, frozen)
        self.assertLess(len(compact_json(packed)), len(compact_json(data)))
        self.assertEqual(packed["task"]["api"], {"$context_ref": "/a~1b~0c"})

    def test_user_marker_collision_disables_interning(self):
        data = {"literal": {"$context_ref": "user data"}, "code": "x" * 200}
        self.assertEqual(intern_context(data), data)

    def test_overlapping_excerpts_preserve_crlf_and_refuse_conflicting_versions(self):
        lines = [f"  line {i}\r\n" for i in range(1, 51)]
        def excerpt(first, last, version="v1"):
            return {"path": "a.ts", "start_line": first, "end_line": last,
                "total_lines": 50, "version": version, "writable": False,
                "content": "".join(lines[first-1:last])}
        merged = merge_excerpts([excerpt(10, 30), excerpt(20, 40)])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["content"], "".join(lines[9:40]))
        self.assertFalse(merged[0]["writable"])
        self.assertEqual(len(merge_excerpts([excerpt(10, 30), excerpt(20, 40, "v2")])), 2)

    def test_broker_keeps_original_task_projection_and_authorized_kinds(self):
        broker = ContextBroker(ContextIndex({"api": {"A": {"description": "save"}}}))
        context = {"requirement": {"id": "R", "description": "original"}, "files": {"a": {"writable": False}}}
        self.assertEqual(broker.build_task_context(TaskCard(), context), context)
        with self.assertRaises(KeyError):
            broker.resolve_query({"kind": "file", "ids": ["secret"]})
        self.assertEqual(broker.resolve_query({"kind": "api", "ids": ["A"]})["records"]["A"]["description"], "save")


class TelemetryContracts(unittest.TestCase):
    def test_mock_queries_reads_and_edits_count_every_call(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.ts").write_text("before\n")
            model = Mock(generate_json=Mock(side_effect=[
                {"context_request": {"kind": "api", "ids": ["A"]}},
                {"tool_calls": [{"tool": "read_file", "path": "a.ts"}]},
                {"tool_calls": [{"tool": "edit_file", "path": "a.ts", "operation": "replace",
                    "old_content": "before", "new_content": "after"}]}]))
            recorder = UsageRecorder(root)
            with recorder.activate(), usage_scope(stage="08"):
                run_file_agent(model, "edit", {}, FileTools(root, ["a.ts"]), lambda: {"status": "GREEN"},
                    2, Mock(), "task", context_index=ContextIndex({"api": {"A": {"description": "save"}}}))
                recorder.finish("GREEN")
            record = json.loads(recorder.path.read_text())
            self.assertEqual(record["totals"]["calls"], 3)
            self.assertIsNone(record["totals"]["input_tokens"])
            self.assertEqual(record["tasks"]["08"]["task"]["context_query"], 1)
            self.assertEqual(record["tasks"]["08"]["task"]["read_file"], 1)
            self.assertEqual((root / "a.ts").read_text(), "after\n")

    def test_interruption_persists_unknown_usage_and_actual_zeroes(self):
        with TemporaryDirectory() as directory:
            recorder = UsageRecorder(directory)
            with self.assertRaises(KeyboardInterrupt), recorder.activate():
                recorder.call(model="test", duration_seconds=1, status="OK",
                    usage={"prompt_tokens": 0, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 0},
                        "completion_tokens_details": {"reasoning_tokens": 1}})
                raise KeyboardInterrupt()
            data = json.loads(recorder.path.read_text())
            self.assertEqual(data["status"], "INTERRUPTED")
            self.assertEqual(data["totals"]["total_tokens"], 2)
            self.assertEqual(data["totals"]["cached_input_tokens"], 0)
            self.assertEqual(data["totals"]["reasoning_tokens"], 1)

    def test_recording_failure_does_not_mask_original_error(self):
        with TemporaryDirectory() as directory:
            recorder = UsageRecorder(directory)
            with patch("infrastructure.shared.usage.recorder.save_json", side_effect=OSError("disk")):
                with self.assertRaisesRegex(ValueError, "business"), recorder.activate():
                    recorder.call(model="x", duration_seconds=0, status="FAILED")
                    raise ValueError("business")
            self.assertEqual(recorder.write_error, "disk")

    def test_provider_usage_is_recorded_once_even_through_agent_wrapper(self):
        from infrastructure.shared.agent.execution import call_model
        from infrastructure.utils.presentation.model_io import invoke
        with TemporaryDirectory() as directory, redirect_stdout(StringIO()):
            response = SimpleNamespace(model_dump=lambda **kw: {"usage": {"prompt_tokens": 7, "completion_tokens": 3}},
                choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))])
            client = Mock()
            client.chat.completions.create.return_value = response
            class Model:
                def generate_json(self, instructions, payload):
                    invoke(client, {"model": "m", "messages": []})
                    return {}
            recorder = UsageRecorder(directory)
            with recorder.activate():
                call_model(Model(), "", {})
            self.assertEqual(recorder.data["totals"]["calls"], 1)
            self.assertEqual(recorder.data["totals"]["total_tokens"], 10)


class GraphContracts(unittest.TestCase):
    def test_new_page_paths_do_not_invalidate_a_completed_backend(self):
        from infrastructure.shared.agent.discovery import project_index
        with TemporaryDirectory() as directory:
            root = Path(directory)
            graph = DependencyGraph(root)
            index = root / ".arc/implementation_paths.json"
            backend = "08_implement_backend_tdd"
            ui = "09_design_ui"
            with graph.activate():
                with graph.stage(backend):
                    save_json(index, {"backend_test": {"A": "backend/tests/a.test.ts"}})
                    project_index(root, kinds=("test_requirement",), file_roots=("backend/tests",)).records()
                with graph.stage(ui):
                    pass
                with graph.stage("10_implement_ui"):
                    save_json(index, {"backend_test": {"A": "backend/tests/a.test.ts"},
                        "page": {"P": "frontend/src/Page.tsx"}})
                self.assertTrue(graph.data["nodes"]["stage:" + backend]["valid"])
                self.assertTrue(graph.data["nodes"]["stage:" + ui]["valid"])
                with graph.stage("12_implement_frontend_tdd"):
                    save_json(index, {"backend_test": {"A": "backend/tests/b.test.ts"},
                        "page": {"P": "frontend/src/Page.tsx"}})
                self.assertFalse(graph.data["nodes"]["stage:" + backend]["valid"])
                self.assertFalse(graph.data["nodes"]["stage:" + ui]["valid"])

    def test_shared_api_page_and_schema_impact_preserves_unrelated_requirements(self):
        with TemporaryDirectory() as directory:
            graph = DependencyGraph(directory)
            arc = Path(directory) / ".arc"
            with graph.activate():
                save_json(arc / "db_schema.json", {"entities": {"User": {"fields": {"id": {"type": "integer"}}}},
                    "requirements": {"R1": ["User.id"]}})
                save_json(arc / "backend_apis.json", {"apis": {"A": {}, "B": {}},
                    "requirements": {"R1": ["A"], "R2": ["A"], "R3": ["B"]}})
                save_json(arc / "ui_design.json", {"pages": {"P": {"root": "root"}}, "elements": {
                    "root": {}, "button": {"parent": "root"}}})
                save_json(arc / "frontend_triggers.json", {"triggers": {"T": {"source": "button",
                    "action": [{"kind": "api", "api": "A"}]}}, "requirements": {"R1": ["T"], "R2": ["T"]}})
            affected = graph.select_rerun_tasks(["api:A"])
            self.assertIn("task:backend:R2", affected)
            self.assertIn("task:frontend:R1", affected)
            self.assertNotIn("task:backend:R3", affected)
            self.assertIn("trigger:T", graph.affected_by("page:P"))
            self.assertIn("api:A", graph.affected_by("schema:User.id"))
            self.assertIn("test:e2e:R1", graph.affected_by("api:A"))
            self.assertFalse(graph.data["pending"])

    def test_queries_include_misses_and_collection_versions(self):
        with TemporaryDirectory() as directory:
            graph = DependencyGraph(directory)
            broker = ContextBroker(ContextIndex({"api": {}}))
            with graph.activate(), graph.stage("design"):
                broker.resolve_query({"kind": "api", "query": "future"})
            first = graph.data["nodes"]["collection:api"]["version"]
            broker.index.collections["api"]["NEW"] = {"description": "future"}
            with graph.activate(), graph.stage("design"):
                broker.resolve_query({"kind": "api", "query": "future"})
            self.assertNotEqual(first, graph.data["nodes"]["collection:api"]["version"])
            self.assertIn("stage:design", graph.downstream("collection:api"))

    def test_interrupted_artifact_publication_stays_pending_and_forces_conservative_rerun(self):
        with TemporaryDirectory() as directory:
            graph = DependencyGraph(directory)
            original = graph._semantic_edges
            graph._semantic_edges = Mock(side_effect=OSError("crash after content"))
            with self.assertRaises(OSError):
                graph.publish(Path(directory) / ".arc/db_schema.json", {"entities": {}})
            reloaded = DependencyGraph(directory)
            self.assertIn("artifact:db_schema.json", reloaded.data["pending"])
            reloaded.register("stage:02")
            self.assertEqual(reloaded.select_rerun_tasks(["schema:unknown"]), ["stage:02"])
            graph._semantic_edges = original


class FailureContracts(unittest.TestCase):
    def test_categories_and_startup_ambiguity(self):
        classifier = FailureClassifier()
        cases = [(json.JSONDecodeError("bad", "{", 0), "ProtocolError"),
            (PermissionError("denied"), "EnvironmentError"),
            (ValueError("src/app.ts(42,3): error TS2304: cannot find name"), "CompileError"),
            (ValueError("obligation coverage missing"), "ContractError"),
            (ValueError("service did not start: SQLITE_ERROR"), "CompileError"),
            (ValueError("service did not start; timeout"), "Unknown"),
            (ValueError("Cannot find module './generated.js'"), "CompileError")]
        for error, expected in cases:
            self.assertEqual(classifier.classify(error).failure_type, expected)
        failure = classifier.classify(cases[2][0])
        self.assertEqual((failure.file, failure.line), ("src/app.ts", 42))
        self.assertEqual(classifier.classify(ValueError("501 not implemented"), expected_red=True).failure_type, "ExpectedRed")
        self.assertNotEqual(classifier.classify(ValueError("501 not implemented")).failure_type, "ExpectedRed")

    def test_invalid_queries_are_bounded_without_disabling_valid_recovery(self):
        model = Mock(generate_json=Mock(return_value={"context_request": {"kind": "wrong", "query": "x"}}))
        with self.assertRaises(RuntimeError):
            propose_json(model, "", {}, lambda v: v, Mock(), "task", 2, ContextIndex({"api": {}}))
        self.assertEqual(model.generate_json.call_count, 4)
        model.generate_json.side_effect = [{"context_request": {"kind": "wrong", "query": "x"}},
            {"context_request": {"kind": "api", "ids": ["A"]}}, {"done": True}]
        self.assertEqual(propose_json(model, "", {}, lambda v: v, Mock(), "task", 1,
            ContextIndex({"api": {"A": {}}}))[0], {"done": True})


class ExperimentalContracts(unittest.TestCase):
    def model(self, response):
        from infrastructure.shared.models.text import TextModel
        model = TextModel.__new__(TextModel)
        model.name, model.reasoning, model.max_output_tokens = "large", "low", 1000
        model._token_parameter, model._reasoning_supported = "max_completion_tokens", True
        model.client = SimpleNamespace(base_url="https://example.test")
        model._request = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
            message=SimpleNamespace(content=json.dumps(response), refusal=None))]))
        return model

    def test_default_transport_keeps_literal_evidence_and_original_output_protocol(self):
        from infrastructure.shared.models.text import JSON_OUTPUT_RULE
        model = self.model({"ok": True})
        record = {"description": "original requirement evidence " * 40}
        payload = {"first": record, "second": deepcopy(record)}
        model.generate_json("design", payload)
        messages = model._request.call_args.kwargs["messages"]
        self.assertFalse(OptimizationOptions().context_dedup)
        self.assertEqual(messages[0]["content"], "design\n" + JSON_OUTPUT_RULE)
        self.assertEqual(json.loads(messages[1]["content"]), payload)

    def test_reference_protocol_is_only_sent_when_opt_in_actually_creates_references(self):
        from infrastructure.shared.agent.context_broker import REFERENCE_RULES
        model = self.model({"ok": True})
        record = {"description": "original requirement evidence " * 40}
        payload = {"first": record, "second": deepcopy(record)}
        with optimization_scope(OptimizationOptions(context_dedup=True)):
            model.generate_json("design", {"single": record})
            self.assertNotIn(REFERENCE_RULES, model._request.call_args.kwargs["messages"][0]["content"])
            model.generate_json("design", payload)
        messages = model._request.call_args.kwargs["messages"]
        self.assertIn(REFERENCE_RULES, messages[0]["content"])
        packed = json.loads(messages[1]["content"])
        self.assertEqual(packed["second"], {"$context_ref": "/first"})
        self.assertEqual(expand_context(packed), payload)

    def test_model_transport_uses_compact_reversible_payload_and_exact_task_routing(self):
        model = self.model({"ok": True})
        record = {"description": "原文\r\n" * 40, "writable": False}
        payload = {"current": record, "old": deepcopy(record)}
        with optimization_scope(OptimizationOptions(small_model="small", small_model_tasks=("simple",))), usage_scope(task="simple"):
            model.generate_json("design", payload)
        arguments = model._request.call_args.kwargs
        self.assertEqual(arguments["model"], "small")
        sent = arguments["messages"][1]["content"]
        self.assertEqual(expand_context(json.loads(sent)), payload)
        self.assertNotIn(": ", sent)
        with optimization_scope(OptimizationOptions(small_model="small", small_model_tasks=("simple",))), usage_scope(task="complex"):
            model.generate_json("design", payload)
        self.assertEqual(model._request.call_args.kwargs["model"], "large")

    def test_model_cache_publishes_only_after_acceptance_and_rejection_invalidates_it(self):
        with TemporaryDirectory() as directory, optimization_scope(OptimizationOptions(cache_dir=directory)):
            model = self.model({"answer": 1})
            model.generate_json("design", {})
            self.assertEqual(list(Path(directory).glob("*.json")), [])
            model.reject_response()
            model.generate_json("design", {})
            self.assertEqual(model._request.call_count, 2)
            model.accept_response()
            self.assertEqual(model.generate_json("design", {}), {"answer": 1})
            self.assertEqual(model._request.call_count, 2)
            model.reject_response()
            model.generate_json("design", {})
            self.assertEqual(model._request.call_count, 3)
            model.accept_response()
            model.generate_json("changed instructions", {})
            self.assertEqual(model._request.call_count, 4)

    def test_model_routing_keeps_provider_capabilities_separate(self):
        from openai import OpenAIError
        model = self.model({"ok": True})
        response = model._request.return_value
        del model._request
        seen = []
        def invoke(client, arguments):
            seen.append(arguments)
            if arguments["model"] == "small":
                if "max_completion_tokens" in arguments:
                    error = OpenAIError("unsupported max_completion_tokens")
                    error.status_code = 400
                    raise error
                if "reasoning_effort" in arguments:
                    error = OpenAIError("unsupported reasoning_effort")
                    error.status_code = 400
                    raise error
            return response
        with patch("infrastructure.shared.models.text.invoke", side_effect=invoke), redirect_stdout(StringIO()):
            model._request(model="small", messages=[], reasoning_effort="low")
            model._request(model="large", messages=[], reasoning_effort="low")
        self.assertEqual(len(seen), 4)
        self.assertIn("max_completion_tokens", seen[-1])
        self.assertIn("reasoning_effort", seen[-1])

    def test_model_cache_identity_includes_output_protocol(self):
        with TemporaryDirectory() as directory, optimization_scope(OptimizationOptions(cache_dir=directory)):
            model = self.model({"ok": True})
            model.generate_json("design", {})
            model.accept_response()
            with patch("infrastructure.shared.models.text.JSON_OUTPUT_RULE", "updated output protocol"):
                model.generate_json("design", {})
            self.assertEqual(model._request.call_count, 2)

    def test_user_reference_marker_is_transmitted_as_literal_data(self):
        model = self.model({"ok": True})
        payload = {"record": {"$context_ref": "literal"}}
        model.generate_json("design", payload)
        messages = model._request.call_args.kwargs["messages"]
        self.assertIn("encoding is disabled", messages[0]["content"])
        self.assertEqual(json.loads(messages[1]["content"]), payload)

    def test_budgets_default_off_and_explicit_limits_apply_across_rounds(self):
        with task_budget():
            consume("calls", 100)
        with optimization_scope(OptimizationOptions(max_task_calls=1, max_read_characters=4)), task_budget():
            consume("calls")
            with self.assertRaises(ValueError):
                consume("calls")
            with self.assertRaises(ValueError):
                consume("read_characters", 5)

    def test_response_cache_identity_isolated_and_corrupt_entries_miss(self):
        with TemporaryDirectory() as directory:
            cache = ResponseCache(directory)
            key = cache.key(model="one", payload={"a": 1})
            self.assertNotEqual(key, cache.key(model="two", payload={"a": 1}))
            cache.put(key, {"ok": True})
            self.assertEqual(cache.get(key), {"ok": True})
            for malformed in ("broken", "[]", "null"):
                (Path(directory) / (key + ".json")).write_text(malformed)
                self.assertIsNone(cache.get(key))

    def test_simple_explicit_schema_only_and_complex_constraints_fall_back(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "schema.json"
            field = {"description": "ID", "type": "integer", "nullable": False,
                "primary_key": True, "unique": True, "references": None, "constraints": []}
            proposal = {"entities": {"Item": {"description": "item", "fields": {"id": field}}}, "related_fields": ["Item.id"]}
            save_json(path, {"R": proposal})
            with optimization_scope(OptimizationOptions(deterministic_schema=str(path))):
                self.assertEqual(deterministic_proposal("02_build_db_schema", "R"), proposal)
                field["constraints"] = ["domain check"]
                save_json(path, {"R": proposal})
                self.assertIsNone(deterministic_proposal("02_build_db_schema", "R"))


class ResumeContracts(unittest.TestCase):
    PASSES = [
        ("00_project_initialization", "project_initialization", "package.json", {}),
        ("01_explore_requirement", "explore_requirement", ".arc/requirements.json", {"id": "ROOT"}),
        ("02_build_db_schema", "build_db_schema", ".arc/db_schema.json", {"entities": {}, "requirements": {}}),
        ("03_infer_db_obligations", "infer_db_obligations", ".arc/db_obligations.json", {"requirements": {}}),
        ("04_check_db_obligations", "check_db_obligations", ".arc/db_obligations.json", {"requirements": {}}),
        ("05_implement_db", "implement_db", "backend/src/db/generated.json", {}),
        ("06_design_backend_apis", "design_backend_apis", ".arc/backend_apis.json", {"apis": {}, "requirements": {}}),
        ("07_check_backend_apis", "check_backend_apis", ".arc/backend_apis.json", {"apis": {}, "requirements": {}}),
        ("08_implement_backend_tdd", "implement_backend_tdd", "backend/src/routes/handler.json", {}),
        ("09_design_ui", "design_frontend_ui", ".arc/ui_design.json", {"pages": {}, "elements": {}}),
        ("10_implement_ui", "implement_frontend_ui", "frontend/src/page.json", {}),
        ("11_design_frontend_triggers", "design_frontend_triggers", ".arc/frontend_triggers.json", {"triggers": {}, "requirements": {}}),
        ("12_implement_frontend_tdd", "implement_frontend_triggers", "frontend/src/main.json", {}),
    ]

    def pipeline(self, root, calls, **configuration):
        compiler = Compiler(root / "input", root / "output", **configuration)
        def load(module):
            _, function, relative, value = next(p for p in self.PASSES if p[0] == module)
            def run(*args, **kwargs):
                calls.append(module)
                save_json(compiler.output_dir / relative, value)
                return {"status": "GREEN", "stage": module}
            return SimpleNamespace(**{function: run})
        with patch("infrastructure.compiler.import_module", side_effect=load), redirect_stdout(StringIO()):
            compiler.compile()
        return compiler

    def test_full_and_resumed_pipeline_match_and_changed_file_reruns_dependent_suffix(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "input").mkdir()
            (root / "input/requirements.yaml").write_text("id: ROOT\n")
            first_calls, resumed_calls, changed_calls = [], [], []
            first = self.pipeline(root, first_calls)
            resumed = self.pipeline(root, resumed_calls, resume=True)
            self.assertEqual(len(first_calls), 13)
            self.assertEqual(resumed_calls, [])
            self.assertEqual(first.artifacts, resumed.artifacts)
            self.assertTrue(all(entry["reused"] for entry in resumed.report["stages"].values()))
            save_json(root / "output/backend/src/routes/handler.json", {"manual": "repair"})
            changed = self.pipeline(root, changed_calls, resume=True)
            self.assertEqual(changed_calls, [p[0] for p in self.PASSES[8:]])
            self.assertEqual(changed.report["status"], first.report["status"])
            self.assertEqual(changed.artifacts, first.artifacts)

    def test_input_configuration_missing_graph_and_unknown_node_force_full_rerun(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "input").mkdir()
            source = root / "input/requirements.yaml"
            source.write_text("id: ROOT\n")
            self.pipeline(root, [])
            source.write_text("id: CHANGED\n")
            calls = []
            self.pipeline(root, calls, resume=True)
            self.assertEqual(len(calls), 13)
            calls = []
            self.pipeline(root, calls, resume=True, port=33001)
            self.assertEqual(len(calls), 13)
            (root / "output/.arc/dependencies.json").unlink()
            calls = []
            self.pipeline(root, calls, resume=True, port=33001)
            self.assertEqual(len(calls), 13)
            calls = []
            self.pipeline(root, calls, resume=True, port=33001, changed_nodes=["api:unknown"])
            self.assertEqual(len(calls), 13)

    def test_preview_does_not_execute_any_pass(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            compiler = self.pipeline(root, [], resume_preview=True)
            self.assertTrue((compiler.arc_dir / "resume-plan.json").is_file())
            self.assertFalse((compiler.output_dir / "package.json").exists())

    def test_manual_api_design_is_preserved_and_only_consumers_are_scheduled(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.pipeline(root, [])
            path = root / "output/.arc/backend_apis.json"
            manual = {"apis": {"manual": {"description": "reviewed edit"}}, "requirements": {}}
            save_json(path, manual)
            manager = self.pipeline(root, [], resume_preview=True).resume_manager
            self.assertFalse(manager.should_run("06_design_backend_apis"))
            self.assertTrue(manager.should_run("07_check_backend_apis"))
            self.assertEqual(manager.result("06_design_backend_apis"), manual)
            self.assertEqual(json.loads(path.read_text()), manual)

    def test_explicit_dependency_invalidation_schedules_consumers_without_file_changes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.pipeline(root, [])
            graph = DependencyGraph(root / "output")
            graph.depend("stage:08_implement_backend_tdd", "api:shared")
            graph.invalidate("api:shared")
            manager = self.pipeline(root, [], resume_preview=True).resume_manager
            self.assertFalse(manager.should_run("07_check_backend_apis"))
            self.assertTrue(manager.should_run("08_implement_backend_tdd"))

    def test_corrupt_resume_state_and_pending_publication_do_not_skip(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            graph = DependencyGraph(root)
            for malformed in ({"schema_version": 1, "stages": {"bad": None}}, [], None):
                save_json(root / ".arc/resume.json", malformed)
                manager = ResumeManager(root, root / "input", graph, enabled=True)
                self.assertTrue(manager.should_run("08"))
                save_json(root / ".arc/dependencies.json", malformed)
                self.assertEqual(DependencyGraph(root).data["nodes"], {})
            graph.data["pending"] = ["artifact:db_schema.json"]
            manager = ResumeManager(root, root / "input", graph, enabled=True)
            self.assertTrue(manager.rerun["all"])

    def test_file_task_reuse_still_runs_gate_and_changed_source_gets_repaired(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "a.ts"
            source.write_text("before\n")
            graph = DependencyGraph(root)
            manager = ResumeManager(root, root / "input", graph)
            def edit(old, new):
                return {"tool_calls": [{"tool": "edit_file", "path": "a.ts", "operation": "replace",
                    "old_content": old, "new_content": new}]}
            model, gate = Mock(generate_json=Mock(return_value=edit("before", "after"))), Mock(return_value={"status": "GREEN"})
            with manager.activate(), graph.activate(), graph.stage("08"), usage_scope(stage="08"):
                run_file_agent(model, "edit", {}, FileTools(root, ["a.ts"]), gate, 2, Mock(), "R")
                manager.mark_task_done("08", "stage", {"status": "GREEN"})
            # a.ts is outside application roots, but per-task versions still protect it.
            resumed = ResumeManager(root, root / "input", graph, enabled=True)
            fresh_model = Mock()
            with resumed.activate(), usage_scope(stage="08"):
                run_file_agent(fresh_model, "edit", {}, FileTools(root, ["a.ts"]), gate, 2, Mock(), "R")
            fresh_model.generate_json.assert_not_called()
            self.assertEqual(gate.call_count, 2)
            source.write_text("damaged\n")
            fresh_model.generate_json.return_value = edit("damaged", "repaired")
            with resumed.activate(), usage_scope(stage="08"):
                run_file_agent(fresh_model, "edit", {}, FileTools(root, ["a.ts"]), gate, 2, Mock(), "R")
            fresh_model.generate_json.assert_called_once()
            self.assertEqual(source.read_text(), "repaired\n")

    def test_replayed_design_is_reaccepted_and_invalid_proposal_falls_back_to_model(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            graph = DependencyGraph(root)
            manager = ResumeManager(root, root / "input", graph)
            model = Mock(generate_json=Mock(return_value={"answer": "original"}))
            with manager.activate(), usage_scope(stage="06"):
                propose_json(model, "design", {"requirement": "R"}, lambda value: value["answer"], Mock(), "R", 1)
            resumed = ResumeManager(root, root / "input", graph, enabled=True)
            accept = Mock(return_value="validated")
            with resumed.activate(), usage_scope(stage="06"):
                result, _ = propose_json(model, "design", {"requirement": "R"}, accept, Mock(), "R", 1)
            self.assertEqual(result, "validated")
            self.assertEqual(model.generate_json.call_count, 1)
            accept.side_effect = [ValueError("contract changed"), "new"]
            model.generate_json.return_value = {"answer": "new"}
            with resumed.activate(), usage_scope(stage="06"):
                result, _ = propose_json(model, "design", {"requirement": "R"}, accept, Mock(), "R", 1)
            self.assertEqual(result, "new")
            self.assertEqual(model.generate_json.call_count, 2)

    def test_file_reuse_rechecks_collection_queries_and_passes_failed_gate_evidence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.ts").write_text("before\n")
            graph = DependencyGraph(root)
            manager = ResumeManager(root, root / "input", graph)
            index = ContextIndex({"api": {"A": {"description": "save"}}})
            def edit(old, new):
                return {"tool_calls": [{"tool": "edit_file", "path": "a.ts", "operation": "replace",
                    "old_content": old, "new_content": new}]}
            model = Mock(generate_json=Mock(side_effect=[{"context_request": {"kind": "api", "query": "missing"}}, edit("before", "after")]))
            with manager.activate(), graph.activate(), graph.stage("08"), usage_scope(stage="08"):
                run_file_agent(model, "edit", {}, FileTools(root, ["a.ts"]), lambda: {}, 2, Mock(), "R", context_index=index)
                manager.mark_task_done("08", "stage", {"status": "GREEN"})
            resumed = ResumeManager(root, root / "input", graph, enabled=True)
            index.collections["api"]["B"] = {"description": "missing"}
            next_model = Mock(generate_json=Mock(return_value=edit("after", "fixed")))
            with resumed.activate(), usage_scope(stage="08"):
                run_file_agent(next_model, "edit", {}, FileTools(root, ["a.ts"]), lambda: {}, 2, Mock(), "R", context_index=index)
            next_model.generate_json.assert_called_once()
            # A stale acceptance result cannot bypass the live gate even if source hashes match.
            graph.flush()
            newest = ResumeManager(root, root / "input", graph, enabled=True)
            final_model = Mock(generate_json=Mock(return_value=edit("fixed", "final")))
            gate = Mock(side_effect=[ValueError("expected 201, actual 500"), {"status": "GREEN"}])
            with newest.activate(), usage_scope(stage="08"):
                run_file_agent(final_model, "edit", {}, FileTools(root, ["a.ts"]), gate, 2, Mock(), "R", context_index=index)
            request = final_model.generate_json.call_args.args[1]
            self.assertIn("expected 201, actual 500", str(request["feedback"]))
            self.assertEqual(gate.call_count, 2)
