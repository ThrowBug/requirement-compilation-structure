"""Declared requirement/step-to-suite links, separated from assertion coverage."""

import json
from pathlib import Path

from infrastructure.utils.files import save_json


def build_coverage_matrix(output):
    output = Path(output)
    arc = output / ".arc"
    def load(name):
        try:
            value = json.loads((arc / name).read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}
    from infrastructure.stages.requirements.tree import RequirementTree
    result = {"schema_version": 1, "scope": "declared suite ownership; step assertions are not inferred", "requirements": {}}
    try:
        tree = RequirementTree(load("requirements.json"))
    except (KeyError, TypeError, ValueError, AttributeError):
        result["status"] = "UNKNOWN_REQUIREMENTS"
        save_json(arc / "coverage.json", result)
        return result
    paths, backend, triggers = load("implementation_paths.json"), load("backend_apis.json"), load("frontend_triggers.json")
    acceptance, frontend = load("independent_acceptance.json"), load("frontend_e2e.json")
    for rid in tree.leaves:
        aids = backend.get("requirements", {}).get(rid, [])
        suites = [paths.get("backend_test", {}).get(aid) for aid in aids]
        suites += [paths.get("e2e_test", {}).get(rid)]
        entry = {"suites": sorted(set(path for path in suites if path)), "steps": {},
            "process_status": frontend.get("requirements", {}).get(rid, {}).get("status", "UNKNOWN"),
            "independent_status": acceptance.get("requirements", {}).get(rid, {}).get("status", "UNKNOWN")}
        for sid in tree.own_steps[rid]:
            entry["steps"][sid] = {"triggers": triggers.get("steps", {}).get(sid, []),
                "candidate_suites": entry["suites"], "assertion_coverage": "NOT_MEASURED"}
        result["requirements"][rid] = entry
    result["status"] = "RECORDED"
    from infrastructure.shared.artifacts.dependency_graph import current_graph
    if graph := current_graph():
        for rid, entry in result["requirements"].items():
            for sid in entry["steps"]:
                node = "coverage:" + sid
                graph.register(node, requirement=rid, assertion_coverage="NOT_MEASURED")
                for path in entry["suites"]:
                    graph.depend(node, "suite:" + path)
        graph.flush()
    save_json(arc / "coverage.json", result)
    return result
