"""Run the complete requirement compiler in one process using the existing passes."""

from importlib import import_module
from pathlib import Path
import traceback
from contextlib import ExitStack
from dataclasses import asdict
import os

from infrastructure.utils.presentation.console import show_panel
from infrastructure.utils.presentation.logging import console_log
from infrastructure.utils.files import save_json
from infrastructure.shared.project.git import checkpoint
from infrastructure.shared.usage.recorder import UsageRecorder, current_recorder, record_event
from infrastructure.shared.artifacts.dependency_graph import DependencyGraph
from infrastructure.shared.project.resume import BAD_STATUS, ResumeManager, runtime_environment, snapshot
from infrastructure.shared.agent.experiments import OptimizationOptions, optimization_scope
from infrastructure.shared.agent.failures import observe_failure


class Compiler:
    """Own shared paths, configuration, and results for one compilation."""

    def __init__(self, input_dir: Path, output_dir: Path, model_log: bool = False, port: int = 3000,
            *, resume=False, resume_preview=False, changed_nodes=(), optimizations=None, until_stage=12) -> None:
        if not 1 <= port <= 65535:
            raise ValueError("Backend port must be between 1 and 65535")
        self.input_dir = Path(input_dir).expanduser().resolve()
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.arc_dir = self.output_dir / ".arc"
        self.model_log = model_log
        if not 0 <= until_stage <= 12:
            raise ValueError("Last stage must be between 0 and 12")
        self.port = port
        self.until_stage = until_stage
        self.resume_enabled, self.resume_preview = resume or resume_preview, resume_preview
        self.changed_nodes = tuple(changed_nodes)
        self.optimizations = optimizations or OptimizationOptions()
        self.resume_manager = None
        self.artifacts: dict[str, object] = {}
        self.current_stage: str | None = None
        self.report = {"status": "NOT_STARTED", "stages": {}}

    def _run(self, module: str, function: str, *arguments, **options):
        """Attempt every pass; keep failures explicit without aborting later passes."""
        self.current_stage = module + ".py"
        manager = self.resume_manager
        if manager and not manager.should_run(module):
            result = manager.result(module)
            status = result.get("status", "GREEN") if isinstance(result, dict) else "GREEN"
            self.report["stages"][module] = {"status": status, "reused": True}
            from infrastructure.shared.artifacts.dependency_graph import current_graph
            current_graph().last_stage = module
            record_event("stage_reused", stage=module)
            if recorder := current_recorder():
                recorder.stage_result(module, status)
            return result
        before = snapshot(self.output_dir, application=True) if manager else None
        if manager:
            manager.mark_task_started(module, "stage")
        try:
            with console_log(self.output_dir, stage=self.current_stage,
                    model_log=self.model_log, debug_model_io=self.model_log):
                result = getattr(import_module(module), function)(*arguments, **options)
        except KeyboardInterrupt as error:
            if manager:
                manager.mark_task_failed(module, "stage", error)
            self.report["status"] = "INTERRUPTED"
            self.report["stages"][module] = {"status": "INTERRUPTED", "error": str(error)}
            save_json(self.arc_dir / "compilation.json", self.report)
            checkpoint(self.output_dir, stage=module, task="stage report", action="Record interrupted pass",
                status="INTERRUPTED", details=self.report["stages"][module])
            raise
        except Exception as error:
            observe_failure(error, will_retry=False, stage=module)
            result = {"status": "FAILED", "error": str(error),
                "error_type": type(error).__name__, "traceback": traceback.format_exc()}
            if getattr(error, "failures", None):
                result.update(status="COMPLETED_WITH_FAILURES", failures=error.failures)
            show_panel("[FAILED; continuing] " + module,
                result["error"] + "\nAccepted artifacts remain available. Attempt the next pass.", tone="error")
        status = result.get("status", "GREEN") if isinstance(result, dict) else "GREEN"
        self.report["stages"][module] = {"status": status}
        if manager:
            manager.mark_task_done(module, "stage", result, before)
        if recorder := current_recorder():
            recorder.stage_result(module, status)
        if isinstance(result, dict):
            for key in ("failures", "verification", "error", "error_type", "traceback"):
                if key in result:
                    self.report["stages"][module][key] = result[key]
        save_json(self.arc_dir / "compilation.json", self.report)
        checkpoint(self.output_dir, stage=module, task="stage report", action="Publish pass result",
            status=status, details=self.report["stages"][module])
        return result

    def compile(self) -> Path:
        """Own persistent telemetry, graph and resume configuration for the whole run."""
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[1] / ".env", encoding="utf-8-sig")
        configuration = {"port": self.port, "optimizations": asdict(self.optimizations), "environment": runtime_environment(),
            "models": {key: os.getenv(key) for key in ("MODEL", "VISUAL_MODEL", "OPENAI_BASE_URL", "VISUAL_BASE_URL",
                "MODEL_REASONING", "VISUAL_REASONING", "MAX_OUTPUT_TOKENS")}}
        if self.optimizations.deterministic_schema:
            from hashlib import sha256
            configuration["deterministic_schema_hash"] = sha256(Path(self.optimizations.deterministic_schema).read_bytes()).hexdigest()
        graph = DependencyGraph(self.output_dir)
        self.resume_manager = ResumeManager(self.output_dir, self.input_dir, graph, configuration,
            enabled=self.resume_enabled, changed_nodes=self.changed_nodes)
        save_json(self.arc_dir / "resume-plan.json", self.resume_manager.rerun)
        if self.resume_preview:
            show_panel("Resume impact preview", str(self.resume_manager.rerun))
            return self.output_dir
        recorder = UsageRecorder(self.output_dir, metadata={**self.resume_manager.identity,
            "resume": self.resume_enabled, "optimizations": asdict(self.optimizations)})
        with ExitStack() as stack:
            stack.enter_context(recorder.activate())
            stack.enter_context(graph.activate())
            stack.enter_context(optimization_scope(self.optimizations))
            stack.enter_context(self.resume_manager.activate())
            result = self._compile()
            recorder.finish(self.report["status"])
            return result

    def _compile(self) -> Path:
        """Execute the 13 compilation passes in order and return the generated application directory."""
        self.artifacts.clear()
        self.report = {"status": "RUNNING", "stages": {}}
        show_panel("Requirement compiler | Pipeline",
            f"Input: {self.input_dir}\nOutput: {self.output_dir}\nPasses: 13\nDetailed model logs: {'enabled' if self.model_log else 'disabled'}")
        stages = (
            ("project_files", "00_project_initialization", "project_initialization", self.output_dir),
            ("requirements", "01_explore_requirement", "explore_requirement", self.input_dir, self.arc_dir),
            ("db_schema", "02_build_db_schema", "build_db_schema", self.arc_dir),
            ("db_obligations", "03_infer_db_obligations", "infer_db_obligations", self.arc_dir),
            ("db_obligations", "04_check_db_obligations", "check_db_obligations", self.arc_dir),
            ("db_implementations", "05_implement_db", "implement_db", self.arc_dir),
            ("backend_apis", "06_design_backend_apis", "design_backend_apis", self.arc_dir),
            ("backend_apis", "07_check_backend_apis", "check_backend_apis", self.arc_dir),
            ("backend_checks", "08_implement_backend_tdd", "implement_backend_tdd", self.arc_dir),
            ("ui_design", "09_design_ui", "design_frontend_ui", self.input_dir, self.arc_dir),
            ("ui_checks", "10_implement_ui", "implement_frontend_ui", self.input_dir, self.arc_dir),
            ("frontend_triggers", "11_design_frontend_triggers", "design_frontend_triggers", self.input_dir, self.arc_dir),
            ("frontend_e2e", "12_implement_frontend_tdd", "implement_frontend_triggers", self.input_dir, self.arc_dir),
        )
        for number, (artifact, module, function, *args) in enumerate(stages):
            if number > self.until_stage:
                break
            options = {"port": self.port} if number == 0 else {}
            self.artifacts[artifact] = self._run(module, function, *args, **options)
            if self.until_stage < 12 and self.report["stages"][module]["status"] in BAD_STATUS:
                break
        self.current_stage = None
        incomplete = [stage for stage, result in self.report["stages"].items()
            if result["status"] in {"FAILED", "COMPLETED_WITH_FAILURES"}]
        self.report["status"] = ("COMPLETED_WITH_FAILURES" if incomplete else
            "GREEN_PREFIX" if self.until_stage < 12 else "GREEN")
        from infrastructure.shared.artifacts.coverage import build_coverage_matrix
        build_coverage_matrix(self.output_dir)
        save_json(self.arc_dir / "compilation.json", self.report)
        checkpoint(self.output_dir, stage="compiler", task="pipeline", action="Publish final compilation report",
            status=self.report["status"], details={"stages": self.report["stages"], "incomplete_stages": incomplete})
        show_panel("[DONE] Requirement compilation | " + self.report["status"],
            f"Application: {self.output_dir}\nReport: {self.arc_dir / 'compilation.json'}"
            + ("\nIncomplete stages: " + ", ".join(incomplete) if incomplete else ""),
            tone="error" if incomplete else "success")
        return self.output_dir
