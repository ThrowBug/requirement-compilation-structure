"""Requirement-derived browser acceptance isolated from application repair."""

from hashlib import sha256
import json
import re
import subprocess
import yaml

from infrastructure.shared.agent.tools import FileToolError, FileTools
from infrastructure.shared.artifacts.dependency_graph import digest
from infrastructure.shared.agent.feedback import compact_feedback
from infrastructure.shared.agent.loop import FileGateHandoff, run_file_agent
from infrastructure.shared.agent.context import SourceFiles
from infrastructure.shared.agent.discovery import project_index
from infrastructure.shared.agent.recovery import implementation_task
from infrastructure.shared.agent.tasks import require_design_task, UpstreamTaskError
from infrastructure.shared.project.entrypoints import selected_path
from infrastructure.stages.backend.context import requirement_context, related_schema
from infrastructure.stages.backend.testing import BackendChecks, require_green as require_backend_green
from infrastructure.stages.requirements.tree import RequirementTree
from infrastructure.stages.frontend.triggers.implementation import scenario_context, IMPLEMENT_INSTRUCTIONS, editable_runtime_path, test_run_status
from infrastructure.stages.frontend.triggers.e2e_runtime import FIXTURE, OBSERVATIONS
from infrastructure.stages.frontend.triggers.testing import E2EChecks, test_path
from infrastructure.stages.frontend.triggers.scope import TriggerCheckFailure
from infrastructure.stages.frontend.triggers.design import api_ids
from infrastructure.stages.frontend.ui.runtime import APPLICATION_ENTRYPOINTS
from infrastructure.stages.frontend.ui.testing import FrontendChecks, presentation_files, require_green
from infrastructure.utils.files import read_artifact, save_bytes, save_json, save_text
from infrastructure.shared.project.git import checkpoint

INSTRUCTIONS = '''Write the independent browser acceptance suite for ONE original requirement.
Derive expected results from both the original requirement description and its Given/When/Then scenarios.
An explicit behavior in the description remains required even if no scenario restates it.
Apply parent_requirements only where a shared rule applies to the current scenario.
Interface details are supplied only to locate UI elements/routes and prepare valid Given
fixtures; they are not authority to weaken an original business requirement. Neither
application source nor implementation-owned tests are supplied. Do not infer expected
behavior from an observed implementation result. This suite drives implementation and regression.

Write a small Playwright suite at test_path using the supplied test/expect/database
fixture. Drive When actions through the real browser without mocks or injected state.
Use UI IDs or accessible locators for actions. Assert original required visible business
outcomes, not design-inferred internal labels or every DOM attribute. Cover main success,
critical rejection, correct identity/ownership, and refresh/persistence WHEN required.
Use named_role_controls as a checklist of explicit role/name contracts on the routes
visited by this requirement. Cover applicable shared navigation even when the scenario
also has a narrower account action. A link appearance does not prove its session behavior.
For exact names in the original requirements, use exact: true in getByRole and getByLabel.
Scope shared text or links to their semantic container before selecting them.
Use two distinct identities where identity isolation matters; a test that only creates
one account cannot reject an implementation returning the first account for everyone.
For required rejection-without-writes, verify the relevant persisted result is unchanged.
Prefer user-visible outcomes over unspecified cookie/hash encodings or storage strategies.
Transport URLs chosen by generated API designs are not original business requirements.
Do not invent a required endpoint, response shape or status from a generated API name.
If original_requirement explicitly specifies an HTTP contract, assert that exact contract.
Otherwise assert visible completion and the required persisted result, not a particular
internal URL or response body. For a single required mutation, use observeMutations from
the supplied fixture around the When action and its retrying visible-completion assertion;
assert one matching mutation across ALL endpoints, using submitted business identity/data
to exclude unrelated mutations. An earlier and a newer registration URL are not two
required submissions. Preserve rejection-without-writes and distinct-identity assertions.
Request data remain inspectable after navigation; do not wait for an unspecified response
body after a full document navigation. Do not count all application requests globally.
Use test_count_hint as a size guide, never as a reason to omit an explicit outcome.
Cover every required scenario outcome, including cancel/no-write where stated.

The database fixture restores requirement-defined startup data before each test. Reuse
those records when required; do not assume an empty database or insert duplicate identities.
Use prerequisite records to isolate the current requirement from other requirements'
registration/creation implementations. Arrange scenario-specific absence or conflicting
Given states in the isolated test database; still drive the current When through the UI.
Seed additional minimal valid Given records using the documented database/test utilities. Application
sources, shared fixtures, original requirements and other suites are not editable.
For successful submissions, perform every required action (including checking consent),
including when reusing a shared form for setup. For dates relative to today, derive valid
input dates at test execution and compare stored dates with the values actually submitted.
Never assert a guessed year or prefix, or rely on implied consent/default form state.
During generation only TypeScript is checked; missing application behavior is a useful
acceptance failure, never a reason to weaken assertions. Use edit_file to create or repair only test_path under frontend/tests/e2e/acceptance.'''

REVIEW_INSTRUCTIONS = '''Review this independent acceptance test against the original requirement.
This is a separate test review, not implementation repair. Actual failure evidence is
provided. Diagnose test_requirement_mismatch, test_overconstraint, environment, or
implementation. Change ONLY test_path if specific original requirement or fixture evidence
shows the test is wrong. Preserve every required business outcome. If the implementation
or environment is wrong, return tool_calls=[] and explain it in diagnosis.reason; the failed acceptance remains
failed. Compilation passing is enough to finish this review; you are NOT asked to turn a
business failure GREEN. Never adjust expected values just to match observed wrong behavior.
Use original requirements as authority; no application source or process tests are available.'''

REVIEW_INSTRUCTIONS += '''
Review the complete failing workflow and its later assertions, not just the first error.
Check all required input actions, consent and fixture preconditions. Compare persisted
dates against the submitted values, not a fixed year prefix when the requirement is relative.
Fix missing actions or unjustified assertions in the test; do not ask the application to
default consent to true to make an incomplete test pass. Use cause=uncertain and no edits
if the evidence cannot distinguish a test defect from an implementation defect.
Review endpoint assumptions as well as selectors. A generated API URL is not an original
requirement. If the original does not prescribe transport, correct a hardcoded endpoint
wait into visible-business completion, persisted-result checks and observeMutations.
Preserve detection of duplicate writes/submissions across endpoint aliases. Never change
an explicit original HTTP contract or remove success, rejection or identity coverage.
'''

ORACLE_POLICY_VERSION = 3


EXACT_ROLE = re.compile(r'["“]([^"”\r\n]+)["”]\s*\(\s*role\s*=\s*([a-z][\w-]*)\s*\)')
SELECTOR_AST = r"""
const fs = require('node:fs');
const ts = require('typescript');
const { source, names } = JSON.parse(fs.readFileSync(0, 'utf8'));
const pairs = new Set(names.map(([role, name]) => role + '\0' + name));
const parsed = ts.createSourceFile('acceptance.spec.ts', source, ts.ScriptTarget.Latest, true);
const points = [];
function visit(node) {
  if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)
      && node.expression.name.text === 'getByRole') {
    const [role, options] = node.arguments;
    if (role && ts.isStringLiteral(role) && options && ts.isObjectLiteralExpression(options)
        && !options.properties.some(p => ts.isSpreadAssignment(p) || p.name?.text === 'exact')) {
      const name = options.properties.find(p => ts.isPropertyAssignment(p) && p.name?.text === 'name');
      if (name && ts.isStringLiteral(name.initializer)
          && pairs.has(role.text + '\0' + name.initializer.text)) {
        points.push(options.getStart(parsed) + 1);
      }
    }
  }
  ts.forEachChild(node, visit);
}
visit(parsed);
let result = source;
for (const index of points.sort((a, b) => b - a)) {
  result = result.slice(0, index) + ' exact: true,' + result.slice(index);
}
process.stdout.write(result);
"""


def declared_role_names(task):
    original = task["original_requirement"]
    texts = [original["description"], *(record["description"] for record in task.get("parent_requirements", ()))]
    texts.extend(step[key] for step in original["steps"] for key in ("Given", "When", "Then"))
    return {(role, label) for value in texts if isinstance(value, str)
        for label, role in EXACT_ROLE.findall(value)}


def normalize_exact_role_locators(output, path, task):
    """Strengthen literal role selectors only when the original YAML declares the name."""
    names = declared_role_names(task)
    if not names or not (output / "node_modules/typescript/package.json").is_file():
        return
    suite = output / path
    source = suite.read_text(encoding="utf-8-sig")
    try:
        rewritten = subprocess.run(["node", "-e", SELECTOR_AST], cwd=output,
            input=json.dumps({"source": source, "names": sorted(names)}, ensure_ascii=False),
            encoding="utf-8", capture_output=True, timeout=15, check=True).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("Unable to normalize literal selectors with the installed TypeScript parser") from error
    if rewritten != source:
        save_text(suite, rewritten)


class ReviewOracle(FileGateHandoff):
    """Ask the isolated reviewer to evaluate a suspected oracle problem."""
    def __init__(self, result, diagnosis):
        self.result, self.diagnosis = result, diagnosis


def current_suite_failure(result, path, ran_regressions):
    """A mixed or unattributed regression cannot authorize changing the current oracle."""
    failures = (result or {}).get('failures', [])
    if not failures or result.get('errors'):
        return False
    if not ran_regressions:
        return True
    return all(failure_in_suite(message, path) for message in failures)


def failure_in_suite(message, path):
    location = next((line.strip().removeprefix('Test file: ').replace('\\', '/')
        for line in reversed(message.splitlines()) if line.strip().startswith('Test file: ')), '')
    return bool(location) and (location == path or path.endswith('/' + location))


def require_acceptance(result):
    require_green(result)
    if result.get('skipped') or result.get('flaky'):
        raise ValueError('Independent acceptance includes skipped or unstable tests')


def acceptance_path(rid):
    return 'frontend/tests/e2e/acceptance/' + sha256(rid.encode()).hexdigest()[:12] + '.spec.ts'


def original_requirements(input_dir):
    """Read the original YAML again; no generated design is used as the test oracle."""
    path = input_dir / 'requirements.yaml'
    if not path.is_file():
        raise ValueError('Independent acceptance requires original input/requirements.yaml')
    return RequirementTree(yaml.safe_load(path.read_text(encoding='utf-8-sig')))


def trigger_handoff(output, design, rid, triggers, implemented):
    """Separate current actions and reuse; IDs do not require dedicated source files."""
    owned = set(design['requirements'].get(rid, ()))
    result = {}
    for tid in sorted(triggers):
        path = selected_path(output, 'trigger', tid) or f'frontend/src/triggers/{tid}.ts'
        existing = path if (output / path).is_file() else None
        result[tid] = {'owner': design.get('owners', {}).get(tid),
            'status': 'reuse' if tid in implemented else 'implement' if tid in owned else 'dependency',
            'existing_handler': existing}
    return result


def frontend_repair_context(context, apis):
    """Keep frontend wiring evidence; larger designs remain available on demand."""
    direct = {trigger["source"] for trigger in context["triggers"].values()}
    direct.update(action["ui"] for trigger in context["triggers"].values()
        for action in trigger["action"] if action["kind"] == "ui")
    controls = {"input", "checkbox", "select", "button", "form", "link", "radio", "radiogroup"}
    interface_props = {"label", "name", "text", "role", "placeholder", "input_type", "visible", "href", "options"}
    pages = {}
    for pid, page in context["page_subtrees"].items():
        elements = {}
        for uid, element in page["elements"].items():
            if uid in direct or element.get("type") in controls:
                elements[uid] = {**{key: element[key] for key in ("type", "parent", "data") if key in element},
                    "prop": {key: value for key, value in element.get("prop", {}).items() if key in interface_props}}
        pages[pid] = {"route": page["route"], "root": page["root"], "elements": elements}
    return {"page_subtrees": pages, "triggers": context["triggers"],
        "backend_apis": {aid: {key: api[key] for key in ("signature", "request", "response", "description")
            if key in api} for aid, api in apis.items()}}


class FirstEditWorkspace(FileTools):
    """Use the first turn for an edit when runtime and source evidence is complete."""
    def __init__(self, workspace):
        super().__init__(workspace.output, writable=workspace.writable, references=workspace.files,
            target=workspace.target, allow_write=workspace.allow_write, readable=workspace.readable)
        self.edit_first = True
        self.received_initial_feedback = False

    def schemas(self):
        return [tool for tool in super().schemas() if not self.edit_first or tool["tool"] != "read_file"]

    def effect(self, calls):
        effect = super().effect(calls)
        if self.edit_first and effect == "read":
            raise FileToolError("READ_DEFERRED",
                "Use the supplied source and verified HTTP contract for the first edit; read access follows feedback.")
        return effect


def label_stylesheets(workspace):
    """Retain styles that can inject visible decoration into accessible names."""
    selected = set()
    for path in workspace.files:
        if path.endswith(".css") and workspace.path(path).is_file():
            css = workspace.path(path).read_text(encoding="utf-8-sig")
            if re.search(r"""::(?:before|after)\s*\{[^}]*\bcontent\s*:\s*['"][^'"]*[\*∗]""",
                    css, re.I | re.S):
                selected.add(path)
    return selected


def paired_action_context(ui, design, rid):
    """Only expand a repair to one peer that binds a different API on the same control."""
    definitions = design["triggers"]
    def bindings(owner):
        return {(definitions[tid]["source"], definitions[tid]["event"]): set(api_ids(definitions[tid]))
            for tid in design["requirements"].get(owner, ()) if api_ids(definitions[tid])}
    own = bindings(rid)
    peers = []
    for other in design["requirements"]:
        if other == rid:
            continue
        alternate = bindings(other)
        if any(own[key] != alternate[key] for key in own.keys() & alternate.keys()):
            peers.append(other)
    if len(peers) == 1:
        peer = peers[0]
        return peer, scenario_context(ui, design, peer)
    return None, None


def run_independent_acceptance(input_dir, arc_dir, schema, backend, ui, design, model, console,
        max_turns=10, *, process_report, primary=False, max_tests=2):
    output = arc_dir.parent.resolve()
    tree = original_requirements(input_dir)
    parents = {child: owner for owner, children in tree.children.items() for child in children}
    checks = E2EChecks(output)
    ui_checks = FrontendChecks(output)
    backend_checks = BackendChecks(output)
    verified_http = (read_artifact(arc_dir, 'backend_tdd.json').get('green', {})
        if primary and (arc_dir / 'backend_tdd.json').is_file() else {})
    selection = SourceFiles(output, ui)
    backend_selection = SourceFiles(output, root='backend/src', tests_root='backend/tests')
    context_index = project_index(output,
        kinds=('schema', 'api', 'page', 'ui_element', 'ui_data', 'trigger', 'requirement',
            'test_requirement', 'convention', 'file'),
        file_roots=('frontend/src', 'frontend/tests', 'backend/src', 'backend/tests'))
    accepted = []
    implemented = set()
    verified_process = set()
    pending_verification = any(check['status'] == 'FAILED'
        for check in process_report.get('verification', {}).values())
    previous = read_artifact(arc_dir, 'independent_acceptance.json') if (arc_dir / 'independent_acceptance.json').is_file() else {}
    report = {'status': 'RUNNING', 'requirements': {
        rid: {'status': 'PENDING', 'test_path': acceptance_path(rid)} for rid in tree.leaves}}
    save_json(arc_dir / 'independent_acceptance.json', report)
    for rid in tree.leaves:
        path = acceptance_path(rid)
        entry = {'status': 'GENERATING', 'test_path': path}
        report['requirements'][rid] = entry
        label = 'Independent acceptance / ' + rid
        try:
            require_design_task(arc_dir, "06_design_backend_apis", rid, "backend_apis.json", backend["requirements"])
            require_design_task(arc_dir, "11_design_frontend_triggers", rid,
                "frontend_triggers.json", design["requirements"])
            context = scenario_context(ui, design, rid)
            aids = set(backend['requirements'].get(rid, []))
            for trigger in context['triggers'].values():
                aids.update(api_ids(trigger))
            apis = {aid: backend['apis'][aid] for aid in aids}
            task = {'original_requirement': requirement_context(tree, rid), 'test_path': path,
                'ui_interfaces': {pid: {key: page[key] for key in ('route', 'root', 'elements')}
                    for pid, page in context['page_subtrees'].items()},
                'schema': related_schema(schema, apis.values())}
            if primary:
                task['test_count_hint'] = max_tests
                task['parent_requirements'] = []
                parent = parents.get(rid)
                while parent is not None:
                    if tree.requirements[parent]['description']:
                        task['parent_requirements'].insert(0, tree.requirements[parent])
                    parent = parents.get(parent)
                task['named_role_controls'] = [
                    {'role': role, 'name': name} for role, name in sorted(declared_role_names(task))]
                entry['target_triggers'] = sorted(design['requirements'].get(rid, ()))
                entry['original_requirement_hash'] = digest(
                    [task['original_requirement'], task['parent_requirements']])
            if not task['ui_interfaces']:
                entry.update(status='FAILED', error='No UI interface available for independent requirement acceptance; not verified')
                continue
            workspace = FileTools.for_target(output, 'acceptance_test', rid, fixed_path=path,
                references=[FIXTURE, OBSERVATIONS, 'backend/tests/support/context.ts'],
                readable=[path, FIXTURE, OBSERVATIONS, 'backend/tests/support/context.ts'])

            def compile_suite(*, normalize=True):
                if not (output / path).is_file():
                    raise ValueError('Create the supplied acceptance test_path')
                if primary and normalize:
                    normalize_exact_role_locators(output, path, task)
                result = checks.typecheck([path], full_source=False)
                if result['returncode']:
                    raise ValueError(result['output'])
                return result

            def review_suite(feedback, action):
                verdict = {}
                suite = output / path
                before = suite.read_bytes()

                def validate(response):
                    diagnosis = response.get('diagnosis')
                    cause = diagnosis.get('cause') if isinstance(diagnosis, dict) else None
                    reason = diagnosis.get('reason') if isinstance(diagnosis, dict) else None
                    if cause not in {'test_requirement_mismatch', 'test_overconstraint', 'environment',
                            'implementation', 'uncertain'} or not isinstance(reason, str) or not reason.strip():
                        raise ValueError('Return a supported review diagnosis and its evidence in diagnosis.reason')
                    if response['tool_calls'] and cause not in {'test_requirement_mismatch', 'test_overconstraint'}:
                        raise ValueError('Only a test defect diagnosis may propose test edits; otherwise return tool_calls=[]')
                    if cause not in {'test_requirement_mismatch', 'test_overconstraint'} and suite.read_bytes() != before:
                        # A no-edit verdict must be based on the original suite,
                        # not a draft that failed the previous review gate.
                        save_bytes(suite, before)
                        raise ValueError('Discarded the unaccepted test draft. Review the restored original suite.')
                    verdict.update(diagnosis)

                try:
                    review = run_file_agent(model, REVIEW_INSTRUCTIONS, task, workspace,
                        lambda: compile_suite(normalize=False), max_turns, console, label + ' / ' + action, include_conventions=False,
                        feedback=compact_feedback(feedback), validate_response=validate)
                except BaseException:
                    if not suite.is_file() or suite.read_bytes() != before:
                        save_bytes(suite, before)
                        checkpoint(output, stage='12', task=rid, action='Restore rejected independent test draft',
                            status='REJECTED', details={'test_path': path})
                    raise
                return verdict, review['turns'], before != suite.read_bytes()

            if not (output / path).is_file():
                agent = run_file_agent(model, INSTRUCTIONS, task, workspace, compile_suite,
                    max_turns, console, label + ' / Generation', include_conventions=False)
                entry['generation_turns'] = agent['turns']
            elif (prior := previous.get('requirements', {}).get(rid, {})).get('oracle_policy') != ORACLE_POLICY_VERSION or (
                    primary and prior.get('original_requirement_hash') != entry['original_requirement_hash']):
                # Existing suites may encode the old generated-URL oracle. Review
                # them also when the original requirement changed under a stable ID.
                reason = ('acceptance_oracle_policy_changed' if prior.get('oracle_policy') != ORACLE_POLICY_VERSION
                    else 'original_requirement_changed')
                verdict, turns, _ = review_suite({'review_reason': reason,
                    'policy_version': ORACLE_POLICY_VERSION}, 'Policy review')
                if primary and verdict['cause'] in {'environment', 'uncertain'}:
                    raise ValueError('Could not revalidate existing acceptance suite: ' + verdict['reason'])
                entry['policy_review_turns'] = turns
            entry['oracle_policy'] = ORACLE_POLICY_VERSION
            result = checks.tests([path])
            entry["result"] = result
            if test_run_status(result) != 'GREEN' and not primary:
                # A separate, requirement-grounded review may correct the test, but its
                # gate intentionally does not demand behavioral GREEN.
                entry['initial_failure'] = result
                save_json(arc_dir / 'independent_acceptance.json', report)
                checkpoint(output, stage="12", task=rid, action="Record independent acceptance before test review",
                    status="FAILED", details={"test_path": path, "checks": result})
                verdict, turns, changed = review_suite(result, 'Test review')
                entry.update(review_turns=turns, review_diagnosis=verdict)
                if changed:
                    result = checks.tests([path])
                    if test_run_status(result) != 'GREEN':
                        verdict, turns, changed = review_suite(result, 'Post-correction review')
                        entry.update(post_correction_review_turns=turns, review_diagnosis=verdict)
                        if changed:
                            result = checks.tests([path])
                entry["result"] = result
                if test_run_status(result) != 'GREEN' and (verdict['cause'] != 'implementation' or changed):
                    raise ValueError('Independent suite still fails without a confirmed implementation defect: '
                        + verdict['reason'])
            if test_run_status(result) != 'GREEN' or pending_verification:
                entry['status'] = 'REPAIRING'
                save_json(arc_dir / 'independent_acceptance.json', report)
                process_ids = [key for key, value in process_report['requirements'].items()
                    if pending_verification or value.get('status') == 'GREEN' or key == rid]
                process_files = [test_path(key, output) for key in process_ids
                    if (output / test_path(key, output)).is_file()]
                acceptance_files = [acceptance_path(key) for key in [*accepted, rid]]
                ui_files = presentation_files(output, ui['pages'])
                peer, paired = paired_action_context(ui, design, rid) if primary else (None, None)
                if peer in tree.requirements and paired:
                    context = {'page_subtrees': {**context['page_subtrees'], **paired['page_subtrees']},
                        'triggers': {**context['triggers'], **paired['triggers']}}
                    for trigger in paired['triggers'].values():
                        aids.update(api_ids(trigger))
                    apis = {aid: backend['apis'][aid] for aid in aids}
                repair_task = {**context, 'requirement': task['original_requirement'], 'test_path': path,
                    'backend_apis': apis, 'target_trigger_ids': sorted(context['triggers']),
                    'repair_reason': 'final_verification_and_acceptance' if pending_verification else 'independent_acceptance',
                    'application_entrypoints': list(APPLICATION_ENTRYPOINTS),
                    'backend_entrypoints': {'registry': 'backend/src/api/register.ts',
                        'app': 'backend/src/app.ts', 'server': 'backend/src/server.ts'}}
                handoff = trigger_handoff(output, design, rid, context['triggers'], implemented)
                handlers = [entry['existing_handler'] for entry in handoff.values() if entry['existing_handler']]
                if primary:
                    repair_task.update(frontend_repair_context(context, apis))
                    if peer in tree.requirements and paired:
                        repair_task['paired_requirement'] = requirement_context(tree, peer)
                    repair_task['parent_requirements'] = task['parent_requirements']
                    repair_task['trigger_handoff'] = handoff
                    repair_task['verified_http_api_ids'] = sorted(aid for aid in aids if aid in verified_http)
                    repair_task['target_trigger_ids'] = [
                        tid for tid, item in handoff.items() if item['status'] != 'reuse']
                    if peer in tree.requirements and paired:
                        repair_task['target_trigger_ids'] = sorted(set(repair_task['target_trigger_ids'])
                            | set(design['requirements'].get(peer, ())))
                repair_workspace = selection.workspace(
                    paths=[*selection.paths('page', context['page_subtrees']), *handlers],
                    references=[path, *(['frontend/vite.config.ts', FIXTURE] if primary else
                        [*ui_files, FIXTURE, OBSERVATIONS]), 'frontend/src/ui-runtime.ts',
                        *APPLICATION_ENTRYPOINTS, 'frontend/package.json', 'frontend/tsconfig.json'])
                repair_workspace.allow_write = lambda relative: editable_runtime_path(output, relative)
                prefer_edit = bool(primary and aids and aids <= verified_http.keys() and result.get('failed')
                    and not result.get('errors'))
                if primary:
                    label_styles = label_stylesheets(repair_workspace)
                    repair_workspace.files = {path for path in repair_workspace.files
                        if not path.endswith('.css') or path in label_styles}
                    repair_task['accessible_name_stylesheets'] = sorted(label_styles)
                    if prefer_edit:
                        repair_workspace = FirstEditWorkspace(repair_workspace)
                else:
                    backend_selection.include(repair_workspace, backend_selection.paths('api', aids))
                latest_acceptance = result
                ran_regressions = False
                repair_reviews = 0

                def repair_gate():
                    nonlocal latest_acceptance, ran_regressions
                    latest_acceptance = None
                    evidence = {}
                    check = 'typecheck'
                    try:
                        evidence['typecheck'] = checks.typecheck([*process_files, *acceptance_files])
                        if evidence['typecheck']['returncode']:
                            raise ValueError(evidence['typecheck']['output'])
                        check = 'acceptance'
                        latest_acceptance = evidence['acceptance'] = checks.tests(acceptance_files)
                        ran_regressions = len(acceptance_files) > 1
                        require_acceptance(latest_acceptance)
                        if process_files:
                            check = 'smoke'
                            evidence['smoke'] = checks.tests(process_files)
                            require_acceptance(evidence['smoke'])
                        if ui_files:
                            check = 'ui'
                            evidence['ui'] = ui_checks.tests(ui_files)
                            require_green(evidence['ui'])
                        if pending_verification or any(relative.startswith('backend/') for relative in repair_workspace.changed_paths):
                            check = 'backend_typecheck'
                            evidence['backend_typecheck'] = backend_checks.typecheck()
                            if evidence['backend_typecheck']['returncode']:
                                raise ValueError(evidence['backend_typecheck']['output'])
                            suites = [selected for aid in backend['apis']
                                if (selected := selected_path(output, 'backend_test', aid))
                                and (output / selected).is_file()]
                            if suites:
                                check = 'backend'
                                evidence['backend'] = backend_checks.tests(suites)
                                require_backend_green(evidence['backend'])
                        check = 'build'
                        evidence['build'] = checks.build()
                        if evidence['build']['returncode']:
                            raise ValueError(evidence['build']['output'])
                    except (ValueError, RuntimeError) as error:
                        message = f'Independent acceptance repair failed at {check}' if check in evidence else str(error)
                        raise TriggerCheckFailure(message, evidence) from error
                    return evidence

                def repair_feedback(value):
                    if prefer_edit:
                        if repair_workspace.received_initial_feedback:
                            repair_workspace.edit_first = False
                        repair_workspace.received_initial_feedback = True
                    feedback = selection.feedback(repair_workspace, backend_selection.feedback(repair_workspace, value))
                    repair_workspace.files.update(relative for relative in acceptance_files
                        if any(failure_in_suite(message, relative)
                            for message in (latest_acceptance or {}).get('failures', [])))
                    return feedback

                with implementation_task(output, entry, console, label + ' / Repair'):
                    instructions = IMPLEMENT_INSTRUCTIONS + '\n\n' + (
                        'Independent acceptance suites are read-only for implementation repair. '
                        'Preserve their original requirement outcomes and repair the real application. '
                        'Do not change consent defaults or persisted inputs to accommodate missing test actions '
                        'or invented expectations. Treat newly reached assertions and failures of earlier '
                        'passing suites as application repair evidence. Only request isolated review for a '
                        'specific current-suite assertion or fixture that conflicts with the original requirement.')
                    if primary:
                        instructions += (' The suite is the sole behavioral test for this requirement. '
                            'Use trigger_handoff: reuse accepted actions and the listed existing handlers; '
                            'do not recreate their files or add a second binding. Handler files are optional. '
                            'Use the supplied HTTP method, path, request and response directly for frontend wiring. '
                            'The backend routes listed in verified_http_api_ids already passed real HTTP tests. '
                            'If the failure is a UI locator, navigation or missing event binding, start with the '
                            'supplied frontend source; do not read the server, database initializer or route '
                            'registry for those failures. Inspect backend sources only for concrete runtime evidence. '
                            'When exact labels fail, check both literal punctuation and text generated by CSS '
                            '::before/::after; keep decorative required markers out of accessible names. '
                            'Before editing, provide diagnosis.cause=implementation or environment with a '
                            'specific diagnosis.reason. If evidence shows a test defect, report diagnosis.cause '
                            'as test_requirement_mismatch or test_overconstraint with no edits '
                            'so an isolated reviewer can check the suite.')
                        if peer in tree.requirements and paired:
                            instructions += (' The paired_requirement shares an existing control but uses a '
                                'different API. Implement one event binding for the shared control using the '
                                'applicable input and operation; satisfy both declared behaviors and preserve '
                                'the paired session/navigation actions. The paired suite will run separately.')
                        if prefer_edit:
                            instructions += (' The first turn has all relevant source and verified interfaces; '
                                'submit a concrete edit or a test-defect diagnosis directly. '
                                'Use read_file for a specific remaining question only after gate feedback.')
                    turns_used = 0
                    repair_evidence = {'acceptance': result,
                        'verification': process_report.get('verification', {}) if pending_verification else {}}

                    def route_oracle_problem(response):
                        diagnosis = response.get('diagnosis')
                        cause = diagnosis.get('cause') if isinstance(diagnosis, dict) else None
                        reason = diagnosis.get('reason') if isinstance(diagnosis, dict) else None
                        if cause in {'test_requirement_mismatch', 'test_overconstraint'}:
                            if not isinstance(reason, str) or not reason.strip() or response['tool_calls']:
                                raise ValueError('Request test review with specific requirement evidence and no edits')
                            if not current_suite_failure(latest_acceptance, path, ran_regressions):
                                raise ValueError('Only failures attributable to the current suite can request test review; preserve earlier suites')
                            if repair_reviews:
                                raise ValueError('The isolated test review budget is spent. Use the reviewed oracle and available application evidence.')
                            raise ReviewOracle(latest_acceptance, diagnosis)
                        if primary and (cause not in {'implementation', 'environment'}
                                or not isinstance(reason, str) or not reason.strip()):
                            raise ValueError('Diagnose the application or runtime failure with concrete evidence before editing')

                    while turns_used < max_turns:
                        try:
                            repair = run_file_agent(model, instructions, repair_task, repair_workspace,
                                repair_gate, max_turns - turns_used, console, label + ' / Repair',
                                feedback=repair_evidence, feedback_filter=repair_feedback, context_index=context_index,
                                allow_discovery=not prefer_edit,
                                validate_response=route_oracle_problem)
                            turns_used += repair['turns']
                            accepted_checks = repair['checks']
                            break
                        except ReviewOracle as handoff:
                            turns_used += handoff.turns
                            repair_reviews += 1
                            entry['new_failure'] = handoff.result
                            verdict, turns, changed = review_suite(
                                {'acceptance': handoff.result, 'suspected_defect': handoff.diagnosis}, 'Test defect review')
                            entry.update(new_failure_review_turns=turns, new_failure_diagnosis=verdict)
                            if changed:
                                try:
                                    accepted_checks = repair_gate()
                                except TriggerCheckFailure as error:
                                    repair_evidence = {'acceptance': latest_acceptance, 'regression': error.feedback}
                                    continue
                                break
                            if verdict['cause'] != 'implementation':
                                raise ValueError('New failure was not attributed to the application: ' + verdict['reason'])
                            repair_evidence = {**repair_evidence, 'acceptance': handoff.result}
                    else:
                        raise ValueError('Application repair budget exhausted after independent test review')
                    entry.update(status='GREEN', repair_turns=turns_used,
                        result=accepted_checks['acceptance'], repair_checks=accepted_checks)
                if entry['status'] == 'FAILED':
                    continue
                result = entry['result']
                verified_process.update(key for key in process_ids if test_path(key, output) in process_files)
                pending_verification = False
            require_acceptance(result)
            process_entry = process_report['requirements'].get(rid)
            if process_entry and process_entry.get('status') == 'FAILED' and rid in verified_process:
                process_entry['previous_failure'] = dict(process_entry)
                process_entry.update(status='GREEN', recovery='independent_acceptance_repair')
                save_json(arc_dir / 'frontend_e2e.json', process_report)
            entry.update(status='GREEN', result=result)
            accepted.append(rid)
            implemented.update(design['requirements'].get(rid, ()))
        except (ValueError, RuntimeError) as error:
            entry.update(status='FAILED', error=str(error))
            if isinstance(error, UpstreamTaskError):
                entry["blocked_by"] = error.blocked_by
        finally:
            save_json(arc_dir / 'independent_acceptance.json', report)
            checkpoint(output, stage="12", task=rid, action="Publish independent requirement acceptance",
                status=entry['status'], details=entry)
            console.show(label + ' | ' + entry['status'], entry.get('error', 'Real acceptance checks completed'))
    report['status'] = 'GREEN' if report['requirements'] and all(
        entry['status'] == 'GREEN' for entry in report['requirements'].values()) else 'COMPLETED_WITH_FAILURES'
    save_json(arc_dir / 'independent_acceptance.json', report)
    checkpoint(output, stage="12", task="independent acceptance", action="Publish final independent acceptance report",
        status=report['status'], details=report)
    return report
