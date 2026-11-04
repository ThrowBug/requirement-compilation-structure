"""One context boundary for all design and file tasks, with reversible interning."""

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json

from infrastructure.shared.usage.recorder import record_event


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


REFERENCE_RULES = """Repeated context objects may be represented by {\"$context_ref\":\"/path\"}.
These are JSON Pointer references to the identical complete object elsewhere in the
same input. Resolve them before reasoning; references never mean absent evidence."""


def has_context_marker(node):
    if isinstance(node, dict):
        return "$context_ref" in node or any(has_context_marker(v) for v in node.values())
    return isinstance(node, list) and any(has_context_marker(v) for v in node)


def intern_context(value):
    """Intern identical nontrivial objects. User objects containing our marker opt out."""
    if has_context_marker(value):
        return deepcopy(value)
    seen = {}
    def visit(node, path):
        if isinstance(node, (dict, list, str)):
            encoded = compact_json(node)
            if len(encoded) >= 160:
                if encoded in seen:
                    reference = {"$context_ref": seen[encoded]}
                    if len(compact_json(reference)) < len(encoded):
                        return reference
                seen[encoded] = path
            if isinstance(node, dict):
                return {key: visit(item, path + "/" + key.replace("~", "~0").replace("/", "~1"))
                    for key, item in node.items()}
            if isinstance(node, list):
                return [visit(item, path + "/" + str(i)) for i, item in enumerate(node)]
        return node
    return visit(value, "")


def expand_context(value):
    """Reference reconstruction used in equivalence checks and offline inspection."""
    def resolve(node, visiting):
        if isinstance(node, dict) and set(node) == {"$context_ref"}:
            pointer = node["$context_ref"]
            if pointer in visiting:
                raise ValueError("Cyclic context reference")
            target = value
            for part in pointer.split("/")[1:]:
                key = part.replace("~1", "/").replace("~0", "~")
                target = target[int(key)] if isinstance(target, list) else target[key]
            return resolve(target, {*visiting, pointer})
        if isinstance(node, dict):
            return {k: resolve(v, visiting) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(v, visiting) for v in node]
        return deepcopy(node)
    return resolve(value, set())


@dataclass(frozen=True)
class TaskCard:
    stage: str = ""
    task: str = ""
    requirement_ids: tuple = ()
    allowed_paths: tuple = ()
    required_artifacts: tuple = ()
    allowed_queries: tuple = ()
    must_preserve_evidence: tuple = ()
    acceptance: tuple = ()
    budget: dict = field(default_factory=dict)


class ContextBroker:
    """Wrap an existing authorized index; registering records never grants file access."""
    def __init__(self, index=None):
        self.index = index
        self.collections = {}
        self._record_versions = {}
        self._result_versions = set()

    def register_records(self, kind, records):
        self.collections[kind] = deepcopy(records)

    def build_task_context(self, task_card, context=None):
        if context is None:
            context = {}
        # Keep existing per-stage projections intact during migration.
        request = deepcopy(context)
        self.last_task_card = task_card
        if task_card.allowed_queries:
            request["context_kinds"] = list(task_card.allowed_queries)
        record_event("context_built", characters=len(compact_json(request)))
        return request

    def records(self):
        return self.index.records() if self.index is not None else self.collections

    def query(self, query):
        return self.resolve_query(query)

    def resolve_query(self, query):
        from infrastructure.shared.agent.discovery import ContextIndex
        result = (self.index if self.index is not None else ContextIndex(self.collections)).query(query)
        repeated = 0
        for category, records in (("record", result.get("records", {})),
                ("candidate", {item["id"]: item for item in result.get("candidates", [])})):
            for identity, value in records.items():
                key = (query.get("kind"), category, identity)
                version = content_version(compact_json(value))
                repeated += int(self._record_versions.get(key) == version)
                self._record_versions[key] = version
        result_version = content_version(compact_json(result))
        self.record_usage(query=query, records=len(result.get("records", {})),
            candidates=len(result.get("candidates", [])), duplicate_records=repeated,
            duplicate_result=result_version in self._result_versions, characters=len(compact_json(result)))
        self._result_versions.add(result_version)
        from infrastructure.shared.artifacts.dependency_graph import observe_query
        from infrastructure.shared.project.resume import track_query
        collections = self.records()
        observe_query(query, collections)
        track_query(query.get("kind"), collections.get(query.get("kind"), {}))
        return result

    def record_usage(self, **details):
        record_event("context_query", **details)


def merge_excerpts(excerpts):
    """Merge compatible ranges; keep line endings and every non-range metadata field."""
    merged = []
    for item in sorted(excerpts, key=lambda e: (e["start_line"], e["end_line"])):
        item = deepcopy(item)
        if merged:
            old = merged[-1]
            metadata = lambda e: {k: v for k, v in e.items() if k not in {"content", "start_line", "end_line"}}
            if metadata(old) == metadata(item) and item["start_line"] <= old["end_line"] + 1:
                old_lines, new_lines = old["content"].splitlines(keepends=True), item["content"].splitlines(keepends=True)
                overlap = max(0, old["end_line"] - item["start_line"] + 1)
                offset = item["start_line"] - old["start_line"]
                shared = min(overlap, len(new_lines))
                if old_lines[offset:offset + shared] == new_lines[:shared]:
                    old["content"] += "".join(new_lines[overlap:])
                    old["end_line"] = max(old["end_line"], item["end_line"])
                    continue
        merged.append(item)
    return merged


def content_version(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
