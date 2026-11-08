"""Step 12: connect frontend triggers to backend APIs through functional smoke tests."""

from contextlib import ExitStack
from pathlib import Path

from infrastructure.utils.cli import create_parser, resolve_directories
from infrastructure.stages.frontend.triggers.context import load_e2e_context
from infrastructure.stages.frontend.triggers.implementation import check_frontend_baseline, prepare_trigger_runtime, verify_frontend
from infrastructure.stages.frontend.triggers.acceptance import run_independent_acceptance
from infrastructure.shared.models.text import TextModel
from infrastructure.utils.presentation.console import DemoConsole
from infrastructure.utils.presentation.logging import console_log
from infrastructure.utils.files import save_json


def implement_frontend_triggers(input_dir: Path, arc_dir: Path, pause: bool = False,
        max_turns: int = 10, max_tests: int = 2) -> dict:
    if min(max_turns, max_tests) < 1:
        raise ValueError("--max-turns and --max-tests must be positive")
    tree, schema, backend, ui, interactions, images, contexts = load_e2e_context(arc_dir, input_dir)
    report = {"status": "PREPARING", "requirements": {rid: {"status": "NO_DIRECT_TRIGGERS", "triggers": []}
        for rid in tree.requirements if tree.children[rid]}}
    save_json(arc_dir / "frontend_e2e.json", report)
    try:
        if not ui["pages"]:
            report.update({"status": "NO_UI", "summary": {"requirements": 0, "triggers": 0, "e2e_tests": 0, "ui_tests": 0, "failed": 0}})
            save_json(arc_dir / "frontend_e2e.json", report)
            return report
        model, console = TextModel(), DemoConsole(pause)
        _, checks = prepare_trigger_runtime(arc_dir, backend, ui, interactions, console, max_tests)
        check_frontend_baseline(arc_dir, ui, checks, model, console, report, max_turns)
        acceptance = run_independent_acceptance(input_dir, arc_dir, schema, backend, ui, interactions,
            model, console, max_turns, process_report=report, primary=True, max_tests=max_tests)
        report["requirements"].update(acceptance["requirements"])
        if request := acceptance.get("upstream_repair_request"):
            report["upstream_repair_request"] = request
        suites = [entry["test_path"] for entry in acceptance["requirements"].values()
            if (arc_dir.parent / entry["test_path"]).is_file()]
        result = verify_frontend(arc_dir, tree, backend, ui, console, report, suites=suites, schema=schema)
        result["verification"]["independent_acceptance"] = {"status": "GREEN" if acceptance["status"] == "GREEN" else "FAILED",
            "report": ".arc/independent_acceptance.json"}
        if acceptance["status"] != "GREEN":
            result["status"] = "COMPLETED_WITH_FAILURES"
        save_json(arc_dir / "frontend_e2e.json", result)
        print(f"Frontend functional smoke {result['status']}: {result['summary']}\nReport: {arc_dir / 'frontend_e2e.json'}", flush=True)
        return result
    except BaseException as error:
        report.update({"status": "FAILED", "error": str(error)})
        if request := getattr(error, "upstream_repair_request", None):
            report["upstream_repair_request"] = request
        for requirement in report["requirements"].values():
            if requirement["status"] not in {"GREEN", "NO_TRIGGERS", "NO_DIRECT_TRIGGERS"}:
                requirement["status"] = "FAILED"
        save_json(arc_dir / "frontend_e2e.json", report)
        raise


def main() -> None:
    parser = create_parser("Step 12: Frontend/Backend Integration with Playwright Functional Smoke Tests")
    parser.add_argument("--max-turns", type=int, default=10, help="Maximum edit rounds per requirement's test/implementation agent")
    parser.add_argument("--max-tests", type=int, default=2, help="Maximum functional smoke cases per requirement (default: 2)")
    args = parser.parse_args()
    with ExitStack() as stack:
        try:
            input_dir, output_dir = resolve_directories(args)
            stack.enter_context(console_log(output_dir, debug_model_io=args.debug_model_io))
            result = implement_frontend_triggers(input_dir, output_dir / ".arc", args.pause, args.max_turns, args.max_tests)
        except (OSError, ValueError, RuntimeError) as error:
            parser.exit(1, f"Execution failed: {error}\n")

    if result["status"] == "COMPLETED_WITH_FAILURES":
        parser.exit(1, "All implementation tasks were attempted; some acceptance checks failed. See the .arc report.\n")


if __name__ == "__main__":
    main()
