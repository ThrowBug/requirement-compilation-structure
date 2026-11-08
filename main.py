"""Compile a requirement directory into an executable application."""

import argparse
from pathlib import Path
import sys

from infrastructure.compiler import Compiler
from infrastructure.utils.cli import resolve_directories
from infrastructure.utils.presentation.logging import configure_console
from infrastructure.shared.agent.experiments import OptimizationOptions
from infrastructure.shared.models import ProviderTransportUnavailable


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Read public options and validate paths and the backend port before compilation."""
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("requirement_path", type=Path,
        help="Requirement directory containing requirements.yaml and reference assets")
    parser.add_argument("-o", "--output-dir", type=Path, required=True,
        help="Directory for the generated application and .arc artifacts")
    parser.add_argument("--model-log", "--debug-model-io", dest="model_log", action="store_true",
        help="Show detailed model I/O and save per-call prompt/response logs; usage and timing are always shown")
    parser.add_argument("--port", type=int, default=3000,
        help="Default generated backend port and frontend API proxy target (default: 3000)")
    parser.add_argument("--resume", action="store_true", help="Reuse version-checked completed stages and validated design proposals")
    parser.add_argument("--resume-preview", action="store_true", help="Write .arc/resume-plan.json without running stages")
    parser.add_argument("--until-stage", type=int, choices=range(13), default=12, metavar="00-12",
        help="Stop after this stage and save a reusable prefix in the output directory")
    parser.add_argument("--changed-node", action="append", default=[], help="Explicit dependency node to invalidate (repeatable)")
    dedup = parser.add_mutually_exclusive_group()
    dedup.add_argument("--context-dedup", action="store_true", help="Experimentally encode repeated evidence as JSON Pointer references")
    dedup.add_argument("--no-context-dedup", action="store_true", help="Keep literal evidence (default; retained for existing commands)")
    parser.add_argument("--experimental-max-task-calls", type=int)
    parser.add_argument("--experimental-max-read-characters", type=int)
    parser.add_argument("--experimental-small-model")
    parser.add_argument("--experimental-small-model-task", action="append", default=[])
    parser.add_argument("--experimental-cache-dir", type=Path)
    parser.add_argument("--experimental-deterministic-schema", type=Path, help="Reviewed per-requirement simple schema proposals, keyed by requirement ID")
    parser.add_argument("--experimental-failure-advice", action="store_true")
    args = parser.parse_args(argv)
    try:
        args.optimizations = OptimizationOptions(context_dedup=args.context_dedup,
            max_task_calls=args.experimental_max_task_calls,
            max_read_characters=args.experimental_max_read_characters,
            small_model=args.experimental_small_model, small_model_tasks=tuple(args.experimental_small_model_task),
            cache_dir=str(args.experimental_cache_dir.resolve()) if args.experimental_cache_dir else None,
            deterministic_schema=str(args.experimental_deterministic_schema.resolve()) if args.experimental_deterministic_schema else None,
            failure_advice=args.experimental_failure_advice)
        if args.experimental_deterministic_schema and not args.experimental_deterministic_schema.is_file():
            raise ValueError("Deterministic schema proposal file does not exist")
        if not 1 <= args.port <= 65535:
            raise ValueError("--port must be between 1 and 65535")
        args.requirement_path, args.output_dir = resolve_directories(
            argparse.Namespace(input=args.requirement_path, output=args.output_dir))
        if not args.requirement_path.is_dir():
            raise ValueError(f"Input directory does not exist: {args.requirement_path}")
        if not (args.requirement_path / "requirements.yaml").is_file():
            raise ValueError(f"Missing requirement document: {args.requirement_path / 'requirements.yaml'}")
        if args.output_dir.exists() and not args.output_dir.is_dir():
            raise ValueError(f"Output must be a directory: {args.output_dir}")
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return args


def main(argv: list[str] | None = None) -> None:
    configure_console()
    args = parse_args(argv)
    options = {}
    if args.resume or args.resume_preview or args.changed_node:
        options.update(resume=args.resume, resume_preview=args.resume_preview, changed_nodes=args.changed_node)
    if args.until_stage != 12:
        options["until_stage"] = args.until_stage
    if args.optimizations != OptimizationOptions():
        options["optimizations"] = args.optimizations
    compiler = Compiler(args.requirement_path, args.output_dir, model_log=args.model_log, port=args.port, **options)
    try:
        compiler.compile()
    except KeyboardInterrupt:
        print(f"Compilation interrupted at {compiler.current_stage or 'startup'}.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as error:
        print(f"Compilation failed at {compiler.current_stage or 'startup'}: {error}", file=sys.stderr)
        raise SystemExit(1 if isinstance(error, ProviderTransportUnavailable) else 0)
    if compiler.report["status"] == "COMPLETED_WITH_FAILURES":
        print(f"All passes completed. Report: {compiler.arc_dir / 'compilation.json'}", file=sys.stderr)
        raise SystemExit(0)


if __name__ == "__main__":
    main()
