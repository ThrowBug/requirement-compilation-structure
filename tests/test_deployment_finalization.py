"""Final application cleanup removes only compiler runtime databases."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from infrastructure.shared.project.finalization import (
    clean_runtime_database,
    record_runtime_database_origin,
)


class DeploymentFinalizationTests(unittest.TestCase):
    def test_clean_runtime_database_removes_database_and_sidecars(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "backend/data"
            data.mkdir(parents=True)
            origin = record_runtime_database_origin(root)
            self.assertEqual(origin["preexisting"], [])
            for name in ("app.db", "app.db-shm", "app.db-wal"):
                (data / name).write_text(name, encoding="utf-8")
            (data / "keep.txt").write_text("keep", encoding="utf-8")

            result = clean_runtime_database(root, origin)

            self.assertEqual(result["status"], "GREEN")
            self.assertEqual(set(result["removed"]), {
                "backend/data/app.db", "backend/data/app.db-shm", "backend/data/app.db-wal"})
            self.assertFalse((data / "app.db").exists())
            self.assertEqual((data / "keep.txt").read_text(encoding="utf-8"), "keep")

    def test_existing_database_is_preserved(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "backend/data"
            data.mkdir(parents=True)
            (data / "app.db").write_text("user data", encoding="utf-8")

            origin = record_runtime_database_origin(root)
            result = clean_runtime_database(root, origin)

            self.assertEqual(result["removed"], [])
            self.assertEqual(result["preserved"], ["backend/data/app.db"])
            self.assertEqual((data / "app.db").read_text(encoding="utf-8"), "user data")

    def test_missing_origin_manifest_is_conservative(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "backend/data"
            data.mkdir(parents=True)
            (data / "app.db").write_text("keep", encoding="utf-8")

            result = clean_runtime_database(root)

            self.assertEqual(result["removed"], [])
            self.assertEqual(result["preserved"], ["backend/data/app.db"])

    def test_clean_runtime_database_is_idempotent(self):
        with TemporaryDirectory() as directory:
            result = clean_runtime_database(directory)
            self.assertEqual(result["status"], "GREEN")
            self.assertEqual(result["removed"], [])
            self.assertEqual(result["preserved"], [])


if __name__ == "__main__":
    unittest.main()
