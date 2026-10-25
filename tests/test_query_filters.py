"""Safe composite filters on synthetic stores; no personal memory or files."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

from richi import graph, memory, query_filters


class QueryFilterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-query-filters-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "synthetic.sqlite3"
        self.conn = memory.connect(self.path, create=True)
        self.addCleanup(self.conn.close)
        memory.initialize(self.conn, self.path)
        query_filters.prepare(self.conn, memory)
        for project in ("alpha", "beta", "gamma"):
            memory.project_put(self.conn, {"id": project, "name": project})
        self.entry("task:alpha-1", project_ids=["alpha"], tags=["SQL", "stable"], aliases=["КС", "O'Brien"],
                   title="Pool 100%_ready", jira_key="DEMO-1", work_state="done", verified_at="2026-10-01")
        self.entry("task:beta-2", project_ids=["alpha", "beta"], tags=["sql"], aliases=["кс"],
                   title="POOL 100xready", knowledge_state="hypothesis", verified_at="2026-10-20")
        self.entry("unscoped", project_ids=[], knowledge_state="superseded", title="Проект")
        self.entity("concept:one", aliases=["КС"], tags=["SQL"], title="ПРОЕКТ")
        self.entity("component:one", kind="component", title="Pool")
        self.entity("concept:unscoped")
        self.edge("concept-alpha", "entity:concept:one", "project:alpha", kind="used_in")
        self.edge("component-beta", "project:beta", "entity:component:one", kind="informs")

    def entry(self, identity, **changes):
        value = dict(id=identity, kind="note", title="Observation", summary="Synthetic evidence.",
                     project_ids=[], sources=[{"reference": "test://evidence"}],
                     tags=[], aliases=[], knowledge_state="confirmed", verified_at=None)
        value.update(changes)
        memory.entry_put(self.conn, value)

    def entity(self, identity, **changes):
        value = dict(id=identity, kind="concept", title="Definition", summary="Synthetic definition.",
                     sources=[{"reference": "test://definition"}], tags=[], aliases=[],
                     knowledge_state="confirmed", verified_at=None)
        value.update(changes)
        graph.put(self.conn, value, "entity", memory)

    def edge(self, identity, source, target, **changes):
        value = dict(id=identity, from_ref=source, to_ref=target, kind="references", description="Synthetic link.",
                     sources=[{"reference": "test://link"}], knowledge_state="confirmed")
        value.update(changes)
        graph.put(self.conn, value, "edge", memory)

    def refs(self, expression):
        parsed = query_filters.parse(expression, memory)
        parsed.prepare(self.conn)
        refs = []
        for family, table in (("entry", "entries"), ("entity", "entities")):
            sql, parameters = parsed.sql(family)
            refs.extend(family + ":" + row[0] for row in self.conn.execute(
                "SELECT e.id FROM " + table + " AS e WHERE " + sql + " ORDER BY e.id", parameters))
        return set(refs)

    def test_boolean_precedence_parentheses_and_case_insensitive_keywords(self):
        self.assertEqual(self.refs("family = 'entry' AND id = 'unscoped' OR family = 'entity' AND kind = 'component'"),
                         {"entry:unscoped", "entity:component:one"})
        self.assertEqual(self.refs("family='entry' and not (knowledge_state='superseded' Or knowledge_state='hypothesis')"),
                         {"entry:task:alpha-1"})
        self.assertEqual(self.refs("NOT NOT id = 'task:alpha-1'"), {"entry:task:alpha-1"})
        self.assertEqual(self.refs(None), {"entry:task:alpha-1", "entry:task:beta-2", "entry:unscoped",
                                         "entity:concept:one", "entity:component:one", "entity:concept:unscoped"})

    def test_scalar_comparisons_and_date_strings(self):
        self.assertEqual(self.refs("verified_at >= '2026-10-01' AND verified_at < '2026-10-20'"),
                         {"entry:task:alpha-1"})
        self.assertEqual(self.refs("verified_at > '2026-10-01' AND verified_at <= '2026-10-20'"),
                         {"entry:task:beta-2"})
        self.assertEqual(self.refs("id != 'task:alpha-1' AND family = 'entry'"),
                         {"entry:task:beta-2", "entry:unscoped"})
        self.assertEqual(self.refs("title='POOL 100%_ready'"), set())
        self.assertEqual(self.refs("TITLE='Pool 100%_ready'"), {"entry:task:alpha-1"})
        self.assertEqual(self.refs("created_at IS NOT NULL AND updated_at IS NOT NULL"), self.refs(None))

    def test_dates_compare_instants_with_offsets_and_validate_literals(self):
        self.entry("instant-a", verified_at="2026-10-25T10:00:00+02:00")
        self.entry("instant-b", verified_at="2026-10-25T08:00:00Z")
        self.entry("instant-c", verified_at="2026-10-25X08:00:00+00:00")
        self.entry("instant-d", verified_at="2026-10-25T10:00:00+02:00:00")
        self.entry("instant-e", verified_at="2026-10-26T07:00:00+23:00")
        self.assertEqual(self.refs("verified_at = '2026-10-25T09:00:00+01:00'"),
                         {"entry:instant-a", "entry:instant-b", "entry:instant-c", "entry:instant-d", "entry:instant-e"})
        self.assertEqual(self.refs("verified_at > '2026-10-25T07:59:59Z'"),
                         {"entry:instant-a", "entry:instant-b", "entry:instant-c", "entry:instant-d", "entry:instant-e"})
        self.assertEqual(self.refs("verified_at IN ('2026-10-25T08:00:00Z')"),
                         {"entry:instant-a", "entry:instant-b", "entry:instant-c", "entry:instant-d", "entry:instant-e"})
        self.assertEqual(self.refs("verified_at LIKE '2026-10-25T10:%'"), {"entry:instant-a", "entry:instant-d"})
        self.assertEqual(self.refs("verified_at = '2026-10-26T07:00:00+23:00'"),
                         {"entry:instant-a", "entry:instant-b", "entry:instant-c", "entry:instant-d", "entry:instant-e"})
        for literal in ("2026-13-01", "2026-10-25T08:00:00", "2026-W41-1", "yesterday"):
            with self.subTest(literal=literal), self.assertRaises(memory.MemoryError):
                query_filters.parse("updated_at > '" + literal + "'", memory)
        sql, _ = query_filters.parse("updated_at > '2026-10-01'", memory).sql("entry")
        self.assertIn("julianday(e.updated_at)", sql)

    def test_missing_fields_follow_sql_null_logic(self):
        self.assertEqual(self.refs("family='entity' AND jira_key IS NULL AND work_state IS NULL"),
                         {"entity:concept:one", "entity:component:one", "entity:concept:unscoped"})
        self.assertEqual(self.refs("jira_key != 'DEMO-1'"), set())
        self.assertEqual(self.refs("NOT jira_key = 'DEMO-1'"), set())
        self.assertEqual(self.refs("jira_key IS NOT NULL"), {"entry:task:alpha-1"})
        self.assertEqual(self.refs("work_state IN ('done','blocked')"), {"entry:task:alpha-1"})
        self.assertEqual(self.refs("work_state NOT IN ('done')"), set())

    def test_in_values_quote_escaping_and_exact_array_membership(self):
        self.assertEqual(self.refs("id IN ('task:alpha-1','concept:one')"),
                         {"entry:task:alpha-1", "entity:concept:one"})
        self.assertEqual(self.refs("family NOT IN ('entry') AND kind='component'"), {"entity:component:one"})
        self.assertEqual(self.refs("tags HAS 'SQL'"), {"entry:task:alpha-1", "entity:concept:one"})
        self.assertEqual(self.refs("tags HAS 'sql'"), {"entry:task:beta-2"})
        self.assertEqual(self.refs("aliases HAS 'O''Brien'"), {"entry:task:alpha-1"})
        self.assertEqual(self.refs("aliases HAS 'КС'"), {"entry:task:alpha-1", "entity:concept:one"})
        self.assertEqual(self.refs("aliases HAS 'К'"), set())
        self.assertEqual(self.refs("NOT (tags HAS 'SQL')"), self.refs("tags NOT HAS 'SQL'"))

    def test_like_wildcards_escape_and_documented_ascii_case_rules(self):
        self.assertEqual(self.refs("title LIKE 'pool%'"),
                         {"entry:task:alpha-1", "entry:task:beta-2", "entity:component:one"})
        self.assertEqual(self.refs(r"title LIKE 'Pool 100\%\_ready'"), {"entry:task:alpha-1"})
        self.assertEqual(self.refs("title LIKE 'POOL 100_ready'"), {"entry:task:beta-2"})
        self.assertEqual(self.refs("title LIKE 'проект'"), set())
        self.assertEqual(self.refs("title LIKE 'Про%'"), {"entry:unscoped"})
        self.assertEqual(self.refs("title NOT LIKE 'Pool%'"), self.refs("NOT (title LIKE 'Pool%')"))
        self.entry("slash", title=r"c:\folder")
        self.assertEqual(self.refs(r"title LIKE 'c:\\folder'"), {"entry:slash"})

    def test_project_collections_have_clear_positive_negative_and_null_semantics(self):
        self.assertEqual(self.refs("project = 'alpha'"),
                         {"entry:task:alpha-1", "entry:task:beta-2", "entity:concept:one"})
        self.assertEqual(self.refs("project = 'beta'"), {"entry:task:beta-2", "entity:component:one"})
        self.assertEqual(self.refs("project = 'alpha' AND project = 'beta'"), {"entry:task:beta-2"})
        self.assertEqual(self.refs("project != 'alpha'"),
                         {"entity:component:one", "entry:unscoped", "entity:concept:unscoped"})
        self.assertEqual(self.refs("project NOT IN ('alpha')"), self.refs("project != 'alpha'"))
        self.assertEqual(self.refs("NOT (project='alpha')"), self.refs("project != 'alpha'"))
        self.assertEqual(self.refs("project IS NULL"), {"entry:unscoped", "entity:concept:unscoped"})
        self.assertEqual(self.refs("project IS NOT NULL"), self.refs(None) - self.refs("project IS NULL"))
        self.assertEqual(self.refs("project LIKE 'b%'"), {"entry:task:beta-2", "entity:component:one"})
        self.assertEqual(self.refs("project IN ('alpha','beta')"), self.refs("project IS NOT NULL"))
        self.assertEqual(self.refs("project > 'alpha'"), self.refs("project='beta'"))

    def test_entity_scope_rejects_wrong_kind_direction_state_and_unbacked_edges(self):
        self.entity("concept:wrong-kind")
        self.entity("concept:reverse")
        self.entity("concept:hypothesis-edge")
        self.entity("concept:empty-source")
        self.entity("component:hypothesis-edge", kind="component")
        self.edge("wrong-kind", "entity:concept:wrong-kind", "project:gamma", kind="informs")
        self.edge("reverse", "project:gamma", "entity:concept:reverse", kind="used_in")
        self.edge("hypothesis", "entity:concept:hypothesis-edge", "project:gamma", kind="used_in", knowledge_state="hypothesis")
        self.edge("empty-source", "entity:concept:empty-source", "project:gamma", kind="used_in")
        self.conn.execute("UPDATE graph_edges SET sources = '[]' WHERE id = 'empty-source'")
        self.edge("component-hypothesis", "project:gamma", "entity:component:hypothesis-edge", knowledge_state="hypothesis")
        self.assertEqual(self.refs("project='gamma'"), set())
        self.edge("duplicate-meaning", "entity:concept:one", "project:alpha", kind="used_in")
        parsed = query_filters.parse("project='alpha'", memory)
        sql, params = parsed.sql("entity")
        rows = self.conn.execute("SELECT e.id FROM entities AS e WHERE " + sql, params).fetchall()
        self.assertEqual([row[0] for row in rows], ["concept:one"])

    def test_parameterization_and_injection_rejection(self):
        payload = "x' OR 1=1 --"
        expression = "id = '" + payload.replace("'", "''") + "'"
        parsed = query_filters.parse(expression, memory)
        sql, params = parsed.sql("entry")
        self.assertNotIn(payload, sql)
        self.assertEqual(params, [payload])
        self.assertEqual(self.refs(expression), set())
        invalid = ["id='x'; DROP TABLE entries", "id='x' -- comment", "id='x' /* comment */",
                   "e.id='x'", "random()='x'", "summary='x'", "id LIKE (SELECT id FROM entries)",
                   "id=x", "id=1", "id=NULL", "id <> 'x'", "id BETWEEN 'a' AND 'z'", 'id="x"',
                   "tags='SQL'", "id HAS 'x'", "id IN ()", "id IN ('x',)", "id NOT = 'x'",
                   "(id='x'", "id='x')", "id='unterminated", "AND id='x'", "id='x' OR", ""]
        for expression in invalid:
            with self.subTest(expression=expression), self.assertRaises(memory.MemoryError):
                query_filters.parse(expression, memory)
        with self.assertRaises(memory.MemoryError):
            parsed.sql("entry", "e; DELETE FROM entries")
        with self.assertRaises(memory.MemoryError):
            parsed.sql("project")

    def test_complexity_limits(self):
        query_filters.parse("(" * 16 + "id='x'" + ")" * 16, memory)
        query_filters.parse(" OR ".join("id='x'" for _ in range(64)), memory)
        query_filters.parse("id IN (" + ",".join("'x'" for _ in range(64)) + ")", memory)
        in_clause = "id IN (" + ",".join("'x'" for _ in range(64)) + ")"
        query_filters.parse(" OR ".join([in_clause] * 4), memory)
        with self.assertRaisesRegex(memory.MemoryError, "bound parameters"):
            query_filters.parse(" OR ".join([in_clause] * 4) + " OR id='x'", memory)
        with self.assertRaisesRegex(memory.MemoryError, "bound parameters"):
            query_filters.parse(" OR ".join([in_clause] * 4) + " OR family IS NULL", memory)
        invalid = ["(" * 17 + "id='x'" + ")" * 17, "NOT " * 17 + "id='x'",
                   " OR ".join("id='x'" for _ in range(65)),
                   "id IN (" + ",".join("'x'" for _ in range(65)) + ")",
                   "id='" + "x" * 4000 + "'", "id='x\x00y'"]
        for expression in invalid:
            with self.subTest(length=len(expression)), self.assertRaises(memory.MemoryError):
                query_filters.parse(expression, memory)

    def test_matches_ref_exact_ids_no_mutation_and_caller_transaction(self):
        self.entry("-task:native-id", project_ids=["alpha"])
        parsed = query_filters.parse("project='alpha' AND family='entry'", memory)
        statements = []
        self.conn.set_trace_callback(statements.append)
        with memory.transaction(self.conn, write=False):
            self.assertTrue(parsed.matches_ref(self.conn, "entry:-task:native-id"))
            self.assertFalse(parsed.matches_ref(self.conn, "entry:missing"))
            self.assertFalse(parsed.matches_ref(self.conn, "entity:concept:one"))
            self.assertTrue(self.conn.in_transaction)
        self.conn.set_trace_callback(None)
        self.assertFalse(any(s.lstrip().split(" ", 1)[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"} for s in statements))
        self.assertFalse(parsed.matches_ref(self.conn, "project:alpha"))
        with self.assertRaises(memory.MemoryError):
            parsed.matches_ref(self.conn, "entry:")

    def test_json_capability_and_schema_one_missing_entities(self):
        broken = Mock()
        broken.execute.side_effect = sqlite3.OperationalError("no such function: json_each")
        with self.assertRaisesRegex(memory.MemoryError, "JSON1"):
            query_filters.prepare(broken, memory)
        self.assertFalse(query_filters.parse(None, memory))
        self.assertTrue(query_filters.parse("id='a'", memory))
        self.assertFalse(query_filters.parse("id='a'", memory).requires_json)
        self.assertTrue(query_filters.parse("tags HAS 'a' OR project IS NULL", memory).requires_json)
        legacy = sqlite3.connect(":memory:")
        self.addCleanup(legacy.close)
        self.assertFalse(query_filters.parse(None, memory).matches_ref(legacy, "entity:missing"))


if __name__ == "__main__":
    unittest.main()
