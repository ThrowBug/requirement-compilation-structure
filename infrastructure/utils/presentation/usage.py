"""Content-free usage records, independent of verbose model logging."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
from time import monotonic


_task = ContextVar("model_usage_task", default=None)
TOKEN_KEYS = ("Input", "Cached input", "Output", "Reasoning", "Non-reasoning output", "Total")


@contextmanager
def model_task(kind, label, attempt):
    """Attach stage-owned task identity without inserting it into model prompts."""
    token = _task.set({"kind": kind, "label": label, "attempt": attempt})
    try:
        yield
    finally:
        _task.reset(token)


def task_info():
    return dict(_task.get() or {})


def count_event(name):
    from infrastructure.utils.presentation.logging import session_info
    session = session_info()
    if session is not None:
        events = session.setdefault("usage_events", {})
        events[name] = events.get(name, 0) + 1


def request_sizes(arguments):
    """Measure serialized regions, never persist their text or credentials."""
    regions = {}
    for index, message in enumerate(arguments.get("messages", [])):
        content = message.get("content", "")
        encoded = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        regions[str(index)] = {"role": message.get("role"), "characters": len(encoded),
            "utf8_bytes": len(encoded.encode("utf-8"))}
        if isinstance(content, str) and message.get("role") == "user":
            try:
                payload = json.loads(content)
            except ValueError:
                continue
            if isinstance(payload, dict):
                regions[str(index)]["sections"] = {key: len(json.dumps(value, ensure_ascii=False,
                    separators=(",", ":")).encode("utf-8")) for key, value in payload.items()}
    return regions


def usage_snapshot(status):
    """Unknown usage stays null; report counts expose partial provider coverage."""
    from infrastructure.utils.presentation.logging import session_info
    session = session_info()
    if session is None:
        return None
    stats = session.get("model_stats", {})
    return {"run_id": session["run_id"], "stage_run_id": session["stage_run_id"],
        "stage": session["stage"], "started_at": session["started_at"], "status": status,
        "elapsed_seconds": monotonic() - session["started"],
        "sdk_calls": stats.get("calls", 0), "sdk_failures": stats.get("failed", 0),
        "http_attempts": session.get("http_attempts", 0),
        "model_seconds": stats.get("seconds", 0),
        "tokens": {key: {"reported_sum": stats.get(key),
            "reports": stats.get(key + " reports", 0)} for key in TOKEN_KEYS},
        "events": dict(session.get("usage_events", {})),
        "calls": list(session.get("usage_calls", []))}


def persist_usage(status):
    """Append one stage record even when detailed request logging is disabled."""
    from infrastructure.utils.presentation.logging import session_info
    session = session_info()
    record = usage_snapshot(status)
    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    directory = session["output"] / ".arc"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "model_usage.jsonl").open("a", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(record, ensure_ascii=False) + "\n")
