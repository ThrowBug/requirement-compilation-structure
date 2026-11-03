"""Check native option values independently from fixture-driven selection state."""

import unittest

from infrastructure.stages.frontend.ui.testing import existence_suite


class UiControlAssertions(unittest.TestCase):
    def design(self, fixture):
        def element(kind, data=(), **props):
            return {"type": kind, "parent": "root", "data": list(data), "prop": props}
        return {"pages": {"registration": {"route": "/register", "root": "root",
                "fixtures": {"sample": fixture}}},
            "bindings": {"gender": {"initial": ""}, "agree": {"initial": False},
                "country": {"initial": "China"}},
            "elements": {"root": {"type": "page", "parent": None, "data": [], "prop": {}},
                "male": element("input", ["gender"], input_type="radio", value="Male"),
                "female": element("input", ["gender"], input_type="radio", value="Female"),
                "agree": element("checkbox", ["agree"]),
                "native-agree": element("input", ["agree"], input_type="checkbox"),
                "country": element("select", ["country"], options=["China", "France"]),
                "static": element("input", value="fixed"),
                "static-radio": element("input", input_type="radio", value="Option")}}

    def test_radio_value_is_constant_while_fixture_selects_an_option(self):
        for selected in ("", "Male", "Female"):
            with self.subTest(selected=selected):
                suite = existence_suite(self.design({"gender": selected}), "registration")
                for uid, value in (("male", "Male"), ("female", "Female")):
                    locator = f'page.getByTestId("{uid}")'
                    self.assertIn(f'await expect({locator}).toHaveValue("{value}");', suite)
                    negate = "" if selected == value else ".not"
                    self.assertIn(f"await expect({locator}){negate}.toBeChecked();", suite)
                    self.assertNotIn(f'await expect({locator}).toHaveValue("");', suite)

    def test_checkbox_types_select_and_static_values_keep_native_semantics(self):
        for checked in (False, True):
            suite = existence_suite(self.design({"agree": checked, "country": "France"}), "registration")
            for uid in ("agree", "native-agree"):
                negate = "" if checked else ".not"
                self.assertIn(f'await expect(page.getByTestId("{uid}")){negate}.toBeChecked();', suite)
                self.assertNotIn(f'await expect(page.getByTestId("{uid}")).toHaveValue', suite)
            self.assertIn('await expect(page.getByTestId("country")).toHaveValue("France");', suite)
            self.assertIn('await expect(page.getByTestId("static")).toHaveValue("fixed");', suite)
            self.assertIn('await expect(page.getByTestId("static-radio")).toHaveValue("Option");', suite)
            self.assertNotIn('expect(page.getByTestId("static-radio")).toBeChecked', suite)
