"""Regression tests for checking an existing SQLite database copy."""

from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest

from infrastructure.stages.database.testing import DatabaseChecks, PROBE_SOURCE


class ExistingDatabaseCheckTests(unittest.TestCase):
    def test_sqlite_backup_preserves_existing_rows(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.db"
            target = Path(directory) / "target.db"
            connection = sqlite3.connect(source)
            connection.execute("CREATE TABLE Book (id INTEGER PRIMARY KEY, title TEXT)")
            connection.execute("INSERT INTO Book (title) VALUES ('existing')")
            connection.commit()
            connection.close()

            DatabaseChecks._copy_database(source, target)

            copied = sqlite3.connect(target)
            self.assertEqual(copied.execute("SELECT title FROM Book").fetchone()[0], "existing")
            copied.close()

    def test_probe_contains_existing_database_initialization_and_startup(self):
        self.assertIn("plan.existing_database", PROBE_SOURCE)
        self.assertIn("existing database initialization", PROBE_SOURCE)
        self.assertIn("existing server startup", PROBE_SOURCE)

    def test_existing_copy_is_marked_before_preservation_check(self):
        marker = "seedMarker(plan.existing_database)"
        phase = 'phase = "existing database initialization"'
        self.assertIn(marker, PROBE_SOURCE)
        self.assertLess(PROBE_SOURCE.index(marker), PROBE_SOURCE.index(phase))


if __name__ == "__main__":
    unittest.main()
