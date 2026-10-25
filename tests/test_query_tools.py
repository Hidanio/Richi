"""Exact synthetic metadata queries, output bounds and read-only isolation."""
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from richi import graph, memory, output, query_tools


class QueryToolsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-query-tools-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "synthetic.sqlite3"
        self.conn = memory.connect(self.path, create=True)
        self.addCleanup(self.conn.close)
        memory.initialize(self.conn, self.path)
        for identity in ("alpha", "beta", "O'Brien"):
            memory.project_put(self.conn, {"id": identity, "name": identity})

    def entry(self, identity, **changes):
        value = dict(id=identity, kind="note", title="Synthetic title", summary="Recorded observation.",
                     sources=[{"reference": "test://source"}], project_ids=["alpha"],
                     tags=[], aliases=[], knowledge_state="confirmed", verified_at=None)
        value.update(changes)
        memory.entry_put(self.conn, value)

    def entity(self, identity, **changes):
        value = dict(id=identity, kind="concept", title="Synthetic concept", summary="Recorded meaning.",
                     sources=[{"reference": "test://source"}], tags=[], aliases=[],
                     knowledge_state="confirmed", verified_at=None)
        value.update(changes)
        graph.put(self.conn, value, "entity", memory)

    def edge(self, identity, source, target, **changes):
        value = dict(id=identity, from_ref=source, to_ref=target, kind="used_in", description="Explicit scope.",
                     sources=[{"reference": "test://edge"}], knowledge_state="confirmed")
        value.update(changes)
        graph.put(self.conn, value, "edge", memory)

    def args(self, action, **changes):
        value = dict(command="query", action=action, family="entry", project=None, where=None,
                     include_hypotheses=False, include_superseded=False, max_chars=8000, limit=20,
                     fields=query_tools.DEFAULT_FIELDS, after=None, by=["kind"])
        value.update(changes)
        return SimpleNamespace(**value)

    def query(self, action, compact=False, conn=None, **changes):
        args = self.args(action, **changes)
        with output.json_mode(compact=compact):
            result = query_tools.run(conn or self.conn, args, memory)
            serialized = output.rendered(result)
            self.assertLessEqual(len(serialized), args.max_chars)
            self.assertEqual(result["budget"]["output_chars"], len(serialized))
        return result

    def test_count_and_exists_cover_all_records_beyond_retrieval_caps(self):
        with memory.transaction(self.conn, write=True):
            for index in range(205):
                self.entry("note:" + str(index))
            self.entry("guess", knowledge_state="hypothesis")
            self.entry("old", knowledge_state="superseded")
            self.entity("concept")
        self.assertEqual(self.query("count")["count"], 205)
        self.assertEqual(self.query("count", family="all")["count"], 206)
        result = self.query("count", family="all", include_hypotheses=True, include_superseded=True)
        self.assertEqual(result["count"], 208)
        self.assertTrue(result["exact"])
        self.assertTrue(self.query("exists", where="id='note:204'")["exists"])
        self.assertFalse(self.query("exists", where="knowledge_state='hypothesis'")["exists"])
        self.assertTrue(self.query("exists", where="knowledge_state='hypothesis'", include_hypotheses=True)["exists"])
        self.assertFalse(self.query("exists", where="id='missing'")["exists"])

    def test_strict_project_and_composite_filter_use_justified_scope(self):
        self.entry("a", project_ids=["alpha", "beta"], tags=["keep"])
        self.entry("b", project_ids=["beta"])
        self.entry("quote", project_ids=["O'Brien"])
        self.entity("right")
        self.entity("reversed")
        self.entity("weak")
        self.entity("component", kind="component")
        self.edge("right", "entity:right", "project:alpha")
        self.edge("reversed", "project:alpha", "entity:reversed")
        self.edge("weak", "entity:weak", "project:alpha", knowledge_state="hypothesis")
        self.edge("component", "project:alpha", "entity:component", kind="informs")
        self.assertEqual(self.query("count", family="all", project="alpha")["count"], 3)
        self.assertEqual(self.query("count", family="all", project="alpha", where="tags HAS 'keep'")["count"], 1)
        self.assertEqual(self.query("count", project="O'Brien")["count"], 1)
        self.assertEqual(self.query("count", project="not-registered")["count"], 0)
        self.assertEqual(self.query("count", family="all", where="project IS NULL")["count"], 2)

    def test_facets_null_groups_all_families_top_n_and_exact_sums(self):
        self.entry("one", kind="note")
        self.entry("two", kind="note")
        self.entry("three", kind="task", work_state="done", jira_key="DEMO-1")
        self.entry("four", kind="task", work_state="done", jira_key="DEMO-1")
        self.entry("guess", knowledge_state="hypothesis")
        self.entity("meaning")
        result = self.query("facets", family="all", by=["family", "work_state"])
        self.assertEqual(result["columns"], ["family", "work_state", "count"])
        self.assertEqual(result["rows"], [["entry", None, 2], ["entry", "done", 2], ["entity", None, 1]])
        self.assertEqual(sum(row[-1] for row in result["rows"]), self.query("count", family="all")["count"])
        self.assertFalse(result["has_more"])
        self.assertTrue(result["counts_exact"])
        limited = self.query("facets", family="all", by=["kind"], limit=1)
        self.assertEqual(limited["rows"], [["note", 2]])
        self.assertTrue(limited["has_more"])
        self.assertEqual(self.query("facets", family="entity", by=["jira_key"])["rows"], [[None, 1]])

    def test_facets_budgets_keep_exact_groups_and_explicit_omissions(self):
        for index in range(20):
            self.entry("group:" + str(index), jira_key=str(index).zfill(2) + "x" * 350)
        for compact in (False, True):
            result = self.query("facets", by=["jira_key"], limit=20, max_chars=2000, compact=compact)
            self.assertTrue(result["has_more"])
            self.assertTrue(result["counts_exact"])
            self.assertGreater(len(result["rows"]), 0)
            self.assertLess(len(result["rows"]), 20)
            self.assertEqual(result["returned"], len(result["rows"]))
            self.assertTrue(all(count == 1 and len(key) == 352 for key, count in result["rows"]))
        self.assertEqual(self.query("count")["count"], 20)

    def test_facets_do_not_hydrate_huge_keys_or_reorder_oversized_lookahead(self):
        self.entry("small", jira_key="a-small")
        self.entry("large", jira_key="z" * 100000)
        original_factory = self.conn.row_factory

        def bounded_factory(cursor, row):
            self.assertFalse(any(isinstance(value, str) and len(value) > 8000 for value in row),
                             "An unbounded grouping key reached Python")
            return original_factory(cursor, row)

        self.conn.row_factory = bounded_factory
        try:
            result = self.query("facets", by=["jira_key"], limit=1, max_chars=2000)
            self.assertEqual(result["rows"], [["a-small", 1]])
            self.assertTrue(result["has_more"])
            with self.assertRaisesRegex(memory.MemoryError, "facet key.*budget"):
                self.query("facets", by=["jira_key"], limit=2, max_chars=2000)
        finally:
            self.conn.row_factory = original_factory

    def test_select_pages_are_binary_ordered_complete_and_exact(self):
        identities = ["z", "A", "a:part", "-leading", "O'Brien", "я"]
        for identity in identities:
            self.entry(identity)
            self.entity(identity)
        expected = sorted(family + ":" + identity for family in ("entry", "entity") for identity in identities)
        for compact in (False, True):
            after, actual = None, []
            for _ in range(20):
                page = self.query("select", family="all", fields="id,work_state,jira_key,ref,id", limit=2,
                                  after=after, compact=compact, max_chars=2000)
                self.assertEqual(page["columns"], ["ref", "id", "work_state", "jira_key"])
                self.assertFalse(page["full_records"])
                self.assertEqual(page["returned"], len(page["rows"]))
                actual.extend(row[0] for row in page["rows"])
                self.assertEqual(page["oversized"], [])
                if not page["has_more"]:
                    self.assertIsNone(page["next_after"])
                    break
                self.assertGreater(page["next_after"], after or "")
                after = page["next_after"]
            else:
                self.fail("Select pagination failed to finish")
            self.assertEqual(actual, expected)
        self.assertEqual(self.query("select", after="entry:zzzz", where="id='A'")["rows"], [])

    def test_select_budget_reports_whole_oversize_rows_and_progresses(self):
        self.entry("a-large", title="x" * 40000)
        self.entry("b-medium", title="x" * 900)
        self.entry("c-medium", title="y" * 900)
        self.entry("d-small", title="Last")
        for compact in (False, True):
            after, selected, omitted = None, [], []
            for _ in range(12):
                page = self.query("select", fields="title", max_chars=2000, after=after, compact=compact)
                selected.extend(page["rows"])
                omitted.extend(item["ref"] for item in page["oversized"])
                for item in page["oversized"]:
                    self.assertEqual(item["read_more"]["args"], ["fetch", "--ref", item["ref"]])
                if not page["has_more"]:
                    break
                self.assertGreater(page["next_after"], after or "")
                after = page["next_after"]
            else:
                self.fail("Oversize pagination failed to finish")
            self.assertEqual(omitted, ["entry:a-large"])
            self.assertEqual(selected, [["entry:b-medium", "x" * 900], ["entry:c-medium", "y" * 900], ["entry:d-small", "Last"]])

    def test_select_never_silently_truncates_embedded_nul_or_unicode(self):
        self.entry("nul")
        # Simulate a legacy stored value; current writes reject embedded NUL.
        self.conn.execute("UPDATE entries SET title = ? WHERE id = ?", ("before\x00after", "nul"))
        self.assertEqual(self.query("select", fields="title")["rows"], [["entry:nul", "before\x00after"]])
        self.conn.execute("UPDATE entries SET title = ? WHERE id = ?", ("x\x00" + "я" * 20000, "nul"))
        result = self.query("select", fields="title", max_chars=2000)
        self.assertEqual(result["rows"], [])
        self.assertEqual([row["ref"] for row in result["oversized"]], ["entry:nul"])
        self.assertFalse(result["has_more"])

    def test_unknown_fields_injection_and_invalid_limits_are_rejected(self):
        self.entry("safe")
        invalid = [dict(action="select", fields="summary"), dict(action="select", fields="id; DROP TABLE entries"),
                   dict(action="select", fields="id,"), dict(action="select", after="project:alpha"),
                   dict(action="select", after="entry:"), dict(action="count", where="id='x'; DELETE FROM entries"),
                   dict(action="count", where="title='x' OR"), dict(action="count", family="project"),
                   dict(action="count", max_chars=1999), dict(action="select", limit=201),
                   dict(action="facets", by=["summary"]), dict(action="facets", by=[]),
                   dict(action="facets", by=["kind", "family", "jira_key", "knowledge_state"])]
        before = self.conn.total_changes
        for changes in invalid:
            action = changes.pop("action")
            with self.subTest(action=action, changes=changes), self.assertRaises(memory.MemoryError):
                query_tools.run(self.conn, self.args(action, **changes), memory)
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(self.query("count")["count"], 1)

    def test_readonly_connection_preserves_bytes_and_caller_transaction(self):
        self.entry("one")
        before = self.path.read_bytes()
        conn = memory.connect(self.path, readonly=True)
        try:
            with memory.transaction(conn, write=False):
                for action in ("count", "exists", "facets", "select"):
                    self.query(action, conn=conn, where="verified_at IS NULL")
                    self.assertTrue(conn.in_transaction)
                self.assertEqual(conn.total_changes, 0)
        finally:
            conn.close()
        self.assertEqual(self.path.read_bytes(), before)

    def test_query_uses_one_snapshot_without_owning_callers_transaction(self):
        self.entry("before")
        writer = memory.connect(self.path)
        self.addCleanup(writer.close)
        original = query_tools._selection

        def interleaved(conn, args, api):
            selection = original(conn, args, api)
            original_conn = self.conn
            self.conn = writer
            try:
                self.entry("after")
            finally:
                self.conn = original_conn
            return selection

        with patch.object(query_tools, "_selection", side_effect=interleaved):
            self.assertEqual(self.query("count")["count"], 1)
        self.assertEqual(self.query("count")["count"], 2)

    def test_schema_one_all_means_available_families_without_migration(self):
        path = self.path.parent / "legacy.sqlite3"
        legacy = sqlite3.connect(path, isolation_level=None)
        legacy.row_factory = sqlite3.Row
        legacy.executescript((memory.BASE / "schema.sql").read_text())
        legacy.execute("PRAGMA user_version = 1")
        legacy.execute("INSERT INTO metadata VALUES ('search_backend', 'scan')")
        memory.entry_put(legacy, dict(id="old", kind="note", title="Legacy", summary="Synthetic legacy record.",
                                      knowledge_state="confirmed", sources=[], project_ids=[]))
        legacy.close()
        before = path.read_bytes()
        conn = memory.connect(path, readonly=True)
        try:
            result = self.query("count", conn=conn, family="all")
            self.assertEqual(result["count"], 1)
            self.assertEqual(result["scope"]["families"], ["entry"])
            self.assertEqual(self.query("select", conn=conn, family="all", fields="id")["rows"], [["entry:old", "old"]])
            with self.assertRaises(memory.MemoryError):
                self.query("count", conn=conn, family="entity")
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        finally:
            conn.close()
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
