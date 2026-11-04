"""Retry model proposals when parsing or consuming their returned records fails."""

import json

from infrastructure.shared.agent.discovery import DISCOVERY_RULES, DiscoverySession, DiscoveryStalled, context_query
from infrastructure.shared.project.git import checkpoint
from infrastructure.shared.agent.context_broker import ContextBroker
from infrastructure.shared.agent.execution import agent_task, call_model, confirm_response
from infrastructure.shared.agent.failures import observe_failure, repair_advice
from infrastructure.shared.agent.experiments import deterministic_proposal, options
from infrastructure.shared.project.resume import current_resume
from infrastructure.shared.usage.recorder import current_scope, record_event


RESPONSE_ERRORS = (ValueError, KeyError, TypeError, AttributeError, IndexError)
REPAIR_INSTRUCTIONS = """The previous proposal was rejected. Read correction.error and correction.details
in the input and fix that failure. correction.previous_response is the rejected
answer, not an accepted example. Return a complete replacement JSON object in the
original output format, including every required top-level section. Do not return
only the unchanged portion, a partial patch, or the correction metadata itself.
DSML/XML tool-call text is forbidden. If requesting context, return the allowed
context_request JSON object directly, never invoke/parameter tags. Otherwise return
the complete stage result. invalid_response is rejected data; never copy its format."""


def response_feedback(error, response):
    """Describe an actual response-consumption failure, without adding design rules."""
    message = getattr(error, "model_message", None) or str(error)
    if isinstance(error, KeyError):
        key = error.args[0] if error.args else "(unknown)"
        message = f"Missing key {key!r} while reading the model response. Return the requested complete records."
    if isinstance(error, json.JSONDecodeError):
        message += (" Output must be one valid JSON object, without DSML/XML tool-call tags, "
            "Markdown or text outside the object. For an allowed context query return "
            '{"context_request":{"kind":"<advertised kind>","query":"<needed fact>"}}. '
            "Otherwise return the stage result as JSON. Do not repeat the rejected format.")
    feedback = {"error": message, "error_type": type(error).__name__, "previous_response": response}
    if isinstance(error, json.JSONDecodeError):
        feedback["invalid_response"] = error.doc
    if hasattr(error, "feedback"):
        details = error.feedback
        feedback["details"] = details
        if not isinstance(details, dict):
            return feedback
        calls = response.get("tool_calls") if isinstance(response, dict) else None
        index = details.get("call_index")
        if (details.get("code") == "WHOLE_FILE_REPLACEMENT_NOT_ALLOWED"
                and isinstance(calls, list) and isinstance(index, int) and 1 <= index <= len(calls)):
            # Keep the offending operation and other batch calls without echoing two rejected bodies.
            feedback["previous_response"] = {**response, "tool_calls": [
                {key: value for key, value in call.items() if key not in {"old_content", "new_content"}}
                if i == index and isinstance(call, dict) else call for i, call in enumerate(calls, 1)]}
    return feedback


@agent_task("design")
def propose_json(model, instructions, request, accept, console, label, max_attempts=10, context_index=None,
        repair_fields=()):
    """Generate and repair a proposal within a finite total model-call budget."""
    if max_attempts < 1:
        raise ValueError("--max-attempts must be positive")
    request = dict(request)
    broker = ContextBroker(context_index)
    if context_index is not None:
        context_index = broker
    resume = current_resume()
    scope = current_scope()
    key = resume.design_key(scope.get("stage", ""), label, instructions,
        {"request": request, "collections": broker.records()}) if resume else None
    replay = resume.design_proposal(key) if resume else None
    deterministic = deterministic_proposal(scope.get("stage", ""), label.split()[-1])
    for source, proposal in (("resume", replay), ("deterministic", deterministic)):
        if proposal is not None:
            from copy import deepcopy
            try:
                accepted = accept(deepcopy(proposal))
                record_event("design_replay", source=source)
                return accepted, proposal
            except RESPONSE_ERRORS:
                record_event("design_replay_rejected", source=source)
    if context_index is not None:
        instructions += "\n\n" + DISCOVERY_RULES
        request["context_kinds"] = list(context_index.records())
    discovery = DiscoverySession(context_index) if context_index is not None else None
    completion = None
    for attempt in range(1, max_attempts + 1):
        progress = f"{attempt}/{max_attempts}"
        console.show(f"{label} | Model proposal {progress}", "Generate the proposal; response-reading and obligation errors receive repair feedback.")
        response = None
        try:
            prompt = instructions if "correction" not in request else instructions + "\n\n" + REPAIR_INSTRUCTIONS
            if completion:
                field, _ = completion
                prompt = ("OUTPUT MODE: MISSING-FIELD REPAIR. "
                    f"Complete correction.previous_response by returning a JSON object with only {field!r}. "
                    "The compiler merges it with the previous proposal and checks the complete result. " \
                    "If another section must change, return a complete replacement instead. " \
                    "The stage format below describes that complete proposal. "
                    "You may still request advertised context before answering.\n\n" + instructions)
            response = call_model(model, prompt, request, broker=broker,
                call_type="design_repair" if "correction" in request else "design")
            stalled = False
            while isinstance(response, dict) and "context_request" in response:
                query = context_query(response, enabled=context_index is not None)
                console.show(label + " | Context query", json.dumps(query, ensure_ascii=False))
                try:
                    request["context_result"] = discovery.resolve(query)
                    stalled = False
                except DiscoveryStalled as error:
                    if stalled:
                        raise
                    stalled = True
                    request["context_result"] = {"error": str(error),
                        "next_action": "Stop repeating this query; read different evidence or return the normal stage output."}
                except (ValueError, KeyError, TypeError, AttributeError, IndexError) as error:
                    if stalled:
                        raise
                    stalled = True
                    request["context_result"] = {"error": str(error),
                        "next_action": "Read different advertised evidence or return the normal stage output."}
                request["context_records"] = discovery.evidence
                request["context_candidates"] = discovery.candidates
                response = call_model(model, prompt, request, broker=broker, call_type="query_continue")
            if completion and isinstance(response, dict) and set(response) == {completion[0]}:
                response = {**completion[1], **response}
            accepted = accept(response)
            confirm_response(model, True)
            if resume:
                resume.save_design(key, response)
            checkpoint(task=label, action=f"Accept design proposal {attempt}",
                details={"attempt": attempt, "sections": list(response) if isinstance(response, dict) else []})
            return accepted, response
        except RESPONSE_ERRORS as error:
            confirm_response(model, False)
            failure = observe_failure(error, round=attempt, will_retry=attempt < max_attempts, phase="protocol")
            feedback = response_feedback(error, response)
            completion = None
            if (isinstance(error, KeyError) and error.args and error.args[0] in repair_fields
                    and isinstance(response, dict) and error.args[0] not in response):
                completion = (error.args[0], response)
                feedback["error"] = f"Missing {error.args[0]!r}; supply that field or a complete corrected proposal."
            if options().failure_advice:
                feedback["repair_advice"] = repair_advice(failure)
            feedback["failed_attempt"] = attempt
            checkpoint(task=label, action=f"Reject design proposal {attempt}", status="REJECTED", details=feedback)
            console.show(f"{label} | Repair feedback", json.dumps(feedback, ensure_ascii=False, indent=2))
            if attempt >= max_attempts:
                raise RuntimeError(f"{label} exhausted {max_attempts} proposals: {feedback['error']}") from error
            request["correction"] = feedback
