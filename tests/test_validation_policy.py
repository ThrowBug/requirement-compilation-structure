"""Database obligation inference and API acceptance policies."""

from copy import deepcopy
from importlib import import_module
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from infrastructure.stages.backend.primitives import validate_design, ObligationCoverageError
from infrastructure.stages.database.obligations.records import normalize_inference
from infrastructure.stages.backend.testing import require_green as backend_green
from infrastructure.stages.frontend.ui.testing import require_green as browser_green
from infrastructure.utils.files import save_json


class ValidationPolicy(unittest.TestCase):
    def test_api_coverage_uses_only_read_write_sets(self):
        api = {"read": ["Table.a"], "write": [], "signature": "arbitrary metadata"}
        design = {"apis": {"A": api, "a": deepcopy(api)}, "requirements": {"REQ": ["A", "A"]}}
        obligations = {"requirements": {"REQ": {"read": ["Table.a"], "write": []}}}
        validate_design(design, obligations)
        design["apis"]["A"]["read"] = []
        with self.assertRaises(ObligationCoverageError): validate_design(design, obligations)
        design["apis"]["A"].update(read=[], write=["Table.a"])
        with self.assertRaises(ObligationCoverageError): validate_design(design, obligations)

    def test_normalization_is_separate_from_schema_resolution(self):
        result = normalize_inference({"requirement": {"read": ["Unknown.field"], "write": []}}, [])
        self.assertEqual(result["requirement"]["read"], ["Unknown.field"])

    def test_stage03_does_not_require_all_related_fields_to_be_classified(self):
        from test_backend_contracts import prepare_artifacts
        stage = import_module("03_infer_db_obligations")
        proposal = {"requirement": {"read": [], "write": []}, "steps": {}}
        with TemporaryDirectory() as temporary:
            arc, _, _ = prepare_artifacts(Path(temporary))
            inference = Mock(generate_json=lambda _, payload: {**proposal, "steps": {step["id"]: {"read": [], "write": []} for step in payload["steps"]}})
            reviewer = Mock(generate_json=Mock(return_value={"decision": "no_write", "reason": "No persistence in this fixture."}))
            raw = json.loads((arc / "requirements.json").read_text())
            for req in raw["children"]:
                req["description"] = "Display a static panel without any database access."
                req["scenarios"] = []
            save_json(arc / "requirements.json", raw)
            with patch.object(stage, "TextModel", side_effect=[inference, reviewer]), \
                    patch.object(stage, "DemoConsole", return_value=Mock()):
                result = stage.infer_db_obligations(arc)
        self.assertEqual(result["requirements"]["REQ-A"], {"read": [], "write": []})

    def test_stage03_reviews_empty_writes_once_and_preserves_read_only_requirements(self):
        from test_backend_contracts import prepare_artifacts
        stage = import_module("03_infer_db_obligations")
        with TemporaryDirectory() as temporary:
            arc, schema, _ = prepare_artifacts(Path(temporary))
            raw = json.loads((arc / "requirements.json").read_text())
            raw["children"][1]["description"] = "Read a saved note without changing it."
            raw["children"][2]["description"] = "Display a help panel."
            scenarios = [req["scenarios"][0]["steps"][0] for req in raw["children"]]
            scenarios[0].update(Given="A blank editor", When="Save the submitted note", Then="Persist the note text.")
            scenarios[1].update(Given="A note was saved during setup", When="View the note", Then="Display the existing text.")
            scenarios[2].update(When="Expand the help panel", Then="Display help.")
            schema["requirements"]["REQ-C"] = []
            save_json(arc / "requirements.json", raw)
            save_json(arc / "db_schema.json", schema)
            proposals, reviews = [], []

            def infer(_, request):
                rid = request["requirement"]["id"]
                proposals.append(rid)
                if rid == "REQ-A" and proposals.count(rid) > 1:
                    self.assertIn("Persist the note text.", request["correction"]["error"])
                effects = {"read": ["Note.text"] if rid == "REQ-B" else [],
                    "write": ["Note.text"] if rid == "REQ-A" and proposals.count(rid) == 3 else []}
                return {"requirement": effects, "steps": {step["id"]: effects for step in request["steps"]}}

            def review(_, request):
                rid = request["requirement"]["id"]
                reviews.append(rid)
                self.assertEqual(request["candidate"]["requirement"]["write"], [])
                if rid == "REQ-A":
                    return {"decision": "missing_write", "reason": "Saving changes persisted note text.",
                        "evidence": "Persist the note text."}
                return {"decision": "no_write", "reason": "The save is a Given fixture; viewing only reads."}

            with patch.object(stage, "TextModel", side_effect=[Mock(generate_json=infer), Mock(generate_json=review)]), \
                    patch.object(stage, "DemoConsole", return_value=Mock()):
                result = stage.infer_db_obligations(arc)
            self.assertEqual(proposals, ["REQ-A", "REQ-A", "REQ-A", "REQ-B", "REQ-C"])
            self.assertEqual(reviews, ["REQ-A", "REQ-B"])
            self.assertEqual(result["requirements"]["REQ-A"]["write"], ["Note.text"])
            self.assertEqual(result["requirements"]["REQ-B"], {"read": ["Note.text"], "write": []})

    def test_actual_test_failures_still_block_acceptance(self):
        backend_green({"returncode": 0, "tests": 1, "pass": 1, "fail": 0})
        browser_green({"returncode": 0, "passed": 1, "failed": 0})
        with self.assertRaises(ValueError): backend_green({"returncode": 1, "tests": 1, "pass": 0, "fail": 1})
        with self.assertRaises(ValueError): browser_green({"returncode": 1, "passed": 0, "failed": 1})
