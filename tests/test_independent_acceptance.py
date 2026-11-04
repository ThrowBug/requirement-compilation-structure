"""Independent tests cannot be rewritten by the application repair task."""
from tool_fixtures import propose_files
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import yaml

from infrastructure.shared.agent.context import SourceFiles
from infrastructure.stages.frontend.triggers.acceptance import run_independent_acceptance, acceptance_path, ORACLE_POLICY_VERSION
from infrastructure.stages.frontend.triggers.e2e_runtime import OBSERVATIONS
from infrastructure.utils.files import save_json, save_text
from test_frontend_flow import frontend_fixture


class IndependentAcceptance(unittest.TestCase):
    def test_generated_routes_and_conventions_do_not_become_the_acceptance_oracle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir, ui, triggers, backend = self.setup_workspace(root)
            raw = yaml.safe_load((input_dir / 'requirements.yaml').read_text())
            raw['children'][0]['description'] += ' Explicit original HTTP contract: POST /api/original, status 201.'
            (input_dir / 'requirements.yaml').write_text(yaml.safe_dump(raw))
            backend['apis']['API-SAVE']['signature'] = {'method': 'POST', 'path': '/api/generated-old'}
            save_json(root / '.arc/shared_conventions.json', {'transport': {
                'description': 'Always use /api/generated-old', 'requirements': ['REQ-A'], 'fields': []}})
            seen = []
            def generate(prompt, payload):
                seen.append(payload)
                self.assertNotIn('api_interfaces', payload['task'])
                self.assertNotIn('shared_conventions', payload['task'])
                self.assertNotIn('backend_apis', payload['task'])
                self.assertIn(OBSERVATIONS, payload['allowed_read_paths'])
                self.assertIn('observeMutations', prompt)
                return propose_files(root, [{'path': payload['task']['test_path'], 'content': 'business outcomes'}])
            checks = Mock()
            checks.typecheck.return_value = {'returncode': 0}
            checks.tests.return_value = {'returncode': 0, 'passed': 1, 'failed': 0}
            with patch('infrastructure.stages.frontend.triggers.acceptance.E2EChecks', return_value=checks), \
                    patch('infrastructure.stages.frontend.triggers.acceptance.BackendChecks'), \
                    patch('infrastructure.stages.frontend.triggers.acceptance.FrontendChecks'):
                report = run_independent_acceptance(input_dir, root / '.arc', {'entities': {}}, backend, ui,
                    triggers, Mock(generate_json=generate), Mock(),
                    process_report={'requirements': {}, 'verification': {}})
            self.assertEqual(report['status'], 'GREEN')
            self.assertIn('/api/original', str(seen[0]['task']['original_requirement']))

    def test_existing_url_bound_suites_are_reviewed_once_against_original_requirements(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir, ui, triggers, backend = self.setup_workspace(root)
            for rid in ('REQ-A', 'REQ-B', 'REQ-C'):
                save_text(root / acceptance_path(rid), 'old endpoint wait; success rejection identity assertions')
            save_json(root / '.arc/independent_acceptance.json', {'requirements': {}})
            def generate(prompt, payload):
                self.assertEqual(payload['feedback'][0]['review_reason'], 'acceptance_oracle_policy_changed')
                return propose_files(root, [{'path': payload['task']['test_path'],
                    'content': 'observe mutations; success rejection identity assertions'}],
                    diagnosis={'cause': 'test_overconstraint', 'reason': 'Generated endpoint is not an original contract'})
            model = Mock(generate_json=generate)
            checks = Mock()
            checks.typecheck.return_value = {'returncode': 0}
            checks.tests.return_value = {'returncode': 0, 'passed': 1, 'failed': 0}
            with patch('infrastructure.stages.frontend.triggers.acceptance.E2EChecks', return_value=checks), \
                    patch('infrastructure.stages.frontend.triggers.acceptance.BackendChecks'), \
                    patch('infrastructure.stages.frontend.triggers.acceptance.FrontendChecks'):
                report = run_independent_acceptance(input_dir, root / '.arc', {'entities': {}}, backend, ui,
                    triggers, model, Mock(), process_report={'requirements': {}, 'verification': {}})
                self.assertEqual(report['status'], 'GREEN')
                self.assertTrue(all(entry['oracle_policy'] == ORACLE_POLICY_VERSION for entry in report['requirements'].values()))
                no_calls = Mock()
                second = run_independent_acceptance(input_dir, root / '.arc', {'entities': {}}, backend, ui,
                    triggers, no_calls, Mock(), process_report={'requirements': {}, 'verification': {}})
                no_calls.generate_json.assert_not_called()
            self.assertEqual(second['status'], 'GREEN')
            self.assertIn('test_overconstraint', (root / '.arc/test_repairs.jsonl').read_text())

    def setup_workspace(self, root):
        raw, tree, ui, triggers, backend, _ = frontend_fixture()
        input_dir = root / 'input'
        input_dir.mkdir()
        (input_dir / 'requirements.yaml').write_text(yaml.safe_dump(raw))
        return input_dir, ui, triggers, backend

    def test_real_behavior_failure_survives_separate_review_and_later_requirements_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir, ui, triggers, backend = self.setup_workspace(root)
            requests = []
            def generate(prompt, payload):
                requests.append(payload)
                task = payload['task']
                if 'repair_reason' in task:
                    self.assertFalse(payload['files'][acceptance_path('REQ-A')]['writable'])
                    return propose_files(root, [], diagnosis={'cause': 'implementation', 'reason': 'Required result is missing'})
                self.assertEqual([path for path, value in payload['files'].items() if value['writable']], [task['test_path']])
                self.assertNotIn('file', payload['context_kinds'])
                self.assertNotIn('triggers', task)
                if payload['feedback']:
                    return propose_files(root, [], diagnosis={'cause': 'implementation', 'reason': 'Required result is missing'})
                return propose_files(root, [{'path': task['test_path'], 'content': 'original requirement assertions'}])
            checks = Mock()
            checks.typecheck.return_value = {'returncode': 0}
            failure = {'returncode': 1, 'passed': 0, 'failed': 1, 'failures': ['wrong account']}
            green = {'returncode': 0, 'passed': 1, 'failed': 0}
            checks.tests.side_effect = lambda paths: failure if acceptance_path('REQ-A') in paths else green
            checks.build.return_value = {'returncode': 0}
            with patch('infrastructure.stages.frontend.triggers.acceptance.E2EChecks', return_value=checks), \
                    patch('infrastructure.stages.frontend.triggers.acceptance.BackendChecks'), \
                    patch('infrastructure.stages.frontend.triggers.acceptance.FrontendChecks'):
                result = run_independent_acceptance(input_dir, root / '.arc', {'entities': {}}, backend, ui,
                    triggers, Mock(generate_json=generate), Mock(), max_turns=1,
                    process_report={'requirements': {}, 'verification': {}})
            self.assertEqual(result['status'], 'COMPLETED_WITH_FAILURES')
            self.assertEqual(result['requirements']['REQ-A']['status'], 'FAILED')
            self.assertEqual(result['requirements']['REQ-C']['status'], 'GREEN')
            self.assertEqual((root / acceptance_path('REQ-A')).read_text(), 'original requirement assertions')
            self.assertEqual(len(requests), 5)
            self.assertEqual(sum('repair_reason' in request['task'] for request in requests), 1)

    def test_wrong_test_can_be_corrected_only_by_review_task_and_is_audited(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir, ui, triggers, backend = self.setup_workspace(root)
            checks = Mock()
            checks.typecheck.return_value = {'returncode': 0}
            failure = {'returncode': 1, 'passed': 0, 'failed': 1, 'failures': ['invalid selector']}
            green = {'returncode': 0, 'passed': 1, 'failed': 0}
            checks.tests.side_effect = [failure, green, green, green]
            def generate(prompt, payload):
                corrected = bool(payload['feedback'])
                return propose_files(root, [{'path': payload['task']['test_path'], 'content': 'correct selector' if corrected else 'initial suite'}], diagnosis={'cause': 'test_overconstraint', 'reason': 'Selector contradicts original label'})
            with patch('infrastructure.stages.frontend.triggers.acceptance.E2EChecks', return_value=checks), \
                    patch('infrastructure.stages.frontend.triggers.acceptance.BackendChecks'), \
                    patch('infrastructure.stages.frontend.triggers.acceptance.FrontendChecks'):
                report = run_independent_acceptance(input_dir, root / '.arc', {'entities': {}}, backend, ui,
                    triggers, Mock(generate_json=generate), Mock(),
                    process_report={'requirements': {}, 'verification': {}})
            self.assertEqual(report['status'], 'GREEN')
            path = acceptance_path('REQ-A')
            workspace = SourceFiles(root).workspace(references=[path])
            self.assertFalse(workspace.state()[path]['writable'])
            SourceFiles(root).include(workspace, [path])
            self.assertFalse(workspace.state()[path]['writable'])
            self.assertIn('test_overconstraint', (root / '.arc/test_repairs.jsonl').read_text())
            self.assertIn('initial suite', (root / '.arc/test_repairs.jsonl').read_text())
