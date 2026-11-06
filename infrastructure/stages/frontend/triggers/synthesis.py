"""Design requirement-scoped triggers after checking upstream API capabilities."""

import json
from copy import deepcopy
from hashlib import sha256

from infrastructure.stages.frontend.triggers.prompts import INSTRUCTIONS
from infrastructure.stages.frontend.triggers.design import apply_decomposition, empty_triggers, relevant_pages, trigger_obligations, api_ids, validate_coverage, validate_shared_changes
from infrastructure.stages.frontend.ui.design import data_symbols
from infrastructure.utils.files import save_json, read_artifact
from infrastructure.stages.database.obligations.records import normalize_obligations
from infrastructure.shared.agent.proposals import propose_json
from infrastructure.shared.agent.tasks import TaskFailures, require_design_task
from infrastructure.shared.agent.discovery import ContextIndex
from infrastructure.utils.paths import FILE_STEM_RULE, validate_file_stems
from infrastructure.shared.agent.conventions import load_conventions, select_conventions
from infrastructure.stages.backend.primitives import validate_design, ObligationCoverageError, explain_accepted_regression


def unique_api_evidence(apis, required):
    """Make uniquely supplied database effects visible before the model proposes bindings."""
    result = {}
    for mode in ("read", "write"):
        for field in required[mode]:
            providers = [aid for aid, api in apis.items() if field in api[mode]]
            if len(providers) == 1:
                result.setdefault(providers[0], {}).setdefault(mode, []).append(field)
    return result


def scope_shared_variants(current, proposal, rid, accepted):
    """Separate current API branches from an already accepted action on the same control."""
    if proposal.get("shared_changes"):
        return proposal, {}
    variants = {}
    used = {tid.casefold() for tid in current["triggers"].keys() | proposal["triggers"].keys()}
    direct = set(proposal["requirement_triggers"])
    for tid, trigger in proposal["triggers"].items():
        old = current["triggers"].get(tid)
        if (old is None or tid not in direct or not any(
                tid in current["requirements"].get(other, ()) for other in accepted)):
            continue
        if ((old["source"], old["event"]) != (trigger["source"], trigger["event"])
                or set(api_ids(old)) == set(api_ids(trigger))):
            continue
        suffix = sha256(rid.encode()).hexdigest()[:8]
        alias = f"{tid}-{suffix}"
        if alias.casefold() in used:
            continue
        variants[tid] = alias
        used.add(alias.casefold())
    if not variants:
        return proposal, variants
    scoped = deepcopy(proposal)
    for tid, alias in variants.items():
        scoped["triggers"][alias] = scoped["triggers"].pop(tid)
    scoped["requirement_triggers"] = [variants.get(tid, tid) for tid in scoped["requirement_triggers"]]
    scoped["step_triggers"] = {sid: [variants.get(tid, tid) for tid in ids]
        for sid, ids in scoped["step_triggers"].items()}
    return scoped, variants


def synthesize_triggers(arc_dir, ui, tree, backend, contexts, model, console, max_attempts=10, *, dependencies=None):
    result = empty_triggers(tree)
    obligations = normalize_obligations(read_artifact(arc_dir, "db_obligations.json"), tree)
    accepted = []
    failures = TaskFailures(arc_dir, "11_design_frontend_triggers", console)
    save_json(arc_dir / "frontend_triggers.json", result)
    for rid in tree.leaves:
        with failures.task(rid):
            label = f"Trigger design {rid}"
            if not relevant_pages(ui, rid):
                continue
            require_design_task(arc_dir, "06_design_backend_apis", rid, "backend_apis.json", backend["requirements"])
            try:
                validate_design({"apis": backend["apis"],
                    "requirements": {rid: backend["requirements"][rid]}},
                    obligations, complete=False, tree=None)
            except ObligationCoverageError as error:
                raise ValueError(f'Upstream backend contract cannot cover {rid}: {error}. '
                    'Review stage 03 obligations and stage 06 API design before generating triggers.') from error
            pages = relevant_pages(ui, rid)
            # Entry and destination controls may belong to an explicit prerequisite.
            # Include those pages once rather than making the model rediscover them.
            for dependency in (dependencies or {}).get(rid, ()):
                pages.update(relevant_pages(ui, dependency))
            elements = {uid for page in pages.values() for uid in page["elements"]}
            local_ui = {**{key: value for key, value in ui.items() if key != "presentation_contracts"},
                "pages": {pid: ui["pages"][pid] for pid in pages},
                "elements": {uid: ui["elements"][uid] for uid in elements},
                "requirements": {rid: ui["requirements"].get(rid, [])},
                "steps": {sid: ui["steps"].get(sid, []) for sid in tree.own_steps[rid]}}
            existing = {tid: trigger for tid, trigger in result["triggers"].items() if trigger["source"] in elements}
            aids = set(backend["requirements"].get(rid, [])) | {aid for trigger in existing.values() for aid in api_ids(trigger)}
            apis = {aid: backend["apis"][aid] for aid in sorted(aids)}
            conventions = load_conventions(arc_dir)
            request = {"requirement": contexts[rid], "ui": local_ui,
                "required_obligations": obligations["requirements"][rid],
                "step_obligations": {sid: obligations["steps"][sid] for sid in tree.own_steps[rid]},
                "shared_conventions": select_conventions(conventions, requirements=[rid]),
                "existing_triggers": existing, "trigger_owners": {tid: result["owners"][tid] for tid in existing},
                "backend_apis": apis,
                "unique_api_evidence": unique_api_evidence(apis, obligations["requirements"][rid])}
            def accept(proposal):
                for sid in tree.own_steps[rid]:
                    proposal["step_triggers"][sid]
                if set(proposal["step_triggers"]) - set(tree.own_steps[rid]):
                    raise ValueError("Do not reassign steps owned by another requirement")
                scoped, variants = scope_shared_variants(result, proposal, rid, accepted)
                if variants:
                    try:
                        candidate = apply_decomposition(result, scoped, rid)
                        validate_file_stems(candidate["triggers"], "trigger ID")
                        validate_coverage(candidate, backend, obligations, tree, [*accepted, rid])
                        validate_shared_changes(result, scoped, rid, accepted)
                    except (ValueError, KeyError, TypeError):
                        # Preserve ordinary rejection and diagnostics if a rename
                        # does not fully satisfy the current and prior contracts.
                        pass
                    else:
                        console.show(label + " | Scoped shared control variants", json.dumps(variants))
                        proposal = scoped
                updated = apply_decomposition(result, proposal, rid)
                validate_file_stems(updated["triggers"], "trigger ID")
                try:
                    validate_coverage(updated, backend, obligations, tree, [*accepted, rid])
                except ObligationCoverageError as error:
                    explain_accepted_regression(error, result, updated, rid, "triggers",
                        lambda trigger: trigger_obligations(trigger, backend), tree)
                    raise
                validate_shared_changes(result, proposal, rid, accepted)
                derived = {tid: trigger_obligations(updated["triggers"][tid], backend)
                    for tid in updated["requirements"][rid]}
                console.show(label + " | Derived DB effects", json.dumps(derived, ensure_ascii=False, indent=2))
                return updated

            # With every page supplied, another page/control search can return
            # no new UI evidence. Larger, partial scopes retain discovery.
            complete_ui = set(pages) == set(ui["pages"])
            index = None if complete_ui else ContextIndex(lambda: {
                "convention": conventions, "api": backend["apis"], "page": ui["pages"],
                "ui_element": ui["elements"], "ui_data": data_symbols(ui["elements"]), "trigger": result["triggers"]})
            instructions = INSTRUCTIONS + "\nTrigger IDs: " + FILE_STEM_RULE
            if complete_ui:
                instructions += ("\nAll page/control definitions and the assigned API evidence are already in the "
                    "request. Return a proposal directly; no semantic lookup is needed.")
            result, _ = propose_json(model, instructions, request, accept, console, label, max_attempts,
                context_index=index)
            accepted.append(rid)
            console.artifact("frontend_triggers", result, label)
            save_json(arc_dir / "frontend_triggers.json", result)
    failures.finish()
    save_json(arc_dir / "frontend_triggers.json", result)
    return result
