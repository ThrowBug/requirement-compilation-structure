"""Persistent dependency graph with conservative stage and precise semantic edges."""

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path

from infrastructure.shared.agent.context_broker import compact_json


_active = ContextVar("artifact_graph", default=None)
_stage = ContextVar("artifact_stage", default=None)
ARTIFACTS = {"requirements.json", "db_schema.json", "db_obligations.json", "backend_apis.json",
    "ui_design.json", "frontend_triggers.json", "backend_tdd.json", "ui_checks.json",
    "frontend_e2e.json", "independent_acceptance.json", "implementation_paths.json", "shared_conventions.json"}


def digest(value):
    return hashlib.sha256(compact_json(value).encode("utf-8")).hexdigest()


def current_graph():
    return _active.get()


class DependencyGraph:
    def __init__(self, output):
        self.output = Path(output)
        self.path = self.output / ".arc/dependencies.json"
        self.data = {"schema_version": 1, "nodes": {}, "pending": []}
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if (isinstance(saved, dict) and saved.get("schema_version") == 1 and isinstance(saved.get("nodes"), dict)
                    and isinstance(saved.get("pending"), list) and all(isinstance(n, str) for n in saved["pending"])
                    and all(isinstance(v, dict) and isinstance(v.get("depends_on"), list)
                        and all(isinstance(source, str) for source in v["depends_on"])
                        for v in saved["nodes"].values())):
                self.data = saved
        except (OSError, ValueError):
            pass
        self.reverse = {}
        for node, record in self.data["nodes"].items():
            for source in record["depends_on"]:
                self.reverse.setdefault(source, set()).add(node)
        self.last_stage = None

    @contextmanager
    def activate(self):
        token = _active.set(self)
        try:
            yield self
        finally:
            _active.reset(token)

    @contextmanager
    def stage(self, stage):
        node = "stage:" + stage
        self.register(node, stage=stage, complete=False)
        # Global shared mutable artifacts make stage order a conservative dependency.
        if self.last_stage:
            self.depend(node, "stage:" + self.last_stage)
        self.last_stage = stage
        token = _stage.set(node)
        try:
            yield
        except BaseException:
            raise
        else:
            self.register(node, complete=True, valid=True)
        finally:
            try:
                self.flush()
            finally:
                _stage.reset(token)

    def register(self, node, **metadata):
        record = self.data["nodes"].setdefault(node, {"depends_on": [], "valid": True})
        if "version" in metadata:
            if record.get("version") and record["version"] != metadata["version"]:
                for dependent in self.downstream(node):
                    self.data["nodes"][dependent]["valid"] = False
            record["valid"] = True
        record.update(metadata)
        return record

    def depend(self, consumer, source):
        self.register(source)
        record = self.register(consumer)
        if source != consumer and source not in record["depends_on"]:
            record["depends_on"].append(source)
            self.reverse.setdefault(source, set()).add(consumer)

    def upstream(self, node):
        return self._walk([node], reverse=False) - {node}

    def downstream(self, node):
        return self._walk([node], reverse=True) - {node}

    def _walk(self, nodes, reverse):
        seen, pending = set(), list(nodes)
        while pending:
            node = pending.pop()
            if node in seen:
                continue
            seen.add(node)
            if reverse:
                pending.extend(self.reverse.get(node, ()))
            else:
                pending.extend(self.data["nodes"].get(node, {}).get("depends_on", []))
        return seen

    def affected_by(self, node):
        return {node, *self.downstream(node)}

    def invalidate(self, node):
        for key in self.affected_by(node):
            self.register(key)["valid"] = False
        self.flush()

    def select_rerun_tasks(self, changed_nodes):
        unknown = bool(self.data.get("pending")) or any(node not in self.data["nodes"] for node in changed_nodes)
        affected = set(self.data["nodes"]) if unknown else self._walk(changed_nodes, reverse=True)
        return sorted(node for node in affected if node.startswith(("task:", "stage:")))

    def observe_read(self, filename):
        if _stage.get():
            self.depend(_stage.get(), "artifact:" + filename)

    def publish(self, path, value):
        """A pending marker prevents a crash between artifact and manifest being reused."""
        from infrastructure.utils.files import save_bytes
        node = "artifact:" + path.name
        self.data["pending"] = sorted(set(self.data["pending"]) | {node})
        self.flush()
        save_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        record = self.register(node, version=digest(value), stage=(_stage.get() or "").removeprefix("stage:"), valid=True)
        if _stage.get():
            self.depend(node, _stage.get())
        self._semantic_edges(path.name, value)
        self.data["pending"].remove(node)
        self.flush()
        return record

    def _semantic_edges(self, filename, value):
        if not isinstance(value, dict):
            return
        if filename == "db_schema.json":
            for name, entity in value.get("entities", {}).items():
                self.register("schema:" + name, version=digest(entity))
                for field, definition in entity.get("fields", {}).items():
                    self.register(f"schema:{name}.{field}", version=digest(definition))
            for rid, fields in value.get("requirements", {}).items():
                for field in fields:
                    self.depend("obligation:" + rid, "schema:" + field)
        elif filename == "db_obligations.json":
            for rid, obligation in value.get("requirements", {}).items():
                self.register("obligation:" + rid, version=digest(obligation))
                if isinstance(obligation, dict):
                    for field in [*obligation.get("read", []), *obligation.get("write", [])]:
                        self.depend("obligation:" + rid, "schema:" + str(field))
        elif filename == "backend_apis.json":
            for aid, api in value.get("apis", {}).items():
                self.register("api:" + aid, version=digest(api))
            for rid, aids in value.get("requirements", {}).items():
                for aid in aids:
                    self.depend("api:" + aid, "obligation:" + rid)
                    self.depend("task:backend:" + rid, "api:" + aid)
                    self.depend("stage:08_implement_backend_tdd", "task:backend:" + rid)
                    self.depend("test:backend:" + rid, "api:" + aid)
        elif filename == "ui_design.json":
            for pid, page in value.get("pages", {}).items():
                self.register("page:" + pid, version=digest(page))
                root = page.get("root")
                if root:
                    self.depend("ui_element:" + root, "page:" + pid)
            for uid, element in value.get("elements", {}).items():
                self.register("ui_element:" + uid, version=digest(element))
                if element.get("parent"):
                    self.depend("ui_element:" + uid, "ui_element:" + element["parent"])
        elif filename == "frontend_triggers.json":
            for tid, trigger in value.get("triggers", {}).items():
                self.register("trigger:" + tid, version=digest(trigger))
                self.depend("trigger:" + tid, "ui_element:" + trigger["source"])
                for action in trigger.get("action", []):
                    if action.get("kind") == "api":
                        aid = action.get("api")
                        if aid:
                            self.depend("trigger:" + tid, "api:" + aid)
            for rid, tids in value.get("requirements", {}).items():
                for tid in tids:
                    self.depend("task:frontend:" + rid, "trigger:" + tid)
                    self.depend("stage:12_implement_frontend_tdd", "task:frontend:" + rid)
                    self.depend("test:e2e:" + rid, "trigger:" + tid)
        elif filename in {"backend_tdd.json", "frontend_e2e.json", "independent_acceptance.json", "ui_checks.json"}:
            for rid in value.get("requirements", {}):
                self.depend("test:" + filename + ":" + rid, "task:frontend:" + rid if filename != "backend_tdd.json" else "task:backend:" + rid)
        elif filename == "implementation_paths.json":
            for kind, paths in value.items():
                if not isinstance(paths, dict):
                    continue
                for identity, path in paths.items():
                    if not isinstance(path, str):
                        continue
                    node = "suite:" + path if kind.endswith("test") else "source:" + path
                    self.register(node, path=path)
                    source = {"backend_test": "api:", "api": "api:", "trigger": "trigger:",
                        "page": "page:", "e2e_test": "task:frontend:"}.get(kind)
                    if source:
                        self.depend(node, source + identity)

    def flush(self):
        from infrastructure.utils.files import save_bytes
        save_bytes(self.path, (json.dumps(self.data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def save_artifact_with_metadata(path, value):
    graph = current_graph()
    if graph is None:
        graph = DependencyGraph(Path(path).parent.parent)
    return graph.publish(Path(path), value)


def observe_query(query, collections):
    graph = current_graph()
    if graph is None or not _stage.get():
        return
    kind = query.get("kind")
    # Record the collection, including misses and future new matches.
    node = "collection:" + str(kind)
    graph.register(node, version=digest(collections.get(kind, {})))
    graph.depend(_stage.get(), node)
    for identity in query.get("ids", []):
        graph.depend(_stage.get(), str(kind) + ":" + identity)
