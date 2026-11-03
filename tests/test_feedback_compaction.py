"""Diagnostic compaction retains distinct assertions and exposes omission counts."""

from copy import deepcopy
import unittest

from infrastructure.shared.agent.feedback import compact_feedback


class FeedbackCompaction(unittest.TestCase):
    def test_duplicate_ansi_reports_keep_expected_actual_and_distinct_errors(self):
        first = 'file.ts:7 TS2322\nExpected: ""\nReceived: "Male"'
        other = 'file.ts:7 TS2322\nExpected: "Female"\nReceived: "Male"'
        source = {"typecheck": {"returncode": 0, "output": "large successful output"},
            "tests": {"returncode": 1, "output": "duplicate reporter JSON",
                "failures": [first, "\x1b[31m" + first + "\x1b[0m", other]}}
        original = deepcopy(source)
        result = compact_feedback(source)
        self.assertEqual(result["typecheck"], {"returncode": 0})
        self.assertNotIn("output", result["tests"])
        self.assertEqual(result["tests"]["failures"], [first, other])
        self.assertEqual(result["tests"]["failures_duplicates"], 1)
        self.assertEqual(source, original)

    def test_error_limits_are_explicit_and_never_modify_the_original_report(self):
        source = {"tests": {"returncode": 1, "failures": [f"test-{n}: error" for n in range(20)]}}
        result = compact_feedback(source, max_errors=3)
        self.assertEqual(len(result["tests"]["failures"]), 3)
        self.assertEqual(result["tests"]["failures_omitted"], 17)
        self.assertEqual(len(source["tests"]["failures"]), 20)
        text = compact_feedback({"error": "x" * 100}, max_chars=20)["error"]
        self.assertIn("omitted 80 characters", text)
