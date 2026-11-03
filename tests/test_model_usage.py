"""Usage survives real SDK retries, proposal repair, and stage failure without prompts."""

from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx
from openai import OpenAI

from infrastructure.compiler import Compiler
from infrastructure.shared.agent.proposals import propose_json
from infrastructure.shared.agent.loop import run_file_agent
from infrastructure.shared.agent.files import FileWorkspace
from infrastructure.shared.models.text import TextModel
from infrastructure.utils.presentation.logging import console_log, session_info
from infrastructure.utils.presentation.model_io import _http_request, _http_response
from infrastructure.utils.presentation.usage import model_task


class ModelUsage(unittest.TestCase):
    def model(self, handler):
        client = httpx.Client(transport=httpx.MockTransport(handler),
            event_hooks={"request": [_http_request], "response": [_http_response]})
        self.addCleanup(client.close)
        model = TextModel.__new__(TextModel)
        model.client = OpenAI(api_key="test-secret", base_url="https://model.test/v1",
            http_client=client, max_retries=1)
        self.addCleanup(model.client.close)
        model.name, model.max_output_tokens = "test-model", 1024
        model._token_parameter = "max_completion_tokens"
        return model

    def response(self, content='{"ok":true}', usage=True):
        body = {"id": "test", "object": "chat.completion", "created": 0, "model": "test-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}
        if usage:
            body["usage"] = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30,
                "prompt_tokens_details": {"cached_tokens": 8}, "completion_tokens_details": {"reasoning_tokens": 3}}
        return body

    def records(self, root):
        return [json.loads(line) for line in (root / ".arc/model_usage.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_sdk_retry_and_parameter_fallback_are_distinct_without_verbose_logging(self):
        attempts = []
        def handler(request):
            attempts.append(json.loads(request.content))
            if len(attempts) == 1:
                return httpx.Response(500, headers={"retry-after-ms": "1"}, json={"error": {"message": "retry"}})
            if "max_completion_tokens" in attempts[-1]:
                return httpx.Response(400, json={"error": {"message": "Unsupported parameter max_completion_tokens"}})
            return httpx.Response(200, json=self.response())
        model = self.model(handler)
        with TemporaryDirectory() as temporary, redirect_stdout(StringIO()):
            root = Path(temporary)
            with console_log(root, model_log=False, run_id="run-1"):
                with model_task("implementation", "API-login", 1):
                    model.generate_json("private instruction", {"files": {"a.ts": "private source"}})
            record, = self.records(root)
            self.assertFalse((root / "model_calls.jsonl").exists())
            serialized = json.dumps(record)
        self.assertEqual(record["run_id"], "run-1")
        self.assertEqual((record["sdk_calls"], record["sdk_failures"], record["http_attempts"]), (2, 1, 3))
        self.assertEqual(record["tokens"]["Total"], {"reported_sum": 30, "reports": 1})
        self.assertEqual(record["tokens"]["Cached input"]["reported_sum"], 8)
        self.assertEqual(record["tokens"]["Reasoning"]["reported_sum"], 3)
        self.assertEqual(record["calls"][1]["task"]["label"], "API-login")
        self.assertIn("sections", record["calls"][1]["request_regions"]["1"])
        for secret in ("private instruction", "private source", "test-secret"):
            self.assertNotIn(secret, serialized)

    def test_missing_usage_is_unknown_and_rejected_json_is_not_a_network_failure(self):
        contents = iter(["{broken", '{"ok":true}'])
        model = self.model(lambda request: httpx.Response(200, json=self.response(next(contents), usage=False)))
        with TemporaryDirectory() as temporary, redirect_stdout(StringIO()):
            root = Path(temporary)
            with console_log(root, model_log=False):
                propose_json(model, "Design", {}, lambda value: value["ok"], Mock(), "REQ-A")
            record, = self.records(root)
        self.assertEqual(record["tokens"]["Input"], {"reported_sum": None, "reports": 0})
        self.assertEqual(record["sdk_failures"], 0)
        self.assertEqual(record["events"], {"proposal_attempts": 2, "proposal_rejections": 1})
        self.assertEqual(record["calls"][1]["task"]["kind"], "proposal_repair")

    def test_gate_failures_and_edit_rejections_are_separate(self):
        with TemporaryDirectory() as temporary, redirect_stdout(StringIO()):
            root = Path(temporary)
            (root / "target.ts").write_text("old", encoding="utf-8")
            workspace = FileWorkspace(root, ["target.ts"])
            model = Mock(generate_json=Mock(side_effect=[{}, {"edits": []}, {"edits": []}]))
            gate = Mock(side_effect=[ValueError("assertion failed"), {"status": "GREEN"}])
            with console_log(root, model_log=False):
                run_file_agent(model, "Repair", {}, workspace, gate, 3, Mock(), "Page")
            record, = self.records(root)
        self.assertEqual(record["events"], {"edit_rounds": 3, "edit_rejections": 1,
            "repair_rounds": 2, "acceptance_failures": 1})

    def test_compiler_preserves_metrics_for_success_failure_and_interrupt(self):
        for outcome in ("GREEN", "COMPLETED_WITH_FAILURES", ValueError("bad"), KeyboardInterrupt()):
            with self.subTest(outcome=type(outcome).__name__), TemporaryDirectory() as temporary, redirect_stdout(StringIO()):
                root = Path(temporary)
                model = self.model(lambda request: httpx.Response(200, json=self.response()))
                compiler = Compiler(root / "input", root / "out")
                def stage():
                    model.generate_json("Generate", {})
                    if isinstance(outcome, BaseException):
                        raise outcome
                    return {"status": outcome}
                with patch("infrastructure.compiler.import_module", return_value=SimpleNamespace(run=stage)):
                    if isinstance(outcome, BaseException):
                        with self.assertRaises(type(outcome)):
                            compiler._run("stage", "run")
                    else:
                        compiler._run("stage", "run")
                saved = json.loads((root / "out/.arc/compilation.json").read_text(encoding="utf-8"))
                record, = self.records(root / "out")
                expected = ("INTERRUPTED" if isinstance(outcome, KeyboardInterrupt) else "FAILED") if isinstance(outcome, BaseException) else outcome
                self.assertEqual(record["status"], expected)
                self.assertEqual(saved["stages"]["stage"]["model_usage"]["sdk_calls"], 1)
                self.assertEqual(record["tokens"]["Input"]["reported_sum"], 10)
                self.assertIsNone(session_info())

    def test_usage_write_failure_does_not_mask_original_error(self):
        with TemporaryDirectory() as temporary, redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            with patch("infrastructure.utils.presentation.usage.persist_usage", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(ValueError, "original"):
                    with console_log(Path(temporary), model_log=False):
                        raise ValueError("original")
        self.assertIsNone(session_info())
