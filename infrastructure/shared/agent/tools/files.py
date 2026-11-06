"""Workspace-relative file reading and exact local editing operations."""

from difflib import SequenceMatcher

import json
from infrastructure.shared.agent.files import EXCLUDED
from infrastructure.utils.files import normalize_newlines, read_source
from infrastructure.utils.paths import check_path_case, relative_path
from infrastructure.shared.agent.context_broker import content_version
from infrastructure.shared.agent.experiments import consume
from infrastructure.shared.usage.recorder import record_event


# A tiny single-function stub may itself be the only replaceable span. Larger
# files require a local edit so previous implementations are not regenerated.
SMALL_FILE_REPLACEMENT_LIMIT = 160


class FileToolError(ValueError):
    def __init__(self, code, message, path=None, **details):
        super().__init__(message)
        self.feedback = {"code": code, "message": message, **details}
        if path is not None:
            self.feedback["path"] = path


def workspace_path(root, path, *, allow_directory=False):
    """Translate a portable model path into a native path inside the project."""
    parts = relative_path(path).parts
    if any(part.startswith(".") or part in EXCLUDED for part in parts) or parts[:2] == ("backend", "data"):
        raise FileToolError("INVALID_PATH", "Use a project source/configuration path", path)
    check_path_case(root, path)
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise FileToolError("INVALID_PATH", "Symlinks are not allowed", path)
    if not current.resolve().is_relative_to(root.resolve()) or (current.is_dir() and not allow_directory):
        raise FileToolError("INVALID_PATH", "Use a file inside the working directory", path)
    return current


def read_file(files, path, start_line=1, end_line=None):
    """Read the requested lines only; reading never expands the pinned source context."""
    target = workspace_path(files.output, path, allow_directory=True)
    if files.readable is not None and path not in files.readable:
        raise FileToolError("READ_NOT_ALLOWED", "This task cannot read this file; use allowed_read_paths", path,
            allowed_read_paths=sorted(files.readable))
    if not target.is_file():
        directory = path if target.is_dir() else path.rpartition("/")[0]
        candidates = []
        for name in sorted(files.files):
            if (directory and not name.startswith(directory + "/")) or (
                    files.readable is not None and name not in files.readable):
                continue
            try:
                if files.path(name).is_file():
                    candidates.append(name)
            except (ValueError, OSError):
                continue
            if len(candidates) == 6:
                break
        if target.is_dir():
            raise FileToolError("EXPECTED_FILE", "read_file takes a concrete file path, not a directory. "
                "Use a supplied file or discover a specific candidate.", path, candidate_paths=candidates)
        raise FileToolError("FILE_NOT_FOUND", "File does not exist; use an existing supplied path, "
            "or edit_file/create if this task needs a new file", path, candidate_paths=candidates)
    if type(start_line) is not int or start_line < 1 or (end_line is not None and
            (type(end_line) is not int or end_line < start_line)):
        raise FileToolError("INVALID_LINE_RANGE", "Use positive, inclusive line numbers", path)
    source = read_source(target)
    lines = source.splitlines(keepends=True)
    last = min(end_line if end_line is not None else len(lines), len(lines))
    content = "".join(lines[start_line - 1:last])
    consume("read_characters", len(content))
    record_event("read_file", path=path, characters=len(content))
    files.observed_paths.add(path)
    return {"path": path, "content": content, "start_line": start_line, "version": content_version(source),
        "end_line": last, "total_lines": len(lines), "writable": files.can_write(path)}


def bind_target(files, path):
    """Explicitly claim an existing writable output; publish with the whole batch."""
    target = files.path(path)
    declaration = files.target
    if declaration is None:
        raise FileToolError("NO_TASK_TARGET", "This task has no target to bind", path)
    if (declaration.path is not None and path != declaration.path) or (
            files._target_path is not None and path != files._target_path):
        raise FileToolError("TARGET_PATH_MISMATCH", "Preserve this task's declared target path", path)
    if not path.startswith(declaration.directory + "/") or declaration.adapt(path) != path:
        raise FileToolError("TARGET_PATH_MISMATCH", "Use a target inside its declared directory with the required suffix", path)
    if not files.can_write(path):
        raise FileToolError("WRITE_NOT_ALLOWED", "Binding cannot grant access to a protected file", path)
    if files.readable is not None and path not in files.readable:
        raise FileToolError("READ_NOT_ALLOWED", "Binding cannot bypass the readable scope", path,
            allowed_read_paths=sorted(files.readable))
    if not target.is_file() and path not in files._pending:
        raise FileToolError("FILE_NOT_FOUND", "Create a missing target with edit_file/create", path)
    index = files.output / ".arc/implementation_paths.json"
    owners = json.loads(index.read_text(encoding="utf-8-sig")).get(declaration.kind, {}) if index.is_file() else {}
    if any(identity != declaration.identity and owned == path for identity, owned in owners.items()):
        raise FileToolError("TARGET_ALREADY_OWNED", "Another target of this kind already owns this path", path)
    files._target_path = path
    return {"path": path, "target_kind": declaration.kind, "target_id": declaration.identity}


def local_candidate(current, candidate, path):
    """Convert exact full-text candidates only when most lines survive unchanged."""
    before, after = current.splitlines(keepends=True), candidate.splitlines(keepends=True)
    changes = SequenceMatcher(None, before, after, autojunk=False).get_opcodes()
    retained = sum(end - start for kind, start, end, _, _ in changes if kind == "equal")
    if retained * 2 <= max(len(before), len(after)):
        raise FileToolError("WHOLE_FILE_REPLACEMENT_NOT_ALLOWED",
            "The proposal rewrites most existing lines. Read the relevant function and submit "
            "a unique local replacement, preserving unrelated imports, routes and behavior.", path,
            preserved_lines=retained, original_lines=len(before), proposed_lines=len(after))
    result = list(before)
    for kind, start, end, new_start, new_end in reversed(changes):
        if kind != "equal":
            result[start:end] = after[new_start:new_end]
    return "".join(result)


def edit_file(files, path, operation, new_content, old_content=""):
    """Stage a create, unique exact replacement, prepend or append in the current batch."""
    files.path(path)  # Reject unsafe paths before attempting task-specific adaptation.
    requested = path
    if operation == "create" and files.pending_target and files._target_path is None:
        path = files.target.adapt(path)
        files.path(path)
        files._target_path = path
    if not files.can_write(path) and path != files._target_path:
        raise FileToolError("WRITE_NOT_ALLOWED", "File is not writable for this task", path)
    if not isinstance(new_content, str) or not isinstance(old_content, str):
        raise FileToolError("INVALID_ARGUMENTS", "old_content and new_content must be strings", path)
    new_content, old_content = normalize_newlines(new_content), normalize_newlines(old_content)
    target = files.path(path)
    current = files._pending.get(path, read_source(target) if target.is_file() else None)
    if operation == "create":
        if current is not None:
            raise FileToolError("FILE_ALREADY_EXISTS", "Read the existing file and use replace", path)
        if old_content:
            raise FileToolError("INVALID_ARGUMENTS", "create requires empty old_content", path)
        updated = new_content
    elif operation in {"replace", "prepend", "append"}:
        if current is None:
            raise FileToolError("FILE_NOT_FOUND", "Use create for a missing file", path)
        if operation == "replace":
            if not old_content:
                raise FileToolError("INVALID_ARGUMENTS", "replace requires nonempty exact old_content", path)
            if len(current.strip()) > SMALL_FILE_REPLACEMENT_LIMIT and current.strip() == old_content.strip():
                if old_content != current:
                    raise FileToolError("OLD_CONTENT_NOT_FOUND",
                        "Whole-file source must match exactly, including whitespace", path)
                new_content = local_candidate(current, new_content, path)
            first = current.find(old_content)
            count = 0 if first < 0 else (2 if current.find(old_content, first + 1) >= 0 else 1)
            if count != 1:
                raise FileToolError("OLD_CONTENT_NOT_FOUND" if count == 0 else "OLD_CONTENT_NOT_UNIQUE",
                    f"Expected exactly one old_content match, found {count}; read the relevant lines and retry", path)
            updated = current.replace(old_content, new_content, 1)
        else:
            if old_content:
                raise FileToolError("INVALID_ARGUMENTS", "Boundary insertion requires empty old_content", path)
            updated = new_content + current if operation == "prepend" else current + new_content
    else:
        raise FileToolError("INVALID_OPERATION", "Use create, replace, prepend or append", path)
    files._pending[path] = updated
    result = {"path": path, "operation": operation}
    if path != requested:
        result["requested_path"] = requested
        result["notice"] = "Adapted to this task's required destination; use path in future calls and imports"
    return result

