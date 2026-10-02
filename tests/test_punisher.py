"""Punisher behavior against disposable databases; never the user's live memory."""

from argparse import Namespace
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


BASE = Path(__file__).resolve().parents[1] / "src" / "richi"
from richi import punisher
from richi import graph
from richi import memory


class PunisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="memory-punisher-")
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "knowledge.sqlite3"
        self.conn = memory.connect(self.db, create=True)
        self.addCleanup(self.conn.close)
        memory.initialize(self.conn, self.db)
        for project in ("alpha", "beta", "gamma"):
            memory.project_put(self.conn, {"id": project, "name": project, "source": "fixture"})

    def entry(self, identity, **changes):
        value = {"id": identity, "kind": "note", "title": "Record " + identity,
                 "summary": "Evidence for " + identity, "project_ids": ["alpha"],
                 "sources": [{"reference": "test://" + identity}],
                 "knowledge_state": "confirmed", "verified_at": "2026-10-01", "aliases": []}
        value.update(changes)
        memory.entry_put(self.conn, value)
        return value

    def entity(self, identity, **changes):
        value = {"id": identity, "kind": "concept", "title": "Concept " + identity,
                 "summary": "Definition of " + identity, "sources": [{"reference": "test://" + identity}],
                 "knowledge_state": "confirmed", "verified_at": "2026-10-01", "aliases": []}
        value.update(changes)
        graph.put(self.conn, value, "entity", memory)
        return value

    def edge(self, identity, source, target, **changes):
        value = {"id": identity, "from_ref": source, "to_ref": target, "kind": "related_to",
                 "description": "Fixture evidence", "sources": [{"reference": "test://" + identity}],
                 "knowledge_state": "confirmed", "verified_at": "2026-10-01"}
        value.update(changes)
        graph.put(self.conn, value, "edge", memory)
        return value

    def report(self, **changes):
        args = dict(project=None, limit=200, max_chars=100000, stale_days=180, as_of="2026-10-01")
        args.update(changes)
        with sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            conn.execute("BEGIN")
            result = punisher.punisher(conn, Namespace(**args), memory)
            conn.rollback()
        rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        self.assertEqual(result["budget"]["output_chars"], len(rendered))
        self.assertLessEqual(len(rendered), args["max_chars"])
        self.assertEqual(result["candidate_count"], len(result["findings"]) + result["budget"]["omitted_findings"])
        return result

    @staticmethod
    def findings(report, code):
        return [item for item in report["findings"] if item["code"] == code]

    @staticmethod
    def refs(report):
        return {ref for item in report["findings"] for ref in item["refs"]}

    def test_clean_db_is_read_only_deterministic_and_does_not_touch_sources(self):
        self.entry("fine", sources=[{"reference": "/unavailable/private/source", "revision": "abc123"}])
        before = list(self.conn.iterdump())
        with mock.patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                mock.patch("pathlib.Path.exists", side_effect=AssertionError("source I/O forbidden")):
            first = self.report()
            second = self.report()
        self.assertEqual(first, second)
        self.assertEqual(first["health"]["status"], "ok")
        self.assertEqual(first["health"]["integrity"], ["ok"])
        self.assertEqual(first["health"]["counts"]["entries"], 1)
        self.assertEqual(first["findings"], [])
        self.assertEqual(list(self.conn.iterdump()), before)
        self.assertFalse(first["coverage"]["source_content_checked"])

    def test_age_missing_future_and_invalid_dates_have_different_meaning(self):
        self.entry("old", verified_at="2026-04-04")
        self.entry("recent", verified_at="2026-09-30T23:00:00-03:00")
        self.entry("unknown", verified_at=None)
        self.entry("future", verified_at="2026-10-02")
        self.entry("broken-date")
        self.conn.execute("""
UPDATE entries
SET verified_at = ?
WHERE 1=1
    AND id = ?
;""", ("yesterday", "broken-date"))
        result = self.report()
        old = self.findings(result, "verification_age")
        self.assertEqual([item["refs"] for item in old], [["entry:old"]])
        self.assertEqual(old[0]["details"]["age_days"], 180)
        self.assertEqual(old[0]["level"], "review")
        self.assertIn("does not invalidate", old[0]["reason"])
        self.assertEqual(self.findings(result, "verification_date_missing")[0]["refs"], ["entry:unknown"])
        self.assertEqual(self.findings(result, "verification_after_as_of")[0]["refs"], ["entry:future"])
        self.assertEqual(self.findings(result, "invalid_verification_date")[0]["refs"], ["entry:broken-date"])
        self.assertNotIn("entry:recent", self.refs(result))

    def test_nonconfirmed_states_are_info_and_never_age_based_defects(self):
        self.entry("guess", knowledge_state="hypothesis", sources=[], verified_at="2000-01-01")
        self.entry("old-version", knowledge_state="superseded", sources=[], verified_at=None)
        result = self.report()
        self.assertEqual(result["knowledge_states"], {"hypothesis": 1, "superseded": 1})
        self.assertTrue(all(item["level"] == "info" for item in result["findings"]))
        self.assertFalse(self.findings(result, "verification_age"))
        self.assertFalse(self.findings(result, "verification_date_missing"))

    def test_missing_and_malformed_provenance_are_objective_for_confirmed(self):
        self.entry("empty-source")
        self.entry("bad-source")
        self.entry("bad-alias")
        self.conn.executemany("""
UPDATE entries
SET sources = ?
WHERE 1=1
    AND id = ?
;""", [("[]", "empty-source"), ('[{"reference": 42}]', "bad-source")])
        self.conn.execute("""
UPDATE entries
SET aliases = ?
WHERE 1=1
    AND id = ?
;""", ("not-json", "bad-alias"))
        result = self.report()
        self.assertEqual({ref for item in self.findings(result, "missing_sources") for ref in item["refs"]},
                         {"entry:empty-source", "entry:bad-source"})
        self.assertEqual(self.findings(result, "malformed_sources")[0]["refs"], ["entry:bad-source"])
        self.assertEqual(self.findings(result, "malformed_aliases")[0]["refs"], ["entry:bad-alias"])
        self.assertTrue(all(item["level"] == "error" for item in result["findings"]))

    def test_project_scope_is_membership_and_only_direct_confirmed_sourced_used_in(self):
        self.entry("a", verified_at=None)
        self.entry("b", project_ids=["beta"], verified_at=None)
        for identity in ("direct", "indirect", "reverse", "guess", "old", "unsourced", "beta"):
            self.entity(identity, verified_at=None, aliases=["Y"])
        self.edge("scope-a", "entity:direct", "project:alpha", kind="used_in")
        self.edge("indirect-link", "entity:indirect", "entity:direct")
        self.edge("reverse-link", "project:alpha", "entity:reverse", kind="used_in")
        self.edge("guessed-scope", "entity:guess", "project:alpha", kind="used_in", knowledge_state="hypothesis", sources=[])
        self.edge("old-scope", "entity:old", "project:alpha", kind="used_in", knowledge_state="superseded")
        self.edge("unsourced-scope", "entity:unsourced", "project:alpha", kind="used_in")
        self.conn.execute("""
UPDATE graph_edges
SET sources = '[]'
WHERE 1=1
    AND id = 'unsourced-scope'
;""")
        self.edge("scope-b", "entity:beta", "project:beta", kind="used_in")
        self.edge("cross-entry", "entry:a", "entry:b")
        result = self.report(project="alpha")
        records = {ref for ref in self.refs(result) if ref.startswith(("entry:", "entity:"))}
        self.assertEqual(records, {"entry:a", "entity:direct"})
        self.assertEqual(result["coverage"]["inspected"]["entries"], 1)
        self.assertEqual(result["coverage"]["inspected"]["entities"], 1)
        self.assertFalse(self.findings(result, "shared_alias"))
        self.assertFalse(self.findings(result, "orphan_entity"))
        self.assertTrue(result["coverage"]["complete"])

    def test_exact_content_is_candidate_not_merge_and_preserves_project_scopes(self):
        self.entry("first", summary="Same text", aliases=["first"])
        self.entry("second", summary="Same text", project_ids=["beta"], knowledge_state="superseded")
        self.entry("other-kind", summary="Same text", kind="decision")
        self.entry("space", summary="Same  text")
        self.entity("different-family", summary="Same text")
        result = self.report()
        groups = self.findings(result, "exact_content_candidate")
        self.assertEqual(len(groups), 1)
        self.assertEqual(set(groups[0]["refs"]), {"entry:first", "entry:second"})
        members = {member["ref"]: member for member in groups[0]["details"]["members"]}
        self.assertEqual(members["entry:first"]["project_ids"], ["alpha"])
        self.assertEqual(members["entry:second"]["project_ids"], ["beta"])
        self.assertTrue(groups[0]["details"]["mixed_states"])
        self.assertEqual(groups[0]["level"], "review")
        self.assertFalse(self.findings(self.report(project="alpha"), "exact_content_candidate"))

    def test_shared_aliases_are_normalized_ambiguity_not_duplicates(self):
        self.entity("feed-y", aliases=["Y", " y "])
        self.entity("model-y", aliases=["Ｙ"])
        result = self.report()
        groups = self.findings(result, "shared_alias")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["details"]["member_count"], 2)
        self.assertEqual(groups[0]["level"], "info")
        self.assertIn("not duplicate", groups[0]["reason"])
        self.assertFalse(self.findings(result, "exact_content_candidate"))

    def test_orphans_consider_both_directions_and_all_edge_states(self):
        for identity in ("isolated", "incoming", "outgoing"):
            self.entity(identity)
        self.edge("incoming", "project:alpha", "entity:incoming", knowledge_state="hypothesis", sources=[])
        self.edge("outgoing", "entity:outgoing", "project:beta", knowledge_state="superseded")
        result = self.report()
        self.assertEqual([item["refs"] for item in self.findings(result, "orphan_entity")], [["entity:isolated"]])

    def test_confirmed_edges_to_nonconfirmed_records_are_review_not_falsehood(self):
        self.entry("verified")
        self.entry("idea", knowledge_state="hypothesis", sources=[])
        self.edge("supports", "entry:verified", "entry:idea", kind="references")
        result = self.report()
        finding = self.findings(result, "confirmed_edge_nonconfirmed_endpoint")[0]
        self.assertEqual(finding["level"], "review")
        self.assertEqual(finding["details"]["endpoints"], ["entry:idea"])
        self.assertIn("not proof", finding["reason"])

    def test_cross_scope_endpoint_state_is_checked_without_inspecting_foreign_record(self):
        self.entry("verified")
        self.entry("foreign-idea", project_ids=["beta"], knowledge_state="hypothesis", sources=[])
        self.edge("cross-project", "entry:verified", "entry:foreign-idea")
        result = self.report(project="alpha")
        finding = self.findings(result, "confirmed_edge_nonconfirmed_endpoint")[0]
        self.assertEqual(finding["details"]["endpoints"], ["entry:foreign-idea"])
        self.assertNotIn("entry:foreign-idea", self.refs(result))
        self.assertEqual(result["coverage"]["inspected"]["entries"], 1)

    def test_cli_serializes_the_same_bounded_report(self):
        self.entry("verify-cli", verified_at=None)
        process = subprocess.run(
            [sys.executable, "-m", "richi", "--db", str(self.db), "punisher",
             "--project", "alpha", "--as-of", "2026-10-01", "--limit", "3", "--max-chars", "2000"],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result["project"], "alpha")
        self.assertEqual(result["budget"]["output_chars"], len(process.stdout))
        self.assertLessEqual(len(process.stdout), 2000)
        self.assertEqual(result["candidate_count"], 1)

    def test_foreign_key_integrity_is_database_wide_even_with_project_filter(self):
        self.entry("fine")
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("""
INSERT INTO entry_projects (
    entry_id
    , project_id
)
VALUES (?, ?)
;""", ("missing-entry", "beta"))
        self.conn.execute("PRAGMA foreign_keys = ON")
        result = self.report(project="alpha")
        self.assertEqual(result["health"]["scope"], "database")
        self.assertEqual(result["health"]["status"], "error")
        self.assertEqual(result["health"]["foreign_keys"]["count"], 1)
        self.assertEqual(result["health"]["foreign_keys"]["samples"][0]["table"], "entry_projects")

    def test_missing_graph_registry_node_is_found_even_without_fk_violation(self):
        self.entry("unregistered")
        self.conn.execute("""
DELETE FROM graph_nodes
WHERE 1=1
    AND ref = ?
;""", ("entry:unregistered",))
        result = self.report()
        self.assertEqual(result["health"]["foreign_keys"]["count"], 0)
        self.assertEqual(self.findings(result, "graph_node_missing")[0]["refs"], ["entry:unregistered"])

    def test_limit_and_actual_json_budget_preserve_counts_of_omissions(self):
        for index in range(18):
            self.entry("record-" + str(index), title="Длинное название " * 80, verified_at=None,
                       summary="Одинаковый текст", aliases=["Общий термин"])
        limited = self.report(limit=1)
        self.assertEqual(len(limited["findings"]), 1)
        self.assertGreater(limited["candidate_count"], 1)
        self.assertTrue(limited["truncated"])
        for budget in (2000, 2001, 4096, 16000):
            with self.subTest(budget=budget):
                result = self.report(max_chars=budget)
                self.assertTrue(result["truncated"])
                self.assertEqual(result["candidate_count"], limited["candidate_count"])
                self.assertEqual(result["health"]["counts"]["entries"], 18)

    def test_group_caps_are_visible_and_never_change_candidate_to_singleton(self):
        for index in range(12):
            self.entity("meaning-" + str(index), aliases=["same-name"])
        result = self.report()
        group = self.findings(result, "shared_alias")[0]
        self.assertEqual(group["details"]["member_count"], 12)
        self.assertEqual(len(group["refs"]), 8)
        self.assertEqual(group["details"]["omitted_members"], 4)
        self.assertTrue(result["truncated"])

    def test_scan_caps_report_partial_coverage_without_false_orphans(self):
        for index in range(4):
            self.entry("record-" + str(index), verified_at=None)
        self.entity("linked-after-cutoff")
        self.edge("a", "project:alpha", "project:beta")
        self.edge("z", "entity:linked-after-cutoff", "project:alpha")
        with mock.patch.object(punisher, "RECORD_SCAN_LIMIT", 2), mock.patch.object(punisher, "EDGE_SCAN_LIMIT", 1):
            result = self.report()
        self.assertFalse(result["coverage"]["complete"])
        self.assertEqual(result["coverage"]["scanned"]["entries"], 2)
        self.assertEqual(result["coverage"]["eligible_entries"], 4)
        self.assertFalse(self.findings(result, "orphan_entity"))

    def test_legacy_schema_one_can_be_inspected_without_migration(self):
        self.conn.close()
        self.db = Path(self.temp.name) / "legacy.sqlite3"
        self.conn = sqlite3.connect(self.db)
        self.addCleanup(self.conn.close)
        self.conn.executescript((BASE / "schema.sql").read_text())
        self.conn.execute("PRAGMA user_version = 1")
        self.conn.commit()
        result = self.report()
        self.assertEqual(result["schema_version"], 1)
        self.assertTrue(result["coverage"]["complete"])
        self.assertNotIn("entities", result["health"]["counts"])
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_invalid_args_and_unknown_project_fail_without_changes(self):
        before = list(self.conn.iterdump())
        for arguments in ({"limit": 0}, {"limit": True}, {"max_chars": 1999}, {"stale_days": 0},
                          {"as_of": "20261001"}, {"as_of": "2026-02-30"}, {"project": "not-a-project"}):
            with self.subTest(arguments=arguments), self.assertRaises(memory.MemoryError):
                self.report(**arguments)
        self.assertEqual(list(self.conn.iterdump()), before)


if __name__ == "__main__":
    unittest.main()
