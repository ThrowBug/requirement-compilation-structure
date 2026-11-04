"""Conservative failure classification, separate from acceptance and retry limits."""

from dataclasses import asdict, dataclass
import json
import re

from infrastructure.shared.usage.recorder import current_scope, record_event


@dataclass
class Failure:
    failure_type: str
    message: str
    file: str | None = None
    line: int | None = None
    confidence: str = "confirmed"


class FailureClassifier:
    def classify(self, error, *, expected_red=False, phase=None):
        message = str(error)
        feedback = getattr(error, "feedback", {})
        evidence = message + "\n" + json.dumps(feedback, ensure_ascii=False, default=str)
        text = evidence.lower()
        match = re.search(r"([\w./\\-]+\.(?:tsx?|jsx?|sql))[:(](\d+)", evidence)
        location = {"file": match[1], "line": int(match[2])} if match else {}
        if expected_red and ("501" in text or "not implemented" in text):
            kind = "ExpectedRed"
        elif type(error).__name__ == "DiscoveryStalled":
            kind = "NoProgress"
        elif isinstance(error, json.JSONDecodeError) or (feedback.get("code") if isinstance(feedback, dict) else None) in {
                "INVALID_CALL", "INVALID_CALLS", "UNKNOWN_TOOL", "INVALID_ARGUMENTS", "MIXED_TOOL_BATCH"}:
            kind = "ProtocolError"
        elif isinstance(error, (PermissionError, ModuleNotFoundError)) or any(s in text for s in (
                "no space left on device", "enospc", "npm is not installed", "node is not installed")):
            kind = "EnvironmentError"
        elif "coverageerror" in type(error).__name__.lower() or any(s in text for s in (
                "obligation", "undefined database fields", "upstream backend contract")):
            kind = "ContractError"
        elif re.search(r"\berror ts\d+\b", text) or any(s in text for s in (
                "syntaxerror", "sqlite_error", "cannot find module", "compilation failed", "typecheck")):
            kind = "CompileError"
        elif phase == "protocol" and isinstance(error, (KeyError, TypeError, ValueError)):
            kind = "ProtocolError"
        elif any(s in text for s in ("assertion", "expected", "test failed", "functional integration")):
            kind = "TestFailure"
        else:
            kind = "Unknown"
        return Failure(kind, message, **location, confidence="unknown" if kind == "Unknown" else "confirmed")


def observe_failure(error, *, round=None, will_retry=True, stage=None, task=None, **options):
    failure = FailureClassifier().classify(error, **options)
    record_event("failure", **asdict(failure), round=round, will_retry=will_retry,
        stage=stage or current_scope().get("stage", "unknown"), task=task or current_scope().get("task", "stage"))
    return failure


def repair_advice(failure):
    """Advice only; cannot grant permissions, alter a gate, or roll back artifacts."""
    return {
        "ProtocolError": "Correct the indicated JSON fields using the advertised protocol.",
        "CompileError": "Read the named source within the allowed paths and make a local correction.",
        "ContractError": "Preserve canonical definitions; identify the upstream contract requiring review.",
        "EnvironmentError": "Check the confirmed dependency/permission failure before retrying code changes.",
        "NoProgress": "Use different authorized evidence or return the task output.",
        "TestFailure": "Compare the requirement, expected value and actual value; repair the application.",
    }.get(failure.failure_type, "Use the existing repair path and retain the complete failure evidence.")
