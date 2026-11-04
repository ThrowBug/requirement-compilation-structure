"""Requirement and scenario coverage over declared API database effects."""

from copy import deepcopy
from infrastructure.stages.database.obligations.rules import difference, union


class ObligationCoverageError(ValueError):
    def __init__(self, rid, required, combined, gaps):
        self.feedback = {"requirement_id": rid, "required_obligations": required,
            "api_obligation_union": combined, **gaps}
        self.model_message = f"Obligation Propagation failed for {rid}. See correction.details for the coverage gaps."
        super().__init__(f"Obligation Propagation failed for {rid}: {self.feedback}")


def explain_accepted_regression(error, before, after, rid, collection, effects, tree=None):
    """Show only changed dependencies of the failed earlier requirement or step."""
    failed = error.feedback["requirement_id"]
    owner, association, step = failed, "requirements", False
    if failed not in before["requirements"] and tree is not None:
        owner = next((key for key, steps in tree.own_steps.items() if failed in steps), None)
        step = owner is not None
        if step and "steps" in before:
            association = "steps"
    if owner is None or owner == rid:
        return
    identity = failed if association == "steps" else owner
    old_ids = before[association].get(identity, [])
    new_ids = after[association].get(identity, [])
    if not old_ids:
        return
    changes = {}
    for key in sorted(set(old_ids) | set(new_ids)):
        old = effects(before[collection][key]) if key in old_ids else {"read": [], "write": []}
        new = effects(after[collection][key]) if key in new_ids else {"read": [], "write": []}
        added, removed = difference(new, old), difference(old, new)
        if any(added.values()) or any(removed.values()):
            changes[key] = {"added": added, "removed": removed}
    if changes or old_ids != new_ids:
        error.feedback["accepted_regression"] = {"proposed_for": rid, "affected_requirement": owner,
            **({"affected_step": failed} if step else {}), "record_kind": collection,
            "changed_effects": changes, "added_assignments": sorted(set(new_ids) - set(old_ids)),
            "removed_assignments": sorted(set(old_ids) - set(new_ids)),
            "repair": "Preserve the earlier requirement and step coverage. Restore conflicting effects or "
                "use a separate compatible operation for the current requirement."}


def coverage(required, apis):
    combined = union(apis)
    # Required reads are minimum capabilities, not a prohibition on supporting
    # lookups, joins or reusable response projections. Writes retain their boundary.
    return {"missing": difference(required, combined),
        "excess": {"read": [], "write": sorted(set(combined["write"]) - set(required["write"]))}}


def validate_design(design, obligations, complete=True, tree=None):
    """Check required reads, exact possible writes, and each scenario's API coverage."""
    expected = obligations["requirements"] if complete else design["requirements"]
    for rid in expected:
        if tree is not None and tree.children.get(rid):
            continue
        apis = [design["apis"][aid] for aid in design["requirements"][rid]]
        required = obligations["requirements"][rid]
        gaps = coverage(required, apis)
        if any(fields for gap in gaps.values() for fields in gap.values()):
            raise ObligationCoverageError(rid, required, union(apis), gaps)
        if tree is None:
            continue
        combined = union(apis)
        for sid in tree.own_steps[rid]:
            step_required = obligations["steps"][sid]
            missing = difference(step_required, combined)
            if any(missing.values()):
                raise ObligationCoverageError(sid, step_required, combined, {"missing": missing})


def apply_decomposition(design, response, rid, obligations, tree=None):
    result = deepcopy(design)
    result["apis"].update(deepcopy(response["apis"]))
    result["requirements"][rid] = response["requirement_apis"]
    try:
        validate_design(result, obligations, complete=False, tree=tree)
    except ObligationCoverageError as error:
        explain_accepted_regression(error, design, result, rid, "apis", lambda api: api, tree)
        raise
    return result
