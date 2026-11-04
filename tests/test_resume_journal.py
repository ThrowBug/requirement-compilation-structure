"""A restarted process must finish every invalidated stage, even after snapshots advance."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from infrastructure.shared.artifacts.dependency_graph import DependencyGraph
from infrastructure.shared.project.resume import ResumeManager, snapshot
from infrastructure.utils.files import save_json


class ResumeJournal(unittest.TestCase):
    def seed(self, root):
        source = root / "backend/src/handler.ts"
        source.parent.mkdir(parents=True)
        source.write_text("old implementation\n", encoding="utf-8")
        graph = DependencyGraph(root)
        for stage in ("08", "09", "12"):
            graph.register("stage:" + stage, complete=True, valid=True)
        graph.depend("stage:09", "stage:08")
        graph.depend("stage:12", "stage:09")
        graph.flush()
        manager = ResumeManager(root, root / "input", graph)
        manager.mark_task_done("08", "stage", {"status": "GREEN"})
        for stage in ("09", "12"):
            manager.mark_task_done(stage, "stage", {"status": "GREEN"}, snapshot(root, application=True))
        return graph, source

    def test_restart_after_first_rerun_preserves_pending_consumers(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            graph, source = self.seed(root)
            source.write_text("updated implementation\n", encoding="utf-8")
            resumed = ResumeManager(root, root / "input", graph, enabled=True)
            self.assertTrue(resumed.should_run("09"))
            with resumed.activate():
                resumed.mark_task_done("08", "stage", {"status": "GREEN"}, snapshot(root, application=True))
                # Simulate process loss between stages: read only the on-disk journal.
                restarted = ResumeManager(root, root / "input", DependencyGraph(root), enabled=True)
            self.assertFalse(restarted.should_run("08"))
            self.assertTrue(restarted.should_run("09"))
            self.assertTrue(restarted.should_run("12"))

    def test_changed_source_uses_pipeline_order_after_json_keys_are_reordered(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            graph, source = self.seed(root)
            state = json.loads((root / ".arc/resume.json").read_text(encoding="utf-8"))
            state["stages"] = dict(reversed(list(state["stages"].items())))
            save_json(root / ".arc/resume.json", state)
            source.write_text("updated implementation\n", encoding="utf-8")
            resumed = ResumeManager(root, root / "input", graph, enabled=True)
            self.assertEqual(resumed.rerun["stages"], ["08", "09", "12"])

    def test_in_progress_stage_is_durable_before_any_file_write(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            graph, source = self.seed(root)
            source.write_text("updated implementation\n", encoding="utf-8")
            resumed = ResumeManager(root, root / "input", graph, enabled=True)
            with resumed.activate():
                resumed.mark_task_started("08", "stage")
                persisted = json.loads(resumed.path.read_text(encoding="utf-8"))
                self.assertEqual(persisted["stages"]["08"]["status"], "RUNNING")
                self.assertEqual(persisted["stages"]["09"]["status"], "PENDING")
                restarted = ResumeManager(root, root / "input", DependencyGraph(root), enabled=True)
            self.assertTrue(all(restarted.should_run(stage) for stage in ("08", "09", "12")))

    def test_resume_preview_keeps_previous_success_journal_intact(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            graph, source = self.seed(root)
            original = (root / ".arc/resume.json").read_bytes()
            source.write_text("updated implementation\n", encoding="utf-8")
            preview = ResumeManager(root, root / "input", graph, enabled=True)
            self.assertTrue(preview.should_run("09"))
            self.assertEqual(preview.path.read_bytes(), original)
