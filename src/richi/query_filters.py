"""Bounded SQL-like filters compiled from a whitelist, never raw user SQL.

String literals use single quotes; a quote inside a string is doubled (''),
while backslashes are preserved. Text equality/order are BINARY. Date equality/order/IN use SQLite julianday
(SQLite date precision applies). Existing Python ISO timestamp forms that SQLite
cannot parse use a read-only UTC normalization function. LIKE
compares the original stored text. LIKE uses SQLite's default ASCII case-insensitive
matching (non-ASCII remains case-sensitive): % matches any run, _ one character,
and backslash escapes the next pattern character. Use \\% / \\_ for literals and
\\\\ for a literal backslash. HAS tests exact, case-sensitive array membership.

Missing scalar fields are SQL NULL. IS NULL/IS NOT NULL are explicit; = NULL is
rejected to prevent a silently never-matching comparison. Project is a set of
justified project IDs: positive tests mean any matching ID, negative tests mean
none (including an empty set). Project IS NULL selects an empty set; ordinary
scalar fields retain SQL three-valued logic. Entity project scope includes only
confirmed sourced edges; concepts require outgoing used_in, other entity kinds
accept either direction. No knowledge-state policy is bypassed by this filter.
"""

import re
import sqlite3


MAX_CHARS = 4000
MAX_NODES = 128
MAX_DEPTH = 16
MAX_IN = 64
MAX_PARAMETERS = 256
DATE_FIELDS = frozenset({"created_at", "updated_at", "verified_at"})
DATE_LITERAL = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[T ][0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?(?:Z|[+-][0-9]{2}:[0-9]{2}))?\Z")
FIELDS = frozenset({"family", "id", "kind", "knowledge_state", "work_state", "jira_key",
                    "created_at", "updated_at", "verified_at", "project", "tags", "aliases", "title"})
ARRAY_FIELDS = frozenset({"tags", "aliases"})
ENTITY_MISSING = frozenset({"work_state", "jira_key"})
KEYWORDS = frozenset({"AND", "OR", "NOT", "IN", "IS", "NULL", "LIKE", "HAS"})
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z", re.ASCII)
TOKEN = re.compile(r"\s+|'(?:[^']|'')*'|!=|<=|>=|[=<>(),]|[A-Za-z_][A-Za-z0-9_]*", re.ASCII)


class _Parser:
    def __init__(self, expression, api):
        self.api = api
        self.tokens = []
        self.position = 0
        self.nodes = 0
        self.parameters = 0
        cursor = 0
        while cursor < len(expression):
            match = TOKEN.match(expression, cursor)
            if match is None:
                self.fail("unexpected character at position " + str(cursor + 1))
            token = match.group()
            if not token.isspace():
                self.tokens.append((token, cursor + 1))
            cursor = match.end()
        self.tokens.append(("", len(expression) + 1))

    def fail(self, message):
        raise self.api.MemoryError("Invalid --where: " + message)

    def peek(self):
        return self.tokens[self.position][0].upper()

    def take(self, token):
        if self.peek() == token:
            value = self.tokens[self.position][0]
            self.position += 1
            return value
        return None

    def require(self, token):
        if self.take(token) is None:
            self.fail("expected " + token + " at position " + str(self.tokens[self.position][1]))

    def node(self, value):
        self.nodes += 1
        if self.nodes > MAX_NODES:
            self.fail("expression exceeds " + str(MAX_NODES) + " nodes")
        return value

    def expression(self, depth=0):
        values = [self.conjunction(depth)]
        while self.take("OR"):
            self.node(None)
            values.append(self.conjunction(depth))
        return ("or", tuple(values)) if len(values) > 1 else values[0]

    def conjunction(self, depth):
        values = [self.term(depth)]
        while self.take("AND"):
            self.node(None)
            values.append(self.term(depth))
        return ("and", tuple(values)) if len(values) > 1 else values[0]

    def term(self, depth):
        if depth > MAX_DEPTH:
            self.fail("expression nesting exceeds " + str(MAX_DEPTH))
        if self.take("NOT"):
            self.node(None)
            return ("not", self.term(depth + 1))
        if self.take("("):
            value = self.expression(depth + 1)
            self.require(")")
            return value
        return self.predicate()

    def parameter(self):
        self.parameters += 1
        if self.parameters > MAX_PARAMETERS:
            self.fail("expression exceeds " + str(MAX_PARAMETERS) + " bound parameters")

    def literal(self):
        token = self.tokens[self.position][0]
        if not token.startswith("'"):
            self.fail("values must be single-quoted strings; use IS NULL for null")
        self.position += 1
        self.parameter()
        return token[1:-1].replace("''", "'")

    def predicate(self):
        field = self.tokens[self.position][0].lower()
        if field not in FIELDS:
            self.fail("unknown field at position " + str(self.tokens[self.position][1]))
        self.position += 1
        if field == "family":
            self.parameter()
        operator = self.peek()
        if self.take("IS"):
            negated = bool(self.take("NOT"))
            self.require("NULL")
            return self.node(("predicate", field, "is_not_null" if negated else "is_null", None))
        negated = bool(self.take("NOT"))
        if negated:
            operator = self.peek()
            if operator not in {"IN", "LIKE", "HAS"}:
                self.fail("NOT after a field requires IN, LIKE or HAS")
        if operator not in {"=", "!=", "<", "<=", ">", ">=", "IN", "LIKE", "HAS"}:
            self.fail("expected comparison, IN, LIKE, HAS or IS NULL")
        self.position += 1
        if field in ARRAY_FIELDS and operator != "HAS":
            self.fail("tags and aliases support HAS or IS NULL, not scalar comparisons")
        if operator == "HAS" and field not in ARRAY_FIELDS:
            self.fail("HAS is supported only for tags and aliases")
        if operator == "IN":
            self.require("(")
            values = [self.literal()]
            while self.take(","):
                if len(values) == MAX_IN:
                    self.fail("IN accepts at most " + str(MAX_IN) + " values")
                values.append(self.literal())
            self.require(")")
            value = tuple(values)
        else:
            value = self.literal()
        if field in DATE_FIELDS and operator != "LIKE":
            for literal in value if isinstance(value, tuple) else (value,):
                if DATE_LITERAL.fullmatch(literal) is None:
                    self.fail("date comparisons require YYYY-MM-DD or an ISO timestamp with timezone")
                self.api.timestamp(literal, "where " + field)
                try:
                    _iso_timestamp(literal)
                except (OverflowError, ValueError):
                    self.fail("date comparison exceeds the supported UTC range (years 0001 through 9999)")
        operation = operator.lower()
        return self.node(("predicate", field, "not_" + operation if negated else operation, value))


def parse(expression, api):
    """Return a reusable filter; None is a no-op, empty text is an error."""
    if expression is None:
        return ParsedFilter(None, api)
    expression = api.string(expression, "where")
    if len(expression) > MAX_CHARS:
        raise api.MemoryError("where must be at most " + str(MAX_CHARS) + " characters")
    parser = _Parser(expression, api)
    tree = parser.expression()
    if parser.peek():
        parser.fail("unexpected token at position " + str(parser.tokens[parser.position][1]))
    return ParsedFilter(tree, api)


def _iso_timestamp(value):
    """Normalize the public timestamp contract without changing stored values."""
    if value is None:
        return None
    from datetime import date, datetime, timezone
    if len(value) == 10:
        return date.fromisoformat(value).isoformat()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Stored timestamp has no timezone")
    # Invalid/out-of-range storage raises a visible SQLite callback error,
    # rather than silently converting real evidence into a no-match result.
    return parsed.astimezone(timezone.utc).isoformat()


def prepare(conn, api=None, *, need_json=True, need_dates=False):
    """Prepare read-connection capabilities; no schema or persistent functions."""
    if api is None:
        from . import memory as api
    if need_json:
        try:
            conn.execute("SELECT json_array_length('[]'), value FROM json_each('[1]')").fetchone()
        except sqlite3.Error as exc:
            raise api.MemoryError("Composite collection filters require SQLite JSON1 support") from exc
    if need_dates:
        # Re-registering a function fails while another cursor is using it.
        # The connection-local helper is shared by all parsed date filters;
        # A small behavior probe avoids replacing the function while checking
        # that a caller has not reused our reserved name for another function.
        try:
            probe = conn.execute("SELECT richi_iso_timestamp(NULL), "
                                 "richi_iso_timestamp('2000-01-01T01:00:00+01:00')").fetchone()
        except sqlite3.OperationalError as exc:
            if "no such function: richi_iso_timestamp" not in str(exc):
                raise api.MemoryError("Cannot verify reserved date function richi_iso_timestamp") from exc
            conn.create_function("richi_iso_timestamp", 1, _iso_timestamp, deterministic=True)
        else:
            if tuple(probe) != (None, "2000-01-01T00:00:00+00:00"):
                raise api.MemoryError("Incompatible reserved date function richi_iso_timestamp")


class ParsedFilter:
    def __init__(self, tree, api):
        self.tree = tree
        self.api = api
        self._prepared = set()

    def __bool__(self):
        return self.tree is not None

    def _requires(self, predicate):
        def needed(node):
            if node is None:
                return False
            if node[0] in {"and", "or"}:
                return any(needed(child) for child in node[1])
            if node[0] == "not":
                return needed(node[1])
            return predicate(node)
        return needed(self.tree)

    @property
    def requires_json(self):
        return self._requires(lambda node: node[1] in {"project", "tags", "aliases"})

    @property
    def requires_dates(self):
        return self._requires(lambda node: node[1] in DATE_FIELDS and node[2] not in
                              {"is_null", "is_not_null", "like", "not_like"})

    def prepare(self, conn):
        if conn not in self._prepared:
            prepare(conn, self.api, need_json=self.requires_json, need_dates=self.requires_dates)
            self._prepared.add(conn)

    def sql(self, family, alias="e"):
        if family not in {"entry", "entity"}:
            raise self.api.MemoryError("Filter family must be entry or entity")
        if (not isinstance(alias, str) or IDENTIFIER.fullmatch(alias) is None or
                alias.lower() in {"qf_project", "qf_edge", "qf_item"}):
            raise self.api.MemoryError("Invalid internal filter SQL alias")
        return self._compile(self.tree, family, alias)

    def _compile(self, node, family, alias):
        if node is None:
            return "1", []
        if node[0] in {"and", "or"}:
            compiled = [self._compile(child, family, alias) for child in node[1]]
            return ("(" + (" " + node[0].upper() + " ").join(sql for sql, _ in compiled) + ")",
                    [value for _, parameters in compiled for value in parameters])
        if node[0] == "not":
            sql, parameters = self._compile(node[1], family, alias)
            return "(NOT " + sql + ")", parameters
        _, field, operation, value = node
        if field == "project":
            return self._project(family, alias, operation, value)
        if operation in {"has", "not_has"}:
            sql = ("EXISTS (SELECT 1 FROM json_each(" + alias + "." + field + ") AS qf_item "
                   "WHERE 1=1 AND qf_item.type = 'text' AND qf_item.value COLLATE BINARY = ?)")
            return ("(NOT " + sql + ")" if operation == "not_has" else sql), [value]
        if field == "family":
            column, parameters = "?", [family]
        elif family == "entity" and field in ENTITY_MISSING:
            column, parameters = "NULL", []
        else:
            column, parameters = alias + "." + field, []
        sql, values = self._comparison(column, operation, value, date=field in DATE_FIELDS)
        return sql, parameters + values

    @staticmethod
    def _comparison(column, operation, value, date=False):
        if operation == "is_null":
            return "(" + column + " IS NULL)", []
        if operation == "is_not_null":
            return "(" + column + " IS NOT NULL)", []
        if date and operation not in {"like", "not_like"}:
            column = "COALESCE(julianday(" + column + "), julianday(richi_iso_timestamp(" + column + ")))"
        # Python accepts offsets up to 24 hours; SQLite stops below 15.
        # Normalize query values too, without duplicating bound parameters.
        parameter = "julianday(richi_iso_timestamp(?))" if date else "?"
        if operation in {"in", "not_in"}:
            return ("(" + column + " COLLATE BINARY " + ("NOT IN" if operation == "not_in" else "IN") +
                    " (" + ",".join(parameter for _ in value) + "))"), list(value)
        if operation in {"like", "not_like"}:
            return ("(" + column + (" NOT LIKE ?" if operation == "not_like" else " LIKE ?") +
                    " ESCAPE '\\')"), [value]
        return "(" + column + " COLLATE BINARY " + operation + " " + parameter + ")", [value]

    def _project(self, family, alias, operation, value):
        if family == "entry":
            source = "entry_projects AS qf_project"
            scope = "qf_project.entry_id = " + alias + ".id"
            column = "qf_project.project_id"
        else:
            reference = "'entity:' || " + alias + ".id"
            other_ref = ("CASE WHEN qf_edge.from_ref = " + reference +
                         " THEN qf_edge.to_ref ELSE qf_edge.from_ref END")
            # Start from the entity's indexed incoming/outgoing links, then look
            # up the project by its native primary key. Joining two concatenated
            # refs with OR instead can scan projects × edges for every entity.
            source = ("graph_edges AS qf_edge JOIN projects AS qf_project ON "
                      "qf_project.id = substr(" + other_ref + ", 9)")
            scope = ("(qf_edge.from_ref = " + reference + " OR qf_edge.to_ref = " + reference + ") AND "
                     "substr(" + other_ref + ", 1, 8) = 'project:' AND "
                     "qf_edge.knowledge_state = 'confirmed' AND json_array_length(qf_edge.sources) > 0 AND "
                     "(" + alias + ".kind <> 'concept' OR (qf_edge.kind = 'used_in' AND "
                     "qf_edge.from_ref = " + reference + "))")
            column = "qf_project.id"
        exists = "EXISTS (SELECT 1 FROM " + source + " WHERE 1=1 AND " + scope + ")"
        if operation in {"is_null", "is_not_null"}:
            return ("(NOT " + exists + ")" if operation == "is_null" else exists), []
        negative = operation in {"!=", "not_in", "not_like"}
        positive = {"!=": "=", "not_in": "in", "not_like": "like"}.get(operation, operation)
        predicate, parameters = self._comparison(column, positive, value)
        matching = "EXISTS (SELECT 1 FROM " + source + " WHERE 1=1 AND " + scope + " AND " + predicate + ")"
        return ("(NOT " + matching + ")" if negative else matching), parameters

    def matches_ref(self, conn, node_ref):
        self.api.string(node_ref, "node ref")
        family, separator, identity = node_ref.partition(":")
        if separator and family == "project":
            self.api.identifier(identity, "node ref ID")
            return False
        if not separator or family not in {"entry", "entity"}:
            raise self.api.MemoryError("Filter ref must be entry:<id> or entity:<id>")
        self.api.identifier(identity, "node ref ID")
        table = "entries" if family == "entry" else "entities"
        # A schema-v1 store has no entities. A missing entity is never a match.
        if family == "entity" and not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE 1=1 AND type = 'table' AND name = 'entities'").fetchone():
            return False
        self.prepare(conn)
        sql, parameters = self.sql(family)
        return conn.execute("SELECT 1 FROM " + table + " AS e WHERE 1=1 AND e.id = ? AND " + sql,
                            [identity] + parameters).fetchone() is not None
