"""Scenario definitions: validation, normalized dependencies, references and the canonical form."""
import copy
import json
import unittest

from richi import memory, scenario_format, templating
from richi.scenario_format import FormatError, UnsupportedVersion


def review():
    """A synthetic review scenario that uses most of the format."""
    return {
        "format": "richi-scenario",
        "format_version": 1,
        "id": "review",
        "title": "Review",
        "description": "Review the full diff of a branch against its target.",
        "when_to_use": "When the user asks for a review of a branch.",
        "run_title": "Review: {{inputs.branch}}",
        "tags": ["review"],
        "inputs": {
            "branch": {"type": "string", "title": "Branch", "required": True, "picker": "git-branch"},
            "base": {"type": "string", "default": "main"},
            "angles": {"type": "multiselect", "options": ["security", "tests", "api"], "default": ["security"]},
            "strict": {"type": "boolean", "default": False},
        },
        "steps": [
            {"id": "context", "type": "memory.brief",
             "params": {"question": "Risks in {{inputs.base}}...{{inputs.branch}}", "limit": 10}},
            {"id": "angle", "type": "skill", "title": "Review: {{matrix.angle}}",
             "matrix": {"angle": "{{inputs.angles}}"},
             "params": {"skill": {"default": "code-review", "per_agent": {"claude": "engineering:code-review"}},
                        "instruction": "Check {{matrix.angle}}. Known: {{steps.context.summary}}"},
             "hints": {"subagent": True, "timeout_minutes": 30},
             "outputs": {"data": {"findings": {"type": "list", "required": True}}},
             "checks": [{"kind": "data_required", "path": "findings"}]},
            {"id": "merge", "type": "instruction",
             "params": {"text": "Merge {{steps.angle.data.findings}} and rank them."},
             "checks": [{"kind": "min_items", "path": "findings", "count": 1},
                        {"kind": "file_exists", "path": "reports/review.md"}], "retry": {"max": 2}},
            {"id": "approve", "type": "gate",
             "params": {"question": "Accept the findings?", "options": ["all", "some", "none"], "allow_comment": True}},
            {"id": "tests", "type": "command", "needs": ["context"], "if": "inputs.strict",
             "params": {"command": "git diff --stat {{inputs.base | shell}}...{{inputs.branch | shell}}",
                        "expect_exit": 0, "tail_lines": 50},
             "checks": [{"kind": "exit_code", "expect": 0}], "continue_on_failure": True},
            {"id": "done", "type": "checklist", "needs": ["approve", "tests"],
             "if": "steps.approve.answer != 'none'",
             "params": {"items": [{"text": "Reply on {{inputs.branch}}", "required": True}]}},
        ],
        "layout": {"mode": "list", "positions": {"context": [0, 0]}},
    }


class ScenarioFormatTests(unittest.TestCase):
    def assertRejects(self, change, message, where=None):
        definition = review()
        change(definition)
        with self.assertRaisesRegex(FormatError, message) as caught:
            scenario_format.normalize(definition)
        if where is not None:
            self.assertEqual(caught.exception.where, where)
        return caught.exception

    def test_entry_kinds_match_memory_store(self):
        self.assertEqual(set(scenario_format.ENTRY_KINDS), memory.KINDS)

    def test_valid_definition_is_normalized_without_changing_the_input(self):
        original = review()
        kept = copy.deepcopy(original)
        # ancestors() works even before normalization.
        self.assertEqual(scenario_format.ancestors(original)["done"], {"context", "angle", "merge", "approve", "tests"})
        result = scenario_format.normalize(original)
        self.assertEqual(original, kept)
        needs = {step["id"]: step["needs"] for step in result["steps"]}
        self.assertEqual(needs, {"context": [], "angle": ["context"], "merge": ["angle"], "approve": ["merge"],
                                 "tests": ["context"], "done": ["approve", "tests"]})
        self.assertEqual(scenario_format.normalize(result), result)
        self.assertEqual(scenario_format.ancestors(result)["done"], {"context", "angle", "merge", "approve", "tests"})

    def test_explicit_empty_needs_starts_with_the_scenario(self):
        definition = review()
        definition["steps"][3]["needs"] = []
        definition["steps"][5]["if"] = "inputs.strict"
        self.assertEqual(scenario_format.normalize(definition)["steps"][3]["needs"], [])

    def test_canonical_form_and_digest_ignore_key_order(self):
        result = scenario_format.normalize(review())
        shuffled = json.loads(json.dumps(result), object_pairs_hook=lambda pairs: dict(reversed(pairs)))
        self.assertEqual(scenario_format.canonical_json(result), scenario_format.canonical_json(shuffled))
        self.assertEqual(scenario_format.digest(result), scenario_format.digest(shuffled))
        changed = copy.deepcopy(result)
        changed["title"] = "Review 2"
        self.assertNotEqual(scenario_format.digest(result), scenario_format.digest(changed))
        self.assertNotIn(" ", scenario_format.canonical_json({"a": [1, 2]}))

    def test_executor_distinguishes_roles_from_agent_ids(self):
        steps = {step["id"]: step for step in scenario_format.normalize(review())["steps"]}
        self.assertEqual(scenario_format.executor(steps["context"]), ("richi", None))
        self.assertEqual(scenario_format.executor(steps["angle"]), ("agent", None))
        self.assertEqual(scenario_format.executor(steps["approve"]), ("human", None))
        pinned = dict(steps["merge"], executor={"agent": "codex"})
        self.assertEqual(scenario_format.executor(pinned), ("agent", "codex"))
        # A custom agent whose ID happens to be 'human' or 'richi' cannot be confused with a role.
        named_human = dict(steps["merge"], executor={"agent": "human"})
        self.assertEqual(scenario_format.executor(named_human), ("agent", "human"))

    def test_parse(self):
        text = json.dumps(review())
        self.assertEqual(scenario_format.load(text)["id"], "review")
        self.assertEqual(scenario_format.load(text.encode("utf-8"))["id"], "review")
        cases = {
            '{"a": 1, "a": 2}': "duplicate key 'a'",
            "[1]": "must be a JSON object",
            "{": "not valid JSON",
            '{"a": NaN}': "NaN is not a JSON number",
            '{"a": 1e999}': "too large",
            '{"a": ' + "1" * 101 + "}": "longer than 100 digits",
            "[" * 100000 + "]" * 100000: "too deeply|larger than",
        }
        for text, message in cases.items():
            with self.subTest(text=text[:20]), self.assertRaisesRegex(FormatError, message):
                scenario_format.parse(text)
        with self.assertRaisesRegex(FormatError, "UTF-8"):
            scenario_format.parse(b"\xff")
        with self.assertRaisesRegex(FormatError, "UTF-8"):
            scenario_format.parse('{"a": "\ud800"}')
        with self.assertRaisesRegex(FormatError, "larger than"):
            scenario_format.parse('{"a": "' + "x" * scenario_format.MAX_BYTES + '"}')

    def test_newer_format_version_is_checked_before_unknown_top_fields(self):
        newer = {"format": "richi-scenario", "format_version": 2, "on_failure": {}}
        with self.assertRaises(UnsupportedVersion) as caught:
            scenario_format.normalize(newer)
        self.assertEqual(caught.exception.version, 2)
        self.assertIn("update Richi", str(caught.exception))

    def test_top_level_fields_and_python_inputs(self):
        self.assertRejects(lambda d: d.update(on_failure={}), "unknown field on_failure")
        self.assertRejects(lambda d: d.pop("steps"), "missing required field steps")
        self.assertRejects(lambda d: d.update(format="other"), "must be 'richi-scenario'", "format")
        self.assertRejects(lambda d: d.update(format_version=0), "reads scenario format 1, not 0")
        self.assertRejects(lambda d: d.update(format_version=True), "whole number")
        self.assertRejects(lambda d: d.update(id="-bad"), "Scenario ID", "id")
        self.assertRejects(lambda d: d.update(title=""), "nonempty text", "title")
        self.assertRejects(lambda d: d.update(title="a\nb"), "one line", "title")
        self.assertRejects(lambda d: d.update(title="a\u0085b"), "one line", "title")
        self.assertRejects(lambda d: d.update(title="a\u2028b"), "one line", "title")
        self.assertRejects(lambda d: d.update(description="\x00"), "control characters", "description")
        self.assertRejects(lambda d: d.update(tags=["ok", ""]), "nonempty text", "tags[1]")
        self.assertRejects(lambda d: d.update(tags=["   "]), "nonempty text", "tags[0]")
        self.assertRejects(lambda d: d.update(tags=["\ud800"]), "UTF-8", "tags[0]")
        self.assertRejects(lambda d: d.update(steps=[]), "1 to 100 items", "steps")
        self.assertRejects(lambda d: d.update(layout=[]), "must be an object", "layout")
        self.assertRejects(lambda d: d.update(layout={"x": {1, 2}}), "not JSON", "layout")
        self.assertRejects(lambda d: d.update(layout={1: "a"}), "object keys must be text", "layout")
        self.assertRejects(lambda d: d.update(layout={"x": "y" * scenario_format.MAX_LAYOUT}), "larger than", "layout")
        self.assertRejects(lambda d: d["inputs"].update({1: {"type": "string"}}), "object keys must be text")
        deep = {}
        node = deep
        for _ in range(40):
            node["a"] = {}
            node = node["a"]
        self.assertRejects(lambda d: d.update(layout=deep), "nested too deeply", "layout")
        self.assertRejects(lambda d: d.update(run_title="{{steps.merge.summary}}"), "only a step can read")
        self.assertRejects(lambda d: d.update(run_title="{{inputs.nope}}"), "does not declare", "run_title")

    def test_inputs(self):
        def input_field(name, spec):
            return lambda d: d["inputs"].update({name: spec})
        self.assertRejects(input_field("Bad", {"type": "string"}), "Input name", "inputs.Bad")
        self.assertRejects(input_field("x", {}), "missing required field type", "inputs.x")
        self.assertRejects(input_field("x", {"type": "date"}), "type must be one of", "inputs.x.type")
        self.assertRejects(input_field("x", {"type": "string", "extra": 1}), "unknown field extra")
        self.assertRejects(input_field("x", {"type": "select"}), "needs options")
        self.assertRejects(input_field("x", {"type": "select", "options": ["a", "a"]}), "repeat a value")
        self.assertRejects(input_field("x", {"type": "string", "options": ["a"]}), "only select and multiselect")
        self.assertRejects(input_field("x", {"type": "secret", "default": "s"}), "cannot have a default")
        self.assertRejects(input_field("x", {"type": "select", "options": ["a"], "default": "b"}), "must be one of a")
        self.assertRejects(input_field("x", {"type": "multiselect", "options": ["a"], "default": ["a", "a"]}),
                           "must hold at most 1|distinct")
        self.assertRejects(input_field("x", {"type": "number", "default": True}), "must be a number")
        self.assertRejects(input_field("x", {"type": "boolean", "default": "yes"}), "true or false")
        self.assertRejects(input_field("x", {"type": "string", "picker": "color"}), "picker must be one of")
        self.assertRejects(input_field("x", {"type": "string", "required": "yes"}), "true or false")
        self.assertRejects(lambda d: d["inputs"].update({"i%d" % n: {"type": "string"} for n in range(60)}),
                           "at most 50 inputs")
        spec = {"type": "multiselect", "options": ["a", "b"]}
        self.assertEqual(scenario_format.check_input("x", spec, ["b", "a"]), ["b", "a"])
        with self.assertRaisesRegex(FormatError, "distinct"):
            scenario_format.check_input("x", spec, ["c"])
        self.assertEqual(scenario_format.check_input("x", {"type": "string"}, ""), "")
        self.assertEqual(scenario_format.check_input("x", {"type": "text"}, "a\nb"), "a\nb")

    def test_secrets_are_restricted_to_execution_params_and_presence_checks(self):
        def with_secret(change):
            def run(d):
                d["inputs"]["token"] = {"type": "secret"}
                change(d)
            return run

        # Allowed: agent instruction, skill instruction, shell-quoted command, and presence/null condition checks.
        ok = review()
        ok["inputs"]["token"] = {"type": "secret"}
        ok["steps"][1]["params"]["instruction"] = "Use {{inputs.token}}"
        ok["steps"][2]["params"]["text"] = "Use {{inputs.token}}"
        ok["steps"][4]["params"]["command"] = "curl -H {{inputs.token | shell}} https://example.test"
        ok["steps"][4]["if"] = "inputs.token != null and inputs.strict"
        scenario_format.normalize(ok)

        ok_bare = review()
        ok_bare["inputs"]["token"] = {"type": "secret"}
        ok_bare["steps"][4]["if"] = "not inputs.token"
        scenario_format.normalize(ok_bare)

        # Refused: stored fields, cwd, matrix, and value comparisons in conditions.
        self.assertRejects(with_secret(lambda d: d.update(run_title="Run {{inputs.token}}")), "secret input token")
        self.assertRejects(with_secret(lambda d: d["steps"][2].update(title="Step {{inputs.token}}")), "secret input token")
        self.assertRejects(with_secret(lambda d: d["steps"][0]["params"].update(question="{{inputs.token}}")), "secret input token")
        self.assertRejects(with_secret(lambda d: d["steps"][3]["params"].update(question="Use {{inputs.token}}?")), "secret input token")
        self.assertRejects(with_secret(lambda d: d["steps"][4]["params"].update(cwd="sub/{{inputs.token}}")), "secret input token")
        self.assertRejects(with_secret(lambda d: d["steps"][1].update(matrix={"angle": "{{inputs.token}}"})), "secret input token")
        self.assertRejects(with_secret(lambda d: d["steps"][4].update({"if": "inputs.token == 'abc'"})),
                           "only be checked for presence or null")
        self.assertRejects(with_secret(lambda d: d["steps"][4].update({"if": "'a' in inputs.token"})),
                           "only be checked for presence or null")

    def test_command_quoting_and_cwd_rules(self):
        self.assertRejects(lambda d: d["steps"][4]["params"].update(command="git log {{inputs.branch}}"),
                           "must end with \\| shell")
        self.assertRejects(lambda d: d["steps"][4]["params"].update(command="make test \u202e; rm -rf ~ \u202c"),
                           "control or bidirectional")
        self.assertRejects(lambda d: d["steps"][4]["params"].update(cwd="{{steps.context.summary}}"),
                           "cwd cannot read step results")
        # cwd does NOT require | shell.
        ok = review()
        ok["steps"][4]["params"]["cwd"] = "packages/{{inputs.branch}}"
        scenario_format.normalize(ok)

    def test_steps_and_checks(self):
        def step(index, **fields):
            return lambda d: d["steps"][index].update(fields)
        self.assertRejects(lambda d: d["steps"].append("x"), "must be an object", "steps[6]")
        self.assertRejects(step(0, id="Bad"), "Step ID", "steps[0].id")
        self.assertRejects(step(1, id="context"), "used twice", "steps.context")
        self.assertRejects(step(0, type="shell"), "type must be one of", "steps.context.type")
        self.assertRejects(step(0, type=["x"]), "type must be one of")
        self.assertRejects(step(0, unknown=1), "unknown field unknown")
        self.assertRejects(step(2, params={}), "missing required field text", "steps.merge.params")
        self.assertRejects(step(2, params={"text": ""}), "nonempty text", "steps.merge.params.text")
        self.assertRejects(step(3, params={"question": "   "}), "nonempty text", "steps.approve.params.question")
        self.assertRejects(step(0, params={"question": ""}), "nonempty text", "steps.context.params.question")
        self.assertRejects(step(2, params={"text": "x", "skill": "y"}), "unknown field skill")
        self.assertRejects(step(1, params={"skill": {}}), "default skill or one per agent")
        self.assertRejects(step(1, params={"skill": {"default": "bad name"}}), "Skill name")
        self.assertRejects(step(1, params={"skill": {"per_agent": {"Claude": "x"}}}), "Agent ID")
        self.assertRejects(step(3, params={"question": "q", "options": ["one"]}), "2 to 10 items")
        self.assertRejects(step(4, params={"command": "a\nb"}), "one line")
        self.assertRejects(step(4, params={"command": "x", "expect_exit": 256}), "0 to 255")
        self.assertRejects(step(5, params={"items": []}), "1 to 50 items")
        self.assertRejects(step(0, params={"question": "q", "limit": 0}), "1 to 50")
        self.assertRejects(step(0, type="memory.recall", params={"query": "q", "kinds": ["idea"]}), "must be one of task")
        self.assertRejects(step(0, type="condition", params={"expr": "inputs.strict =="}), "expected")
        self.assertRejects(step(2, executor={"agent": "codex", "role": "agent"}), "unknown field role")
        self.assertRejects(step(3, executor={"agent": "codex"}), "can be pinned to an agent")
        self.assertRejects(step(3, executor={"role": "human"}), "unknown field role")
        self.assertRejects(step(2, hints={"subagent": "yes"}), "true or false")
        self.assertRejects(step(2, hints={"timeout_minutes": 0}), "1 to 1440")
        self.assertRejects(step(3, outputs={}), "only instruction, skill and command steps report data")
        self.assertRejects(step(2, outputs={"data": {"x": {"type": "date"}}}), "type must be one of")
        self.assertRejects(step(2, checks=[{"kind": "regex"}]), "kind must be one of")
        self.assertRejects(step(2, checks=[{"kind": ["x"]}]), "kind must be one of")
        self.assertRejects(step(2, checks=[{"kind": "min_items", "path": "findings"}]), "missing required field count")
        self.assertRejects(step(2, checks=[{"kind": "data_required", "path": "Bad Path"}]), "dotted path")
        self.assertRejects(step(2, checks=[{"kind": "exit_code", "expect": 0}]), "only command steps report an exit")
        self.assertRejects(step(4, checks=[{"kind": "exit_code", "expect": 1}]), "conflicts with params.expect_exit")
        self.assertRejects(step(3, checks=[{"kind": "evidence_git"}]), "report results to check")
        self.assertRejects(step(2, outputs={"data": {"note": {"type": "string"}}},
                                checks=[{"kind": "min_items", "path": "note", "count": 1}]),
                           "outputs declares as string")
        # file_exists accepts relative POSIX paths (including uppercase/templates) and rejects traversal/absolute.
        ok = review()
        ok["steps"][1]["checks"].append({"kind": "file_exists", "path": "reports/README-{{matrix.angle}}.md"})
        scenario_format.normalize(ok)
        for bad_path in ("/etc/passwd", "../a.md", "a/../b.md", "a\\b.md", "C:a.md", "a//b.md", "."):
            with self.subTest(bad_path=bad_path):
                self.assertRejects(step(2, checks=[{"kind": "file_exists", "path": bad_path}]),
                                   "relative POSIX|path segments")
        # retry and continue_on_failure are for agent steps only.
        self.assertRejects(step(2, retry={"max": 9}), "0 to 5")
        self.assertRejects(step(3, retry={"max": 1}), "only instruction, skill and command steps can be retried")
        self.assertRejects(step(0, continue_on_failure=True), "continue on failure")
        self.assertRejects(step(2, continue_on_failure=1), "true or false")
        self.assertRejects(step(2, needs="angle"), "must be a list")
        self.assertRejects(step(2, needs=["angle", "angle"]), "names a step twice")

    def test_matrix_validation_and_expansion(self):
        def matrix(value, index=1):
            return lambda d: d["steps"][index].update(matrix=value)
        self.assertRejects(matrix({}), "1 to 3 keys")
        self.assertRejects(matrix({"Bad": ["a"]}), "Matrix key")
        self.assertRejects(matrix({"angle": "angles: {{inputs.angles}}"}), "one {{...}} expression")
        self.assertRejects(matrix({"angle": "{{inputs.angles | join}}"}), "cannot use filters")
        self.assertRejects(matrix({"angle": "{{inputs.branch}}"}), "multiselect input")
        self.assertRejects(matrix({"angle": "{{run.id}}"}), "multiselect input or steps")
        self.assertRejects(matrix({"angle": ["a", "a"]}), "repeats a value")
        self.assertRejects(matrix({"angle": "{{matrix.angle}}"}), "matrix cannot read itself")
        self.assertRejects(matrix({"angle": ["a"]}, index=0), "only agent and human steps can be repeated")
        # Product overflow is rejected at save time.
        self.assertRejects(matrix({"angle": ["a%d" % n for n in range(10)], "b": ["b%d" % n for n in range(6)]}),
                           "more than 50 instances")
        # matrix.* in `if` is rejected (if runs before expansion).
        self.assertRejects(lambda d: d["steps"][1].update({"if": "matrix.angle == 'security'"}),
                           "before matrix expansion")
        # A step whose ID is literally "matrix" may still read its own matrix.* keys.
        named = review()
        named["steps"][1]["id"] = "matrix"
        named["steps"][2]["needs"] = ["matrix"]
        named["steps"][2]["params"]["text"] = "Merge {{steps.matrix.data.findings}}"
        scenario_format.normalize(named)
        # Matrix step exposes aggregate fields (status, items, data, files), not scalar summary/answer.
        self.assertRejects(lambda d: d["steps"][2]["params"].update(text="{{steps.angle.summary}}"),
                           "exposes data, files, items, status")
        ok_items = review()
        ok_items["steps"][2]["params"]["text"] = "First: {{steps.angle.items[0].summary}}"
        scenario_format.normalize(ok_items)
        # Runtime expansion helper enforces types, uniqueness and cap.
        step = scenario_format.normalize(review())["steps"][1]
        self.assertEqual(scenario_format.expand_matrix(step, {"inputs": {"angles": ["security", "api"]}}),
                         [{"angle": "security"}, {"angle": "api"}])
        with self.assertRaises(templating.Unresolved):
            scenario_format.expand_matrix(step, {"inputs": {}})
        with self.assertRaisesRegex(ValueError, "nonempty list"):
            scenario_format.expand_matrix(step, {"inputs": {"angles": []}})
        with self.assertRaisesRegex(ValueError, "more than 50 instances"):
            scenario_format.expand_matrix(step, {"inputs": {"angles": ["a%d" % n for n in range(51)]}})

    def test_graph(self):
        self.assertRejects(lambda d: d["steps"][0].update(needs=["context"]), "cannot depend on itself")
        self.assertRejects(lambda d: d["steps"][0].update(needs=["ghost"]), "unknown step ghost")
        error = self.assertRejects(lambda d: d["steps"][0].update(needs=["merge"]), "cycle")
        self.assertIn("context -> merge -> angle -> context", str(error))

    def test_references(self):
        def text(index, value):
            return lambda d: d["steps"][index]["params"].update(text=value)
        self.assertRejects(text(2, "{{steps.approve.answer}}"), "does not depend on; add it to needs",
                           "steps.merge.params.text")
        self.assertRejects(text(2, "{{steps.merge.summary}}"), "cannot read its own results")
        self.assertRejects(text(2, "{{steps.ghost.summary}}"), "unknown step")
        self.assertRejects(text(2, "{{steps.context.answer}}"), "exposes data, status, summary")
        self.assertRejects(text(2, "{{steps.context.exit_code}}"), "exposes data, status, summary")
        self.assertRejects(text(2, "{{matrix.angle}}"), "matrix key this step does not have")
        self.assertRejects(text(2, "{{inputs.ghost}}"), "does not declare")
        self.assertRejects(text(2, "{{inputs.branch.sub}}"), "scalar input branch has no sub-fields")
        self.assertRejects(text(2, "{{inputs.angles.foo}}"), "only an integer index")
        self.assertRejects(text(2, "{{inputs.angles[0].x}}"), "only an integer index")
        self.assertRejects(text(2, "{{now.dtae}}"), "unknown system value")
        self.assertRejects(text(2, "{{run.ID}}"), "unknown system value")
        self.assertRejects(text(2, "{{agent}}"), "unknown system value")
        self.assertRejects(lambda d: d["steps"][4].update({"if": "steps.merge.status == 'done'"}),
                           "does not depend on", "steps.tests.if")
        definition = review()
        definition["steps"][2]["params"]["text"] = (
            "{{steps.context.summary}} {{inputs.angles[0]}} {{run.id}} {{project.path}} {{agent.id}} {{now.date}}")
        scenario_format.normalize(definition)

    def test_limits(self):
        many = [{"id": "s%d" % n, "type": "instruction", "params": {"text": "x"}} for n in range(101)]
        self.assertRejects(lambda d: d.update(steps=many), "1 to 100 items")
        huge = "x" * 19000
        steps = [{"id": "s%d" % n, "type": "instruction", "params": {"text": huge}} for n in range(15)]
        self.assertRejects(lambda d: d.update(steps=steps), "larger than")


if __name__ == "__main__":
    unittest.main()
