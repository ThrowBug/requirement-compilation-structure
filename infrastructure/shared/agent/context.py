"""Supply task sources and local imports; expand repair context from concrete failures."""

import json
import posixpath
import re

from infrastructure.shared.agent.feedback import compact_feedback
from infrastructure.shared.agent.files import SHARED_SOURCES, SEMANTIC_TEST_ROOT, editable_application_path, editable_sources, source_paths
from infrastructure.shared.agent.tools import FileTools
from infrastructure.shared.project.entrypoints import selected_path
from infrastructure.shared.project.stage_impact import stage_write_allowed
from infrastructure.utils.files import read_source


class SourceFiles:
    """Select sources by semantic ownership, imports, and concrete failure feedback.

    The available-file catalogue is compiler-only. It is never sent wholesale to
    the model. Application integration stays writable; compiler-owned database
    and test infrastructure stay read-only. Generated test suites can be
    corrected against requirements; compiler-owned test utilities remain references.
    """

    def __init__(self, output, ui=None, *, root="frontend/src", tests_root="frontend/tests", stage=None,
            approved_upstream_paths=()):
        """Catalogue the layer once; newly created handlers are supplied explicitly."""
        self.output, self.root, self.tests_root = output, root, tests_root
        self.stage = stage
        self.approved_upstream_paths = set(approved_upstream_paths)
        self.layer = root.split("/", 1)[0]
        self.sources = set(source_paths(output, root))
        self.editable = set(editable_sources(output, root))
        self.tests = set(source_paths(output, tests_root))
        self.ui = ui or {"pages": {}, "elements": {}}

    def paths(self, kind, identities):
        """Return every selected target path, including missing indexed files for repair."""
        selected = set()
        for identity in identities:
            if (relative := selected_path(self.output, kind, identity)) is not None:
                selected.add(relative)
                continue
            # Older local fixtures may predate the path index. Reuse an existing
            # uniquely named implementation, but never guess a new destination.
            if kind == "page":
                matches = {path for path in self.sources
                    if path.rsplit("/", 1)[-1].rsplit(".", 1)[0].casefold() == str(identity).casefold()}
                if len(matches) == 1:
                    selected.update(matches)
        return selected

    def is_test(self, relative):
        """Identify generated suites, leaving shared fixtures and test utilities read-only."""
        return (not relative.startswith(SEMANTIC_TEST_ROOT) and "/tests/e2e/acceptance/" not in relative
            and relative.startswith(self.tests_root + "/") and relative.endswith((".spec.ts", ".test.ts")))

    def include(self, workspace, paths):
        """Add selected files plus transitive local imports, preserving write protection."""
        roots = set(paths)
        pending = list(roots)
        seen = set()
        while pending:
            relative = pending.pop()
            if relative in seen:
                continue
            seen.add(relative)
            workspace.path(relative)
            workspace.files.add(relative)
            if (relative in self.editable or self.is_test(relative)) and workspace.can_write(relative):
                workspace.writable.add(relative)
            target = self.output / relative
            # Shared wiring can import the whole application. Supply its
            # interface only; it must not expand the source context transitively.
            if relative in SHARED_SOURCES or relative not in roots or relative not in self.editable or not target.is_file():
                continue
            source = read_source(target)
            imports = re.findall(r'''(?:\bfrom\s*|\bimport\s*(?:\(\s*)?|\brequire\(\s*)["'](\.[^"']+)["']''', source)
            for imported in imports:
                base = posixpath.normpath(posixpath.join(posixpath.dirname(relative), imported))
                stem = base[:-3] if base.endswith(".js") else base[:-4] if base.endswith(".jsx") else base
                candidates = [base, *(stem + suffix for suffix in (".ts", ".tsx", ".js", ".jsx", ".json", ".css")),
                    *(base + "/index" + suffix for suffix in (".ts", ".tsx", ".js", ".jsx"))]
                found = next((path for path in candidates if path in self.sources), None)
                if found:
                    pending.append(found)

    def workspace(self, *, triggers=(), references=(), target=None, existing=None, paths=(),
            approved_upstream_paths=()):
        """Include all requested handlers; a missing new target retains model-selected naming."""
        self.sources.update(source_paths(self.output, self.root))
        self.editable.update(editable_sources(self.output, self.root))
        self.tests.update(source_paths(self.output, self.tests_root))
        workspace = (FileTools.for_target(self.output, *target, existing=existing)
            if target else FileTools(self.output, ()))
        stage = self.stage
        approved = self.approved_upstream_paths | set(approved_upstream_paths)
        workspace.stage = stage
        workspace.stage_allowed_paths = set(paths)
        workspace.approved_upstream_paths = approved
        if stage is None:
            workspace.allow_write = lambda path: editable_application_path(self.output, path)
        else:
            workspace.allow_write = lambda path: stage_write_allowed(self.output, stage, path,
                explicit_paths=workspace.stage_allowed_paths,
                approved_upstream_paths=workspace.approved_upstream_paths)
        paths = set(paths) | self.paths("trigger", triggers)
        paths.update(workspace.writable)
        # New handlers may have been created since this catalogue was built.
        self.sources.update(paths)
        self.editable.update(path for path in paths if path.startswith(self.root + "/"))
        self.include(workspace, paths)
        workspace.files.update(references)
        for path in references:
            if self.is_test(path) and workspace.can_write(path):
                workspace.writable.add(path)
        return workspace

    def feedback(self, workspace, value):
        """Expose sources/tests named in failures and compact duplicated test-report output.

        Full reports remain in compiler results/logs. The model receives failure
        details once. Successful checks and duplicate JSON `output` are omitted.
        """
        value = compact_feedback(value)
        text = (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)).replace("\\\\", "/").replace("\\", "/")
        application_files = {path for path in workspace.writable if path.startswith(self.root + "/")}
        self.sources.update(application_files)
        self.editable.update(application_files)
        named = {path for path in self.sources | self.tests
            if path in text or path.removeprefix(self.layer + "/") in text}
        # UI assertion feedback often contains a locator rather than the page
        # source path. Follow that element's parents to expose the actual page.
        for uid in self.ui["elements"]:
            if uid not in text:
                continue
            current, visited = uid, set()
            while current in self.ui["elements"] and current not in visited:
                visited.add(current)
                for pid, page in self.ui["pages"].items():
                    if current == page["root"]:
                        named.update(self.paths("page", [pid]))
                current = self.ui["elements"][current].get("parent")
        for pid in self.ui["pages"]:
            if f"frontend/tests/ui/{pid}.spec.ts" in text or f"tests/ui/{pid}.spec.ts" in text:
                named.update(self.paths("page", [pid]))
        if not hasattr(workspace, "context_baseline"):
            workspace.context_baseline = (set(workspace.files), set(workspace.writable))
        base_files, base_writable = workspace.context_baseline
        # Replace this layer's previous failure context; keep other layers intact.
        workspace.files = {path for path in workspace.files if not path.startswith(self.layer + "/")} | base_files
        workspace.writable = {path for path in workspace.writable if not path.startswith(self.layer + "/")} | base_writable
        if workspace.target:
            path = selected_path(self.output, workspace.target.kind, workspace.target.identity)
            if path:
                workspace.files.add(path)
                workspace.writable.add(path)
        self.include(workspace, named)

        return value
