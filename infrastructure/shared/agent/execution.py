"""Shared task scopes for real and mock model calls, independent of model clients."""

from functools import wraps
from inspect import signature
from time import monotonic

from infrastructure.shared.agent.context_broker import ContextBroker, TaskCard
from infrastructure.shared.agent.experiments import consume, model_attempt_scope, options, task_budget
from infrastructure.shared.usage.recorder import current_recorder, current_scope, record_event, usage_scope
from infrastructure.shared.project.resume import current_resume, track_task_queries


def agent_task(kind):
    def decorate(function):
        parameters = signature(function)
        @wraps(function)
        def run(*args, **kwargs):
            bound = parameters.bind(*args, **kwargs)
            bound.apply_defaults()
            arguments = bound.arguments
            with usage_scope(task=bound.arguments["label"], call_type=kind), task_budget():
                try:
                    manager, key = current_resume(), None
                    if kind == "file" and manager:
                        scope, files = current_scope(), arguments["files"]
                        key = manager.file_key(scope.get("stage", ""), arguments["label"], arguments["task"], arguments["instructions"], files)
                        prior = manager.file_result(key, files, arguments.get("context_index"))
                        if prior is not None and arguments.get("feedback") is None:
                            try:
                                prior["checks"] = arguments["gate"]()
                                record_event("file_task_reused", revalidated=True)
                                record_event("task_result", status="GREEN")
                                return prior
                            except (ValueError, RuntimeError, OSError) as error:
                                record_event("file_task_reuse_rejected")
                                bound.arguments["feedback"] = {"error": str(error), "source": "resume_acceptance"}
                    with track_task_queries() as queries:
                        result = function(*bound.args, **bound.kwargs)
                    if key:
                        manager.save_file(key, arguments["files"], result, queries)
                        # Binding a new file target changes pinned paths; save its accepted identity too.
                        final_key = manager.file_key(current_scope().get("stage", ""), arguments["label"], arguments["task"], arguments["instructions"], arguments["files"])
                        if final_key != key:
                            manager.save_file(final_key, arguments["files"], result, queries)
                except BaseException as error:
                    confirm_response(arguments["model"], False)
                    record_event("task_result", status="INTERRUPTED" if isinstance(error, KeyboardInterrupt) else "FAILED")
                    raise
                record_event("task_result", status="GREEN")
                return result
        return run
    return decorate


def call_model(model, instructions, payload, *, broker=None, call_type=None):
    consume("calls")
    scope = current_scope()
    task = payload.get("task")
    requirement = payload.get("requirement", task.get("requirement", {}) if isinstance(task, dict) else {})
    rid = requirement.get("id") if isinstance(requirement, dict) else None
    card = TaskCard(stage=scope.get("stage", ""), task=scope.get("task", ""),
        requirement_ids=(rid,) if rid else (), allowed_paths=tuple(payload.get("files", {})),
        allowed_queries=tuple(payload.get("context_kinds", ())),
        required_artifacts=tuple(key for key in ("ui", "backend_apis", "retrieved_schema", "shared_conventions") if key in payload),
        must_preserve_evidence=tuple(key for key in ("requirement", "feedback", "correction", "read_files") if key in payload),
        acceptance=("stage-owned acceptance",), budget={"max_calls": options().max_task_calls,
            "max_read_characters": options().max_read_characters})
    request = (broker or ContextBroker()).build_task_context(
        card, payload)
    recorder = current_recorder()
    before = len(recorder.data["calls"]) if recorder else 0
    before_events = len(recorder.data["events"]) if recorder else 0
    record_event("model_request")
    started, status = monotonic(), "OK"
    try:
        with usage_scope(call_type=call_type or scope.get("call_type", "model")), model_attempt_scope():
            return model.generate_json(instructions, request)
    except BaseException:
        status = "FAILED"
        raise
    finally:
        cached = recorder and any(event["event"] == "response_cache_hit" for event in recorder.data["events"][before_events:])
        if recorder and len(recorder.data["calls"]) == before and not cached:
            # Mock/custom clients may not pass through the SDK audit boundary.
            recorder.call(model=str(getattr(model, "name", type(model).__name__)),
                duration_seconds=monotonic() - started, status=status,
                call_type=call_type or scope.get("call_type", "model"))


def confirm_response(model, accepted):
    method = getattr(type(model), "accept_response" if accepted else "reject_response", None)
    if method:
        method(model)
