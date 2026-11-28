"""Scenario definitions: the format, its validation and its canonical form.

A scenario is a reusable procedure an agent follows in a chat: input fields,
then steps of a few generic kinds (an instruction, a skill, a command, a
question to the human, a memory lookup, a condition) joined by explicit
dependencies. Nothing here runs a step or touches a store; this module only
decides whether a definition is well formed and returns it normalized:

* every step lists its dependencies in `needs`; a step without the field
  depends on the step before it, which is how a plain list of steps reads,
  and an explicit empty list starts a step with the scenario. `needs`
  expresses completion ordering (`done` or `skipped`): a skipped dependency
  still satisfies ordering so downstream steps run unless their own `if` is
  false. Because the graph is explicit from the first version, a free canvas
  can later show the same definitions without migrating them;
* templates and conditions are parsed (templating.py), and every value they
  name must exist when the step runs: an input the scenario declares, a
  system path (`run.id`, `project.path`, `agent.id`, `now.date`), a result
  field of an ancestor step's kind, or a matrix key of its own (never inside
  `if`, which is evaluated before matrix expansion);
* secret inputs (`type: secret`) are allowed only in agent-facing execution
  params (`instruction.text`, `skill.instruction`, `command.command`) and as
  presence/null checks in conditions, never in persisted fields (`run_title`,
  step `title`, `matrix`, `gate`, `checklist`, `memory.*`, `cwd`);
* every `{{...}}` expression in `command.params.command` must end with
  `| shell` (do not wrap `{{... | shell}}` in outer shell quotes). `cwd` is
  a directory path, not shell syntax, so it does not use `| shell` and may
  not read `steps.*`;
* a matrix step exposes aggregate results: `status`, `items` (per-instance
  records), `data` (with list fields concatenated across instances for kinds
  that report data), and `files` (concatenated across instances for kinds
  that report files). Run instance keys are decoupled from raw matrix values
  and shell-safe (e.g. `<step_id>.0`, `<step_id>.1`).
* `outputs` implicitly validates the shape of reported `data` (no separate
  `data_schema` check kind is needed). `layout` is untrusted free-form JSON
  whose strings any UI must escape on render.
* version compatibility contract: `format_version` is the minimum reader
  level required by a definition. Future readers accept `1 <= version <=
  FORMAT_VERSION`, and writers stamp the lowest version covering the features
  a definition uses. A definition with `version > FORMAT_VERSION` raises
  `UnsupportedVersion` before unknown top-level fields are checked.

The canonical form is sorted, compact JSON; its SHA-256 identifies a version.
"""
import hashlib
import itertools
import json
import math
import re
import unicodedata

from . import agents, templating


FORMAT = "richi-scenario"
FORMAT_VERSION = 1
MAX_BYTES = 256 * 1024
MAX_STEPS = 100
MAX_INPUTS = 50
MAX_MATRIX_KEYS = 3
MAX_MATRIX_VALUES = 50
MAX_MATRIX_INSTANCES = 50
MAX_OPTIONS = 50
MAX_CHECKS = 20
MAX_TAGS = 20
MAX_TITLE = 200
MAX_DESCRIPTION = 4000
MAX_PROJECT_PATH = 500
MAX_LAYOUT = 64 * 1024
MAX_DEPTH = 32
MAX_DIGITS = 100
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
SYSTEM_PATHS = frozenset({("run", "id"), ("project", "path"), ("agent", "id"), ("now", "date")})
# One registry per step kind: natural executor, allowed params, result fields
# exposed to downstream templates/conditions, and step-level capabilities.
STEP_KINDS = {
    "instruction": {
        "executor": "agent",
        "params": frozenset({"text"}),
        "results": frozenset({"summary", "status", "data", "files"}),
        "matrix": True, "outputs": True, "checks": True, "retry": True,
    },
    "skill": {
        "executor": "agent",
        "params": frozenset({"skill", "instruction"}),
        "results": frozenset({"summary", "status", "data", "files"}),
        "matrix": True, "outputs": True, "checks": True, "retry": True,
    },
    "command": {
        "executor": "agent",
        "params": frozenset({"command", "cwd", "expect_exit", "tail_lines"}),
        "results": frozenset({"summary", "status", "data", "files", "exit_code"}),
        "matrix": True, "outputs": True, "checks": True, "retry": True,
    },
    "gate": {
        "executor": "human",
        "params": frozenset({"question", "options", "allow_comment"}),
        "results": frozenset({"summary", "status", "answer", "comment"}),
        "matrix": True, "outputs": False, "checks": False, "retry": False,
    },
    "checklist": {
        "executor": "human",
        "params": frozenset({"items"}),
        "results": frozenset({"summary", "status", "checked"}),
        "matrix": True, "outputs": False, "checks": False, "retry": False,
    },
    "memory.recall": {
        "executor": "richi",
        "params": frozenset({"query", "limit", "kinds"}),
        "results": frozenset({"summary", "status", "data"}),
        "matrix": False, "outputs": False, "checks": False, "retry": False,
    },
    "memory.brief": {
        "executor": "richi",
        "params": frozenset({"question", "limit", "include_past_runs"}),
        "results": frozenset({"summary", "status", "data"}),
        "matrix": False, "outputs": False, "checks": False, "retry": False,
    },
    "memory.record": {
        "executor": "richi",
        "params": frozenset({"kind", "title", "summary", "entry_id", "tags", "knowledge_state", "link_task"}),
        "results": frozenset({"summary", "status", "data"}),
        "matrix": False, "outputs": False, "checks": False, "retry": False,
    },
    "compare": {
        "executor": "richi",
        "params": frozenset({"step", "metrics", "baseline_run"}),
        "results": frozenset({"summary", "status", "data"}),
        "matrix": False, "outputs": False, "checks": False, "retry": False,
    },
    "condition": {
        "executor": "richi",
        "params": frozenset({"expr"}),
        "results": frozenset({"summary", "status", "data"}),
        "matrix": False, "outputs": False, "checks": False, "retry": False,
    },
}
EXECUTORS = {kind: spec["executor"] for kind, spec in STEP_KINDS.items()}
CHECK_KINDS = {"data_required": {"path"}, "min_items": {"path", "count"}, "exit_code": {"expect"},
               "file_exists": {"path"}, "evidence_git": set()}
SECRET_ALLOWED_FIELDS = frozenset({"exec_param", "command"})
# C0/C1 controls (including U+0085 NEL), Unicode line/paragraph separators, and
# bidirectional override/isolate controls (Trojan Source).
_BAD_LINE_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\u200e\u200f\u061c\u202a-\u202e\u2066-\u2069]")


class FormatError(ValueError):
    """A definition that does not follow the format; where is a path such as steps.merge.params.text."""

    def __init__(self, message, where=None):
        super().__init__(message if not where else "%s: %s" % (where, message))
        self.where = where
        self.reason = message


class UnsupportedVersion(FormatError):
    """A definition written for a newer scenario format level than this Richi reads."""

    def __init__(self, message, version, where="format_version"):
        super().__init__(message, where)
        self.version = version


def _short_integer(text):
    if len(text) > MAX_DIGITS:
        raise ValueError("a number longer than %d digits" % MAX_DIGITS)
    return int(text)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise FormatError("duplicate key %r" % key)
        result[key] = value
    return result


def parse(text, *, max_bytes=MAX_BYTES, what="a definition"):
    """The JSON object in text, before validation: one object, no duplicate keys, bounded size."""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FormatError(what + " must be UTF-8 text") from exc
    elif not isinstance(text, str):
        raise FormatError(what + " must be UTF-8 text")
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise FormatError(what + " must be UTF-8 text") from exc
    if len(encoded) > max_bytes:
        raise FormatError("%s is larger than %d bytes" % (what, max_bytes))
    try:
        value = json.loads(text, object_pairs_hook=_unique, parse_int=_short_integer,
                           parse_float=_float, parse_constant=_constant)
    except FormatError:
        raise
    except RecursionError as exc:
        raise FormatError(what + " is nested too deeply") from exc
    except ValueError as exc:
        raise FormatError("not valid JSON: " + str(exc)) from exc
    _shallow(value, what)
    if not isinstance(value, dict):
        raise FormatError(what + " must be a JSON object")
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
    for key in value:
        if not isinstance(key, str):
            raise FormatError("object keys must be text", where)
    if fields is not None:
        unknown = sorted(set(value) - set(fields))
        if unknown:
            raise FormatError("unknown field%s %s" % ("s" if len(unknown) > 1 else "", ", ".join(unknown)), where)
    for name in required:
        if name not in value:
            raise FormatError("missing required field " + name, where)
    return value


def _line(value, where, limit=MAX_TITLE, empty=False, strict_format=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise FormatError("must be %stext" % ("" if empty else "nonempty "), where)
    if len(value) > limit:
        raise FormatError("is longer than %d characters" % limit, where)
    if _BAD_LINE_CHARS.search(value):
        raise FormatError("must be one line without control or bidirectional characters", where)
    if strict_format and any(unicodedata.category(ch) == "Cf" for ch in value):
        raise FormatError("must not hold invisible format characters", where)
    _utf8(value, where)
    return value


def _prose(value, where, limit=MAX_DESCRIPTION, empty=True):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise FormatError("must be %stext" % ("" if empty else "nonempty "), where)
    if len(value) > limit:
        raise FormatError("is longer than %d characters" % limit, where)
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", value):
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


def _template(value, where, limit=templating.MAX_TEMPLATE, line=False, empty=True, strict_format=False):
    if line:
        _line(value, where, limit, empty=empty, strict_format=strict_format)
    else:
        _prose(value, where, limit, empty=empty)
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
    """Free-form JSON (such as layout) that can be stored and copied back as it is."""
    stack = [(value, depth)]
    while stack:
        item, depth = stack.pop()
        if depth > MAX_DEPTH:
            raise FormatError("is nested too deeply", where)
        if item is None or isinstance(item, bool):
            continue
        if isinstance(item, int):
            if len(str(abs(item))) > MAX_DIGITS:
                raise FormatError("holds a number longer than %d digits" % MAX_DIGITS, where)
            continue
        if isinstance(item, float):
            if not math.isfinite(item):
                raise FormatError("holds a number that is not finite", where)
            continue
        if isinstance(item, str):
            _utf8(item, where)
            continue
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise FormatError("object keys must be text", where)
                _utf8(key, where)
                stack.append((child, depth + 1))
            continue
        if isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
            continue
        raise FormatError("holds a value that is not JSON", where)


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
        if not isinstance(kind, str) or kind not in INPUT_TYPES:
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
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise FormatError("must be a number", where)
        if isinstance(value, int):
            if len(str(abs(value))) > MAX_DIGITS:
                raise FormatError("number is longer than %d digits" % MAX_DIGITS, where)
        elif not math.isfinite(value):
            raise FormatError("must be a finite number", where)
    elif kind == "boolean":
        _bool(value, where)
    elif kind == "select":
        if value not in spec["options"]:
            raise FormatError("must be one of " + ", ".join(spec["options"]), where)
    elif kind == "multiselect":
        _list(value, where, len(spec["options"]))
        if any(not isinstance(item, str) or item not in spec["options"] for item in value) or len(set(value)) != len(value):
            raise FormatError("must list distinct values of " + ", ".join(spec["options"]), where)
    return value


# ---------------------------------------------------------------- steps


def _params(step, here, found):
    kind = step["type"]
    params = step.get("params", {})
    where = here + ".params"

    def text(name, field_kind, required=True, line=False, limit=templating.MAX_TEMPLATE, empty=True, strict_format=False):
        if name not in params:
            if required:
                raise FormatError("missing required field " + name, where)
            return
        parsed = _template(params[name], where + "." + name, limit, line=line, empty=empty, strict_format=strict_format)
        found.append((where + "." + name, field_kind, parsed))

    _object(params, where, STEP_KINDS[kind]["params"])
    if kind == "instruction":
        text("text", "exec_param", empty=False)
    elif kind == "skill":
        if "skill" not in params:
            raise FormatError("missing required field skill", where)
        _skill(params["skill"], where + ".skill")
        text("instruction", "exec_param", required=False)
    elif kind == "command":
        text("command", "command", line=True, limit=4000, empty=False, strict_format=True)
        if "cwd" in params:
            _project_path(params["cwd"], where + ".cwd", found, field_kind="cwd")
        if "expect_exit" in params:
            _integer(params["expect_exit"], where + ".expect_exit", 0, 255)
        if "tail_lines" in params:
            _integer(params["tail_lines"], where + ".tail_lines", 0, 500)
    elif kind == "gate":
        text("question", "stored_param", limit=4000, empty=False)
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
            found.append((spot + ".text", "stored_param", _template(item["text"], spot + ".text", 1000, line=True, empty=False)))
            if "required" in item:
                _bool(item["required"], spot + ".required")
    elif kind == "memory.recall":
        text("query", "stored_param", line=True, limit=2000, empty=False)
        if "limit" in params:
            _integer(params["limit"], where + ".limit", 1, 50)
        if "kinds" in params:
            _list(params["kinds"], where + ".kinds", len(ENTRY_KINDS), 1)
            for index, entry_kind in enumerate(params["kinds"]):
                if entry_kind not in ENTRY_KINDS:
                    raise FormatError("must be one of " + ", ".join(ENTRY_KINDS), "%s.kinds[%d]" % (where, index))
    elif kind == "memory.brief":
        text("question", "stored_param", limit=2000, empty=False)
        if "limit" in params:
            _integer(params["limit"], where + ".limit", 1, 50)
        if "include_past_runs" in params:
            _bool(params["include_past_runs"], where + ".include_past_runs")
    elif kind == "memory.record":
        entry_kind = params.get("kind")
        if entry_kind not in ENTRY_KINDS:
            raise FormatError("kind must be one of " + ", ".join(ENTRY_KINDS), where + ".kind")
        text("title", "stored_param", line=True, limit=MAX_TITLE, empty=False)
        text("summary", "stored_param", line=False, limit=4000, empty=False)
        text("entry_id", "stored_param", required=False, line=True, limit=100, empty=False)
        if "tags" in params:
            _list(params["tags"], where + ".tags", 10, 0)
            for index, tag in enumerate(params["tags"]):
                _line(tag, "%s.tags[%d]" % (where, index), 48)
        if "knowledge_state" in params and params["knowledge_state"] not in ("confirmed", "hypothesis"):
            raise FormatError("knowledge_state must be confirmed or hypothesis", where + ".knowledge_state")
        if "link_task" in params:
            _bool(params["link_task"], where + ".link_task")
    elif kind == "compare":
        target_step = params.get("step")
        if not isinstance(target_step, str) or not STEP_ID.fullmatch(target_step):
            raise FormatError("step must name a valid ancestor step ID", where + ".step")
        if "metrics" in params:
            _list(params["metrics"], where + ".metrics", 20, 1)
            for index, metric_path in enumerate(params["metrics"]):
                _data_path(metric_path, "%s.metrics[%d]" % (where, index))
        text("baseline_run", "stored_param", required=False, line=True, limit=100, empty=False)
    elif kind == "condition":
        if "expr" not in params:
            raise FormatError("missing required field expr", where)
        found.append((where + ".expr", "condition", _condition(params["expr"], where + ".expr")))


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
    _object(value, where, {"agent"}, ("agent",))
    if STEP_KINDS[step["type"]]["executor"] != "agent":
        raise FormatError("only instruction, skill and command steps can be pinned to an agent", where)
    if not agents.valid_id(value["agent"]):
        raise FormatError(agents.ID_HELP, where + ".agent")


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


def _checks(value, where, step, found):
    _list(value, where, MAX_CHECKS)
    declared_data = (step.get("outputs") or {}).get("data") or {}
    expect_exit = (step.get("params") or {}).get("expect_exit") if step["type"] == "command" else None
    for index, check in enumerate(value):
        spot = "%s[%d]" % (where, index)
        _object(check, spot)
        kind = check.get("kind")
        if not isinstance(kind, str) or kind not in CHECK_KINDS:
            raise FormatError("kind must be one of " + ", ".join(sorted(CHECK_KINDS)), spot + ".kind")
        _object(check, spot, CHECK_KINDS[kind] | {"kind"}, tuple(sorted(CHECK_KINDS[kind])))
        if not STEP_KINDS[step["type"]]["checks"]:
            raise FormatError("only instruction, skill and command steps report results to check", spot)
        if kind in ("data_required", "min_items"):
            _data_path(check["path"], spot + ".path")
        elif kind == "file_exists":
            _project_path(check["path"], spot + ".path", found)
        if kind == "min_items":
            _integer(check["count"], spot + ".count", 1, 10000)
            if check["path"] in declared_data and declared_data[check["path"]]["type"] != "list":
                raise FormatError("min_items targets %s, which outputs declares as %s" % (
                    check["path"], declared_data[check["path"]]["type"]), spot + ".path")
        if kind == "exit_code":
            _integer(check["expect"], spot + ".expect", 0, 255)
            if step["type"] != "command":
                raise FormatError("only command steps report an exit code", spot)
            if expect_exit is not None and check["expect"] != expect_exit:
                raise FormatError("exit_code check conflicts with params.expect_exit", spot)


def _data_path(value, where):
    """A dotted path into a step's reported data, such as findings or summary.counts."""
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}(\.[a-z0-9_]{1,48}){0,7}", value):
        raise FormatError("must be a dotted path of lowercase names, such as findings", where)


def _project_path(value, where, found, *, field_kind="stored_param"):
    """A relative POSIX file path inside the project (optionally templated); PR 3 enforces symlink containment."""
    parsed = _template(value, where, MAX_PROJECT_PATH, line=True, empty=False, strict_format=True)
    if value.startswith("/") or "\\" in value or re.match(r"[A-Za-z]:", value):
        raise FormatError("must be a relative POSIX path inside the project", where)
    segments = value.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise FormatError("must not contain empty, '.' or '..' path segments", where)
    found.append((where, field_kind, parsed))


def _matrix(value, where, found):
    _object(value, where)
    if not 1 <= len(value) <= MAX_MATRIX_KEYS:
        raise FormatError("must have 1 to %d keys" % MAX_MATRIX_KEYS, where)
    static_product = 1
    for key, values in value.items():
        spot = "%s.%s" % (where, key)
        if not INPUT_NAME.fullmatch(key):
            raise FormatError("Matrix key: 1-48 lowercase letters, digits or '_', starting with a letter", spot)
        if isinstance(values, str):
            template = _template(values, spot, 1000, line=True, empty=False)
            if template.single is None:
                raise FormatError("must be a list or one {{...}} expression that gives a list", spot)
            if template.single.filters:
                raise FormatError("a matrix expression cannot use filters", spot)
            found.append((spot, "matrix", template))
        else:
            _list(values, spot, MAX_MATRIX_VALUES, 1)
            for index, item in enumerate(values):
                _line(item, "%s[%d]" % (spot, index), 100)
            if len(set(values)) != len(values):
                raise FormatError("repeats a value", spot)
            static_product *= len(values)
    if static_product > MAX_MATRIX_INSTANCES:
        raise FormatError("matrix expands to more than %d instances" % MAX_MATRIX_INSTANCES, where)


def expand_matrix(step, context):
    """Expand a step's matrix against context into [{key: value}, ...], bounded by MAX_MATRIX_INSTANCES.

    Raises templating.Unresolved for a missing dynamic source, or ValueError for invalid/oversize values.
    """
    matrix = step.get("matrix")
    if not matrix:
        return []
    keys, lists = [], []
    total = 1
    for key, source in matrix.items():
        if isinstance(source, str):
            raw = templating.value(templating.parse_template(source), context)
            if not isinstance(raw, list) or not raw:
                raise ValueError("matrix.%s must resolve to a nonempty list" % key)
            seen = set()
            for item in raw:
                try:
                    _line(item, "matrix." + key, 100)
                except FormatError as exc:
                    raise ValueError(str(exc)) from exc
                if item in seen:
                    raise ValueError("matrix.%s repeats %r" % (key, item))
                seen.add(item)
            values = list(raw)
        else:
            values = list(source)
        total *= len(values)
        if total > MAX_MATRIX_INSTANCES:
            raise ValueError("matrix expands to more than %d instances" % MAX_MATRIX_INSTANCES)
        keys.append(key)
        lists.append(values)
    return [dict(zip(keys, combo)) for combo in itertools.product(*lists)]


def _step(step, index, ids):
    here = "steps[%d]" % index
    _object(step, here, STEP_FIELDS, ("id", "type"))
    if not isinstance(step["id"], str) or not STEP_ID.fullmatch(step["id"]):
        raise FormatError(STEP_ID_HELP, here + ".id")
    here = "steps.%s" % step["id"]
    if step["id"] in ids:
        raise FormatError("step ID is used twice", here)
    kind = step["type"]
    if not isinstance(kind, str) or kind not in STEP_KINDS:
        raise FormatError("type must be one of " + ", ".join(sorted(STEP_KINDS)), here + ".type")
    spec = STEP_KINDS[kind]
    found = []
    if "title" in step:
        found.append((here + ".title", "title", _template(step["title"], here + ".title", MAX_TITLE, line=True, empty=False)))
    _params(step, here, found)
    if "if" in step:
        found.append((here + ".if", "if", _condition(step["if"], here + ".if")))
    if "matrix" in step:
        if not spec["matrix"]:
            raise FormatError("only agent and human steps can be repeated by a matrix", here + ".matrix")
        _matrix(step["matrix"], here + ".matrix", found)
    if "executor" in step:
        _executor(step, here)
    if "hints" in step:
        _hints(step["hints"], here + ".hints")
    if "outputs" in step:
        if not spec["outputs"]:
            raise FormatError("only instruction, skill and command steps report data", here + ".outputs")
        _outputs(step["outputs"], here + ".outputs")
    if "checks" in step:
        _checks(step["checks"], here + ".checks", step, found)
    if "retry" in step:
        if not spec["retry"]:
            raise FormatError("only instruction, skill and command steps can be retried", here + ".retry")
        _object(step["retry"], here + ".retry", {"max"}, ("max",))
        _integer(step["retry"]["max"], here + ".retry.max", 0, MAX_RETRIES)
    if "continue_on_failure" in step:
        if not spec["retry"]:
            raise FormatError("only instruction, skill and command steps can continue on failure", here + ".continue_on_failure")
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
    needs = {}
    previous = None
    for step in steps:
        step_needs = step["needs"] if "needs" in step else ([previous] if previous else [])
        for need in step_needs:
            if need == step["id"]:
                raise FormatError("a step cannot depend on itself", "steps.%s.needs" % step["id"])
            if need not in known:
                raise FormatError("unknown step " + need, "steps.%s.needs" % step["id"])
        needs[step["id"]] = step_needs
        previous = step["id"]
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


def _step_result_fields(step):
    """Top-level fields exposed under steps.<id>.* for this step."""
    kind_results = STEP_KINDS[step["type"]]["results"]
    if "matrix" not in step:
        return kind_results
    fields = {"status", "items"}
    if "data" in kind_results:
        fields.add("data")
    if "files" in kind_results:
        fields.add("files")
    return frozenset(fields)


def _check_secret_condition(condition, secret_names, where):
    """Allow a secret input in a condition only as a presence or ==/!= null check."""
    def is_secret(node):
        return node[0] == "path" and len(node[1].parts) == 2 and node[1].parts[0] == "inputs" and node[1].parts[1] in secret_names

    def walk(node):
        kind = node[0]
        if kind in ("literal", "path"):
            return
        if kind == "not":
            walk(node[1])
            return
        if kind in ("and", "or"):
            for child in node[1:]:
                walk(child)
            return
        if kind == "cmp":
            op, left, right = node[1], node[2], node[3]
            if is_secret(left) or is_secret(right):
                other = right if is_secret(left) else left
                if op in ("==", "!=") and other == ("literal", None):
                    return
                raise FormatError("a secret input in a condition may only be checked for presence or null", where)
            walk(left)
            walk(right)
            return
        if kind == "list":
            for child in node[1]:
                walk(child)

    walk(condition.tree)


def _references(definition, found, ancestry):
    inputs = definition.get("inputs", {})
    secret_inputs = {name for name, spec in inputs.items() if spec["type"] == "secret"}
    steps = {step["id"]: step for step in definition["steps"]}
    for step in definition["steps"]:
        if step["type"] == "compare":
            target_id = (step.get("params") or {}).get("step")
            where = f"steps.{step['id']}.params.step"
            if target_id not in steps:
                raise FormatError("compare names an unknown step: " + str(target_id), where)
            if target_id == step["id"]:
                raise FormatError("compare cannot compare a step with itself", where)
            if target_id not in ancestry[step["id"]]:
                raise FormatError("compare reads step %s, which this step does not depend on; add it to needs" % target_id, where)
    for step_id, items in found:
        step = steps.get(step_id)
        for where, field_kind, parsed in items:
            if field_kind == "command":
                for expr in parsed.expressions():
                    if not expr.filters or expr.filters[-1][0] != "shell":
                        raise FormatError("expressions in command must end with | shell: " + expr.path.text, where)
            if field_kind in ("if", "condition") and secret_inputs:
                _check_secret_condition(parsed, secret_inputs, where)
            for path in parsed.paths():
                parts = path.parts
                root = parts[0]
                if root in ("run", "project", "agent", "now"):
                    if parts not in SYSTEM_PATHS:
                        allowed = ", ".join(".".join(item) for item in sorted(SYSTEM_PATHS))
                        raise FormatError("unknown system value %s; use one of %s" % (path.text, allowed), where)
                elif root == "inputs":
                    if len(parts) < 2 or parts[1] not in inputs:
                        raise FormatError("names an input the scenario does not declare: " + path.text, where)
                    input_type = inputs[parts[1]]["type"]
                    if input_type == "multiselect":
                        if len(parts) > 3 or (len(parts) == 3 and not isinstance(parts[2], int)):
                            raise FormatError("multiselect input %s allows only an integer index: %s" % (parts[1], path.text), where)
                    elif len(parts) > 2:
                        raise FormatError("scalar input %s has no sub-fields: %s" % (parts[1], path.text), where)
                    if parts[1] in secret_inputs and field_kind not in SECRET_ALLOWED_FIELDS and field_kind not in ("if", "condition"):
                        raise FormatError("secret input %s cannot be used in %s" % (parts[1], where), where)
                    if field_kind == "matrix":
                        if input_type != "multiselect" or len(parts) != 2:
                            raise FormatError("a matrix input source must be a multiselect input: " + path.text, where)
                elif root == "steps":
                    if step is None:
                        raise FormatError("only a step can read another step's results: " + path.text, where)
                    if field_kind == "cwd":
                        raise FormatError("cwd cannot read step results: " + path.text, where)
                    if len(parts) < 2 or parts[1] not in steps:
                        raise FormatError("names an unknown step: " + path.text, where)
                    if parts[1] == step_id:
                        raise FormatError("a step cannot read its own results: " + path.text, where)
                    if parts[1] not in ancestry[step_id]:
                        raise FormatError("reads step %s, which this step does not depend on; add it to needs"
                                          % parts[1], where)
                    target = steps[parts[1]]
                    allowed_fields = _step_result_fields(target)
                    if len(parts) > 2 and parts[2] not in allowed_fields:
                        raise FormatError("step %s exposes %s, not %s: %s" % (
                            parts[1], ", ".join(sorted(allowed_fields)), parts[2], path.text), where)
                    if field_kind == "matrix" and (len(parts) < 4 or parts[2] != "data"):
                        raise FormatError("a matrix step source must read steps.<id>.data.<field>: " + path.text, where)
                elif root == "matrix":
                    keys = (step or {}).get("matrix", {})
                    if field_kind == "matrix":
                        raise FormatError("a matrix cannot read itself: " + path.text, where)
                    if field_kind == "if":
                        raise FormatError("if is evaluated before matrix expansion and cannot read matrix.*: " + path.text, where)
                    if len(parts) < 2 or parts[1] not in keys:
                        raise FormatError("names a matrix key this step does not have: " + path.text, where)
                    if len(parts) > 2:
                        raise FormatError("matrix value %s has no sub-fields: %s" % (parts[1], path.text), where)
                if field_kind == "matrix" and root not in ("inputs", "steps"):
                    raise FormatError("a matrix source must be a multiselect input or steps.<id>.data.<field>: " + path.text, where)


# ---------------------------------------------------------------- whole definition


def normalize(definition):
    """The definition validated, with explicit needs; FormatError names the first problem."""
    _object(definition, None)
    if "format" not in definition:
        raise FormatError("missing required field format")
    if definition["format"] != FORMAT:
        raise FormatError("must be %r" % FORMAT, "format")
    if "format_version" not in definition:
        raise FormatError("missing required field format_version")
    version = definition["format_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise FormatError("must be a whole number", "format_version")
    if version > FORMAT_VERSION:
        raise UnsupportedVersion(
            "this Richi reads scenario format %d, not %d; update Richi" % (FORMAT_VERSION, version),
            version, "format_version")
    if version != FORMAT_VERSION:
        raise FormatError("this Richi reads scenario format %d, not %d" % (FORMAT_VERSION, version), "format_version")
    _object(definition, None, TOP_FIELDS, TOP_REQUIRED)
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
        top.append(("run_title", "run_title", _template(definition["run_title"], "run_title", MAX_TITLE, line=True, empty=False)))
    if "tags" in definition:
        _list(definition["tags"], "tags", MAX_TAGS)
        for index, tag in enumerate(definition["tags"]):
            _line(tag, "tags[%d]" % index, 60)
    if "inputs" in definition:
        _inputs(definition["inputs"])
    if "layout" in definition:
        _object(definition["layout"], "layout")
        _shallow(definition["layout"], "layout")
        if len(canonical_json(definition["layout"]).encode("utf-8")) > MAX_LAYOUT:
            raise FormatError("is larger than %d bytes" % MAX_LAYOUT, "layout")
    steps = _list(definition["steps"], "steps", MAX_STEPS, 1)
    ids = set()
    for index, step in enumerate(steps):
        found.append((step.get("id") if isinstance(step, dict) else None, _step(step, index, ids)))
        ids.add(step["id"])
    result = json.loads(json.dumps(definition))
    previous = None
    for step in result["steps"]:
        if "needs" not in step:
            step["needs"] = [previous] if previous else []
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
    """(role, agent_id) who performs the step: ('agent', 'codex'), ('agent', None), ('human', None) or ('richi', None)."""
    pinned = step.get("executor") or {}
    if "agent" in pinned:
        return ("agent", pinned["agent"])
    return (STEP_KINDS[step["type"]]["executor"], None)
