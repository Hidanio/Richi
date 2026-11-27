"""Scenario definitions: the format, its validation and its canonical form.

A scenario is a reusable procedure an agent follows in a chat: input fields,
then steps of a few generic kinds (an instruction, a skill, a command, a
question to the human, a memory lookup, a condition) joined by explicit
dependencies. Nothing here runs a step or touches a store; this module only
decides whether a definition is well formed and returns it normalized:

* every step lists its dependencies in `needs`; a step without the field
  depends on the step before it, which is how a plain list of steps reads,
  and an explicit empty list starts a step with the scenario. Because the
  graph is explicit from the first version, a free canvas can later show the
  same definitions without migrating them;
* templates and conditions are parsed (templating.py), and every value they
  name must exist when the step runs: an input the scenario declares, a step
  the step depends on (directly or through others), a matrix key of its own;
* fields this version does not know are refused, so a definition never claims
  a behavior no reader implements. A later format raises FORMAT_VERSION.

The canonical form is sorted, compact JSON; its SHA-256 identifies a version.
"""
import hashlib
import json
import math
import re

from . import agents, templating


FORMAT = "richi-scenario"
FORMAT_VERSION = 1
MAX_BYTES = 256 * 1024
MAX_STEPS = 100
MAX_INPUTS = 50
MAX_MATRIX_KEYS = 3
MAX_MATRIX_VALUES = 50
MAX_OPTIONS = 50
MAX_CHECKS = 20
MAX_TAGS = 20
MAX_TITLE = 200
MAX_DESCRIPTION = 4000
MAX_LAYOUT = 64 * 1024
MAX_DEPTH = 32
MAX_RETRIES = 5
ENTRY_KINDS = ("task", "decision", "fact", "note", "investigation", "experiment", "procedure")
# Scenario IDs share the shape of rule IDs; step IDs and input names are shorter
# and simpler, since templates name them: {{steps.review.summary}}.
SCENARIO_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}\Z")
SCENARIO_ID_HELP = "Scenario ID: 1-100 letters, digits, '.', '_', ':' or '-', starting with a letter or digit"
STEP_ID = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
STEP_ID_HELP = "Step ID: 1-48 lowercase letters, digits, '_' or '-', starting with a letter"
INPUT_NAME = re.compile(r"[a-z][a-z0-9_]{0,47}\Z")
INPUT_NAME_HELP = "Input name: 1-48 lowercase letters, digits or '_', starting with a letter"
TAG = re.compile(r"[^\x00-\x1f\x7f]{1,60}\Z")

TOP_FIELDS = {"format", "format_version", "id", "title", "description", "when_to_use", "run_title", "tags",
              "inputs", "steps", "layout"}
TOP_REQUIRED = ("format", "format_version", "id", "title", "steps")
STEP_FIELDS = {"id", "type", "title", "params", "needs", "if", "matrix", "executor", "hints", "outputs", "checks",
               "retry", "continue_on_failure"}
INPUT_TYPES = ("string", "text", "number", "boolean", "select", "multiselect", "secret")
INPUT_FIELDS = {"type", "title", "description", "required", "default", "options", "picker"}
PICKERS = ("git-branch", "git-revision", "project", "entry", "skill", "file")
OUTPUT_TYPES = ("string", "number", "boolean", "list", "object")
HINT_FIELDS = {"subagent": bool, "worktree": bool, "timeout_minutes": int}
# Who performs a step of each type. An agent step may be pinned to one agent
# with executor.agent; a human step is answered by the person in the chat.
EXECUTORS = {"instruction": "agent", "skill": "agent", "command": "agent", "gate": "human", "checklist": "human",
             "memory.recall": "richi", "memory.brief": "richi", "condition": "richi"}
CHECK_KINDS = {"data_required": {"path"}, "min_items": {"path", "count"}, "exit_code": {"expect"},
               "file_exists": {"path"}, "evidence_git": set()}


class FormatError(ValueError):
    """A definition that does not follow the format; where is a path such as steps[2].params.text."""

    def __init__(self, message, where=None):
        super().__init__(message if not where else "%s: %s" % (where, message))
        self.where = where
        self.reason = message


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise FormatError("duplicate key %r" % key)
        result[key] = value
    return result


def parse(text):
    """The definition in JSON text, before validation: one object, no duplicate keys."""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FormatError("a definition must be UTF-8 text") from exc
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise FormatError("a definition is larger than %d bytes" % MAX_BYTES)
    try:
        value = json.loads(text, object_pairs_hook=_unique, parse_int=agents._short_integer,
                           parse_float=_float, parse_constant=_constant)
    except FormatError:
        raise
    except RecursionError as exc:
        raise FormatError("a definition is nested too deeply") from exc
    except ValueError as exc:
        raise FormatError("not valid JSON: " + str(exc)) from exc
    if not isinstance(value, dict):
        raise FormatError("a definition must be a JSON object")
    return value


def _constant(name):
    raise FormatError("%s is not a JSON number" % name)


def _float(text):
    value = float(text)
    if not math.isfinite(value):
        raise FormatError("a number is too large")
    return value


# ---------------------------------------------------------------- small checks


def _object(value, where, fields=None, required=()):
    if not isinstance(value, dict):
        raise FormatError("must be an object", where)
    if fields is not None:
        unknown = sorted(set(value) - set(fields))
        if unknown:
            raise FormatError("unknown field%s %s" % ("s" if len(unknown) > 1 else "", ", ".join(unknown)), where)
    for name in required:
        if name not in value:
            raise FormatError("missing required field " + name, where)
    return value


def _line(value, where, limit=MAX_TITLE, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise FormatError("must be %stext" % ("" if empty else "nonempty "), where)
    if len(value) > limit:
        raise FormatError("is longer than %d characters" % limit, where)
    if "\n" in value or "\r" in value or re.search(r"[\x00-\x08\x0b-\x1f\x7f]", value):
        raise FormatError("must be one line without control characters", where)
    _utf8(value, where)
    return value


def _prose(value, where, limit=MAX_DESCRIPTION):
    if not isinstance(value, str):
        raise FormatError("must be text", where)
    if len(value) > limit:
        raise FormatError("is longer than %d characters" % limit, where)
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value):
        raise FormatError("must not hold control characters", where)
    _utf8(value, where)
    return value


def _utf8(value, where):
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise FormatError("holds text that is not UTF-8", where) from exc


def _bool(value, where):
    if not isinstance(value, bool):
        raise FormatError("must be true or false", where)
    return value


def _integer(value, where, least, most):
    if isinstance(value, bool) or not isinstance(value, int) or not least <= value <= most:
        raise FormatError("must be a whole number from %d to %d" % (least, most), where)
    return value


def _list(value, where, most, least=0):
    if not isinstance(value, list):
        raise FormatError("must be a list", where)
    if not least <= len(value) <= most:
        raise FormatError("must hold %d to %d items" % (least, most) if least else
                          "must hold at most %d items" % most, where)
    return value


def _template(value, where, limit=templating.MAX_TEMPLATE, line=False):
    (_line(value, where, limit) if line else _prose(value, where, limit))
    try:
        return templating.parse_template(value)
    except templating.TemplateError as exc:
        raise FormatError(str(exc), where) from exc


def _condition(value, where):
    _line(value, where, templating.MAX_CONDITION)
    try:
        return templating.parse_condition(value)
    except templating.TemplateError as exc:
        raise FormatError(str(exc), where) from exc


def _shallow(value, where, depth=0):
    """Free-form JSON (a layout) that can be stored and copied back as it is."""
    stack = [(value, depth)]
    while stack:
        item, depth = stack.pop()
        if depth > MAX_DEPTH:
            raise FormatError("is nested too deeply", where)
        if isinstance(item, str):
            _utf8(item, where)
        elif isinstance(item, dict):
            for key in item:
                _utf8(key, where)
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise FormatError("holds a number that is not finite", where)


# ---------------------------------------------------------------- inputs


def _inputs(value):
    where = "inputs"
    _object(value, where)
    if len(value) > MAX_INPUTS:
        raise FormatError("must declare at most %d inputs" % MAX_INPUTS, where)
    for name, spec in value.items():
        here = "inputs.%s" % name
        if not INPUT_NAME.fullmatch(name):
            raise FormatError(INPUT_NAME_HELP, here)
        _object(spec, here, INPUT_FIELDS, ("type",))
        kind = spec["type"]
        if kind not in INPUT_TYPES:
            raise FormatError("type must be one of " + ", ".join(INPUT_TYPES), here + ".type")
        if "title" in spec:
            _line(spec["title"], here + ".title")
        if "description" in spec:
            _prose(spec["description"], here + ".description", 1000)
        if "required" in spec:
            _bool(spec["required"], here + ".required")
        if "picker" in spec and spec["picker"] not in PICKERS:
            raise FormatError("picker must be one of " + ", ".join(PICKERS), here + ".picker")
        options = spec.get("options")
        if kind in ("select", "multiselect"):
            if options is None:
                raise FormatError("a %s input needs options" % kind, here)
            _list(options, here + ".options", MAX_OPTIONS, 1)
            for index, option in enumerate(options):
                _line(option, "%s.options[%d]" % (here, index), 100)
            if len(set(options)) != len(options):
                raise FormatError("options repeat a value", here + ".options")
        elif options is not None:
            raise FormatError("only select and multiselect inputs have options", here + ".options")
        if "default" in spec:
            if kind == "secret":
                raise FormatError("a secret input cannot have a default", here + ".default")
            check_input(name, spec, spec["default"], here + ".default")


def check_input(name, spec, value, where=None):
    """Raise FormatError unless value fits the input spec; returns the value."""
    where = where or "inputs.%s" % name
    kind = spec["type"]
    if kind in ("string", "secret"):
        _line(value, where, 2000, empty=True)
    elif kind == "text":
        _prose(value, where, 20000)
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value in (
                float("inf"), float("-inf")):
            raise FormatError("must be a number", where)
    elif kind == "boolean":
        _bool(value, where)
    elif kind == "select":
        if value not in spec["options"]:
            raise FormatError("must be one of " + ", ".join(spec["options"]), where)
    elif kind == "multiselect":
        _list(value, where, len(spec["options"]))
        unknown = [item for item in value if item not in spec["options"]]
        if unknown or len(set(map(str, value))) != len(value):
            raise FormatError("must list distinct values of " + ", ".join(spec["options"]), where)
    return value


# ---------------------------------------------------------------- steps


def _params(step, here, found):
    kind = step["type"]
    params = step.get("params", {})
    where = here + ".params"

    def text(name, required=True, line=False, limit=templating.MAX_TEMPLATE):
        if name not in params:
            if required:
                raise FormatError("missing required field " + name, where)
            return
        found.append((where + "." + name, _template(params[name], where + "." + name, limit, line)))

    shapes = {"instruction": {"text"}, "skill": {"skill", "instruction"},
              "command": {"command", "cwd", "expect_exit", "tail_lines"},
              "gate": {"question", "options", "allow_comment"}, "checklist": {"items"},
              "memory.recall": {"query", "limit", "kinds"}, "memory.brief": {"question", "limit"},
              "condition": {"expr"}}
    _object(params, where, shapes[kind])
    if kind == "instruction":
        text("text")
    elif kind == "skill":
        if "skill" not in params:
            raise FormatError("missing required field skill", where)
        _skill(params["skill"], where + ".skill")
        text("instruction", required=False)
    elif kind == "command":
        text("command", line=True, limit=4000)
        text("cwd", required=False, line=True, limit=1000)
        if "expect_exit" in params:
            _integer(params["expect_exit"], where + ".expect_exit", 0, 255)
        if "tail_lines" in params:
            _integer(params["tail_lines"], where + ".tail_lines", 0, 500)
    elif kind == "gate":
        text("question", limit=4000)
        if "options" in params:
            _list(params["options"], where + ".options", 10, 2)
            for index, option in enumerate(params["options"]):
                _line(option, "%s.options[%d]" % (where, index), 100)
            if len(set(params["options"])) != len(params["options"]):
                raise FormatError("options repeat a value", where + ".options")
        if "allow_comment" in params:
            _bool(params["allow_comment"], where + ".allow_comment")
    elif kind == "checklist":
        if "items" not in params:
            raise FormatError("missing required field items", where)
        _list(params["items"], where + ".items", 50, 1)
        for index, item in enumerate(params["items"]):
            spot = "%s.items[%d]" % (where, index)
            _object(item, spot, {"text", "required"}, ("text",))
            found.append((spot + ".text", _template(item["text"], spot + ".text", 1000, line=True)))
            if "required" in item:
                _bool(item["required"], spot + ".required")
    elif kind == "memory.recall":
        text("query", line=True, limit=2000)
        if "limit" in params:
            _integer(params["limit"], where + ".limit", 1, 50)
        if "kinds" in params:
            _list(params["kinds"], where + ".kinds", len(ENTRY_KINDS), 1)
            for index, entry_kind in enumerate(params["kinds"]):
                if entry_kind not in ENTRY_KINDS:
                    raise FormatError("must be one of " + ", ".join(ENTRY_KINDS), "%s.kinds[%d]" % (where, index))
    elif kind == "memory.brief":
        text("question", limit=4000)
        if "limit" in params:
            _integer(params["limit"], where + ".limit", 1, 50)
    elif kind == "condition":
        if "expr" not in params:
            raise FormatError("missing required field expr", where)
        found.append((where + ".expr", _condition(params["expr"], where + ".expr")))


def _skill(value, where):
    """One skill for every agent (default) and/or one per agent ID, as rules name them."""
    _object(value, where, {"default", "per_agent"})
    if not value:
        raise FormatError("must name a default skill or one per agent", where)
    if "default" in value and not agents.valid_skill(value["default"]):
        raise FormatError(agents.SKILL_HELP, where + ".default")
    if "per_agent" in value:
        _object(value["per_agent"], where + ".per_agent")
        if not value["per_agent"] and "default" not in value:
            raise FormatError("must name a default skill or one per agent", where)
        for agent, skill in value["per_agent"].items():
            if not agents.valid_id(agent):
                raise FormatError(agents.ID_HELP, "%s.per_agent.%s" % (where, agent))
            if not agents.valid_skill(skill):
                raise FormatError(agents.SKILL_HELP, "%s.per_agent.%s" % (where, agent))


def _executor(step, here):
    value = step["executor"]
    where = here + ".executor"
    _object(value, where, {"agent", "role"})
    if len(value) != 1:
        raise FormatError("must name either agent or role", where)
    natural = EXECUTORS[step["type"]]
    if "agent" in value:
        if natural != "agent":
            raise FormatError("only instruction, skill and command steps can be pinned to an agent", where)
        if not agents.valid_id(value["agent"]):
            raise FormatError(agents.ID_HELP, where + ".agent")
    elif value["role"] != natural:
        raise FormatError("a %s step is performed by %s" % (step["type"], natural), where + ".role")


def _hints(value, where):
    _object(value, where, HINT_FIELDS)
    for name, kind in HINT_FIELDS.items():
        if name in value:
            if kind is bool:
                _bool(value[name], where + "." + name)
            else:
                _integer(value[name], where + "." + name, 1, 1440)


def _outputs(value, where):
    _object(value, where, {"data", "files"})
    if "data" in value:
        _object(value["data"], where + ".data")
        if len(value["data"]) > 50:
            raise FormatError("must declare at most 50 fields", where + ".data")
        for name, spec in value["data"].items():
            spot = "%s.data.%s" % (where, name)
            if not INPUT_NAME.fullmatch(name):
                raise FormatError("Field name: 1-48 lowercase letters, digits or '_', starting with a letter", spot)
            _object(spec, spot, {"type", "required"}, ("type",))
            if spec["type"] not in OUTPUT_TYPES:
                raise FormatError("type must be one of " + ", ".join(OUTPUT_TYPES), spot + ".type")
            if "required" in spec:
                _bool(spec["required"], spot + ".required")
    if "files" in value:
        _object(value["files"], where + ".files", {"required"})
        if "required" in value["files"]:
            _bool(value["files"]["required"], where + ".files.required")


def _checks(value, where, step):
    _list(value, where, MAX_CHECKS)
    for index, check in enumerate(value):
        spot = "%s[%d]" % (where, index)
        _object(check, spot)
        kind = check.get("kind")
        if not isinstance(kind, str) or kind not in CHECK_KINDS:
            raise FormatError("kind must be one of " + ", ".join(sorted(CHECK_KINDS)), spot + ".kind")
        _object(check, spot, CHECK_KINDS[kind] | {"kind"}, tuple(sorted(CHECK_KINDS[kind])))
        if EXECUTORS[step["type"]] != "agent":
            raise FormatError("only instruction, skill and command steps report results to check", spot)
        if "path" in check:
            _data_path(check["path"], spot + ".path")
        if kind == "min_items":
            _integer(check["count"], spot + ".count", 1, 10000)
        if kind == "exit_code":
            _integer(check["expect"], spot + ".expect", 0, 255)
            if step["type"] != "command":
                raise FormatError("only command steps report an exit code", spot)


def _data_path(value, where):
    """A dotted path into a step's reported data, such as findings or summary.counts."""
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}(\.[a-z0-9_]{1,48}){0,7}", value):
        raise FormatError("must be a dotted path of lowercase names, such as findings", where)


def _matrix(value, where, found):
    _object(value, where)
    if not 1 <= len(value) <= MAX_MATRIX_KEYS:
        raise FormatError("must have 1 to %d keys" % MAX_MATRIX_KEYS, where)
    for key, values in value.items():
        spot = "%s.%s" % (where, key)
        if not INPUT_NAME.fullmatch(key):
            raise FormatError("Matrix key: 1-48 lowercase letters, digits or '_', starting with a letter", spot)
        if isinstance(values, str):
            template = _template(values, spot, 1000, line=True)
            if template.single is None:
                raise FormatError("must be a list or one {{...}} expression that gives a list", spot)
            found.append((spot, template))
        else:
            _list(values, spot, MAX_MATRIX_VALUES, 1)
            for index, item in enumerate(values):
                _line(item, "%s[%d]" % (spot, index), 100)
            if len(set(values)) != len(values):
                raise FormatError("repeats a value", spot)


def _step(step, index, ids):
    here = "steps[%d]" % index
    _object(step, here, STEP_FIELDS, ("id", "type"))
    if not isinstance(step["id"], str) or not STEP_ID.fullmatch(step["id"]):
        raise FormatError(STEP_ID_HELP, here + ".id")
    here = "steps.%s" % step["id"]
    if step["id"] in ids:
        raise FormatError("step ID is used twice", here)
    if not isinstance(step["type"], str) or step["type"] not in EXECUTORS:
        raise FormatError("type must be one of " + ", ".join(sorted(EXECUTORS)), here + ".type")
    found = []
    if "title" in step:
        found.append((here + ".title", _template(step["title"], here + ".title", MAX_TITLE, line=True)))
    _params(step, here, found)
    if "if" in step:
        found.append((here + ".if", _condition(step["if"], here + ".if")))
    if "matrix" in step:
        if EXECUTORS[step["type"]] == "richi":
            raise FormatError("only agent and human steps can be repeated by a matrix", here + ".matrix")
        _matrix(step["matrix"], here + ".matrix", found)
    if "executor" in step:
        _executor(step, here)
    if "hints" in step:
        _hints(step["hints"], here + ".hints")
    if "outputs" in step:
        if EXECUTORS[step["type"]] != "agent":
            raise FormatError("only instruction, skill and command steps report data", here + ".outputs")
        _outputs(step["outputs"], here + ".outputs")
    if "checks" in step:
        _checks(step["checks"], here + ".checks", step)
    if "retry" in step:
        _object(step["retry"], here + ".retry", {"max"}, ("max",))
        _integer(step["retry"]["max"], here + ".retry.max", 0, MAX_RETRIES)
    if "continue_on_failure" in step:
        _bool(step["continue_on_failure"], here + ".continue_on_failure")
    if "needs" in step:
        _list(step["needs"], here + ".needs", MAX_STEPS)
        for item in step["needs"]:
            if not isinstance(item, str) or not STEP_ID.fullmatch(item):
                raise FormatError(STEP_ID_HELP, here + ".needs")
        if len(set(step["needs"])) != len(step["needs"]):
            raise FormatError("names a step twice", here + ".needs")
    return found


# ---------------------------------------------------------------- graph


def _graph(steps):
    """Raise FormatError for an unknown dependency or a cycle; return each step's ancestors."""
    known = {step["id"] for step in steps}
    for step in steps:
        for need in step["needs"]:
            if need == step["id"]:
                raise FormatError("a step cannot depend on itself", "steps.%s.needs" % step["id"])
            if need not in known:
                raise FormatError("unknown step " + need, "steps.%s.needs" % step["id"])
    needs = {step["id"]: step["needs"] for step in steps}
    order, state = [], {}
    for start in needs:
        if start in state:
            continue
        stack = [(start, iter(needs[start]))]
        state[start] = "open"
        while stack:
            node, children = stack[-1]
            child = next(children, None)
            if child is None:
                state[node] = "done"
                order.append(node)
                stack.pop()
            elif state.get(child) == "open":
                cycle = [item for item, _ in stack]
                cycle = cycle[cycle.index(child):] + [child]
                raise FormatError("steps depend on each other in a cycle: " + " -> ".join(cycle), "steps")
            elif child not in state:
                state[child] = "open"
                stack.append((child, iter(needs[child])))
    ancestors = {}
    for node in order:
        mine = set()
        for need in needs[node]:
            mine.add(need)
            mine.update(ancestors[need])
        ancestors[node] = mine
    return ancestors


def ancestors(definition):
    """Every step a step depends on, directly or through others, by step ID."""
    return _graph(definition["steps"])


def _references(definition, found, ancestry):
    inputs = definition.get("inputs", {})
    steps = {step["id"]: step for step in definition["steps"]}
    for step_id, items in found:
        step = steps.get(step_id)
        for where, parsed in items:
            for path in parsed.paths():
                parts = path.parts
                root = parts[0]
                if root == "inputs":
                    if len(parts) < 2 or parts[1] not in inputs:
                        raise FormatError("names an input the scenario does not declare: " + path.text, where)
                elif root == "steps":
                    if step is None:
                        raise FormatError("only a step can read another step's results: " + path.text, where)
                    if len(parts) < 2 or parts[1] not in steps:
                        raise FormatError("names an unknown step: " + path.text, where)
                    if parts[1] == step_id:
                        raise FormatError("a step cannot read its own results: " + path.text, where)
                    if parts[1] != step_id and parts[1] not in ancestry[step_id]:
                        raise FormatError("reads step %s, which this step does not depend on; add it to needs"
                                          % parts[1], where)
                    if len(parts) > 2 and parts[2] not in ("summary", "status", "answer", "comment", "data",
                                                           "files", "exit_code", "checked"):
                        raise FormatError("a step result has summary, status, answer, comment, data, files, "
                                          "exit_code and checked: " + path.text, where)
                elif root == "matrix":
                    keys = (step or {}).get("matrix", {})
                    if ".matrix." in where:
                        raise FormatError("a matrix cannot read itself: " + path.text, where)
                    if len(parts) < 2 or parts[1] not in keys:
                        raise FormatError("names a matrix key this step does not have: " + path.text, where)


# ---------------------------------------------------------------- whole definition


def normalize(definition):
    """The definition validated, with explicit needs; FormatError names the first problem."""
    _object(definition, None, TOP_FIELDS, TOP_REQUIRED)
    if definition["format"] != FORMAT:
        raise FormatError("must be %r" % FORMAT, "format")
    version = definition["format_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise FormatError("must be a whole number", "format_version")
    if version != FORMAT_VERSION:
        raise FormatError("this Richi reads scenario format %d, not %d%s" % (
            FORMAT_VERSION, version, "; update Richi" if version > FORMAT_VERSION else ""), "format_version")
    if not isinstance(definition["id"], str) or not SCENARIO_ID.fullmatch(definition["id"]):
        raise FormatError(SCENARIO_ID_HELP, "id")
    _line(definition["title"], "title")
    found = []
    top = []
    if "description" in definition:
        _prose(definition["description"], "description")
    if "when_to_use" in definition:
        _prose(definition["when_to_use"], "when_to_use", 1000)
    if "run_title" in definition:
        top.append(("run_title", _template(definition["run_title"], "run_title", MAX_TITLE, line=True)))
    if "tags" in definition:
        _list(definition["tags"], "tags", MAX_TAGS)
        for index, tag in enumerate(definition["tags"]):
            if not isinstance(tag, str) or not TAG.fullmatch(tag):
                raise FormatError("must be one line of 1-60 characters", "tags[%d]" % index)
            _utf8(tag, "tags[%d]" % index)
    if "inputs" in definition:
        _inputs(definition["inputs"])
    if "layout" in definition:
        _object(definition["layout"], "layout")
        _shallow(definition["layout"], "layout")
        if len(canonical_json(definition["layout"]).encode("utf-8")) > MAX_LAYOUT:
            raise FormatError("is larger than %d bytes" % MAX_LAYOUT, "layout")
    steps = _list(definition["steps"], "steps", MAX_STEPS, 1)
    result = json.loads(json.dumps(definition))
    ids = set()
    previous = None
    for index, step in enumerate(steps):
        found.append((step.get("id") if isinstance(step, dict) else None, _step(step, index, ids)))
        ids.add(step["id"])
        normalized = result["steps"][index]
        if "needs" not in normalized:
            normalized["needs"] = [previous] if previous else []
        previous = step["id"]
    ancestry = _graph(result["steps"])
    _references(result, [(None, top)] + found, ancestry)
    if len(canonical_json(result).encode("utf-8")) > MAX_BYTES:
        raise FormatError("a definition is larger than %d bytes" % MAX_BYTES)
    return result


def load(text):
    """Parse and normalize JSON text of a definition."""
    return normalize(parse(text))


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(definition):
    """SHA-256 of the canonical form: the same definition always gives the same digest."""
    return hashlib.sha256(canonical_json(definition).encode("utf-8")).hexdigest()


def executor(step):
    """Who performs the step: an agent ID, 'agent' (any agent), 'human' or 'richi'."""
    pinned = step.get("executor", {})
    return pinned.get("agent") or EXECUTORS[step["type"]]
