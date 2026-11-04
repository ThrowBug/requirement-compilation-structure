"""Saved run comparison and declared coverage must not overstate evidence."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from contextlib import redirect_stderr
from io import StringIO
from unittest.mock import patch

from infrastructure.shared.artifacts.coverage import build_coverage_matrix
from infrastructure.shared.usage.compare import compare_runs
from infrastructure.utils.files import save_json


class RunReports(unittest.TestCase):
    def test_reference_encoding_cli_is_explicit_and_flags_cannot_conflict(self):
        import main
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input"
            source.mkdir()
            (source / "requirements.yaml").write_text("id: ROOT\n")
            args = [str(source), "-o", str(root / "output")]
            self.assertFalse(main.parse_args(args).optimizations.context_dedup)
            self.assertTrue(main.parse_args([*args, "--context-dedup"]).optimizations.context_dedup)
            self.assertFalse(main.parse_args([*args, "--no-context-dedup"]).optimizations.context_dedup)
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                main.parse_args([*args, "--context-dedup", "--no-context-dedup"])

    def test_cli_passes_resume_and_experimental_configuration_and_rejects_invalid_limits(self):
        import main
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input"
            source.mkdir()
            (source / "requirements.yaml").write_text("id: ROOT\n")
            args = [str(source), "--output-dir", str(root / "output"), "--resume-preview", "--changed-node", "api:A",
                "--experimental-max-task-calls", "3", "--experimental-small-model", "small",
                "--experimental-small-model-task", "Schema design R", "--no-context-dedup"]
            with patch.object(main, "Compiler") as compiler:
                main.main(args)
            configuration = compiler.call_args.kwargs
            self.assertTrue(configuration["resume_preview"])
            self.assertEqual(configuration["changed_nodes"], ["api:A"])
            self.assertEqual(configuration["optimizations"].max_task_calls, 3)
            self.assertFalse(configuration["optimizations"].context_dedup)
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                main.parse_args([str(source), "-o", str(root / "output"), "--experimental-max-task-calls", "0"])

    def test_comparison_rejects_missing_usage_changed_inputs_and_outcome_regressions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            left, right = root / "baseline", root / "candidate"
            record = {"metadata": {"input_hash": "same"}, "totals": {"usage_complete": True,
                "total_tokens": 100, "input_tokens": 70, "output_tokens": 30, "calls": 1},
                "total_duration_seconds": 5, "status": "GREEN"}
            for target in (left, right):
                save_json(target / ".arc/usage/usage.json", record)
                save_json(target / ".arc/compilation.json", {"status": "GREEN", "stages": {"08": {"status": "GREEN"}}})
            self.assertTrue(compare_runs(left, right)["eligible_for_cost_comparison"])
            save_json(right / ".arc/compilation.json", {"status": "COMPLETED_WITH_FAILURES"})
            self.assertFalse(compare_runs(left, right)["eligible_for_cost_comparison"])
            self.assertTrue(compare_runs(left, right)["outcome_changes"])
            record["metadata"]["input_hash"] = "different"
            record["totals"]["usage_complete"] = False
            save_json(right / ".arc/usage/usage.json", record)
            comparison = compare_runs(left, right)
            self.assertFalse(comparison["same_input"])
            self.assertFalse(comparison["usage_complete"])

    def test_coverage_records_requirement_suite_without_claiming_assertion_coverage(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            save_json(root / ".arc/requirements.json", {"id": "R", "name": "save", "description": "save",
                "scenarios": [{"id": "SC", "name": "success", "steps": [{"id": "S", "text": "save"}]}], "children": []})
            save_json(root / ".arc/implementation_paths.json", {"e2e_test": {"R": "frontend/tests/save.spec.ts"}})
            save_json(root / ".arc/frontend_triggers.json", {"steps": {"S": ["T"]}})
            result = build_coverage_matrix(root)
            self.assertEqual(result["status"], "RECORDED")
            step = result["requirements"]["R"]["steps"]["S"]
            self.assertEqual(step["candidate_suites"], ["frontend/tests/save.spec.ts"])
            self.assertEqual(step["assertion_coverage"], "NOT_MEASURED")

    def test_missing_requirements_are_explicitly_unknown(self):
        with TemporaryDirectory() as directory:
            self.assertEqual(build_coverage_matrix(directory)["status"], "UNKNOWN_REQUIREMENTS")
