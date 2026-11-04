"""Shared tool definitions, parameter schemas and execution effects."""

from .files import SMALL_FILE_REPLACEMENT_LIMIT, bind_target, edit_file, read_file


def definition(name, description, properties, required, example):
    return {"tool": name, "description": description,
        "parameters": {"type": "object",
            "properties": {"tool": {"type": "string", "enum": [name]}, **properties},
            "required": ["tool", *required], "additionalProperties": False},
        "example": example}


STRING = {"type": "string"}
TOOL_REGISTRY = {
    "bind_target": (bind_target, definition("bind_target", "Claim an already existing writable file as task.file_target. "
        "Use this when an earlier task created the current page/API/test file without registering its target. "
        "This registers ownership, does not change contents, and still runs acceptance.",
        {"path": STRING}, ["path"],
        {"tool": "bind_target", "path": "frontend/src/pages/Register.tsx"}), "write"),
    "read_file": (read_file, definition("read_file", "Read a relevant project file or inclusive 1-based line range. "
        "Prefer supplied files; read only missing evidence needed for this task.",
        {"path": STRING, "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1}}, ["path"],
        {"tool": "read_file", "path": "backend/src/db/initialize.ts", "start_line": 1, "end_line": 40}), "read"),
    "edit_file": (edit_file, definition("edit_file", "Create a new file, replace one unique exact text span, "
        "or prepend/append to an existing file. Paths are relative to the project working directory. "
        "replace needs a local old_content span copied from supplied/read source. "
        f"Whole-file replacement is limited to {SMALL_FILE_REPLACEMENT_LIMIT} characters after trimming. "
        "Preserve earlier requirements and extend shared code incrementally.",
        {"path": STRING, "operation": {"type": "string", "enum": ["create", "replace", "prepend", "append"]},
            "old_content": STRING, "new_content": STRING}, ["path", "operation", "new_content"],
        {"tool": "edit_file", "path": "backend/src/example.ts", "operation": "replace",
            "old_content": "exact existing span", "new_content": "replacement span"}), "write"),
}

