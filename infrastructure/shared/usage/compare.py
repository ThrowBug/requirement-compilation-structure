"""Compare paired saved runs: python -m infrastructure.shared.usage.compare A B."""

import argparse
import json
from pathlib import Path

from infrastructure.utils.files import save_json


def compare_runs(baseline, candidate):
    def load(root, relative):
        try:
            value = json.loads((Path(root) / relative).read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}
    def outcomes(root):
        result = {}
        def visit(value, path):
            if isinstance(value, dict):
                if isinstance(value.get("status"), str):
                    result[path] = value["status"]
                for key, item in value.items():
                    visit(item, path + "/" + key)
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    visit(item, path + "/" + str(index))
        for name in ("compilation", "backend_tdd", "ui_checks", "frontend_e2e", "independent_acceptance"):
            visit(load(root, f".arc/{name}.json"), name)
        return result
    left, right = (load(root, ".arc/usage/usage.json") for root in (baseline, candidate))
    old, new = outcomes(baseline), outcomes(candidate)
    changes = {key: {"baseline": old.get(key), "candidate": new.get(key)}
        for key in old.keys() | new.keys() if old.get(key) != new.get(key)}
    same_input = bool(left.get("metadata", {}).get("input_hash")) and left.get("metadata", {}).get("input_hash") == right.get("metadata", {}).get("input_hash")
    complete = all(record.get("totals", {}).get("usage_complete") is True
        and record.get("totals", {}).get("total_tokens") is not None for record in (left, right))
    metrics = {}
    for key in ("total_tokens", "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "calls"):
        a, b = left.get("totals", {}).get(key), right.get("totals", {}).get(key)
        metrics[key] = {"baseline": a, "candidate": b, "delta": b - a if a is not None and b is not None else None}
    for key in ("total_duration_seconds",):
        a, b = left.get(key), right.get(key)
        metrics[key] = {"baseline": a, "candidate": b, "delta": b - a if a is not None and b is not None else None}
    return {"same_input": same_input, "usage_complete": complete, "outcomes_equal": bool(old) and old == new,
        "outcome_changes": changes, "metrics": metrics,
        "candidate_status": right.get("status"),
        "eligible_for_cost_comparison": same_input and complete and bool(old) and old == new,
        "note": "Compare repeated paired runs and business outcomes before enabling an experimental policy."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = compare_runs(args.baseline, args.candidate)
    if args.output:
        save_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
