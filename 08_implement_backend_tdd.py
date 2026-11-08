"""Step 08: implement backend APIs against real-server HTTP acceptance suites."""

import json
import re
from contextlib import ExitStack
from pathlib import Path, PurePosixPath

from infrastructure.shared.agent.context import SourceFiles
from infrastructure.shared.agent.discovery import project_index
from infrastructure.shared.project.entrypoints import selected_path
from infrastructure.shared.agent.loop import run_file_agent
from infrastructure.shared.agent.recovery import implementation_task, final_checks, complete_report
from infrastructure.shared.agent.tasks import require_design_task, UpstreamTaskError
from infrastructure.stages.backend.context import load_context, requirement_context, api_requirement_uses, supplied_sources, related_schema
from infrastructure.utils.files import read_artifact
from infrastructure.stages.backend.runtime import REGISTER_PATH, TEST_RUNNER_PATH, TEST_SUPPORT_PATH, prepare_runtime, test_path
from infrastructure.stages.backend.testing import BackendChecks, require_green, test_run_status
from infrastructure.stages.database.testing import DatabaseChecks
from infrastructure.utils.cli import create_parser, resolve_directories
from infrastructure.shared.models.text import TextModel
from infrastructure.utils.presentation.console import DemoConsole
from infrastructure.utils.presentation.logging import console_log
from infrastructure.shared.project.dependencies import install_dependencies
from infrastructure.utils.files import save_json, save_text
from infrastructure.shared.project.git import checkpoint


TEST_INSTRUCTIONS = """Write one HTTP acceptance suite for task.api before implementing it.
Derive expected outcomes from the requirement scenarios, API method/path, I/O, P/Q and DB
changes. Use edit_file to create the .test.ts target under backend/tests or update test_path.
Use { test } from node:test and assert from node:assert/strict with async test callbacks.
Import { createApiTest, fixturePasswordHash } as needed from task.support_module with a
relative .js ESM path. Do not import handlers, createApp, server or test aliases.

At the start of each test call const { database, baseURL } = createApiTest(t).
The compiler owns a real backend process running backend/src/server.ts, its assigned port
and its temporary on-disk database. The helper restores prerequisite data before each case
and opens that SAME database for Given setup and SQL observations. Test files/cases run
sequentially. Do not start a server, choose a port, create a separate database, run tests
concurrently, mock HTTP or call business functions directly.

Exercise EVERY success/rejection case through real HTTP:
const response = await fetch(new URL('/api/example', baseURL), {
  method: 'POST', headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ example: 'value' }), redirect: 'manual'
});
assert.equal(response.status, expectedStatus);
const body = await response.json();
Adapt the example to the canonical contract. Substitute encoded concrete route parameter
values, construct query strings and JSON bodies, and verify actual status/body/headers.
For 204 or other empty responses use text() or skip JSON parsing. Use redirect:'manual'
when validating status/Location. Node fetch does not maintain browser cookies: obtain
required cookies from real responses using headers.getSetCookie(), strip attributes and
send them in a Cookie header for subsequent requests when this API requires it. Supply
only contract-required authentication; do not test another requirement's login flow as
an incidental dependency. Necessary existing sessions/accounts can be prepared via SQL.

Seed only required Given state directly through database with parameterized SQL and valid
values; use 0/1 booleans. Read the actual shared helper/SQL definitions rather than guessing
fixture arguments or password encodings. Initialization loads db/seeds/ENTITY.sql; reuse
existing prerequisites instead of assuming empty tables or duplicating identities. Arrange
scenario-specific absence/conflicting states in this isolated database. Do not invoke
another requirement's registration/creation API to manufacture Given data.
Use the support module, table DDL, initializer, seed loader and relevant seed files
already in input.files. A comment-only SQL file has no seed rows. The canonical API
contract defines the URL; the runtime files already show the initialization sequence.
If test_path_exists is false, create it. Start with the test edit. Do not call read_file
for source already included in input.files.

Within max_tests cover meaningful success and critical requirement-defined rejection.
Assert relevant persisted results/forbidden writes with SQL where needed; HTTP is the
only way to execute the operation. Do not assert unspecified wording, random identifiers,
hashes or timestamps. Do not inspect source text, add mocks, skip/focus tests or hardcode
success. Browser workflows belong to frontend E2E. Missing routes/behavior should fail real
assertions; do not expect 404/501 merely because this API is not implemented yet.

The compiler compiles the suite, runs accepted HTTP regressions, then runs this suite
against the actual backend. Assertion RED establishes missing behavior. Existing behavior
may already be GREEN; no stub replaces it. Backend startup failures are implementation
feedback, not proof the test is wrong. During generation repair test syntax/fixtures and
existing compilation problems, not the new operation. Generated tests remain correctable
against requirement evidence; preserve valid success/rejection coverage and previous tests.
"""

IMPLEMENT_INSTRUCTIONS = """Implement task.api according to its canonical contract against its generated tests.
Use edit_file to create task.file_target under backend/src or repair implementation_path. Export:
export async function handle(request: ApiRequest, connection: Database.Database): Promise<ApiResponse>.
The export is always handle; signature.name identifies the business operation.
Tests exercise the operation through HTTP, not through an exported test alias. Import ApiRequest and ApiResponse from backend/src/api/types.ts
using a relative .js ESM import. Return status/body/optional headers, not Express objects.

Implement request validation, API preconditions, success/rejection responses and DB
effects according to the contract and scenarios. Use the supplied connection, parameterized
better-sqlite3 SQL, and transactions for related writes. Respect schema-generated integer
keys and use node:crypto for UUIDs or hashing when the schema/contract requires them.
Use the supplied test utilities and generated tests to understand existing fixture data
conventions. Preserve compatibility when reading those records. Production storage must
satisfy the contract and keep plaintext passwords out of persistent data.
Preserve required comparison rules and shared credential/header conventions.
ApiRequest body and query values are unknown: assign them to locals and narrow their
types before string, date, or password operations.

Do not start a server, open another database, change schema or seed production data.
Use installed dependencies and Node standard modules. Implement the operation directly;
no DB-access primitive layer is generated. The compiler owns the HTTP test runner and fixture.
Predefined credentials in seeds/*.sql use seed_password_hash; its scrypt encoding is
defined in backend/src/db/seed.ts and must remain readable by credential handlers.
Use known_api_files for existing handler locations instead of guessing endpoint filenames.
Do not read a missing implementation file: create the target when implementation_path_exists
is false. Keep related routes and handlers intact.
Preserve unrelated behavior when repairing other supplied writable backend sources.
Generated tests can be corrected against requirement and API evidence when their
expectations or fixtures are wrong or overly strict. Preserve required success and
rejection coverage; do not weaken a valid assertion to accept broken behavior.
SQL, designs, test utilities and compiler infrastructure remain references.
Do not pre-create alternate tables from the server or bypass canonical DDL. Enforce rules
relative to the current date in application validation before writes, without weakening
the required success and rejection cases.
The compiler starts the real server and runs current/accepted HTTP API suites after TypeScript checks. Repair implementation
failures without weakening assertions or detecting tests to fake business behavior.
"""

MANUAL_WIRING = """
Use task.api_wiring.registry to register the route through its existing registerApis function.
Use a local edit inside the registry, preserving imports, previous routes and middleware.
Adapt Express params/query/body/headers to ApiRequest and forward response status, body and headers.
Translate path placeholders into Express route parameters. Inspect app/server wiring only
when a failing check names it. Read only missing source evidence.
"""

COMPILER_WIRING = """
Implement only the handler and any necessary business helpers. The compiler binds the canonical
HTTP method/path and adapts request and response values before the real test gate. The registry,
app and server are read-only for this task. A test's pre-implementation 404 is expected.
Start with the handler edit using input.files; do not read source already supplied there.
"""

ROUTE_SEGMENT = r"(?:[A-Za-z0-9._~-]+|:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\})"
HTTP_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
PROTECTED_WIRING = {REGISTER_PATH, "backend/src/app.ts", "backend/src/server.ts"}


def compiler_routes(output, apis):
    """Select deterministic wiring only for the untouched registry and simple Express paths."""
    template = Path(__file__).resolve().parent / "infrastructure/shared/project/templates" / REGISTER_PATH
    registry = output / REGISTER_PATH
    if not (registry.is_file() and template.is_file()
            and registry.read_text(encoding="utf-8-sig") == template.read_text(encoding="utf-8-sig")):
        return None
    routes = {}
    patterns = set()
    for aid, api in apis.items():
        signature = api.get("signature", {})
        path, method = signature.get("path"), signature.get("method")
        if (method not in HTTP_METHODS or not isinstance(path, str)
                or not re.fullmatch(r"/api(?:/" + ROUTE_SEGMENT + r")+", path)):
            return None
        if method in {"GET", "HEAD"} and path.lower() == "/api/health":
            return None
        path = re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", r":\1", path)
        parameters = re.findall(r":([A-Za-z_][A-Za-z0-9_]*)", path)
        pattern = (method, re.sub(r":[A-Za-z_][A-Za-z0-9_]*", ":", path).lower())
        if len(parameters) != len(set(parameters)) or pattern in patterns:
            return None
        patterns.add(pattern)
        routes[aid] = (method.lower(), path)
    return routes


def write_compiler_registry(output, routes, paths):
    """Regenerate only the compiler-owned HTTP adapter from accepted and current handlers."""
    imports, registrations = [], []
    # Literal routes must be registered before overlapping parameterized routes.
    for index, aid in enumerate(sorted(paths, key=lambda key: (
            routes[key][0] != "head",
            tuple(part.startswith(":") for part in routes[key][1].split("/")), routes[key], key))):
        method, route = routes[aid]
        path = PurePosixPath(paths[aid]).relative_to(PurePosixPath(REGISTER_PATH).parent)
        module = "./" + str(path.with_suffix(".js"))
        imports.append(f'import {{ handle as handler{index} }} from {json.dumps(module)};')
        registrations.append(f'  app.{method}({json.dumps(route)}, adapter(handler{index}, connection));')
    source = """import type { Express, RequestHandler } from "express";
import type Database from "better-sqlite3";
import type { ApiHandler } from "./types.js";
""" + "\n".join(imports) + """

function adapter(handle: ApiHandler, connection: Database.Database): RequestHandler {
  return async (request, response, next) => {
    try {
      const headers = Object.fromEntries(Object.entries(request.headers).map(([key, value]) =>
        [key, Array.isArray(value) ? value.join(", ") : value]));
      const result = await handle({ params: request.params as Record<string, string>,
        query: request.query as Record<string, unknown>, body: request.body ?? {}, headers }, connection);
      response.status(result.status);
      for (const [name, value] of Object.entries(result.headers ?? {})) response.setHeader(name, value);
      const type = response.getHeader("content-type");
      if (result.body === undefined || result.status === 204 || result.status === 304) response.end();
      else if (typeof result.body === "string" && typeof type === "string" && !type.includes("json")) response.send(result.body);
      else response.json(result.body);
    } catch (error) {
      next(error);
    }
  };
}

export function registerApis(app: Express, connection: Database.Database): void {
""" + "\n".join(registrations) + "\n}\n"
    save_text(output / REGISTER_PATH, source)


def scoped_feedback(selection, workspace, compiler_owned):
    if compiler_owned:
        allow = workspace.allow_write
        workspace.allow_write = lambda path: path not in PROTECTED_WIRING and allow(path)
        workspace.writable.difference_update(PROTECTED_WIRING)

    def filter_feedback(value):
        feedback = selection.feedback(workspace, value)
        if compiler_owned:
            workspace.writable.difference_update(PROTECTED_WIRING)
        return feedback
    return filter_feedback


def implement_backend_tdd(arc_dir: Path, pause: bool = False, max_turns: int = 10, max_tests: int = 5) -> dict:
    if max_turns < 1:
        raise ValueError("--max-turns must be positive")
    if max_tests < 1:
        raise ValueError("--max-tests must be positive")
    tree, schema, obligations = load_context(arc_dir)
    contracts = read_artifact(arc_dir, "backend_apis.json")
    output = arc_dir.parent.resolve()
    if not (output / "backend/src/db/initialize.ts").is_file():
        raise ValueError("Run 00_project_initialization.py and 05_implement_db.py first")
    for name in schema["entities"]:
        if not (output / f"backend/src/db/tables/{name}.sql").is_file():
            raise ValueError(f"Missing implemented SQL for {name}; run step 05")
    prepare_runtime(output)
    console = DemoConsole(pause)
    console.show("TDD preparation | Install dependencies", f"Workspace: {output}\nCommand: npm install --include=dev --no-audit --no-fund")
    install_dependencies(output)
    checkpoint(output, stage="08", task="runtime", action="Prepare backend wiring and dependencies",
        details={"api_ids": list(contracts["apis"]), "http_test_runner": TEST_RUNNER_PATH})
    blocked = {}
    for rid in tree.leaves:
        try:
            require_design_task(arc_dir, "06_design_backend_apis", rid, "backend_apis.json", contracts["requirements"])
        except UpstreamTaskError as error:
            blocked[rid] = {"status": "FAILED", "apis": [], "error": str(error), "blocked_by": error.blocked_by}
    if not contracts["apis"]:
        report = {"status": "NO_APIS", "tests": {}, "red": {}, "green": {}, "agents": {}, "summary": {"tests": 0, "pass": 0, "fail": 0}}
        if blocked:
            report.update(status="COMPLETED_WITH_FAILURES", requirements=blocked)
        save_json(arc_dir / "backend_tdd.json", report)
        if blocked:
            print(f"Backend implementation blocked for {len(blocked)} requirement(s); see .arc/backend_tdd.json. No model calls were run.")
        else:
            print("No Backend API Primitives require implementation; no tests or model calls were run.")
        return report
    checks = BackendChecks(output)
    routes = compiler_routes(output, contracts["apis"])
    wired = {}
    selection = SourceFiles(output, root="backend/src", tests_root="backend/tests", stage="08")
    model = TextModel() if contracts["apis"] else None
    context_index = project_index(output,
        kinds=("schema", "api", "requirement", "test_requirement", "convention", "file"),
        file_roots=("backend/src", "backend/tests"))
    owners = {}
    for aid in contracts["apis"]:
        ids = [rid for rid in tree.leaves if aid in contracts["requirements"].get(rid, [])]
        if ids:
            owners[aid] = ids
    targets = {aid: selected_path(output, "backend_test", aid) or test_path(ids[0], aid) for aid, ids in owners.items()}
    report = {"status": "RUNNING", "requirements": {}, "tests": {}, "red": {}, "green": {}, "agents": {}}
    # Publish a current status before work; an old GREEN is never reused as proof.
    save_json(arc_dir / "backend_tdd.json", report)
    try:
        files_by_api = {aid: [relative] for aid, relative in targets.items()}
        accepted, attempted = [], set()
        for requirement_number, rid in enumerate(tree.leaves, 1):
            if rid in blocked:
                report["requirements"][rid] = blocked[rid]
                save_json(arc_dir / "backend_tdd.json", report)
                continue
            ids = contracts["requirements"].get(rid, [])
            pending = [aid for aid in ids if aid not in attempted]
            requirement_label = f"Requirement {requirement_number}/{len(tree.leaves)} {rid}"
            console.show(f"{requirement_label} | Begin requirement TDD", json.dumps(requirement_context(tree, rid), ensure_ascii=False, indent=2))
            report["requirements"][rid] = {"status": "RUNNING", "apis": ids}
            save_json(arc_dir / "backend_tdd.json", report)
            if not ids:
                report["requirements"][rid]["status"] = "NO_DIRECT_APIS"
                console.show(f"{requirement_label} | No direct APIs", "Child APIs are not inherited. Proceed to the next requirement.")
                save_json(arc_dir / "backend_tdd.json", report)
                continue
            reused = [aid for aid in ids if aid in accepted]
            if reused:
                console.show(f"{requirement_label} | Reuse accepted APIs", ", ".join(reused) + " already passed their canonical contract tests.")
            for aid in pending:
                attempted.add(aid)
                entry = report.setdefault("apis", {}).setdefault(aid, {"status": "GENERATING_TESTS", "api": aid})
                with implementation_task(output, entry, console, "08 / " + aid):
                    api_owners = owners[aid]
                    api = contracts["apis"][aid]
                    relative = targets[aid]
                    label = f"{rid} / {aid} (uses: {', '.join(api_owners)})"
                    console.show(f"{label} | Test Generation: P / Q / R / W", json.dumps(api, ensure_ascii=False, indent=2))

                    def test_gate(aid=aid):
                        chosen = selected_path(output, "backend_test", aid) or relative
                        targets[aid] = chosen
                        files_by_api[aid] = [chosen]
                        generation_files = [targets[key] for key in [*accepted, aid]]
                        compiled = checks.typecheck(generation_files)
                        if compiled["returncode"]:
                            raise ValueError(f"Test TypeScript compilation failed: {compiled['output']}")
                        if accepted:
                            require_green(checks.tests([targets[key] for key in accepted]))
                        result = checks.tests(files_by_api[aid])
                        status = test_run_status(result)
                        if status == "RED_CONFIRMED":
                            report["red"][aid] = {key: result[key] for key in ("tests", "pass", "fail")}
                        entry["test_status"] = status
                        console.show(f"{aid} | HTTP tests: {status}", result["output"])
                        save_json(arc_dir / "backend_tdd.json", report)
                        checkpoint(output, stage="08", task=aid, action="Run HTTP tests before implementation",
                            status=status, details={"test_path": chosen, "checks": result})
                        return {"typecheck": compiled, "tests": result, "status": status}

                    context = {
                        "api_id": aid, "api": api,
                        "requirement": requirement_context(tree, rid),
                        "db_schema": related_schema(schema, [api]), "test_path": relative,
                        "test_path_exists": (output / relative).is_file(),
                        "max_tests": max_tests,
                        "support_module": TEST_SUPPORT_PATH,
                    }
                    sources = supplied_sources(output, api, schema)
                    seeds = [path.replace("/tables/", "/seeds/") for path in sources if "/tables/" in path]
                    runtime_sources = [path for path in
                        ["backend/src/db/initialize.ts", "backend/src/db/seed.ts", *seeds]
                        if (output / path).is_file()]
                    workspace = selection.workspace(target=("backend_test", aid), existing=relative,
                        references=[*sources, *runtime_sources])
                    agent = run_file_agent(model, TEST_INSTRUCTIONS, context, workspace,
                        test_gate, max_turns, console, label + " / Test Generation",
                        feedback_filter=scoped_feedback(selection, workspace, routes is not None), context_index=context_index)
                    for owner in api_owners:
                        report["tests"].setdefault(owner, {})[aid] = targets[aid]
                    report["agents"][f"tests:{aid}"] = {"turns": agent["turns"]}
                    save_json(arc_dir / "backend_tdd.json", report)
                    entry["status"] = "IMPLEMENTING"
                    api = contracts["apis"][aid]
                    console.show(f"{aid} | Implementation context", (
                        json.dumps(api, ensure_ascii=False, indent=2)
                        + "\nContract tests: " + ", ".join(files_by_api[aid])
                        + "\nAPI may directly operate the database; no further Primitive decomposition."
                    ))
                    regression_files = [relative for key in [*accepted, aid] for relative in files_by_api[key]]

                    def implementation_checks():
                        compiled = checks.typecheck(regression_files)
                        if compiled["returncode"]:
                            return {"typecheck": compiled, "tests": None}
                        tested = checks.tests(regression_files)
                        return {"typecheck": compiled, "tests": tested}

                    def implementation_gate():
                        if routes is not None:
                            write_compiler_registry(output, routes, {**wired, aid: handler_path})
                        result = implementation_checks()
                        if result["typecheck"]["returncode"]:
                            raise ValueError(result["typecheck"]["output"])
                        require_green(result["tests"])
                        return result

                    api_entry = selected_path(output, "api", aid)
                    handler_path = f"backend/src/api/{aid}.ts" if routes is not None else api_entry
                    current = implementation_checks() if api_entry and (output / api_entry).is_file() else agent["checks"]
                    context = {
                        "api_id": aid, "api": api, "db_schema": related_schema(schema, [api]),
                        "requirement": requirement_context(tree, rid),
                        "implementation_path": handler_path,
                        "implementation_path_exists": bool(handler_path and (output / handler_path).is_file()),
                        "known_api_files": {key: path for key in contracts["apis"]
                            if (path := selected_path(output, "api", key)) and (output / path).is_file()},
                    }
                    credentials = {field for mode in ("read", "write") for field in api[mode]
                        if "password" in field.casefold()}
                    credential_sources = [path for key in accepted
                        if credentials.intersection(contracts["apis"][key]["write"])
                        and (path := selected_path(output, "api", key)) and (output / path).is_file()]
                    if routes is None:
                        context["api_wiring"] = {"registry": REGISTER_PATH, "app": "backend/src/app.ts",
                            "server": "backend/src/server.ts"}
                    else:
                        context["compiler_wires_http"] = True
                    workspace = selection.workspace(target=("api", aid),
                        paths=[REGISTER_PATH] if routes is None else [handler_path],
                        references=[*files_by_api[aid], *sources, *runtime_sources, *credential_sources])
                    if routes is not None:
                        workspace.target.path = handler_path
                    wiring_instructions = COMPILER_WIRING if routes is not None else MANUAL_WIRING
                    agent = run_file_agent(model, IMPLEMENT_INSTRUCTIONS + wiring_instructions, context, workspace,
                        implementation_gate, max_turns, console, aid + " / Implementation", feedback=current,
                        feedback_filter=scoped_feedback(selection, workspace, routes is not None), context_index=context_index)
                    accepted.append(aid)
                    if routes is not None:
                        wired[aid] = handler_path
                    result = agent["checks"]["tests"]
                    report["green"][aid] = {key: result[key] for key in ("tests", "pass", "fail")}
                    report["agents"][f"implementation:{aid}"] = {"turns": agent["turns"]}
                    save_json(arc_dir / "backend_tdd.json", report)
                    console.show(f"{aid} | GREEN: contract and regression tests pass", result["output"])
                    entry["status"] = "GREEN"
                save_json(arc_dir / "backend_tdd.json", report)
            report["requirements"][rid]["status"] = "GREEN" if all(aid in accepted for aid in ids) else "FAILED"
            save_json(arc_dir / "backend_tdd.json", report)
            console.show(f"{requirement_label} | " + report["requirements"][rid]["status"], "Proceed to the next requirement; failed APIs remain recorded.")
        all_files = [targets[aid] for aid in accepted]
        console.show("Final regression | All requirements complete", "Compile the full backend and run all canonical API suites.")
        verification = final_checks(console,
            typecheck=(checks.typecheck, None),
            tests=(lambda: checks.tests(all_files), require_green if all_files else None),
            build=(checks.build, None),
            database=(lambda: DatabaseChecks(output, schema).run(), None))
        final = verification["tests"].get("result") or {}
        report["summary"] = {key: final.get(key, 0) for key in ("tests", "pass", "fail")}
        complete_report(report, [*report.get("apis", {}).values(), *blocked.values()], verification)
        save_json(arc_dir / "backend_tdd.json", report)
        print(f"TDD {report['status']}: {report['summary']}\nReport: {arc_dir / 'backend_tdd.json'}", flush=True)
        return report
    except BaseException as error:
        report["status"] = "FAILED"
        report["error"] = str(error)
        for requirement in report["requirements"].values():
            if requirement["status"] in {"RUNNING", "RED_CONFIRMED"}:
                requirement["status"] = "FAILED"
        save_json(arc_dir / "backend_tdd.json", report)
        raise


def main() -> None:
    parser = create_parser("Step 08: TDD-driven API Implementation (P / Q / Dataflow Obligations)")
    parser.add_argument("--max-turns", type=int, default=10, help="Maximum edit rounds per test/implementation agent")
    parser.add_argument("--max-tests", type=int, default=5, help="Maximum key contract tests per API (default: 5)")
    args = parser.parse_args()
    with ExitStack() as stack:
        try:
            _, output = resolve_directories(args)
            stack.enter_context(console_log(output, debug_model_io=args.debug_model_io))
            result = implement_backend_tdd(output / ".arc", args.pause, args.max_turns, args.max_tests)
        except (OSError, ValueError, RuntimeError) as error:
            parser.exit(1, f"Execution failed: {error}\n")

    if result["status"] == "COMPLETED_WITH_FAILURES":
        parser.exit(1, "All implementation tasks were attempted; some acceptance checks failed. See the .arc report.\n")


if __name__ == "__main__":
    main()
