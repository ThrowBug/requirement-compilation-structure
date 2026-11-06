"""Isolate failed design tasks while preserving successful neighboring artifacts."""

from contextlib import contextmanager

from infrastructure.shared.project.git import checkpoint
from infrastructure.utils.files import save_json, read_artifact


class UpstreamTaskError(ValueError):
    def __init__(self, stage, identity, artifact, reason):
        self.blocked_by = {"stage": stage, "task": identity, "artifact": artifact, "reason": reason}
        super().__init__(f"Blocked by {stage} / {identity} ({artifact}): {reason}")


def require_design_task(arc_dir, stage, identity, artifact, assignments):
    """Distinguish accepted empty associations from failed or missing designs."""
    report = arc_dir / "stage_tasks" / (stage + ".json")
    if report.is_file():
        entry = read_artifact(report.parent, report.name).get("tasks", {}).get(identity)
        if entry is None:
            raise UpstreamTaskError(stage, identity, artifact, "Task has not published an accepted result")
        if entry.get("status") != "GREEN":
            raise UpstreamTaskError(stage, identity, artifact, entry.get("error", "Task is not accepted"))
    if identity not in assignments:
        raise UpstreamTaskError(stage, identity, artifact, "No accepted requirement association; not an explicit empty list")


class StageIncomplete(RuntimeError):
    def __init__(self, stage, failures):
        self.failures = failures
        super().__init__(f'{stage}: {len(failures)} task(s) failed; all tasks were attempted. '
            'Continue with available artifacts.')


class TaskFailures:
    """Track failures in memory without writing per-task status files."""

    def __init__(self, arc_dir, stage, console):
        self.stage, self.console = stage, console
        self.report_path = arc_dir / 'stage_tasks' / f'{stage}.json'
        self.report = {'status': 'RUNNING', 'tasks': {}}

    @contextmanager
    def task(self, identity):
        try:
            yield
        except Exception as error:
            if isinstance(error, OSError):
                raise
            self.report['tasks'][identity] = {'status': 'FAILED', 'error': str(error),
                'error_type': type(error).__name__}
            if isinstance(error, UpstreamTaskError):
                self.report["tasks"][identity]["blocked_by"] = error.blocked_by
            self.console.show(self.stage + ' / ' + identity + ' | FAILED; continuing', str(error))
        else:
            self.report['tasks'][identity] = {'status': 'GREEN'}
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        save_json(self.report_path, self.report)
        checkpoint(self.report_path.parent.parent.parent, stage=self.stage,
            task=identity, action='Publish design task result', status=self.report['tasks'][identity]['status'],
            details=self.report['tasks'][identity])

    def finish(self):
        failures = [{'task': identity, **entry} for identity, entry in self.report['tasks'].items()
            if entry['status'] == 'FAILED']
        self.report['status'] = 'COMPLETED_WITH_FAILURES' if failures else 'GREEN'
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        save_json(self.report_path, self.report)
        checkpoint(self.report_path.parent.parent.parent, stage=self.stage, task='stage report',
            action='Publish design stage result', status=self.report['status'], details=self.report)
        if failures:
            raise StageIncomplete(self.stage, failures)
