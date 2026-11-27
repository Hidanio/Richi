"""Read-only templates and conditions for scenario definitions.

A scenario's text fields may name values that only exist once a run is under
way: `{{inputs.branch}}`, `{{steps.review.summary}}`, `{{matrix.angle}}`. A
condition (`if`, the `condition` step) compares such values. Both are parsed
when a definition is saved, so a typo is reported to its author rather than to
the agent that runs the step, and both are evaluated by this module alone: no
Python is ever evaluated, a template cannot call anything, and the only
operations are the few filters and comparisons below.

Paths are dotted names with optional `[index]` parts. Their first name is one
of the roots a caller allows (ROOTS by default). A template refuses a value it
cannot resolve unless its `default` filter supplies one; a condition reads a
missing value as null, since a step that was skipped has no answer to compare.
"""
import json
import math
import re
from typing import NamedTuple, Optional, Tuple


ROOTS = frozenset({"inputs", "steps", "matrix", "run", "project", "agent", "now"})
FILTERS = {"join": (0, 1), "default": (1, 1), "first": (0, 0), "length": (0, 0)}
MAX_TEMPLATE = 20000
MAX_EXPRESSIONS = 200
MAX_CONDITION = 1000
MAX_DEPTH = 32
MAX_RENDERED = 200000
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]{0,15})(?:\.[0-9]{1,16})?")
_OPEN, _CLOSE = "{{", "}}"


class TemplateError(ValueError):
    """A template or condition that cannot be parsed; position counts characters from 0."""

    def __init__(self, message, position=None):
        super().__init__(message if position is None else "%s at character %d" % (message, position + 1))
        self.position = position


class Unresolved(ValueError):
    """A value a template names but the run does not have (yet)."""

    def __init__(self, path):
        super().__init__("no value for " + path)
        self.path = path


class Path(NamedTuple):
    parts: Tuple[object, ...]
    text: str

    @property
    def root(self):
        return self.parts[0]


class Expression(NamedTuple):
    path: Path
    filters: Tuple[Tuple[str, tuple], ...]


class Template(NamedTuple):
    parts: Tuple[object, ...]
    source: str

    def paths(self):
        return [part.path for part in self.parts if isinstance(part, Expression)]

    @property
    def single(self):
        """The one expression of a template that is nothing else, such as "{{inputs.angles}}"."""
        if len(self.parts) == 1 and isinstance(self.parts[0], Expression):
            return self.parts[0]
        return None


class Condition(NamedTuple):
    tree: tuple
    source: str

    def paths(self):
        found, stack = [], [self.tree]
        while stack:
            node = stack.pop()
            if node[0] == "path":
                found.append(node[1])
            elif node[0] == "list":
                stack.extend(node[1])
            elif node[0] in ("not",):
                stack.append(node[1])
            elif node[0] in ("and", "or", "cmp"):
                stack.extend(node[2:] if node[0] == "cmp" else node[1:])
        return found


# ---------------------------------------------------------------- lexing


class _Scanner:
    """Tokens of one expression or condition: names, numbers, strings and punctuation."""

    SYMBOLS = ("==", "!=", "<=", ">=", "<", ">", "(", ")", "[", "]", ",", ".", "|")

    def __init__(self, text, offset=0):
        self.text, self.offset, self.index = text, offset, 0
        self.tokens = []
        while True:
            token = self._next()
            self.tokens.append(token)
            if token[0] == "end":
                break
        self.at = 0

    def _fail(self, message, index=None):
        raise TemplateError(message, self.offset + (self.index if index is None else index))

    def _next(self):
        text = self.text
        while self.index < len(text) and text[self.index] in " \t\r\n":
            self.index += 1
        start = self.index
        if start >= len(text):
            return ("end", None, start)
        char = text[start]
        if char in "'\"":
            return self._string(char)
        number = _NUMBER.match(text, start)
        if number and (char == "-" or char.isdigit()):
            self.index = number.end()
            value = number.group()
            return ("number", float(value) if "." in value else int(value), start)
        name = _NAME.match(text, start)
        if name:
            self.index = name.end()
            return ("name", name.group(), start)
        for symbol in self.SYMBOLS:
            if text.startswith(symbol, start):
                self.index += len(symbol)
                return ("symbol", symbol, start)
        self._fail("unexpected character %r" % char)

    def _string(self, quote):
        text, start = self.text, self.index
        self.index += 1
        value = []
        while self.index < len(text):
            char = text[self.index]
            if char == quote:
                self.index += 1
                return ("string", "".join(value), start)
            if char == "\\":
                following = text[self.index + 1:self.index + 2]
                if following not in ("\\", "'", '"', "n", "t"):
                    self._fail("unknown escape \\" + following)
                value.append({"n": "\n", "t": "\t"}.get(following, following))
                self.index += 2
                continue
            value.append(char)
            self.index += 1
        self._fail("unterminated string", start)

    def peek(self, kind=None, value=None):
        token = self.tokens[self.at]
        if kind is not None and token[0] != kind:
            return None
        if value is not None and token[1] != value:
            return None
        return token

    def take(self, kind=None, value=None, what=None):
        token = self.peek(kind, value)
        if token is None:
            found = self.tokens[self.at]
            shown = "end of text" if found[0] == "end" else repr(found[1])
            raise TemplateError("expected %s, found %s" % (what or value or kind, shown), self.offset + found[2])
        self.at += 1
        return token

    def position(self):
        return self.offset + self.tokens[self.at][2]


def _path(scanner, roots):
    token = scanner.take("name", what="a name")
    if roots is not None and token[1] not in roots:
        raise TemplateError("unknown name %r; use one of %s" % (token[1], ", ".join(sorted(roots))),
                            scanner.offset + token[2])
    parts = [token[1]]
    while True:
        if scanner.peek("symbol", "."):
            scanner.take()
            name = scanner.peek("name")
            number = scanner.peek("number")
            if name:
                parts.append(scanner.take()[1])
            elif number and isinstance(number[1], int) and number[1] >= 0:
                parts.append(scanner.take()[1])
            else:
                scanner.take("name", what="a name after '.'")
        elif scanner.peek("symbol", "["):
            scanner.take()
            token = scanner.peek("number") or scanner.peek("string")
            if token is None or (token[0] == "number" and (not isinstance(token[1], int) or token[1] < 0)):
                raise TemplateError("expected an index or a quoted key", scanner.position())
            parts.append(scanner.take()[1])
            scanner.take("symbol", "]")
        else:
            break
        if len(parts) > MAX_DEPTH:
            raise TemplateError("path is too long", scanner.position())
    return Path(tuple(parts), _path_text(parts))


def _path_text(parts):
    text = str(parts[0])
    for part in parts[1:]:
        text += "[%d]" % part if isinstance(part, int) else ("." + part if _NAME.fullmatch(part) else "[%s]" % json.dumps(part))
    return text


def _literal(scanner):
    token = scanner.peek()
    if token[0] in ("string", "number"):
        return scanner.take()[1]
    if token[0] == "name" and token[1] in ("true", "false", "null"):
        scanner.take()
        return {"true": True, "false": False, "null": None}[token[1]]
    raise TemplateError("expected a quoted string, a number, true, false or null", scanner.position())


# ---------------------------------------------------------------- templates


def parse_template(text, roots=ROOTS):
    """Template of literal text and {{ path | filter(arg) }} expressions."""
    if not isinstance(text, str):
        raise TemplateError("a template must be text")
    if len(text) > MAX_TEMPLATE:
        raise TemplateError("template is longer than %d characters" % MAX_TEMPLATE)
    parts, index, expressions = [], 0, 0
    while True:
        start = text.find(_OPEN, index)
        stray = text.find(_CLOSE, index)
        if stray != -1 and (start == -1 or stray < start):
            raise TemplateError("'}}' without '{{'", stray)
        if start == -1:
            if index < len(text):
                parts.append(text[index:])
            break
        if start > index:
            parts.append(text[index:start])
        end = text.find(_CLOSE, start + 2)
        if end == -1:
            raise TemplateError("'{{' without '}}'", start)
        expressions += 1
        if expressions > MAX_EXPRESSIONS:
            raise TemplateError("more than %d expressions" % MAX_EXPRESSIONS, start)
        parts.append(_expression(text[start + 2:end], start + 2, roots))
        index = end + 2
    return Template(tuple(parts), text)


def _expression(text, offset, roots):
    scanner = _Scanner(text, offset)
    path = _path(scanner, roots)
    filters = []
    while scanner.peek("symbol", "|"):
        scanner.take()
        name = scanner.take("name", what="a filter name")
        if name[1] not in FILTERS:
            raise TemplateError("unknown filter %r; use one of %s" % (name[1], ", ".join(sorted(FILTERS))),
                                offset + name[2])
        args = []
        if scanner.peek("symbol", "("):
            scanner.take()
            if not scanner.peek("symbol", ")"):
                args.append(_literal(scanner))
                while scanner.peek("symbol", ","):
                    scanner.take()
                    args.append(_literal(scanner))
            scanner.take("symbol", ")")
        least, most = FILTERS[name[1]]
        if not least <= len(args) <= most:
            raise TemplateError("filter %s takes %s" % (name[1], _arity(least, most)), offset + name[2])
        filters.append((name[1], tuple(args)))
    scanner.take("end", what="'|' or '}}'")
    return Expression(path, tuple(filters))


def _arity(least, most):
    if least == most:
        return "%d argument%s" % (least, "" if least == 1 else "s") if least else "no arguments"
    return "%d to %d arguments" % (least, most)


def lookup(context, path):
    """The value at path in a context of nested dicts and lists; Unresolved when absent."""
    value = context
    for part in path.parts:
        if isinstance(value, dict) and isinstance(part, str) and part in value:
            value = value[part]
        elif isinstance(value, list) and isinstance(part, int) and part < len(value):
            value = value[part]
        else:
            raise Unresolved(path.text)
    return value


def _apply(expression, context):
    filters = list(expression.filters)
    try:
        value = lookup(context, expression.path)
    except Unresolved:
        fallback = next((args[0] for name, args in filters if name == "default"), _MISSING)
        if fallback is _MISSING:
            raise
        value = fallback
    for name, args in filters:
        if name == "default":
            value = args[0] if value is None else value
        elif name == "join":
            if not isinstance(value, list):
                raise ValueError("join needs a list at " + expression.path.text)
            value = (args[0] if args else ", ").join(_text(item) for item in value)
        elif name == "first":
            value = value[0] if isinstance(value, list) and value else None
        elif name == "length":
            if not isinstance(value, (list, dict, str)):
                raise ValueError("length needs a list, an object or text at " + expression.path.text)
            value = len(value)
    return value


_MISSING = object()


def _text(value):
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def render(template, context):
    """Text of the template; Unresolved names the first value the context lacks."""
    pieces, size = [], 0
    for part in template.parts:
        piece = part if isinstance(part, str) else _text(_apply(part, context))
        size += len(piece)
        if size > MAX_RENDERED:
            raise ValueError("rendered text is longer than %d characters" % MAX_RENDERED)
        pieces.append(piece)
    return "".join(pieces)


def value(template, context):
    """The value itself for a template that is one expression, its text otherwise.

    A matrix lists its values as "{{inputs.angles}}": the list, not its JSON text.
    """
    single = template.single
    return _apply(single, context) if single is not None else render(template, context)


# ---------------------------------------------------------------- conditions


_COMPARISONS = ("==", "!=", "<", "<=", ">", ">=")


def parse_condition(text, roots=ROOTS):
    """Condition with and, or, not, comparisons, in and not in; nothing is evaluated."""
    if not isinstance(text, str) or not text.strip():
        raise TemplateError("a condition must be nonempty text")
    if len(text) > MAX_CONDITION:
        raise TemplateError("condition is longer than %d characters" % MAX_CONDITION)
    scanner = _Scanner(text)
    tree = _or(scanner, roots, 0)
    scanner.take("end", what="end of condition")
    return Condition(tree, text)


def _nested(scanner, depth):
    if depth > MAX_DEPTH:
        raise TemplateError("condition is nested too deeply", scanner.position())
    return depth + 1


def _or(scanner, roots, depth):
    depth = _nested(scanner, depth)
    items = [_and(scanner, roots, depth)]
    while scanner.peek("name", "or"):
        scanner.take()
        items.append(_and(scanner, roots, depth))
    return items[0] if len(items) == 1 else ("or",) + tuple(items)


def _and(scanner, roots, depth):
    items = [_not(scanner, roots, depth)]
    while scanner.peek("name", "and"):
        scanner.take()
        items.append(_not(scanner, roots, depth))
    return items[0] if len(items) == 1 else ("and",) + tuple(items)


def _not(scanner, roots, depth):
    if scanner.peek("name", "not"):
        scanner.take()
        return ("not", _not(scanner, roots, _nested(scanner, depth)))
    return _comparison(scanner, roots, depth)


def _comparison(scanner, roots, depth):
    left = _operand(scanner, roots, depth)
    token = scanner.peek()
    if token[0] == "symbol" and token[1] in _COMPARISONS:
        scanner.take()
        return ("cmp", token[1], left, _operand(scanner, roots, depth))
    if scanner.peek("name", "in"):
        scanner.take()
        return ("cmp", "in", left, _operand(scanner, roots, depth))
    if scanner.peek("name", "not") and scanner.tokens[scanner.at + 1][:2] == ("name", "in"):
        scanner.take()
        scanner.take()
        return ("cmp", "not in", left, _operand(scanner, roots, depth))
    return left


def _operand(scanner, roots, depth):
    token = scanner.peek()
    if token[0] == "symbol" and token[1] == "(":
        scanner.take()
        inner = _or(scanner, roots, depth)
        scanner.take("symbol", ")")
        return inner
    if token[0] == "symbol" and token[1] == "[":
        scanner.take()
        items = []
        if not scanner.peek("symbol", "]"):
            items.append(("literal", _literal(scanner)))
            while scanner.peek("symbol", ","):
                scanner.take()
                items.append(("literal", _literal(scanner)))
        scanner.take("symbol", "]")
        return ("list", tuple(items))
    if token[0] in ("string", "number") or (token[0] == "name" and token[1] in ("true", "false", "null")):
        return ("literal", _literal(scanner))
    if token[0] == "name" and token[1] in ("and", "or", "not", "in"):
        raise TemplateError("expected a value, found %r" % token[1], scanner.position())
    return ("path", _path(scanner, roots))


def evaluate(condition, context):
    """True or False; ValueError for a comparison of values that cannot be compared."""
    return _truth(_eval(condition.tree, context))


def _eval(node, context):
    kind = node[0]
    if kind == "literal":
        return node[1]
    if kind == "list":
        return [_eval(item, context) for item in node[1]]
    if kind == "path":
        try:
            return lookup(context, node[1])
        except Unresolved:
            return None
    if kind == "not":
        return not _truth(_eval(node[1], context))
    if kind == "and":
        return all(_truth(_eval(item, context)) for item in node[1:])
    if kind == "or":
        return any(_truth(_eval(item, context)) for item in node[1:])
    operator, left, right = node[1], _eval(node[2], context), _eval(node[3], context)
    if operator in ("in", "not in"):
        if isinstance(right, (list, dict)):
            found = left in right
        elif isinstance(right, str) and isinstance(left, str):
            found = left in right
        elif right is None:
            found = False
        else:
            raise ValueError("'in' needs a list, an object or text on its right")
        return found if operator == "in" else not found
    if operator == "==":
        return _equal(left, right)
    if operator == "!=":
        return not _equal(left, right)
    if _number(left) and _number(right) or isinstance(left, str) and isinstance(right, str):
        return {"<": left < right, "<=": left <= right, ">": left > right, ">=": left >= right}[operator]
    raise ValueError("cannot compare %s with %s using %s" % (_kind(left), _kind(right), operator))


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _equal(left, right):
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    return left == right


def _kind(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if _number(value):
        return "a number"
    if isinstance(value, str):
        return "text"
    return "a list" if isinstance(value, list) else "an object"


def _truth(value):
    if isinstance(value, (list, dict, str)):
        return bool(value)
    return bool(value) if value is not None else False


def references(item) -> Optional[list]:
    """Paths a parsed template or condition reads."""
    return item.paths()
