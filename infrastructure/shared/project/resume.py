"""Version-checked stage resume and validated design replay; no acceptance bypass."""

from contextvars import ContextVar
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
import platform
import re
import shutil
from pathlib import Path

from infrastructure.shared.artifacts.dependency_graph import ARTIFACTS, digest
from infrastructure.utils.files import save_json


_active = ContextVar("resume_manager", default=None)
_task_queries = ContextVar("resume_task_queries", default=None)
EXCLUDED = {".git", ".venv", "node_modules", "__pycache__", "dist", "build", "coverage",
    "test-results", "playwright-report", "data"}
BAD_STATUS = {"FAILED", "INTERRUPTED", "PENDING", "RUNNING", "COMPLETED_WITH_FAILURES"}
# Edited intermediate designs are inputs to their consumers, not requests to
# regenerate and overwrite that same design from the original requirement.
MANUAL_ARTIFACTS = {
    ".arc/requirements.json": ("01_explore_requirement", "02_build_db_schema"),
    ".arc/db_schema.json": ("02_build_db_schema", "03_infer_db_obligations"),
    ".arc/db_obligations.json": ("03_infer_db_obligations", "04_check_db_obligations"),
    ".arc/backend_apis.json": ("06_design_backend_apis", "07_check_backend_apis"),
    ".arc/ui_design.json": ("09_design_ui", "10_implement_ui"),
    ".arc/frontend_triggers.json": ("11_design_frontend_triggers", "12_implement_frontend_tdd"),
}


def current_resume():
    return _active.get()


def runtime_environment():
    node = shutil.which("node")
    binary = Path(node) if node else None
    stat = binary.stat() if binary and binary.is_file() else None
    return {"python": platform.python_version(), "platform": platform.platform(),
        "node": str(binary.resolve()) if binary else None,
        "node_version_marker": [stat.st_size, stat.st_mtime_ns] if stat else None}


def snapshot(root, *, application=False):
    root = Path(root)
    result = {}
    if not root.exists():
        return result
    for directory, dirs, files in os.walk(root, followlinks=False):
        symlink_dirs = [d for d in dirs if (Path(directory) / d).is_symlink()]
        for name in symlink_dirs:
            relative = (Path(directory) / name).relative_to(root).as_posix()
            result[relative] = "symlink-directory"
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED and d not in symlink_dirs and (d != ".arc" or application))
        for filename in sorted(files):
            path = Path(directory) / filename
            relative = path.relative_to(root).as_posix()
            if application:
                if relative.startswith(".arc/") and relative not in {".arc/" + name for name in ARTIFACTS}:
                    continue
                if not relative.startswith(("backend/", "frontend/", "db/", ".arc/")) and filename not in {
                        "package.json", "package-lock.json", "tsconfig.json", "vite.config.ts", "playwright.config.ts"}:
                    continue
            if path.is_symlink():
                # Never trust a reused task whose workspace acquired a symlink.
                result[relative] = "symlink:" + str(path.resolve())
                continue
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    if application:
        for name in ("node_modules/.package-lock.json", "node_modules/better-sqlite3/build/Release/better_sqlite3.node"):
            path = root / name
            if path.is_file():
                stat = path.stat()
                result[name] = f"runtime:{stat.st_size}:{stat.st_mtime_ns}"
    return result


def code_manifest():
    root = Path(__file__).resolve().parents[3]
    paths = sorted([*root.glob("*.py"), *(p for p in root.joinpath("infrastructure").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts)])
    hashes = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    for name in ("uv.lock", "pyproject.toml"):
        if (root / name).is_file():
            hashes[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
    return hashes


def prefix_compatible(previous, identity, code_files):
    """Only reuse a checkpoint that ended before a known stage-local code change."""
    old_identity = previous.get("identity", {})
    if any(old_identity.get(key) != identity[key] for key in ("input_hash", "configuration_hash")):
        return False
    if old_identity == identity:
        return True
    old_files = previous.get("code_files")
    if not isinstance(old_files, dict) or digest(old_files) != old_identity.get("code_hash"):
        return False
    changed = {p for p in old_files.keys() | code_files.keys() if old_files.get(p) != code_files.get(p)}
    affected = []
    for path in changed:
        # Shared helpers can affect earlier stages; never infer their scope here.
        match = re.fullmatch(r"(08|09|10|11|12)_[^/]+\.py", path)
        if match:
            affected.append(int(match[1]))
        elif path.startswith("infrastructure/stages/frontend/triggers/"):
            name = path.removeprefix("infrastructure/stages/frontend/triggers/")
            affected.append(12 if name in {"acceptance.py", "implementation.py", "e2e_runtime.py",
                "scope.py", "context.py", "testing.py"} else 11)
        else:
            return False
    if not affected:
        return False
    return all(re.match(r"^\d\d(?:_|$)", stage) and int(stage[:2]) < min(affected)
        for stage in previous["stages"])


class ResumeManager:
    def __init__(self, output, input_dir, graph, configuration=None, enabled=False, changed_nodes=()):
        self.output, self.graph = Path(output), graph
        self.path = self.output / ".arc/resume.json"
        self.enabled = enabled
        input_snapshot = snapshot(input_dir)
        self.unsafe_inputs = any(value.startswith("symlink") for value in input_snapshot.values())
        self.code_files = code_manifest()
        self.identity = {"input_hash": digest(input_snapshot), "code_hash": digest(self.code_files),
            "configuration_hash": digest(configuration or {})}
        self.state = {"schema_version": 1, "identity": self.identity, "code_files": self.code_files,
            "stages": {}, "design_tasks": {}, "file_tasks": {}, "snapshot": {}}
        self.previous = self.load_state()
        self.compatible = prefix_compatible(self.previous, self.identity, self.code_files)
        self.rerun = self.plan_rerun(changed_nodes)
        self.reused = []
        self._started = False
        if enabled and not self.rerun["all"]:
            for key in ("stages", "snapshot"):
                self.state[key] = deepcopy(self.previous[key])
            if self.previous.get("identity") == self.identity:
                for key in ("design_tasks", "file_tasks"):
                    self.state[key] = deepcopy(self.previous[key])

    @contextmanager
    def activate(self):
        self.begin_run()
        token = _active.set(self)
        try:
            yield self
        finally:
            _active.reset(token)

    def begin_run(self):
        """Persist the whole dirty suffix before any stage can advance its snapshot."""
        if self._started:
            return
        for stage, entry in self.state["stages"].items():
            if self.should_run(stage):
                entry["status"] = "PENDING"
        self.flush()
        self._started = True

    def mark_task_started(self, stage, task):
        entry = self.state["stages"].setdefault(stage, {
            "result": {"status": "RUNNING"}, "outputs": []})
        entry["status"] = "RUNNING"
        self.flush()

    def load_state(self):
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if (isinstance(state, dict) and state.get("schema_version") == 1 and all(isinstance(state.get(k), dict)
                    for k in ("identity", "stages", "design_tasks", "file_tasks", "snapshot"))
                    and all(isinstance(v, dict) and isinstance(v.get("status"), str) and "result" in v
                        and isinstance(v.get("outputs"), list) and all(isinstance(p, str) for p in v["outputs"])
                        for v in state["stages"].values())
                    and all(isinstance(k, str) and isinstance(v, str) for k, v in state["snapshot"].items())):
                return state
        except (ValueError, OSError):
            pass
        return {}

    def plan_rerun(self, changed_nodes=()):
        stages = self.previous.get("stages", {})
        if (not self.enabled or not self.compatible or self.graph.data.get("pending")
                or self.unsafe_inputs or not self.graph.data["nodes"]
                or any("stage:" + stage not in self.graph.data["nodes"] for stage in stages)
                or any(entry.get("status") not in BAD_STATUS
                    and self.graph.data["nodes"].get("stage:" + stage, {}).get("complete") is not True
                    for stage, entry in stages.items())):
            return {"all": True, "stages": [], "reason": "fresh run or input/code/configuration/metadata changed"}
        before, after = self.previous.get("snapshot", {}), snapshot(self.output, application=True)
        if any(value.startswith("symlink") for value in [*before.values(), *after.values()]):
            return {"all": True, "stages": [], "reason": "symlink dependency requires full validation"}
        changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
        affected = set(self.graph.select_rerun_tasks(changed_nodes)) if changed_nodes else set()
        if changed_nodes and any(n not in self.graph.data["nodes"] for n in changed_nodes):
            return {"all": True, "stages": [], "reason": "unknown changed node"}
        roots = {node.removeprefix("stage:") for node in affected if node.startswith("stage:")}
        roots.update(node.removeprefix("stage:") for node, entry in self.graph.data["nodes"].items()
            if node.startswith("stage:") and entry.get("valid") is False)
        manual = {}
        for path in changed:
            if path in MANUAL_ARTIFACTS and path in after:
                try:
                    value = json.loads((self.output / path).read_text(encoding="utf-8-sig"))
                    if not isinstance(value, dict):
                        raise ValueError("Artifact must be an object")
                except (ValueError, OSError):
                    return {"all": True, "stages": [], "reason": "invalid manually edited artifact"}
                producer, consumer = MANUAL_ARTIFACTS[path]
                manual[producer] = path
                roots.add(consumer)
                continue
            # Stage-level writes are shared: use the earliest recorded writer of changed paths.
            owners = [s for s, entry in stages.items() if path in entry.get("outputs", [])]
            if not owners:
                return {"all": True, "stages": [], "changed_files": changed, "reason": "untracked dependency"}
            roots.update(owners)
        roots.update(s for s, entry in stages.items() if entry.get("status") in BAD_STATUS)
        ordered = sorted(stages)
        if roots:
            first = min((ordered.index(s) for s in roots if s in ordered), default=0)
            selected = ordered[first:]
        else:
            selected = []
        return {"all": False, "stages": selected, "changed_files": changed,
            "manual_artifacts": manual,
            "affected_nodes": sorted(affected), "reason": "validated snapshots; conservative shared-stage suffix"}

    def should_run(self, stage, task=None):
        if task is not None:
            # Reusing a stage also reuses its tasks. Tasks inside a rerun stage
            # receive the finer context/source/gate checks at the agent boundary.
            return self.should_run(stage)
        return self.rerun["all"] or stage in self.rerun["stages"] or stage not in self.state["stages"]

    def result(self, stage):
        self.reused.append(stage)
        if path := self.rerun.get("manual_artifacts", {}).get(stage):
            result = json.loads((self.output / path).read_text(encoding="utf-8-sig"))
            self.state["stages"][stage]["result"] = result
            return result
        return self.state["stages"][stage]["result"]

    def mark_task_done(self, stage, task, result, before=None):
        after = snapshot(self.output, application=True)
        before = before or {}
        changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
        old = self.state["stages"].get(stage, {})
        status = result.get("status", "GREEN") if isinstance(result, dict) else "GREEN"
        self.state["stages"][stage] = {"status": status, "result": result,
            "outputs": sorted(set(old.get("outputs", [])) | set(changed))}
        self.state["snapshot"] = after
        self.flush()

    def mark_task_failed(self, stage, task, error):
        self.state["stages"][stage] = {"status": "FAILED", "result": {"status": "FAILED", "error": str(error)}, "outputs": []}
        self.state["snapshot"] = snapshot(self.output, application=True)
        self.flush()

    def design_key(self, stage, task, instructions, request):
        return digest({"stage": stage, "task": task, "instructions": instructions, "request": request})

    def design_proposal(self, key):
        if self.enabled and not self.unsafe_inputs and self.previous.get("identity") == self.identity:
            return self.previous.get("design_tasks", {}).get(key)
        return None

    def save_design(self, key, proposal):
        self.state["design_tasks"][key] = proposal
        self.flush()

    def file_key(self, stage, label, task, instructions, files):
        return digest({"stage": stage, "task": label, "context": task, "instructions": instructions,
            "pinned": sorted(files.files), "writable": sorted(files.writable),
            "readable": sorted(files.readable) if files.readable is not None else None})

    def file_result(self, key, files, index=None):
        if (not self.enabled or self.rerun["all"] or self.previous.get("identity") != self.identity
                or self.graph.data.get("pending")):
            return None
        entry = self.previous.get("file_tasks", {}).get(key)
        if (not isinstance(entry, dict) or not isinstance(entry.get("files"), dict) or not entry["files"]
                or not isinstance(entry.get("queries"), dict) or not isinstance(entry.get("result"), dict)
                or type(entry["result"].get("turns")) is not int):
            return None
        for path, version in entry["files"].items():
            try:
                target = files.path(path)
                if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != version:
                    return None
            except (OSError, ValueError):
                return None
        collections = index.records() if index is not None else {}
        if any(digest(collections.get(kind, {})) != version for kind, version in entry.get("queries", {}).items()):
            return None
        return deepcopy(entry["result"])

    def save_file(self, key, files, result, queries):
        paths = files.files | files.changed_paths | getattr(files, "observed_paths", set())
        versions = {path: hashlib.sha256(files.path(path).read_bytes()).hexdigest()
            for path in sorted(paths) if files.path(path).is_file()}
        self.state["file_tasks"][key] = {"files": versions, "queries": queries, "result": result}
        self.flush()

    def flush(self):
        save_json(self.path, self.state)


@contextmanager
def track_task_queries():
    queries = {}
    token = _task_queries.set(queries)
    try:
        yield queries
    finally:
        _task_queries.reset(token)


def track_query(kind, records):
    if (queries := _task_queries.get()) is not None:
        queries[kind] = digest(records)
