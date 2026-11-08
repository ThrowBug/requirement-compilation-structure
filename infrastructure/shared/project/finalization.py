"""Prepare a generated application directory for deployment."""

import json
from pathlib import Path

from infrastructure.utils.files import save_json


RUNTIME_DATABASE_FILES = (
    "backend/data/app.db",
    "backend/data/app.db-shm",
    "backend/data/app.db-wal",
)
RUNTIME_DATABASE_ORIGIN = ".arc/runtime_database_origin.json"


def record_runtime_database_origin(output, *, reset=False) -> dict:
    """Record which runtime database files existed before this compilation.

    A missing manifest is treated conservatively by cleanup, so an old output
    directory or a user-supplied database is never deleted accidentally.
    """
    root = Path(output).resolve()
    manifest = root / RUNTIME_DATABASE_ORIGIN
    if manifest.is_file() and not reset:
        try:
            value = json.loads(manifest.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            value = None
        if isinstance(value, dict) and isinstance(value.get("preexisting"), list):
            return value
    value = {
        "status": "RECORDED",
        "preexisting": [relative for relative in RUNTIME_DATABASE_FILES
            if (root / relative).exists()],
    }
    save_json(manifest, value)
    return value


def clean_runtime_database(output, origin=None) -> dict:
    """Remove only runtime databases created by this compilation.

    The cleanup is deliberately conservative when no origin manifest exists.
    That preserves a database supplied before the compiler started and keeps a
    failed or legacy output available for diagnosis.
    """
    root = Path(output).resolve()
    origin = origin or {}
    preexisting = set(origin.get("preexisting", ()))
    removed = []
    preserved = []
    for relative in RUNTIME_DATABASE_FILES:
        path = root / relative
        if not path.exists() and not path.is_symlink():
            continue
        if not path.resolve().is_relative_to(root):
            raise ValueError(f"Runtime database path escapes output directory: {relative}")
        if not origin or relative in preexisting:
            preserved.append(relative)
            continue
        path.unlink()
        removed.append(relative)
    return {"status": "GREEN", "removed": removed, "preserved": preserved,
        "origin_manifest": RUNTIME_DATABASE_ORIGIN if origin else None}
