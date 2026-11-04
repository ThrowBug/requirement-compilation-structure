"""Best-effort, atomic telemetry; missing provider usage is never invented."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from uuid import uuid4

from infrastructure.utils.files import save_json


_active = ContextVar("usage_recorder", default=None)
_scope = ContextVar("usage_scope", default={})


def current_recorder():
    return _active.get()


def current_scope():
    return _scope.get()


@contextmanager
def usage_scope(**values):
    token = _scope.set({**_scope.get(), **values})
    try:
        yield
    finally:
        _scope.reset(token)


class UsageRecorder:
    def __init__(self, output, metadata=None):
        self.path = Path(output) / ".arc/usage/usage.json"
        self.started = monotonic()
        self.data = {"schema_version": 1, "run_id": uuid4().hex,
            "started_at": datetime.now(timezone.utc).isoformat(), "metadata": metadata or {},
            "calls": [], "events": [], "stages": {}, "status": "RUNNING"}
        self.write_error = None

    @contextmanager
    def activate(self):
        token = _active.set(self)
        self.flush()
        try:
            yield self
        except BaseException as error:
            self.finish("INTERRUPTED" if isinstance(error, KeyboardInterrupt) else "FAILED")
            raise
        finally:
            self.flush()
            _active.reset(token)

    def call(self, *, model, duration_seconds, status, usage=None, **details):
        usage = usage or {}
        def integer(*values):
            return next((v for v in values if type(v) is int), None)
        input_details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
        output_details = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
        record = {**current_scope(), "model": model, "duration_seconds": duration_seconds,
            "status": status, "usage_available": bool(usage),
            "input_tokens": integer(usage.get("prompt_tokens"), usage.get("input_tokens")),
            "cached_input_tokens": integer(input_details.get("cached_tokens"), usage.get("prompt_cache_hit_tokens")),
            "output_tokens": integer(usage.get("completion_tokens"), usage.get("output_tokens")),
            "reasoning_tokens": integer(output_details.get("reasoning_tokens")), **details}
        self.data["calls"].append(record)
        self.flush()

    def event(self, event, **details):
        self.data["events"].append({**current_scope(), "event": event, **details})
        self.flush()

    def stage_result(self, stage, status):
        self.data["stages"][stage] = {"status": status}
        self.flush()

    def finish(self, status):
        self.data["status"] = status
        self.flush()

    def flush(self):
        calls = self.data["calls"]
        totals = {"calls": len(calls), "failed_calls": sum(c["status"] != "OK" for c in calls),
            "model_seconds": sum(c["duration_seconds"] for c in calls)}
        for name in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens"):
            reported = [c[name] for c in calls if c[name] is not None]
            totals[name] = sum(reported) if reported or not calls else None
            totals[name + "_reports"] = len(reported)
        totals["total_tokens"] = (totals["input_tokens"] + totals["output_tokens"]
            if totals["input_tokens"] is not None and totals["output_tokens"] is not None else None)
        totals["transport_attempts"] = sum(c.get("transport_attempts", 1) for c in calls)
        totals["usage_complete"] = all(c["input_tokens"] is not None and c["output_tokens"] is not None
            and c.get("transport_attempts", 1) == 1 for c in calls)
        tasks = {}
        for call in calls:
            stage, task = call.get("stage", "unknown"), call.get("task", "stage")
            entry = tasks.setdefault(stage, {}).setdefault(task, {"calls": 0, "failed_calls": 0, "duration_seconds": 0,
                "input_tokens": None, "output_tokens": None, "cached_input_tokens": None, "reasoning_tokens": None})
            entry["calls"] += 1
            entry["failed_calls"] += int(call["status"] != "OK")
            entry["duration_seconds"] += call["duration_seconds"]
            for name in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens"):
                if call[name] is not None:
                    entry[name] = (entry[name] or 0) + call[name]
        for event in self.data["events"]:
            entry = tasks.setdefault(event.get("stage", "unknown"), {}).setdefault(event.get("task", "stage"), {})
            name = event["event"]
            if name in {"context_query", "read_file", "edit_round", "http_attempt", "model_request", "response_cache_hit"}:
                entry[name] = entry.get(name, 0) + 1
            if name == "context_query":
                entry["duplicate_context_results"] = entry.get("duplicate_context_results", 0) + int(event.get("duplicate_result", False))
                entry["duplicate_records"] = entry.get("duplicate_records", 0) + event.get("duplicate_records", 0)
            if name in {"context_query", "read_file"}:
                entry[name + "_characters"] = entry.get(name + "_characters", 0) + event.get("characters", 0)
            if name == "serialized_context":
                for key in ("original_characters", "sent_characters"):
                    entry[key] = entry.get(key, 0) + event[key]
            if name == "task_result":
                entry["status"] = event["status"]
        self.data["tasks"] = tasks
        self.data.update(totals=totals, total_duration_seconds=monotonic() - self.started)
        try:
            save_json(self.path, self.data)
        except (OSError, ValueError, TypeError) as error:
            # Observability must never replace the exception from compilation.
            self.write_error = str(error)


def record_event(event, **details):
    if recorder := current_recorder():
        recorder.event(event, **details)
