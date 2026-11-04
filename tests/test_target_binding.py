"""Existing outputs can be claimed explicitly without bypassing scope or gates."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from infrastructure.shared.agent.context import SourceFiles
from infrastructure.shared.agent.loop import run_file_agent
from infrastructure.shared.agent.tools import FileTools, FileToolError
from infrastructure.shared.project.entrypoints import selected_path, remember_path
from infrastructure.utils.files import save_text


class TargetBinding(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = 'frontend/src/pages/Register.tsx'
        save_text(self.root / self.path, 'export function Register() { return "stub"; }\n')

    def workspace(self):
        return SourceFiles(self.root).workspace(target=('page', 'register'))

    def test_precreated_page_is_bound_and_real_gate_runs_without_empty_edits(self):
        workspace = self.workspace()
        self.assertTrue(workspace.pending_target)
        model = Mock(generate_json=Mock(return_value={'tool_calls': [
            {'tool': 'bind_target', 'path': self.path},
            {'tool': 'edit_file', 'path': self.path, 'operation': 'replace',
             'old_content': 'stub', 'new_content': 'implemented'}]}))
        gate = Mock(return_value={'status': 'GREEN'})
        result = run_file_agent(model, '', {}, workspace, gate, 1, Mock(), 'register')
        self.assertEqual(result['turns'], 1)
        self.assertEqual(selected_path(self.root, 'page', 'register'), self.path)
        gate.assert_called_once()
        self.assertIn('implemented', (self.root / self.path).read_text())

    def test_binding_only_still_requires_acceptance_and_retains_target_during_repair(self):
        workspace = self.workspace()
        seen = []
        def generate(_, request):
            seen.append(request)
            if len(seen) == 1:
                return {'tool_calls': [{'tool': 'bind_target', 'path': self.path}]}
            return {'tool_calls': [{'tool': 'edit_file', 'path': self.path,
                'operation': 'replace', 'old_content': 'stub', 'new_content': 'implemented'}]}
        gate = Mock(side_effect=[ValueError('behavior is missing'), {'status': 'GREEN'}])
        result = run_file_agent(Mock(generate_json=generate), '', {}, workspace, gate, 2, Mock(), 'register')
        self.assertEqual(result['turns'], 2)
        self.assertEqual(seen[1]['task']['implementation_path'], self.path)
        self.assertEqual(gate.call_count, 2)

    def test_binding_is_rolled_back_if_later_edit_in_batch_fails(self):
        workspace = self.workspace()
        original = (self.root / self.path).read_bytes()
        with self.assertRaises(FileToolError):
            workspace.execute([{'tool': 'bind_target', 'path': self.path},
                {'tool': 'edit_file', 'path': self.path, 'operation': 'replace',
                 'old_content': 'absent', 'new_content': 'replacement'}])
        self.assertTrue(workspace.pending_target)
        self.assertIsNone(selected_path(self.root, 'page', 'register'))
        self.assertEqual((self.root / self.path).read_bytes(), original)

    def test_binding_cannot_change_fixed_destination_or_bypass_read_only_scope(self):
        protected = 'frontend/src/ui-runtime.ts'
        save_text(self.root / protected, 'protected')
        for workspace, path in [
                (self.workspace(), protected),
                (FileTools.for_target(self.root, 'page', 'register', fixed_path=self.path,
                    writable=[protected]), protected),
                (FileTools(self.root, [self.path]), self.path),
                (self.workspace(), 'backend/src/handler.ts'),
                (self.workspace(), 'frontend/src/missing.tsx')]:
            with self.subTest(path=path), self.assertRaises(FileToolError):
                workspace.execute([{'tool': 'bind_target', 'path': path}])
        self.assertIsNone(selected_path(self.root, 'page', 'register'))

    def test_binding_preserves_owned_paths_and_validates_read_permissions(self):
        workspace = FileTools.for_target(self.root, 'page', 'register', writable=[self.path], readable=[])
        with self.assertRaises(FileToolError):
            workspace.execute([{'tool': 'bind_target', 'path': self.path}])
        self.assertIsNone(selected_path(self.root, 'page', 'register'))

    def test_binding_publication_failure_restores_index_and_task_state(self):
        workspace = self.workspace()
        original_bind = workspace.target.bind
        def fail_after_bind(*args):
            original_bind(*args)
            raise OSError('index publication interrupted')
        with patch.object(workspace.target, 'bind', side_effect=fail_after_bind), self.assertRaises(OSError):
            workspace.execute([{'tool': 'bind_target', 'path': self.path}])
        self.assertTrue(workspace.pending_target)
        self.assertIsNone(selected_path(self.root, 'page', 'register'))

    def test_binding_does_not_steal_another_page_target(self):
        remember_path(self.root, 'page', 'other', self.path)
        with self.assertRaises(FileToolError) as error:
            self.workspace().execute([{'tool': 'bind_target', 'path': self.path}])
        self.assertEqual(error.exception.feedback['code'], 'TARGET_ALREADY_OWNED')
        self.assertEqual(selected_path(self.root, 'page', 'other'), self.path)
        self.assertIsNone(selected_path(self.root, 'page', 'register'))

    def test_three_page_workflow_claims_precreated_stubs_in_one_call_per_page(self):
        login = 'frontend/src/pages/Login.tsx'
        home = 'frontend/src/pages/Home.tsx'
        (self.root / self.path).unlink()
        home_model = Mock(generate_json=Mock(return_value={'tool_calls': [
            {'tool': 'edit_file', 'path': path, 'operation': 'create',
             'new_content': 'export const text = "stub";'} for path in (home, self.path, login)]}))
        run_file_agent(home_model, '', {}, SourceFiles(self.root).workspace(target=('page', 'home')),
            lambda: {'status': 'GREEN'}, 1, Mock(), 'home')
        for identity, path in [('register', self.path), ('login', login)]:
            model = Mock(generate_json=Mock(return_value={'tool_calls': [
                {'tool': 'bind_target', 'path': path},
                {'tool': 'edit_file', 'path': path, 'operation': 'replace',
                 'old_content': 'stub', 'new_content': identity}]}))
            gate = Mock(return_value={'status': 'GREEN'})
            run_file_agent(model, '', {}, SourceFiles(self.root).workspace(target=('page', identity)),
                gate, 1, Mock(), identity)
            gate.assert_called_once()
            model.generate_json.assert_called_once()
            self.assertEqual(selected_path(self.root, 'page', identity), path)
