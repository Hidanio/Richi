"""Scenario templates and conditions: parsing, rendering, evaluation and their limits."""
import unittest

from richi import templating
from richi.templating import TemplateError, Unresolved


CONTEXT = {
    "inputs": {"branch": "feature_x", "angles": ["security", "tests"], "strict": False, "count": 3, "empty": [],
               "weird": "a'b; rm -rf ~"},
    "steps": {"review": {"summary": "2 findings", "status": "done", "data": {"findings": [{"file": "a.py"}], "p95": 120.5},
                         "answer": None},
              "approve": {"answer": "all", "status": "done"}},
    "matrix": {"angle": "security"},
    "run": {"id": "run-1"},
}


def render(text, context=CONTEXT):
    return templating.render(templating.parse_template(text), context)


def check(text, context=CONTEXT):
    return templating.evaluate(templating.parse_condition(text), context)


class TemplateTests(unittest.TestCase):
    def test_renders_literal_text_and_paths(self):
        self.assertEqual(render("plain"), "plain")
        self.assertEqual(render(""), "")
        self.assertEqual(render("Review {{inputs.branch}} for {{ matrix.angle }}"), "Review feature_x for security")
        self.assertEqual(render("{{steps.review.data.findings[0].file}}"), "a.py")
        self.assertEqual(render("{{steps.review.data.findings.0.file}}"), "a.py")
        self.assertEqual(render('{{steps.review["data"].p95}}'), "120.5")
        nested = {"inputs": {"grid": [["a", "b"]], "nums": {"0": "zero"}}}
        self.assertEqual(render("{{inputs.grid[0][1]}} {{inputs.nums[\"0\"]}}", nested), "b zero")

    def test_formats_values_that_are_not_text(self):
        self.assertEqual(render("{{inputs.strict}}"), "false")
        self.assertEqual(render("{{inputs.count}}"), "3")
        self.assertEqual(render("{{steps.review.answer}}"), "")
        self.assertEqual(render("{{steps.review.data.findings}}"), '[{"file": "a.py"}]')

    def test_literal_braces_and_quote_aware_closing(self):
        # Inline JSON and jq need no escaping for stray '}}'.
        self.assertEqual(render('Reply as {"data": {"findings": []}}'), 'Reply as {"data": {"findings": []}}')
        self.assertEqual(render("a }} b"), "a }} b")
        # Escaped '{{' and '}}' support GitHub Actions and Go/Jinja snippets.
        self.assertEqual(render(r"Check $\{{ secrets.TOKEN }}"), "Check ${{ secrets.TOKEN }}")
        self.assertEqual(render(r"Check $\{{ secrets.TOKEN \}}"), "Check ${{ secrets.TOKEN }}")
        self.assertIsNone(templating.parse_template(r"\{{inputs.branch}}").single)
        # Closing '}}' inside a string literal does not terminate the expression early.
        self.assertEqual(render("{{inputs.missing | default('}}')}}"), "}}")
        self.assertEqual(render('{{inputs.missing | default("a \\"}} b")}}'), 'a "}} b')
        # Backslashes outside \{{ / \}} stay untouched; doubled backslashes before {{ produce literal \ + expression.
        self.assertEqual(render(r"C:\work\file {{inputs.branch}}"), r"C:\work\file feature_x")
        self.assertEqual(render(r"C:\work\\{{inputs.branch}}"), r"C:\work\feature_x")
        self.assertEqual(render(r"\\\{{x}}"), r"\{{x}}")

    def test_filters_and_missing_propagation(self):
        self.assertEqual(render("{{inputs.angles | join}}"), "security, tests")
        self.assertEqual(render("{{inputs.angles | join(' / ')}}"), "security / tests")
        self.assertEqual(render("{{inputs.angles | first}}"), "security")
        self.assertEqual(render("{{inputs.empty | first | default('none')}}"), "none")
        self.assertEqual(render("{{inputs.angles | length}}"), "2")
        self.assertEqual(render("{{inputs.missing | default('n/a')}}"), "n/a")
        self.assertEqual(render("{{steps.review.answer | default(\"no answer\")}}"), "no answer")
        # default protects preceding filters when the value is missing.
        self.assertEqual(render("{{inputs.missing | length | default(0)}}"), "0")
        self.assertEqual(render("{{inputs.missing | join | default('none')}}"), "none")
        self.assertEqual(render("{{inputs.missing | first | default('none')}}"), "none")
        self.assertEqual(render("{{inputs.missing | shell | default('none')}}"), "none")
        with self.assertRaises(Unresolved):
            render("{{inputs.missing | length}}")
        # shell quotes metacharacters cleanly, including after default.
        self.assertEqual(render("git log {{inputs.weird | shell}}"), "git log 'a'\"'\"'b; rm -rf ~'")
        self.assertEqual(render("echo {{inputs.missing | default('a b') | shell}}"), "echo 'a b'")
        with self.assertRaisesRegex(ValueError, "join needs a list"):
            render("{{inputs.branch | join}}")
        with self.assertRaisesRegex(ValueError, "length needs"):
            render("{{inputs.count | length}}")

    def test_redacted_context_never_leaks_secret_values(self):
        # PR 3/4 renders stored strings with a redacted context where secret inputs are omitted or replaced.
        template = templating.parse_template("Token: {{inputs.token | default('<redacted>')}}")
        self.assertEqual(templating.render(template, {"inputs": {}}), "Token: <redacted>")
        raw = templating.parse_template("Token: {{inputs.token}}")
        with self.assertRaises(Unresolved) as caught:
            templating.render(raw, {"inputs": {}})
        self.assertEqual(caught.exception.path, "inputs.token")

    def test_missing_value_is_unresolved(self):
        with self.assertRaises(Unresolved) as caught:
            render("x {{steps.later.summary}}")
        self.assertEqual(caught.exception.path, "steps.later.summary")
        with self.assertRaises(Unresolved):
            render("{{inputs.angles[5]}}")
        with self.assertRaises(Unresolved):
            templating.value(templating.parse_template("{{inputs.missing}}"), CONTEXT)

    def test_single_expression_keeps_its_value(self):
        template = templating.parse_template("{{inputs.angles}}")
        self.assertEqual(templating.value(template, CONTEXT), ["security", "tests"])
        mixed = templating.parse_template("angles: {{inputs.angles}}")
        self.assertIsNone(mixed.single)
        self.assertEqual(templating.value(mixed, CONTEXT), 'angles: ["security", "tests"]')

    def test_reports_paths_it_reads(self):
        template = templating.parse_template("{{inputs.branch}} {{steps.review.data.p95 | default(0)}}")
        self.assertEqual([path.text for path in template.paths()], ["inputs.branch", "steps.review.data.p95"])
        self.assertEqual([path.root for path in template.paths()], ["inputs", "steps"])

    def test_rejects_malformed_templates_with_positions(self):
        cases = {
            "{{inputs.branch": "'{{' without '}}' at character 1",
            "{{}}": "expected a name",
            "{{ secrets.token }}": "unknown name 'secrets'",
            "{{inputs.branch | upper}}": "unknown filter 'upper'",
            "{{inputs.branch | default}}": "filter default takes 1 argument",
            "{{inputs.branch | first(1)}}": "filter first takes no arguments",
            "{{inputs.branch | shell('x')}}": "filter shell takes no arguments",
            "{{inputs.branch.}}": "a name after '.'",
            "{{inputs.branch[-1]}}": "an index or a quoted key",
            "{{inputs.branch extra}}": "expected '|' or '}}'",
            "{{inputs.branch | default('x)}}": "unterminated string",
            "{{inputs.branch | default('\\q')}}": "unknown escape",
            "{{__import__('os')}}": "unknown name",
            "{{inputs.a; rm}}": "unexpected character ';'",
        }
        for text, message in cases.items():
            with self.subTest(text=text), self.assertRaisesRegex(TemplateError, message):
                templating.parse_template(text)
        with self.assertRaises(TemplateError) as caught:
            templating.parse_template("ok {{ inputs.x | nope }}")
        self.assertEqual(caught.exception.position, 17)

    def test_limits(self):
        with self.assertRaisesRegex(TemplateError, "longer than"):
            templating.parse_template("x" * (templating.MAX_TEMPLATE + 1))
        with self.assertRaisesRegex(TemplateError, "more than"):
            templating.parse_template("{{inputs.a}}" * (templating.MAX_EXPRESSIONS + 1))
        with self.assertRaisesRegex(TemplateError, "path is too long"):
            templating.parse_template("{{inputs" + ".a" * (templating.MAX_DEPTH + 1) + "}}")
        with self.assertRaisesRegex(TemplateError, "must be text"):
            templating.parse_template(5)
        big = {"inputs": {"text": "x" * 2000}}
        with self.assertRaisesRegex(ValueError, "rendered text is longer"):
            render("{{inputs.text}}" * 101, big)

    def test_custom_roots(self):
        template = templating.parse_template("{{item.name}}", roots={"item"})
        self.assertEqual(templating.render(template, {"item": {"name": "n"}}), "n")
        with self.assertRaisesRegex(TemplateError, "unknown name 'inputs'"):
            templating.parse_template("{{inputs.x}}", roots={"item"})


class ConditionTests(unittest.TestCase):
    def test_comparisons(self):
        self.assertTrue(check("steps.approve.answer == 'all'"))
        self.assertTrue(check('steps.approve.answer != "none"'))
        self.assertTrue(check("steps.review.data.p95 > 100"))
        self.assertTrue(check("steps.review.data.p95 <= 120.5"))
        self.assertFalse(check("inputs.count < 3"))
        self.assertTrue(check("inputs.count >= 3"))
        self.assertTrue(check("inputs.branch > 'a'"))
        self.assertTrue(check("inputs.count == 3.0"))

    def test_membership_and_nested_equality(self):
        self.assertTrue(check("'security' in inputs.angles"))
        self.assertTrue(check("'api' not in inputs.angles"))
        self.assertTrue(check("matrix.angle in ['security', 'tests']"))
        self.assertTrue(check("'feature' in inputs.branch"))
        self.assertFalse(check("'x' in steps.review.answer"))
        # Booleans are distinct from numbers inside lists/dicts and `in`.
        self.assertFalse(check("inputs.strict in [0]"))
        self.assertTrue(check("inputs.strict not in [0]"))
        self.assertFalse(check("1 in [true]"))
        self.assertFalse(check("inputs.flag_list == [1]", {"inputs": {"flag_list": [True]}}))
        self.assertFalse(check("inputs.obj == inputs.other", {"inputs": {"obj": {"a": True}, "other": {"a": 1}}}))
        # Missing (null) left operand against dict or string is False, not an error.
        self.assertFalse(check("steps.skipped.answer in 'abc'"))
        self.assertFalse(check("steps.skipped.answer in steps.review.data"))
        # Unhashable or non-text left operands raise ValueError, never TypeError.
        with self.assertRaisesRegex(ValueError, "'in' with an object needs text"):
            check("inputs.angles in steps.review.data")
        with self.assertRaisesRegex(ValueError, "'in' with text needs text"):
            check("inputs.count in inputs.branch")
        with self.assertRaisesRegex(ValueError, "'in' needs"):
            check("'x' in inputs.count")

    def test_logic_and_precedence(self):
        self.assertTrue(check("inputs.strict or inputs.count == 3"))
        self.assertFalse(check("inputs.strict and inputs.count == 3"))
        self.assertTrue(check("not inputs.strict"))
        self.assertTrue(check("not inputs.strict and inputs.count == 3"))
        self.assertTrue(check("inputs.strict or not (inputs.count == 4 or false)"))
        self.assertFalse(check("not not inputs.strict"))
        self.assertTrue(check("inputs.angles"))
        self.assertFalse(check("inputs.empty"))

    def test_missing_values_read_as_null(self):
        self.assertTrue(check("steps.skipped.answer == null"))
        self.assertFalse(check("steps.skipped.answer == 'all'"))
        self.assertTrue(check("steps.skipped.answer != 'all'"))
        self.assertFalse(check("steps.skipped.data.flag"))

    def test_booleans_are_not_numbers(self):
        self.assertFalse(check("inputs.strict == 0"))
        self.assertTrue(check("inputs.strict == false"))
        with self.assertRaisesRegex(ValueError, "cannot compare a boolean with a number"):
            check("inputs.strict < 1")
        with self.assertRaisesRegex(ValueError, "cannot compare text with a number"):
            check("inputs.branch > 1")
        with self.assertRaisesRegex(ValueError, "cannot compare null"):
            check("steps.skipped.data.p95 > 1")

    def test_rejects_malformed_conditions(self):
        cases = {
            "": "nonempty",
            "inputs.count ==": "expected a value|expected a name",
            "inputs.count = 3": "unexpected character '='",
            "(inputs.count == 3": "expected \\)",
            "inputs.count == 3)": "expected end of condition",
            "and inputs.x": "expected a value, found 'and'",
            "inputs.x == 3 3": "expected end of condition",
            "os.system('x')": "unknown name 'os'",
            "inputs.x in [inputs.y]": "expected a quoted string",
            "inputs.x == 12345678901234567": "number is too long",
            "inputs.x == 1.12345678901234567": "number is too long",
            "inputs.x == 01": "invalid number",
        }
        for text, message in cases.items():
            with self.subTest(text=text), self.assertRaisesRegex(TemplateError, message):
                templating.parse_condition(text)
        with self.assertRaisesRegex(TemplateError, "longer than"):
            templating.parse_condition("inputs.a or " * 100 + "inputs.a")
        with self.assertRaisesRegex(TemplateError, "nested too deeply"):
            templating.parse_condition("(" * 40 + "inputs.a" + ")" * 40)
        with self.assertRaisesRegex(TemplateError, "nested too deeply"):
            templating.parse_condition("not " * 40 + "inputs.a")

    def test_reports_paths_it_reads(self):
        condition = templating.parse_condition("steps.a.answer == 'x' and (inputs.b in ['y'] or not matrix.c)")
        self.assertEqual(sorted(path.text for path in condition.paths()), ["inputs.b", "matrix.c", "steps.a.answer"])


if __name__ == "__main__":
    unittest.main()
