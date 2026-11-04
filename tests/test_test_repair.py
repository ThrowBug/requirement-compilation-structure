"""Repair generated tests against requirements and keep UI smoke checks minimal."""
from tool_fixtures import propose_files

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from infrastructure.shared.agent.context import SourceFiles
from infrastructure.shared.agent.loop import run_file_agent
from infrastructure.shared.project.initialize import initialize_project
from infrastructure.stages.frontend.ui import implementation
from infrastructure.stages.frontend.ui.testing import existence_suite, parse_playwright_result
from infrastructure.utils.files import save_text


class TestRepair(unittest.TestCase):
    def test_locked_browser_database_retries_with_a_fresh_directory(self):
        from infrastructure.stages.frontend.triggers.testing import E2EChecks
        with TemporaryDirectory() as directory:
            output = Path(directory)
            checks = E2EChecks(output)
            locked = "Error: EBUSY: resource busy or locked, unlink 'arc-e2e-run/app.db'"
            good = json.dumps({"stats": {"expected": 1, "unexpected": 0, "skipped": 0, "flaky": 0},
                "suites": [], "errors": []})
            with patch.object(checks, "execute", side_effect=[
                    {"returncode": 1, "stdout": "", "stderr": locked, "output": locked},
                    {"returncode": 0, "stdout": good, "stderr": "", "output": good}]) as execute:
                result = checks.tests(["frontend/tests/e2e/acceptance/sample.spec.ts"])
            self.assertEqual(result["passed"], 1)
            self.assertEqual(result["prior_cleanup_error"], locked)
            self.assertNotEqual(execute.call_args_list[0].kwargs["env"]["ARC_E2E_TEMP_ROOT"],
                execute.call_args_list[1].kwargs["env"]["ARC_E2E_TEMP_ROOT"])

    @unittest.skipUnless(os.environ.get('ARC_DB_TEST_NODE_MODULES'), 'Needs local TypeScript')
    def test_literal_role_normalization_is_exact_and_idempotent(self):
        from infrastructure.stages.frontend.triggers.acceptance import normalize_exact_role_locators
        with TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "node_modules").symlink_to(Path(os.environ['ARC_DB_TEST_NODE_MODULES']).resolve())
            path = "browser.spec.ts"
            save_text(output / path, """page.getByRole('link', { name: 'HOME' });
page.getByRole("textbox", { name: "Name" });
page.getByRole("textbox", { name: "Name lookup" });
page.getByRole('link', { name: /HOME/i });
""")
            task = {"original_requirement": {
                "description": '"HOME"(role=link) and "Name"(role=textbox) are exact.', "steps": []}}
            normalize_exact_role_locators(output, path, task)
            source = (output / path).read_text()
            self.assertEqual(source.count("exact: true"), 2)
            self.assertIn('{ name: "Name lookup" }', source)
            self.assertIn('{ name: /HOME/i }', source)
            normalize_exact_role_locators(output, path, task)
            self.assertEqual((output / path).read_text(), source)

    def test_existence_suite_does_not_turn_dependency_state_or_option_objects_into_text_assertions(self):
        from test_frontend_flow import frontend_fixture
        _, _, ui, _, _, _ = frontend_fixture()
        ui['elements']['UI-TEXT']['prop'].update(placeholder='Fixed placeholder', options=[{'label': 'Adult', 'value': 'adult'}])
        ui['elements']['UI-TEXT']['type'] = 'select'
        ui['elements']['UI-COUNT']['data'].append('form.errorVisible')
        suite = existence_suite(ui, 'editor')
        for assertion in ('toHaveAccessibleName', 'toHaveAttribute', 'toHaveValue', 'toContainText', 'getByRole("option"'):
            self.assertNotIn(assertion, suite)
        self.assertIn('toBeVisible', suite)
        self.assertIn('__ARC_UI_DATA__', suite)
        self.assertIn('Object.prototype.hasOwnProperty.call(state.data, key)', suite)
        self.assertIn('form.text', suite)
        self.assertIn('form.errorVisible', suite)

    def test_model_can_attribute_and_correct_a_wrong_test_then_runs_real_gate_again(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            suite = 'backend/tests/registration.test.ts'
            support = 'backend/tests/support/context.ts'
            source = 'backend/src/register.ts'
            save_text(root / suite, 'wrong assertion')
            save_text(root / support, 'compiler fixture')
            save_text(root / source, 'correct implementation')
            selection = SourceFiles(root, root='backend/src', tests_root='backend/tests')
            workspace = selection.workspace(paths=[source], references=[suite, support])
            console = Mock()
            diagnosis = {'cause': 'test_overconstraint', 'reason': 'The requirement allows soft invalidation; the test requires physical deletion.'}
            model = Mock(generate_json=Mock(return_value=propose_files(root, [{'path': suite, 'content': 'observable required outcome'}], diagnosis=diagnosis)))
            gate = Mock(return_value={'passed': True})
            result = run_file_agent(model, 'Implement required behavior.', {'requirement': 'Invalidate the session.'},
                workspace, gate, 1, console, 'Registration', feedback={'error': 'wrong assertion'})
            payload = model.generate_json.call_args.args[1]
            self.assertTrue(payload['files'][suite]['writable'])
            self.assertFalse(payload['files'][support]['writable'])
            self.assertIn('test_requirement_mismatch', model.generate_json.call_args.args[0])
            self.assertEqual((root / source).read_text(), 'correct implementation')
            self.assertEqual((root / suite).read_text(), 'observable required outcome')
            self.assertEqual(result['checks'], {'passed': True})
            gate.assert_called_once_with()
            self.assertTrue(any('Failure diagnosis' in call.args[0] for call in console.show.call_args_list))

    def test_ui_syntax_error_in_existing_test_reaches_model_and_correction_survives_rerun(self):
        from test_frontend_flow import frontend_fixture
        _, _, ui, _, _, _ = frontend_fixture()
        ui['pages'] = {'editor': ui['pages']['editor']}
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            initialize_project(root)
            suite = root / 'frontend/tests/ui/editor.spec.ts'
            save_text(suite, 'invalid compiler-generated assertion')
            checks = Mock()
            checks.typecheck.side_effect = lambda files: {'returncode': 0 if suite.read_text() == 'correct presence assertions' else 1, 'output': 'Invalid option name'}
            green = {'returncode': 0, 'passed': 1, 'failed': 0, 'output': 'passed'}
            checks.tests.return_value = green
            checks.build.return_value = {'returncode': 0, 'output': 'built'}
            def generate(instructions, payload):
                self.assertTrue(payload['files']['frontend/tests/ui/editor.spec.ts']['writable'])
                return propose_files(root, [{'path': 'frontend/src/editor.tsx', 'content': 'export function Page() { return null; }'},
                        {'path': 'frontend/tests/ui/editor.spec.ts', 'content': 'correct presence assertions'}], diagnosis={'cause': 'test_requirement_mismatch', 'reason': 'A select option object was used as a string locator.'})
            model = Mock(generate_json=generate)
            with patch.object(implementation, 'install_dependencies'), patch.object(implementation, 'FrontendChecks', return_value=checks):
                result = implementation.implement_ui(root / '.arc', ui, model, Mock(), {'pages': {}}, max_turns=1)
                self.assertEqual(result['status'], 'GREEN')
                self.assertEqual(suite.read_text(), 'correct presence assertions')
                again = implementation.implement_ui(root / '.arc', ui, Mock(), Mock(), {'pages': {}}, max_turns=1)
                self.assertEqual(again['status'], 'GREEN')
                self.assertEqual(suite.read_text(), 'correct presence assertions')

    def test_environment_diagnostics_survive_playwright_report_parsing(self):
        import json
        report = {'stats': {}, 'errors': [{'message': 'webServer failed'}], 'suites': []}
        parsed = parse_playwright_result({'returncode': 1, 'stdout': json.dumps(report),
            'stderr': 'Error: listen EADDRINUSE 127.0.0.1:41234', 'output': 'full original output'})
        self.assertIn('EADDRINUSE', parsed['stderr'])
        self.assertIn('EADDRINUSE', parsed['output'])


if __name__ == '__main__':
    unittest.main()
