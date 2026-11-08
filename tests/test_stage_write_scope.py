"""Stage-specific SourceFiles write scopes."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from infrastructure.shared.agent.context import SourceFiles


class StageWriteScopeTests(unittest.TestCase):
    def test_stage_12_reads_upstream_sources_but_writes_only_trigger_sources(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in (
                "frontend/src/App.tsx",
                "frontend/src/triggers/save.ts",
                "backend/src/api/register.ts",
                "backend/src/db/initialize.ts",
            ):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("export {};\n", encoding="utf-8")
            source = SourceFiles(root, stage="12")
            workspace = source.workspace(
                paths=["frontend/src/triggers/save.ts"],
                references=[
                    "frontend/src/App.tsx",
                    "backend/src/api/register.ts",
                    "backend/src/db/initialize.ts",
                ],
            )
            self.assertTrue(workspace.can_write("frontend/src/triggers/save.ts"))
            self.assertFalse(workspace.can_write("frontend/src/App.tsx"))
            self.assertFalse(workspace.can_write("backend/src/api/register.ts"))
            self.assertFalse(workspace.can_write("backend/src/db/initialize.ts"))


if __name__ == "__main__":
    unittest.main()
