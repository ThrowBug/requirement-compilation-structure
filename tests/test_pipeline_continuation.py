"""All real passes reach a final report after one schema task exhausts retries."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from importlib import import_module
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import httpx2

import main
from infrastructure.compiler import Compiler
from infrastructure.shared.models import ProviderTransportUnavailable
from infrastructure.shared.models.vision import VisualModel


class PipelineContinuation(unittest.TestCase):
    def test_exhausted_503_stops_compilation_before_another_task_or_pass(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'input'
            source.mkdir()
            (source / 'requirements.yaml').write_text('id: ROOT\nchildren:\n  - id: FIRST\n  - id: SECOND\n')
            compiler = Compiler(source, root / 'application')
            handler = Mock(side_effect=lambda _: httpx2.Response(503, json={'error': {'message': 'Unavailable'}}))
            with httpx2.Client(transport=httpx2.MockTransport(handler)) as client, \
                    patch.dict('os.environ', {'OPENAI_API_KEY': 'mock-key', 'OPENAI_BASE_URL': 'https://example.invalid'}), \
                    patch('infrastructure.shared.models.text.http_client', return_value=client), \
                    patch('openai._base_client.time.sleep'), patch.object(main, 'Compiler', return_value=compiler), \
                    redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as exit_error:
                    main.main([str(source), '-o', str(compiler.output_dir)])
            self.assertEqual(exit_error.exception.code, 1)
            self.assertEqual(handler.call_count, 6)
            report = json.loads((compiler.arc_dir / 'compilation.json').read_text(encoding='utf-8'))
            self.assertEqual(report['status'], 'FAILED')
            self.assertEqual(report['stages']['02_build_db_schema']['error_type'], 'ProviderTransportUnavailable')
            self.assertNotIn('03_infer_db_obligations', report['stages'])

    def test_vision_request_also_retries_503_five_times(self):
        handler = Mock(side_effect=lambda _: httpx2.Response(503, json={'error': {'message': 'Unavailable'}}))
        with httpx2.Client(transport=httpx2.MockTransport(handler)) as client, \
                patch.dict('os.environ', {'VISUAL_API_KEY': 'mock-key', 'VISUAL_BASE_URL': 'https://example.invalid'}), \
                patch('infrastructure.shared.models.vision.http_client', return_value=client), \
                patch('openai._base_client.time.sleep'), redirect_stdout(StringIO()):
            with self.assertRaises(ProviderTransportUnavailable):
                VisualModel().generate_json('Analyze the image', {'image_url': 'data:image/png;base64,AA=='})
        self.assertEqual(handler.call_count, 6)

    def test_real_passes_continue_after_a_design_failure_without_fabricating_obligations(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'input'
            source.mkdir()
            (source / 'requirements.yaml').write_text(
                'id: ROOT\nchildren:\n  - id: BAD\n    name: Invalid proposal\n'
                '  - id: GOOD\n    name: Static module\n    description: No persistent data or pages.\n')
            compiler = Compiler(source, root / 'application')
            calls = []
            def schema_proposal(_, request):
                rid = request['requirement']['id']
                calls.append(rid)
                return {} if rid == 'BAD' else {'entities': {}, 'related_fields': []}
            with ExitStack() as stack, redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                stack.enter_context(patch.object(import_module('02_build_db_schema'), 'TextModel',
                    return_value=Mock(generate_json=schema_proposal)))
                stack.enter_context(patch.object(import_module('03_infer_db_obligations'), 'TextModel',
                    return_value=Mock(generate_json=Mock(return_value={
                        'requirement': {'read': [], 'write': []}, 'steps': {}}))))
                stack.enter_context(patch.object(import_module('09_design_ui'), 'TextModel',
                    return_value=Mock(generate_json=Mock(return_value={
                        'pages': {}, 'elements': {}, 'bindings': {}, 'requirement_ui': [], 'step_ui': {}}))))
                stack.enter_context(patch.object(import_module('05_implement_db'), 'verify_database_runtime',
                    return_value={'status': 'GREEN'}))
                compiler.compile()
            self.assertEqual(calls, ['BAD'] * 10 + ['GOOD'])
            self.assertEqual(len(compiler.report['stages']), 13)
            self.assertEqual(compiler.report['status'], 'COMPLETED_WITH_FAILURES')
            self.assertEqual(compiler.report['stages']['09_design_ui']['status'], 'GREEN')
            self.assertIn('12_implement_frontend_tdd', compiler.report['stages'])
            obligations = json.loads((compiler.arc_dir / 'db_obligations.json').read_text())
            self.assertNotIn('BAD', obligations['requirements'])
            self.assertEqual(obligations['requirements']['GOOD'], {'read': [], 'write': []})
            self.assertIsNone(compiler.current_stage)
