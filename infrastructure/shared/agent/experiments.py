"""Explicit opt-in policies; default execution keeps existing model and budgets."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import re
from pathlib import Path

from infrastructure.shared.artifacts.dependency_graph import digest
from infrastructure.utils.files import save_json


@dataclass(frozen=True)
class OptimizationOptions:
    context_dedup: bool = False
    max_task_calls: int | None = None
    max_read_characters: int | None = None
    small_model: str | None = None
    small_model_tasks: tuple = ()
    cache_dir: str | None = None
    deterministic_schema: str | None = None
    failure_advice: bool = False

    def __post_init__(self):
        for name in ("max_task_calls", "max_read_characters"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(name + " must be a positive integer")
        if self.small_model and not self.small_model_tasks:
            raise ValueError("Small-model routing requires explicit task labels")


_options = ContextVar("optimization_options", default=OptimizationOptions())
_budget = ContextVar("optimization_budget", default=None)
_sdk_attempts = ContextVar("optimization_sdk_attempts", default=None)


def options():
    return _options.get()


@contextmanager
def optimization_scope(value):
    token = _options.set(value)
    try:
        yield
    finally:
        _options.reset(token)


@contextmanager
def task_budget():
    token = _budget.set({"calls": 0, "read_characters": 0})
    try:
        yield
    finally:
        _budget.reset(token)


def consume(name, amount=1):
    budget = _budget.get()
    if budget is None:
        return
    limit = getattr(options(), "max_task_calls" if name == "calls" else "max_read_characters")
    if limit is not None and budget[name] + amount > limit:
        raise ValueError(f"Experimental task {name} budget exhausted ({limit})")
    budget[name] += amount


@contextmanager
def model_attempt_scope():
    token = _sdk_attempts.set({"invocations": 0})
    try:
        yield
    finally:
        _sdk_attempts.reset(token)


def provider_attempt():
    state = _sdk_attempts.get()
    if state is None or state["invocations"]:
        consume("calls")
    if state is not None:
        state["invocations"] += 1


class ResponseCache:
    """Cache validated JSON only; identity includes endpoint, model, controls and input."""
    def __init__(self, directory):
        self.directory = Path(directory)

    def key(self, **identity):
        return digest(identity)

    def get(self, key):
        try:
            record = json.loads((self.directory / (key + ".json")).read_text(encoding="utf-8"))
            if isinstance(record, dict) and record.get("key") == key and isinstance(record.get("response"), dict):
                return record["response"]
        except (ValueError, OSError):
            pass
        return None

    def put(self, key, response):
        if not isinstance(response, dict):
            return
        try:
            save_json(self.directory / (key + ".json"), {"key": key, "response": response})
        except OSError:
            pass


def deterministic_proposal(stage, task):
    """Only explicitly reviewed, complete simple schema patches bypass generation."""
    path = options().deterministic_schema
    if not path or stage != "02_build_db_schema":
        return None
    try:
        proposal = json.loads(Path(path).read_text(encoding="utf-8")).get(task)
        if not isinstance(proposal, dict) or set(proposal) - {"entities", "related_fields"} or "entities" not in proposal:
            return None
        if not isinstance(proposal["entities"], dict):
            return None
        result = {"entities": {}, "related_fields": []}
        for name, entity in proposal["entities"].items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or not isinstance(entity, dict):
                return None
            if set(entity) - {"description", "fields"} or not isinstance(entity.get("fields"), dict) or not entity["fields"]:
                return None
            canonical = {"description": entity.get("description", name), "fields": {}}
            if not isinstance(canonical["description"], str):
                return None
            for field, declaration in entity["fields"].items():
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field):
                    return None
                definition = {"type": declaration} if isinstance(declaration, str) else declaration
                if not isinstance(definition, dict) or set(definition) - {"description", "type", "nullable", "primary_key", "unique", "references", "constraints"}:
                    return None
                if definition.get("type") not in {"string", "integer", "number", "boolean", "date", "datetime", "uuid", "json"}:
                    return None
                if definition.get("references") is not None or definition.get("constraints", []) != []:
                    return None  # Relationships/domain constraints use the model path.
                generated = {"description": definition.get("description", field), "type": definition["type"],
                    "nullable": definition.get("nullable", False), "primary_key": definition.get("primary_key", False),
                    "unique": definition.get("unique", definition.get("primary_key", False)), "references": None, "constraints": []}
                if not isinstance(generated["description"], str) or any(type(generated[k]) is not bool for k in ("nullable", "primary_key", "unique")):
                    return None
                canonical["fields"][field] = generated
                result["related_fields"].append(name + "." + field)
            result["entities"][name] = canonical
        result["related_fields"] = proposal.get("related_fields", result["related_fields"])
        if not isinstance(result["related_fields"], list) or any(not isinstance(item, str) for item in result["related_fields"]):
            return None
        return result
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
