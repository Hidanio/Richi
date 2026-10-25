"""Search predicates, index parity and observable work on synthetic stores."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from richi import compact, graph, memory, recall, search_index
import test_recall_candidates as legacy


class IndexedRecallTests(unittest.TestCase):
    entry = legacy.RecallCandidateTests.entry
    entity = legacy.RecallCandidateTests.entity
    edge = legacy.RecallCandidateTests.edge
    arguments = staticmethod(legacy.RecallCandidateTests.arguments)
    refs = staticmethod(legacy.RecallCandidateTests.refs)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="indexed-recall-")
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "synthetic.sqlite3"
        self.conn = memory.connect(self.db, create=True)
        self.addCleanup(self.conn.close)
        memory.initialize(self.conn, self.db)
        for identity in ("alpha", "beta"):
            memory.project_put(self.conn, {"id": identity, "name": identity})

    def build(self):
        with memory.transaction(self.conn):
            search_index.build(self.conn, memory)

    def query(self, query, **changes):
        return recall.recall(self.conn, self.arguments(query, **changes), memory)

    def test_normal_output_parity_including_ambiguity_guards_and_graph(self):
        self.entry("Straße", aliases=["Ёлочный контур", "route / anchor", "Y"])
        self.entry("task:DEMO-42", kind="task", jira_key="DEMO-42", title="latency result")
        self.entry("prefix", jira_key="DEMO-420", title="latency result")
        self.entry("neighbor")
        self.edge("evidence", "entry:Straße", "entry:neighbor")
        self.entity("concept-a", aliases=["Y", "Ёлочный контур"])
        self.entity("concept-b", aliases=["Y"])
        self.entity("component", kind="component", title="Transport foo.bar / Straße ёлка", tags=["tuned"])
        self.edge("scope", "entity:concept-b", "project:beta", kind="used_in")
        self.entry("idea", knowledge_state="hypothesis", aliases=["route anchor"])
        self.entry("old", knowledge_state="superseded", aliases=["route anchor"])
        queries = ["entry:STRASSE", "елочный контур", "why route anchor", "DEMO42", "DEMO-420",
                   "What is Y?", "foo.bar", "bar", "STRASSE", "елка", "tune", "DEMO-999 latency",
                   "latency QUIC", "unknown", "Yandex", "the", "Z"]
        expected = {}
        for query in queries:
            for limit in (1, 8):
                for budget in (2000, 16000):
                    options = dict(project="beta", limit=limit, max_chars=budget,
                                   include_hypotheses=True, include_superseded=True)
                    with patch.object(recall, "candidates", legacy.legacy_candidates):
                        expected[(query, limit, budget)] = self.query(query, **options)
        self.build()
        for (query, limit, budget), before in expected.items():
            with self.subTest(query=query, limit=limit, budget=budget):
                after = self.query(query, project="beta", limit=limit, max_chars=budget,
                                   include_hypotheses=True, include_superseded=True)
                self.assertEqual(after, before)
                self.assertEqual(len(compact.rendered(after)), after["budget"]["output_chars"])

    def test_filter_precedes_fts_shortlist_and_keeps_full_provenance(self):
        if not recall.table_exists(self.conn, "entries_fts"):
            self.skipTest("FTS5 unavailable")
        with memory.transaction(self.conn):
            for number in range(170):
                self.entry("noise-{:03d}".format(number), title="routeanchor", project_ids=["beta"])
            self.entry("target", kind="decision", title="routeanchor", tags=["speed"],
                       summary="padding " * 800)
        self.assertNotIn("entry:target", self.refs(self.query("routeanchor", limit=200, max_chars=100000)))
        expression = "project = 'alpha' AND (kind IN ('decision', 'experiment') OR tags HAS 'speed') AND NOT work_state = 'planned'"
        before = self.query("routeanchor", where=expression)
        self.assertEqual(self.refs(before), ["entry:target"])
        self.assertEqual(before["results"][0]["sources"], memory.entry_get(self.conn, "target")["sources"])
        self.build()
        self.assertEqual(self.query("routeanchor", where=expression), before)

    def test_filter_is_mandatory_for_graph_and_expected_diagnostics(self):
        self.entry("anchor", aliases=["scopeanchor"])
        self.entry("outside", project_ids=["beta"])
        self.entry("inside")
        self.edge("outside-link", "entry:anchor", "entry:outside")
        self.edge("inside-link", "entry:anchor", "entry:inside")
        self.build()
        value = self.query("scopeanchor", where="project = 'alpha'", expect="entry:outside")
        self.assertEqual(set(self.refs(value)), {"entry:anchor", "entry:inside"})
        self.assertEqual(value["explain"]["expected"]["outcome"], "filtered_where")
        self.assertTrue(all("entry:outside" not in (edge["from_ref"], edge["to_ref"]) for edge in value["edges"]))

    def test_project_boost_keeps_ambiguity_strict_filter_limits_scope(self):
        self.entity("a", aliases=["Y"])
        self.entity("b", aliases=["Y"])
        self.edge("a-scope", "entity:a", "project:alpha", kind="used_in")
        self.edge("b-scope", "entity:b", "project:beta", kind="used_in")
        self.build()
        boosted = self.query("What is Y?", project="alpha", limit=1)
        self.assertEqual(boosted["ambiguity"]["concept_count"], 2)
        scoped = self.query("What is Y?", where="project = 'alpha'", limit=1)
        self.assertEqual(self.refs(scoped), ["entity:a"])
        self.assertNotIn("ambiguity", scoped)

    def test_index_reduces_visited_rows_without_changing_result(self):
        self.entry("needle", aliases=["routeanchor"])
        self.entity("entity-needle", title="routeanchor")
        with memory.transaction(self.conn):
            for number in range(500):
                self.entry("noise-{:03d}".format(number))
                self.entity("noise-{:03d}".format(number))
        expected = self.query("routeanchor")
        before = self.query("routeanchor", explain=True)["explain"]["counts"]
        self.build()
        actual = self.query("routeanchor")
        after = self.query("routeanchor", explain=True)["explain"]["counts"]
        self.assertEqual(actual, expected)
        self.assertEqual(before["entries_scanned"], 501)
        self.assertEqual(before["entities_scanned"], 501)
        self.assertEqual(after["entries_scanned"], 1)
        self.assertEqual(after["entities_scanned"], 1)

    def test_old_writer_dirty_rows_and_new_writer_refresh_keep_results(self):
        self.entry("changed", aliases=["oldroute"])
        self.entity("changed", aliases=["oldroute"])
        self.build()
        for table in ("entries", "entities"):
            self.conn.execute("UPDATE " + table + " SET aliases = ? WHERE id = ?", (json.dumps(["newroute"]), "changed"))
        value = self.query("newroute")
        self.assertEqual(set(self.refs(value)), {"entry:changed", "entity:changed"})
        self.assertTrue(self.query("oldroute")["no_match"])
        self.entry("changed", aliases=["latest"])
        self.entity("changed", aliases=["latest"])
        self.assertEqual(set(self.refs(self.query("latest"))), {"entry:changed", "entity:changed"})
        self.assertTrue(self.query("newroute")["no_match"])

    def test_expected_probe_and_filter_do_not_change_selection(self):
        self.entry("needle", aliases=["routeanchor"])
        self.entry("missing")
        self.build()
        plain = self.query("routeanchor")
        traced = self.query("routeanchor", expect="entry:missing")
        self.assertEqual(traced["results"], plain["results"])
        self.assertEqual(traced["explain"]["expected"]["outcome"], "weak_lexical")
        self.assertEqual(traced["explain"]["counts"]["expected_entry_probes"], 1)

    def test_search_uses_same_filter_before_limit_and_read_calls_do_not_write(self):
        self.entry("a", title="routeanchor", kind="note")
        self.entry("b", title="routeanchor", kind="decision")
        self.build()
        self.conn.close()
        before = self.db.read_bytes()
        self.conn = memory.connect(self.db, readonly=True)
        self.addCleanup(self.conn.close)
        args = SimpleNamespace(query="routeanchor", project=None, include_superseded=False, limit=1,
                               where="kind = 'decision'")
        value = memory.search(self.conn, args, memory.backend(self.conn))
        self.assertEqual([row["id"] for row in value["entries"]], ["b"])
        self.query("routeanchor", where="kind = 'decision'")
        search_index.status(self.conn)
        self.assertEqual(self.db.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
