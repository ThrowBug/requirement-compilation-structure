"""Duplicate observations and explicit schema compilation preserve task acceptance."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from infrastructure.shared.agent.context_broker import ContextBroker
from infrastructure.shared.agent.discovery import ContextIndex
from infrastructure.shared.agent.experiments import OptimizationOptions, optimization_scope
from infrastructure.shared.agent.proposals import propose_json
from infrastructure.shared.usage.recorder import UsageRecorder, usage_scope
from infrastructure.stages.database.schema import apply_patch, check_related_fields
from infrastructure.utils.files import save_json


class ContextObservation(unittest.TestCase):
    def test_duplicate_metrics_count_same_results_and_distinguish_new_versions(self):
        with TemporaryDirectory() as directory:
            recorder = UsageRecorder(directory)
            index = ContextIndex({"api": {"A": {"description": "first"}}})
            broker = ContextBroker(index)
            with recorder.activate(), usage_scope(stage="06", task="R"):
                for _ in range(2):
                    broker.resolve_query({"kind": "api", "ids": ["A"]})
                index.collections["api"]["A"]["description"] = "changed"
                broker.resolve_query({"kind": "api", "ids": ["A"]})
            events = [e for e in recorder.data["events"] if e["event"] == "context_query"]
            self.assertEqual([e["duplicate_records"] for e in events], [0, 1, 0])
            self.assertEqual([e["duplicate_result"] for e in events], [False, True, False])
            self.assertEqual(recorder.data["tasks"]["06"]["R"]["duplicate_context_results"], 1)

    def test_typed_declarations_generate_full_schema_and_still_run_acceptance(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "schema.json"
            save_json(path, {"R": {"entities": {"Item": {"fields": {
                "id": {"type": "integer", "primary_key": True}, "title": "string"}}}}})
            model, calls = Mock(), []
            def accept(proposal):
                calls.append(proposal)
                schema = apply_patch({"entities": {}}, proposal)
                check_related_fields(schema, proposal["related_fields"])
                return schema
            with optimization_scope(OptimizationOptions(deterministic_schema=str(path))), usage_scope(stage="02_build_db_schema"):
                schema, proposal = propose_json(model, "design", {}, accept, Mock(), "Schema design R", 1)
            model.generate_json.assert_not_called()
            self.assertEqual(len(calls), 1)
            self.assertEqual(proposal["related_fields"], ["Item.id", "Item.title"])
            self.assertEqual(schema["entities"]["Item"]["fields"]["id"], {
                "description": "id", "type": "integer", "nullable": False,
                "primary_key": True, "unique": True, "references": None, "constraints": []})

    def test_complex_constraint_or_empty_entity_uses_model_and_gate(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "schema.json"
            for fields in ({}, {"id": {"type": "integer", "constraints": ["positive"]}}):
                save_json(path, {"R": {"entities": {"Item": {"fields": fields}}}})
                model = Mock(generate_json=Mock(return_value={"entities": {}, "related_fields": []}))
                accept = Mock(return_value="accepted model proposal")
                with optimization_scope(OptimizationOptions(deterministic_schema=str(path))), usage_scope(stage="02_build_db_schema"):
                    result, _ = propose_json(model, "design", {}, accept, Mock(), "Schema design R", 1)
                model.generate_json.assert_called_once()
                self.assertEqual(result, "accepted model proposal")
                accept.assert_called_once()
