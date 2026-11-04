"""A stalled query may recover, but persistent no-progress cannot loop forever."""

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from infrastructure.shared.agent.discovery import ContextIndex, DiscoverySession, DiscoveryStalled, project_index
from infrastructure.utils.files import save_json
from infrastructure.shared.agent.loop import AgentExhausted, run_file_agent
from infrastructure.shared.agent.proposals import propose_json
from infrastructure.shared.agent.tools import FileTools


class DiscoveryProgress(unittest.TestCase):
    def test_permuted_missing_ids_do_not_count_as_new_evidence(self):
        session = DiscoverySession(ContextIndex({'test_requirement': {}}))
        session.resolve({'kind': 'test_requirement', 'ids': ['a', 'b', 'c']})
        second = session.resolve({'kind': 'test_requirement', 'ids': ['c', 'a', 'b']})
        self.assertIn('next_action', second)
        with self.assertRaises(DiscoveryStalled):
            session.resolve({'kind': 'test_requirement', 'ids': ['b', 'c', 'a']})

    def test_hashed_acceptance_suites_resolve_to_original_owners_without_file_access(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            save_json(root / '.arc/requirements.json', {'id': 'ROOT', 'children': [
                {'id': 'REQ-A', 'description': 'original business outcome', 'children': []}]})
            suite = 'frontend/tests/e2e/acceptance/opaque-hash.spec.ts'
            save_json(root / '.arc/independent_acceptance.json', {'requirements': {
                'REQ-A': {'test_path': suite}, 'UNKNOWN': {'test_path': 'frontend/tests/unknown.spec.ts'}}})
            index = project_index(root, kinds=('test_requirement', 'requirement'), file_roots=('frontend/tests',))
            result = index.query({'kind': 'test_requirement', 'ids': [suite]})
            self.assertEqual(result['records'][suite]['requirement_ids'], ['REQ-A'])
            self.assertEqual(index.query({'kind': 'requirement', 'ids': ['REQ-A']})['records']['REQ-A']['description'],
                'original business outcome')
            self.assertNotIn('frontend/tests/unknown.spec.ts', index.records()['test_requirement'])
            with self.assertRaises(KeyError):
                index.query({'kind': 'file', 'ids': [suite]})

    def test_repeating_design_query_exhausts_attempts_without_acceptance(self):
        model = Mock(generate_json=Mock(return_value={"context_request": {"kind": "api", "query": "account"}}))
        accept = Mock()
        with self.assertRaisesRegex(RuntimeError, "exhausted 2 proposals"):
            propose_json(model, "", {}, accept, Mock(), "design", 2,
                ContextIndex({"api": {"API-A": {"description": "account"}}}))
        self.assertEqual(model.generate_json.call_count, 6)
        accept.assert_not_called()

    def test_repeating_file_query_exhausts_edit_rounds_without_gate(self):
        with TemporaryDirectory() as directory:
            model = Mock(generate_json=Mock(return_value={"context_request": {"kind": "api", "query": "account"}}))
            gate = Mock()
            with self.assertRaises(AgentExhausted):
                run_file_agent(model, "", {}, FileTools(Path(directory)), gate, 2, Mock(), "edit",
                    context_index=ContextIndex({"api": {"API-A": {"description": "account"}}}))
            self.assertEqual(model.generate_json.call_count, 6)
            gate.assert_not_called()

    def test_repeating_reads_do_not_duplicate_excerpts_or_loop_forever(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.ts").write_text("one\ntwo\n", encoding="utf-8")
            requests = []
            def generate(_, payload):
                requests.append(deepcopy(payload))
                return {"tool_calls": [{"tool": "read_file", "path": "source.ts"}]}
            gate = Mock()
            with self.assertRaises(AgentExhausted):
                run_file_agent(Mock(generate_json=generate), "", {}, FileTools(root), gate, 2, Mock(), "read")
            self.assertEqual(len(requests), 6)
            for request in requests[1:]:
                self.assertEqual(len(request["read_files"]["source.ts"]), 1)
            gate.assert_not_called()

    def test_contained_reads_retain_one_complete_excerpt_and_unchanged_source(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            original = "first\nsecond\nthird\n"
            (root / "source.ts").write_text(original, encoding="utf-8")
            responses = iter([
                {"tool_calls": [{"tool": "read_file", "path": "source.ts"}]},
                {"tool_calls": [{"tool": "read_file", "path": "source.ts", "start_line": 2, "end_line": 2}]},
                {"tool_calls": []}])
            requests = []
            def generate(_, payload):
                requests.append(deepcopy(payload))
                return next(responses)
            gate = Mock(return_value={"green": True})
            run_file_agent(Mock(generate_json=generate), "", {}, FileTools(root), gate, 1, Mock(), "read")
            self.assertEqual(requests[-1]["read_files"]["source.ts"][0]["content"], original)
            self.assertEqual(len(requests[-1]["read_files"]["source.ts"]), 1)
            self.assertEqual(requests[-1]["context_result"], {"read_paths": ["source.ts"]})
            self.assertNotIn("tool_results", requests[1]["context_result"])
            self.assertEqual((root / "source.ts").read_text(encoding="utf-8"), original)
            gate.assert_called_once()
