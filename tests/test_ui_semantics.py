"""Explicit presentation assertions keep their source, scope and write protection."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from infrastructure.shared.agent.context import SourceFiles
from infrastructure.shared.project.initialize import initialize_project
from infrastructure.stages.frontend.ui.design import apply_proposal
from infrastructure.stages.frontend.ui.runtime import prepare_frontend
from infrastructure.stages.frontend.ui.semantics import validated_contracts
from infrastructure.stages.frontend.ui.testing import (
    FrontendChecks, existence_suite, presentation_files, semantic_test_path,
)
from infrastructure.stages.requirements.tree import RequirementTree
from infrastructure.utils.files import save_text


def fixture():
    title = 'Render a button named "Publish".'
    secret = 'The masked password field has the accessible name "Access key".'
    forbidden = 'Do not display a link named "Delete".'
    tree = RequirementTree({"id": "EDITOR", "description": "\n".join((title, secret, forbidden))})
    def node(kind, **props):
        return {"type": kind, "parent": "ROOT", "prop": props, "data": [], "mode": []}
    design = {"pages": {}, "elements": {}, "requirements": {}, "steps": {}}
    proposal = {"pages": {"editor": {"route": "/", "root": "ROOT"}},
        "elements": {"ROOT": {**node("page"), "parent": None}, "PUBLISH": node("button", text="Publish"),
            "KEY": node("input", input_type="password", label="Access key"), "DELETE": node("link", text="Delete")},
        "requirement_ui": ["ROOT", "PUBLISH", "KEY", "DELETE"], "step_ui": {},
        "presentation_contracts": [
            {"element_id": "PUBLISH", "role": "button", "name": "Publish", "evidence": title},
            {"element_id": "KEY", "role": "password", "name": "Access key", "evidence": secret},
            {"element_id": "DELETE", "role": "link", "name": "Delete", "evidence": forbidden},
            {"element_id": "PUBLISH", "role": "button", "name": "Publish", "evidence": 'button named "Publish"'},
        ]}
    return tree, apply_proposal(design, proposal, "EDITOR", tree)


class UiSemantics(unittest.TestCase):
    def test_rebinds_only_to_a_unique_control_in_the_same_requirement(self):
        tree, design = fixture()
        elements = design["elements"]
        entry = design["presentation_contracts"]["EDITOR"][0]
        entry["element_id"] = "CANCEL"
        elements["CANCEL"] = {**elements["PUBLISH"], "prop": {"text": "Cancel"}}
        design["requirements"]["EDITOR"].append("CANCEL")
        checks, _ = validated_contracts(design, tree)
        self.assertEqual(checks["PUBLISH"]["original_element_id"], "CANCEL")
        self.assertNotIn("CANCEL", checks)
        self.assertEqual(entry["element_id"], "CANCEL")

        elements["OTHER_PUBLISH"] = {**elements["PUBLISH"]}
        design["requirements"]["EDITOR"].append("OTHER_PUBLISH")
        checks, deferred = validated_contracts(design, tree)
        self.assertNotIn("PUBLISH", checks)
        self.assertNotIn("OTHER_PUBLISH", checks)
        self.assertEqual(deferred[0]["candidate_element_ids"], ["OTHER_PUBLISH", "PUBLISH"])

        design["requirements"]["EDITOR"] = ["CANCEL"]
        checks, deferred = validated_contracts(design, tree)
        self.assertEqual(checks, {})
        self.assertEqual(deferred[0]["candidate_element_ids"], [])

    def test_stage10_runs_semantics_alongside_preserved_existing_suite(self):
        from infrastructure.stages.frontend.ui.implementation import implement_ui
        tree, design = fixture()
        contracts, _ = validated_contracts(design, tree)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            existing = "frontend/tests/ui/editor.spec.ts"
            semantic = semantic_test_path("editor")
            save_text(root / existing, "// Existing display fixture correction")
            checks = Mock()
            checks.typecheck.return_value = checks.build.return_value = {"returncode": 0, "output": ""}
            checks.tests.return_value = {"returncode": 0, "output": "", "passed": 2, "failed": 0}
            model = Mock()
            with patch("infrastructure.stages.frontend.ui.implementation.prepare_frontend"), \
                    patch("infrastructure.stages.frontend.ui.implementation.install_dependencies"), \
                    patch("infrastructure.stages.frontend.ui.implementation.FrontendChecks", return_value=checks):
                report = implement_ui(root / ".arc", design, model, Mock(), {"pages": {}}, semantic_contracts=contracts)
            self.assertEqual(report["status"], "GREEN", report)
            self.assertEqual(report["pages"]["editor"]["semantic_test_path"], semantic)
            self.assertEqual((root / existing).read_text(), "// Existing display fixture correction")
            self.assertTrue((root / semantic).is_file())
            self.assertTrue(all(call.args == ([existing, semantic],) for call in checks.tests.call_args_list))
            model.generate_json.assert_not_called()

    def test_only_verified_and_visible_contracts_generate_assertions(self):
        tree, design = fixture()
        checks, deferred = validated_contracts(design, tree)
        self.assertEqual(set(checks), {"PUBLISH", "KEY"})
        self.assertEqual(len(deferred), 2)
        design["elements"]["KEY"]["prop"]["visible"] = False
        suite = existence_suite(design, "editor", semantic_checks=checks)
        self.assertIn('getByRole("button", {name: "Publish", exact: true})', suite)
        self.assertNotIn('Access key', suite)
        self.assertNotIn('Delete', suite)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            relative = semantic_test_path("editor")
            save_text(root / relative, suite)
            selection = SourceFiles(root, design)
            workspace = selection.workspace(references=[relative])
            self.assertFalse(workspace.can_write(relative))
            selection.feedback(workspace, {"failure": relative})
            self.assertFalse(workspace.can_write(relative))
            self.assertEqual(presentation_files(root, ["editor"]), [relative])

    def test_derives_quoted_accessible_name_when_design_omits_contract(self):
        tree = RequirementTree({
            "id": "LOGIN",
            "description": 'The username field has the placeholder and accessible name "Email/Username/Mobile number".',
        })
        design = {
            "pages": {"login": {"route": "/login", "root": "ROOT"}},
            "elements": {
                "ROOT": {"type": "page", "parent": None, "prop": {}, "data": [], "mode": []},
                "USERNAME": {"type": "input", "parent": "ROOT", "prop": {
                    "label": "Email/Username/Mobile number",
                    "placeholder": "Email/Username/Mobile number",
                }, "data": [], "mode": ["user_write"]},
            },
            "requirements": {"LOGIN": ["ROOT", "USERNAME"]},
            "steps": {},
        }

        checks, deferred = validated_contracts(design, tree)

        self.assertEqual(checks["USERNAME"]["role"], "textbox")
        self.assertEqual(checks["USERNAME"]["name"], "Email/Username/Mobile number")
        self.assertEqual(deferred, [])

    @unittest.skipUnless(os.environ.get("ARC_DB_TEST_NODE_MODULES"), "Requires local Node and Chromium")
    def test_real_browser_distinguishes_presence_from_role_and_password_name(self):
        tree, design = fixture()
        contracts, _ = validated_contracts(design, tree)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_project(root)
            (root / "node_modules").symlink_to(Path(os.environ["ARC_DB_TEST_NODE_MODULES"]).resolve())
            prepare_frontend(root)
            relative = semantic_test_path("editor")
            save_text(root / relative, existence_suite(design, "editor", semantic_checks=contracts))
            save_text(root / "frontend/tests/ui/editor.spec.ts", existence_suite(design, "editor"))
            source = root / "frontend/src/main.tsx"
            def render(tag, label):
                save_text(source, 'window.__ARC_UI_DATA__ = {page:"editor",data:{}};\n'
                    'document.body.innerHTML = `<main data-testid="ROOT">'
                    f'<{tag} data-testid="PUBLISH">Publish</{tag}>'
                    f'<label for="key">{label}</label><input type="password" id="key" data-testid="KEY">'
                    '<a data-testid="DELETE" href="/">Delete</a></main>`;')
            browser = FrontendChecks(root)
            render("span", "Access key")
            self.assertEqual(browser.tests(["frontend/tests/ui/editor.spec.ts"])["returncode"], 0)
            wrong_role = browser.tests([relative])
            self.assertEqual(wrong_role["failed"], 1, wrong_role)
            self.assertIn("PUBLISH", str(wrong_role["failures"]))
            render("button", "Different key")
            wrong_label = browser.tests([relative])
            self.assertEqual(wrong_label["failed"], 1, wrong_label)
            self.assertIn("KEY", str(wrong_label["failures"]))
            render("button", "Access key")
            self.assertEqual(browser.tests(presentation_files(root, ["editor"]))["returncode"], 0)
