"""Observed recall diagnostics, using disposable databases and fixed questions."""
import json
import sys
from types import SimpleNamespace
import unittest

import test_recall as fixtures

from richi import compact
from richi import memory
from richi import recall


class RecallExplainTests(unittest.TestCase):
    setUp = fixtures.RecallTests.setUp
    process = fixtures.RecallTests.process
    cli = fixtures.RecallTests.cli
    put = fixtures.RecallTests.put
    entry = fixtures.RecallTests.entry
    entity = fixtures.RecallTests.entity
    edge = fixtures.RecallTests.edge

    def run_recall(self, query, **options):
        args = SimpleNamespace(query=query, limit=8, max_chars=16000)
        for key, value in options.items():
            setattr(args, key, value)
        conn = memory.connect(self.db)
        try:
            value = recall.recall(conn, args, memory)
        finally:
            conn.close()
        self.assertLessEqual(len(compact.rendered(value)), args.max_chars)
        self.assertEqual(value["budget"]["output_chars"], len(compact.rendered(value)))
        return value

    def test_legacy_namespace_and_disabled_flags_keep_normal_response(self):
        self.put("entry", [self.entry("first", aliases=["recallneedle"]),
                           self.entry("second", aliases=["recallneedle"])])
        legacy = self.run_recall("recallneedle")
        disabled = self.run_recall("recallneedle", explain=False, expect=None)
        self.assertEqual(legacy, disabled)
        self.assertNotIn("explain", legacy)
        explained = self.run_recall("recallneedle", explain=True)
        for field in ("results", "edges", "selection", "candidate_count", "no_match"):
            self.assertEqual(explained[field], legacy[field])
        counts = explained["explain"]["counts"]
        self.assertEqual(counts["entries_scanned"], 2)
        self.assertEqual(counts["fts_shortlisted"], 2)
        self.assertEqual(counts["direct_candidates"], 2)
        self.assertEqual(counts["selected"], 2)
        self.assertEqual(counts["returned"], 2)
        self.assertEqual(counts["budget_omitted"], 0)

    def test_missing_ref_and_no_candidates_are_not_budget_omissions(self):
        for reference in ("entry:absent", "entity:absent"):
            with self.subTest(reference=reference):
                result = self.run_recall("unfindable", expect=reference)
                self.assertTrue(result["no_match"])
                self.assertEqual(result["explain"]["expected"]["outcome"], "missing")
                self.assertEqual(result["explain"]["counts"]["candidates"], 0)
                self.assertEqual(result["explain"]["counts"]["budget_omitted"], 0)

    def test_cli_expect_implies_explain_and_bounds_serialized_stdout(self):
        self.put("entry", self.entry("cli-record", aliases=["clineedle"]))
        process = self.process("recall", "clineedle", "--expect", "entry:cli-record",
                               "--max-chars", "2000")
        value = json.loads(process.stdout)
        self.assertLessEqual(len(process.stdout), 2000)
        self.assertEqual(value["budget"]["output_chars"], len(process.stdout))
        self.assertIn(value["explain"]["expected"]["outcome"], {"returned", "budget"})
        plain = self.cli("recall", "clineedle")
        self.assertNotIn("explain", plain)

    def test_scan_backend_reports_no_fts_shortlist(self):
        self.put("entry", self.entry("scan-record", title="scanneedle"))
        conn = memory.connect(self.db)
        try:
            conn.execute("DROP TABLE entries_fts")
        finally:
            conn.close()
        value = self.run_recall("scanneedle", expect="entry:scan-record")
        self.assertEqual(value["selection"]["search_backend"], "scan")
        self.assertEqual(value["explain"]["expected"]["outcome"], "returned")
        self.assertEqual(value["explain"]["counts"]["fts_rows_fetched"], 0)
        self.assertEqual(value["explain"]["counts"]["fts_shortlisted"], 0)
        self.assertEqual(value["explain"]["counts"]["entries_scanned"], 1)

    def test_filtered_knowledge_states_and_explicit_opt_in(self):
        self.put("entry", self.entry("old", aliases=["stateword"], knowledge_state="superseded"))
        self.put("entity", self.entity("guess", aliases=["stateword"], knowledge_state="hypothesis"))
        for reference, state, flag in (("entry:old", "superseded", "include_superseded"),
                                       ("entity:guess", "hypothesis", "include_hypotheses")):
            with self.subTest(reference=reference):
                hidden = self.run_recall("stateword", expect=reference)
                expected = hidden["explain"]["expected"]
                self.assertEqual(expected["outcome"], "filtered_state")
                self.assertEqual(expected["knowledge_state"], state)
                self.assertEqual(hidden["explain"]["counts"]["entries_scanned"], 0)
                self.assertEqual(hidden["explain"]["counts"]["entities_scanned"], 0)
                included = self.run_recall("stateword", expect=reference, **{flag: True})
                self.assertEqual(included["explain"]["expected"]["outcome"], "returned")

    def test_direct_guards_report_the_actual_rejection_stage(self):
        self.put("entry", self.entry("latency-note", title="latency observation"))
        for query, reason in (("unrelatedunknown", "weak_lexical"),
                              ("latency QUIC", "subject_guard"),
                              ("latency Z", "short_subject_guard"),
                              ("DEMO-99999 latency", "jira_guard")):
            with self.subTest(query=query):
                value = self.run_recall(query, expect="entry:latency-note")
                self.assertEqual(value["explain"]["expected"]["direct_stage"], reason)
                self.assertEqual(value["explain"]["expected"]["outcome"], reason)
                self.assertEqual(value["explain"]["counts"]["direct_rejected"], {reason: 1})

    def test_fts_shortlist_reports_observed_boundary_without_second_search(self):
        self.put("entry", [self.entry("short-{:03d}".format(i), title="shortlistneedle")
                           for i in range(161)])
        conn = memory.connect(self.db)
        statements = []
        conn.set_trace_callback(statements.append)
        try:
            value = recall.recall(conn, SimpleNamespace(query="shortlistneedle", limit=5,
                                  max_chars=16000, expect="entry:short-160"), memory)
        finally:
            conn.close()
        self.assertEqual(value["explain"]["expected"]["outcome"], "fts_not_shortlisted")
        self.assertTrue(value["explain"]["fts_shortlist_truncated"])
        counts = value["explain"]["counts"]
        self.assertEqual(counts["fts_rows_fetched"], 161)
        self.assertEqual(counts["fts_shortlisted"], 160)
        self.assertEqual(counts["entries_scanned"], 161)
        self.assertEqual(counts["direct_candidates"], 160)
        self.assertEqual(sum("entries_fts MATCH" in sql for sql in statements), 1)

    def test_rank_limit_is_distinct_from_budget(self):
        self.put("entry", [self.entry(identity, aliases=["rankingneedle"])
                           for identity in ("first", "second", "third")])
        value = self.run_recall("rankingneedle", limit=1, expect="entry:third")
        expected = value["explain"]["expected"]
        self.assertEqual(expected["outcome"], "rank_or_limit")
        self.assertEqual(expected["direct_rank"], 3)
        self.assertIsNone(expected["selected_position"])
        self.assertEqual(value["explain"]["counts"]["budget_omitted"], 0)

    def test_lexical_rejection_can_still_return_through_graph(self):
        self.put("entry", [self.entry("seed", aliases=["graphneedle"]), self.entry("neighbor")])
        self.put("edge", self.edge("seed-neighbor", "entry:seed", "entry:neighbor"))
        value = self.run_recall("graphneedle", expect="entry:neighbor")
        normal = self.run_recall("graphneedle")
        self.assertEqual(value["results"], normal["results"])
        expected = value["explain"]["expected"]
        self.assertEqual(expected["direct_stage"], "weak_lexical")
        self.assertEqual(expected["graph_stage"], "candidate")
        self.assertEqual(expected["outcome"], "returned")
        self.assertIsNone(expected["direct_rank"])
        self.assertEqual(expected["returned_position"], 2)
        counts = value["explain"]["counts"]
        self.assertEqual(counts["graph_seeds"], 1)
        self.assertEqual(counts["graph_links_examined"], 1)
        self.assertEqual(counts["graph_candidates"], 1)

    def test_graph_candidate_can_be_omitted_by_limit(self):
        self.put("entry", [self.entry("seed", aliases=["graphlimitneedle"]), self.entry("neighbor")])
        self.put("edge", self.edge("seed-neighbor", "entry:seed", "entry:neighbor"))
        value = self.run_recall("graphlimitneedle", limit=1, expect="entry:neighbor")
        expected = value["explain"]["expected"]
        self.assertEqual(expected["direct_stage"], "weak_lexical")
        self.assertEqual(expected["graph_stage"], "candidate")
        self.assertEqual(expected["outcome"], "rank_or_limit")

    def test_tiny_budget_long_query_ref_and_huge_source_keep_honest_outcome(self):
        identity = "я" * 200
        self.put("entry", self.entry(identity, aliases=["budgetneedle"],
                 sources=[{"reference": "https://example.test/" + "x" * 10000}]))
        query = "budgetneedle" + " what" * 390
        value = self.run_recall(query, expect="entry:" + identity, max_chars=2000)
        self.assertFalse(value["no_match"])
        self.assertEqual(value["results"], [])
        expected = value["explain"]["expected"]
        self.assertEqual(expected["ref"], "entry:" + identity)
        self.assertEqual(expected["outcome"], "budget")
        self.assertEqual(expected["knowledge_state"], "confirmed")
        self.assertEqual(expected["selected_position"], 1)
        self.assertTrue(value["truncated"])

    def test_tiny_budget_preserves_concept_ambiguity_and_hypothesis_state(self):
        self.put("entity", [self.entity("y-" + suffix, aliases=["Y"], knowledge_state="hypothesis",
                            sources=[{"reference": "https://example.test/" + "x" * 10000}])
                            for suffix in ("alpha", "beta", "gamma")])
        value = self.run_recall("Что такое Y?", expect="entity:y-alpha", include_hypotheses=True,
                               max_chars=2000)
        self.assertTrue(value["ambiguity"]["detected"])
        self.assertEqual(value["ambiguity"]["concept_count"], 3)
        self.assertEqual(value["explain"]["expected"]["knowledge_state"], "hypothesis")
        self.assertEqual(value["explain"]["expected"]["outcome"], "budget")
        self.assertFalse(value["no_match"])

    def test_invalid_and_oversized_expected_refs_fail_before_search(self):
        for reference in ("native-id", "project:alpha", "entry:", "entry:" + "a" * 201, "entry:x\ny"):
            with self.subTest(reference=reference), self.assertRaises(memory.MemoryError):
                self.run_recall("needle", expect=reference)


if __name__ == "__main__":
    unittest.main()
