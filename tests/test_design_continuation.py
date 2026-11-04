"""A failed design task leaves honest partial artifacts and later tasks still run."""
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import Mock, patch

from infrastructure.shared.agent.tasks import StageIncomplete
from infrastructure.utils.files import save_json
from test_schema_evolution import field
from tool_fixtures import propose_files


class DesignContinuation(unittest.TestCase):
    def test_schema_accepts_missing_section_completion_without_regenerating_entities(self):
        stage = import_module('02_build_db_schema')
        with TemporaryDirectory() as directory:
            arc = Path(directory) / '.arc'
            save_json(arc / 'requirements.json', {'id': 'ROOT', 'children': [{'id': 'REQ-A', 'name': 'Account'}]})
            entities = {'Account': {'description': 'Account', 'fields': {'id': field('Identifier')}}}
            model = Mock(generate_json=Mock(side_effect=[{'entities': entities}, {'related_fields': ['Account.id']}]))
            with patch.object(stage, 'TextModel', return_value=model), patch.object(stage, 'DemoConsole', return_value=Mock()):
                result = stage.build_db_schema(arc, max_attempts=2)
            self.assertEqual(model.generate_json.call_count, 2)
            self.assertEqual(result['entities'], entities)
            self.assertEqual(result['requirements']['REQ-A'], ['Account.id'])

    def test_schema_missing_associations_repairs_only_that_section_and_revalidates(self):
        stage = import_module('02_build_db_schema')
        with TemporaryDirectory() as directory:
            arc = Path(directory) / '.arc'
            save_json(arc / 'requirements.json', {'id': 'ROOT', 'children': [{'id': 'REQ-A', 'name': 'Account'}]})
            entities = {'Account': {'description': 'Account', 'fields': {'id': field('Identifier')}}}
            requests = []
            def generate(prompt, payload):
                requests.append(payload)
                if len(requests) == 1:
                    return {'entities': entities}
                if len(requests) == 2:
                    self.assertTrue(prompt.startswith('OUTPUT MODE: MISSING-FIELD REPAIR.'))
                    self.assertIn("only 'related_fields'", prompt)
                    self.assertEqual(payload['correction']['previous_response'], {'entities': entities})
                    return {'related_fields': ['Account.missing']}
                self.assertIn('undefined database fields', payload['correction']['error'])
                self.assertIn('complete replacement', prompt)
                return {'entities': entities, 'related_fields': ['Account.id']}
            with patch.object(stage, 'TextModel', return_value=Mock(generate_json=generate)), \
                    patch.object(stage, 'DemoConsole', return_value=Mock()):
                result = stage.build_db_schema(arc, max_attempts=3)
            self.assertEqual(result['requirements']['REQ-A'], ['Account.id'])
            self.assertEqual(result['entities'], entities)

    def test_schema_failure_does_not_stop_later_requirement_or_obligation_inference(self):
        schema_stage = import_module('02_build_db_schema')
        obligation_stage = import_module('03_infer_db_obligations')
        with TemporaryDirectory() as directory:
            arc = Path(directory) / '.arc'
            save_json(arc / 'requirements.json', {'id': 'ROOT', 'children': [
                {'id': 'BAD', 'name': 'Bad design'}, {'id': 'GOOD', 'name': 'Read account'}]})
            calls = []
            def schema_proposal(_, request):
                rid = request['requirement']['id']
                calls.append(rid)
                return {} if rid == 'BAD' else {'entities': {'Account': {
                    'description': 'Account', 'fields': {'id': field('Identifier')}}}, 'related_fields': ['Account.id']}
            with patch.object(schema_stage, 'TextModel', return_value=Mock(generate_json=schema_proposal)), \
                    patch.object(schema_stage, 'DemoConsole', return_value=Mock()):
                with self.assertRaises(StageIncomplete) as failure:
                    schema_stage.build_db_schema(arc, max_attempts=1)
            self.assertEqual(calls, ['BAD', 'GOOD'])
            self.assertEqual(failure.exception.failures[0]['task'], 'BAD')
            schema = json.loads((arc / 'db_schema.json').read_text())
            self.assertNotIn('BAD', schema['requirements'])
            self.assertEqual(schema['requirements']['GOOD'], ['Account.id'])
            model = Mock(generate_json=Mock(return_value={
                'requirement': {'read': ['Account.id'], 'write': []}, 'steps': {}}))
            with patch.object(obligation_stage, 'TextModel', return_value=model), \
                    patch.object(obligation_stage, 'DemoConsole', return_value=Mock()):
                with self.assertRaises(StageIncomplete):
                    obligation_stage.infer_db_obligations(arc, max_attempts=1)
            obligations = json.loads((arc / 'db_obligations.json').read_text())
            self.assertNotIn('BAD', obligations['requirements'])
            self.assertEqual(obligations['requirements']['GOOD']['read'], ['Account.id'])
            model.generate_json.assert_called_once()

    def test_api_design_failure_preserves_later_valid_contracts(self):
        from test_backend_contracts import prepare_artifacts, conditions
        stage = import_module('06_design_backend_apis')
        with TemporaryDirectory() as directory:
            arc, _, design = prepare_artifacts(Path(directory))
            calls = []
            def proposal(_, request):
                rid = request['requirement']['id']
                calls.append(rid)
                if rid == 'REQ-A':
                    return {}
                aids = design['requirements'][rid]
                return {'apis': {aid: {**design['apis'][aid], **conditions(aid)} for aid in aids},
                    'requirement_apis': aids}
            with patch.object(stage, 'TextModel', return_value=Mock(generate_json=proposal)), \
                    patch.object(stage, 'DemoConsole', return_value=Mock()):
                with self.assertRaises(StageIncomplete):
                    stage.design_backend_apis(arc, max_attempts=1)
            self.assertEqual(calls, ['REQ-A', 'REQ-B', 'REQ-C'])
            persisted = json.loads((arc / 'backend_apis.json').read_text())
            self.assertNotIn('REQ-A', persisted['requirements'])
            self.assertEqual(persisted['requirements']['REQ-C'], ['API-COUNT'])

    def test_sql_failure_still_wires_the_other_successful_table(self):
        from test_backend_contracts import prepare_artifacts
        stage = import_module('05_implement_db')
        with TemporaryDirectory() as directory:
            output = Path(directory)
            arc, schema, _ = prepare_artifacts(output)
            schema['entities']['Other'] = {'description': 'Independent table', 'fields': {'id': field('ID')}}
            save_json(arc / 'db_schema.json', schema)
            calls = []
            def proposal(_, request):
                name = request['task']['entity_name']
                calls.append(name)
                return {} if name == 'Note' else propose_files(output, [{'path': request['task']['output_path'], 'content': 'CREATE TABLE Other (id TEXT);'}])
            with patch.object(stage, 'TextModel', return_value=Mock(generate_json=proposal)), \
                    patch.object(stage, 'verify_database_runtime', side_effect=ValueError('Incomplete table implementations')), \
                    patch.object(stage, 'DemoConsole', return_value=Mock()):
                with self.assertRaises(StageIncomplete):
                    stage.implement_db(arc, max_attempts=1)
            self.assertEqual(set(calls), {'Note', 'Other'})
            self.assertTrue((output / 'backend/src/db/tables/Other.sql').is_file())
            self.assertIn('Other.sql', (output / 'backend/src/db/initialize.ts').read_text())

    def test_ui_and_trigger_designs_continue_after_one_bad_proposal(self):
        from test_frontend_flow import frontend_fixture
        from infrastructure.stages.frontend.ui.design import page_context
        from infrastructure.stages.frontend.ui.synthesis import synthesize_ui
        from infrastructure.stages.frontend.triggers.synthesis import synthesize_triggers
        raw, tree, ui, triggers, backend, obligations = frontend_fixture()
        contexts = {rid: {**requirement, 'images': {}, 'steps': [tree.steps[sid] for sid in tree.own_steps[rid]]}
            for rid, requirement in tree.requirements.items()}
        tree.leaves = [rid for rid in tree.leaves if rid != 'REQ-B']
        tree.steps = {sid: step for sid, step in tree.steps.items() if sid != 'STEP-REQ-B'}
        contexts.pop('REQ-B', None)
        for aid, api in backend['apis'].items():
            api['signature'] = {'method': 'POST', 'path': '/api/' + aid, 'name': aid}
        backend['requirements'].pop('REQ-B', None)
        ui['requirements'].pop('REQ-B', None)
        tree.requirements.pop('REQ-B', None)
        tree.children.pop('REQ-B', None)
        tree.own_steps.pop('REQ-B', None)
        obligations['requirements'].pop('REQ-B', None)
        with TemporaryDirectory() as directory:
            arc = Path(directory)
            def ui_proposal(_, request):
                rid = request['requirement']['id']
                if rid == 'REQ-A':
                    return {}
                pid = 'summary' if rid == 'REQ-C' else 'editor'
                page = page_context(ui, pid)
                return {'pages': {pid: {key: value for key, value in ui['pages'][pid].items() if key != 'fixtures'}}, 'elements': page['elements'],
                    'requirement_ui': ui['requirements'][rid],
                    'step_ui': {sid: ui['steps'][sid] for sid in tree.own_steps[rid]}}
            with self.assertRaises(StageIncomplete):
                synthesize_ui(arc, tree, {}, contexts, Mock(generate_json=ui_proposal), Mock(), max_attempts=1)
            design = json.loads((arc / 'ui_design.json').read_text())
            self.assertIn('summary', design['pages'])
            self.assertIn('UI-COUNT', design['elements'])
            obligations['steps'] = {sid: obligations['requirements'][rid]
                for rid in tree.leaves for sid in tree.own_steps[rid]}
            save_json(arc / 'db_obligations.json', obligations)
            def trigger_proposal(_, request):
                rid = request['requirement']['id']
                if rid == 'REQ-A':
                    return {}
                tids = triggers['requirements'][rid]
                return {'triggers': {tid: triggers['triggers'][tid] for tid in tids},
                    'requirement_triggers': tids, 'step_triggers': {sid: tids for sid in tree.own_steps[rid]}}
            with self.assertRaises(StageIncomplete):
                synthesize_triggers(arc, ui, tree, backend, contexts, Mock(generate_json=trigger_proposal), Mock(), max_attempts=1)
            trigger_result = json.loads((arc / 'frontend_triggers.json').read_text())
            self.assertIn('TR-COUNT', trigger_result['triggers'])
