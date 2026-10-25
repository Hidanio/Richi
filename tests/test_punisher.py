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

from cli_environment import cli_environment


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
            capture_output=True, text=True, timeout=20, env=cli_environment(self.db.parent)
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

    def test_long_summary_counts_saved_unicode_without_stripping_or_normalizing(self):
        threshold = 8
        expected = {}
        for name, pattern in (("ascii", "abc"), ("cyrillic", "абв"), ("unicode", "👩\u200d🔬e\u0301"),
                              ("whitespace", " \t\nx ")):
            for length in (threshold - 1, threshold, threshold + 1):
                identity = "task:" + name + "-" + str(length)
                summary = (pattern * threshold)[:length]
                self.entry(identity, kind="task", summary=summary)
                if length > threshold:
                    expected["entry:" + identity] = length
        report = self.report(summary_warn_chars=threshold)
        findings = self.findings(report, "long_summary")
        self.assertEqual({item["refs"][0]: item["details"]["summary_chars"] for item in findings}, expected)
        self.assertEqual(report["summary_warn_chars"], threshold)
        self.assertFalse(self.findings(self.report(summary_warn_chars=threshold + 1), "long_summary"))
        self.assertEqual(len(self.findings(self.report(summary_warn_chars=threshold - 1), "long_summary")), 8)

    def test_long_summary_default_and_threshold_validation(self):
        for length in (5999, 6000, 6001):
            self.entry("task:" + str(length), summary="я" * length)
        report = self.report()
        self.assertEqual(report["summary_warn_chars"], 6000)
        self.assertEqual([item["refs"] for item in self.findings(report, "long_summary")], [["entry:task:6001"]])
        self.assertEqual(memory.parser().parse_args(["punisher"]).summary_warn_chars, 6000)
        for value in (1, 1000000):
            self.assertEqual(self.report(summary_warn_chars=value)["summary_warn_chars"], value)
        before = list(self.conn.iterdump())
        for value in (0, -1, 1000001, True, False, 1.0, "6000", None):
            with self.subTest(value=value), self.assertRaisesRegex(memory.MemoryError, "summary_warn_chars.*1.*1000000"):
                self.report(summary_warn_chars=value)
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_long_summary_states_kinds_and_read_arguments_are_explicit(self):
        expected = {}
        for state in sorted(memory.KNOWLEDGE):
            for kind in sorted(memory.KINDS):
                identity = kind + ":" + state
                record = self.entry(identity, kind=kind, knowledge_state=state, work_state="implemented")
                expected["entry:" + identity] = ("entry", record)
            record = self.entity("concept:" + state, knowledge_state=state)
            expected["entity:" + record["id"]] = ("entity", record)
        before = list(self.conn.iterdump())
        report = self.report(summary_warn_chars=1)
        findings = self.findings(report, "long_summary")
        self.assertEqual(len(findings), len(expected))
        self.assertEqual(report["finding_counts"]["long_summary"], len(expected))
        for item in findings:
            family, record = expected[item["refs"][0]]
            self.assertEqual(item["level"], "review" if record["knowledge_state"] == "confirmed" else "info")
            self.assertEqual(item["details"], {
                "summary_chars": len(record["summary"]), "threshold_chars": 1,
                "kind": record["kind"], "knowledge_state": record["knowledge_state"],
                "read_arguments": [family, "get", record["id"]],
            })
            self.assertIn("Read the full record", item["reason"])
            self.assertIn("Length alone does not", item["reason"])
            self.assertNotIn(record["summary"], json.dumps(item, ensure_ascii=False))
            parsed = memory.parser().parse_args(item["details"]["read_arguments"])
            self.assertEqual((parsed.command, parsed.action, parsed.id), (family, "get", record["id"]))
        self.assertEqual(report["health"], self.report(summary_warn_chars=1000000)["health"])
        self.assertEqual(report["health"]["status"], "ok")
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_long_summary_uses_existing_strict_scope_not_graph_neighbors(self):
        self.entry("task:own", summary="Own detailed record")
        self.entry("task:neighbor", project_ids=["beta"], summary="Foreign detailed record")
        self.edge("neighbor", "entry:task:own", "entry:task:neighbor")
        for identity in ("direct", "indirect", "reverse", "guess", "old", "unsourced", "foreign"):
            self.entity(identity)
        self.edge("scope", "entity:direct", "project:alpha", kind="used_in")
        self.edge("indirect", "entity:indirect", "entity:direct")
        self.edge("reverse", "project:alpha", "entity:reverse", kind="used_in")
        self.edge("guess", "entity:guess", "project:alpha", kind="used_in", knowledge_state="hypothesis")
        self.edge("old", "entity:old", "project:alpha", kind="used_in", knowledge_state="superseded")
        self.edge("unsourced", "entity:unsourced", "project:alpha", kind="used_in", knowledge_state="hypothesis", sources=[])
        self.edge("foreign", "entity:foreign", "project:beta", kind="used_in")
        report = self.report(project="alpha", summary_warn_chars=1)
        self.assertEqual([item["refs"] for item in self.findings(report, "long_summary")],
                         [["entity:direct"], ["entry:task:own"]])
        self.assertEqual(report["coverage"]["inspected"]["entries"], 1)
        self.assertEqual(report["coverage"]["inspected"]["entities"], 1)

    def test_long_summary_does_not_expand_scan_caps_or_claim_complete_coverage(self):
        for index in range(5):
            self.entry("task:" + str(index))
            self.entity("concept:" + str(index))
            self.edge("scope:" + str(index), "entity:concept:" + str(index), "project:alpha", kind="used_in")
        with mock.patch.object(punisher, "RECORD_SCAN_LIMIT", 2), mock.patch.object(punisher, "EDGE_SCAN_LIMIT", 1):
            report = self.report(project="alpha", summary_warn_chars=1)
        self.assertFalse(report["coverage"]["complete"])
        self.assertTrue(report["truncated"])
        self.assertEqual(report["coverage"]["limits"], {"records_per_table": 2, "edges": 1})
        self.assertEqual(report["coverage"]["scanned"], {"entries": 2, "entities": 2, "edges": 1, "relations": 0})
        self.assertEqual(report["coverage"]["inspected"]["entities"], 1)
        self.assertEqual(report["coverage"]["eligible_entries"], 5)
        self.assertEqual(report["finding_counts"]["long_summary"], 3)

    def test_long_summary_is_read_only_for_supported_schemas_without_external_io(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                if version == 1:
                    self.conn.close()
                    self.db = Path(self.temp.name) / "schema-one.sqlite3"
                    self.conn = memory.connect(self.db, create=True)
                    self.addCleanup(self.conn.close)
                    self.conn.executescript((BASE / "schema.sql").read_text())
                    self.conn.executescript(memory.FTS_SQL)
                    self.conn.execute("PRAGMA user_version = 1")
                    memory.project_put(self.conn, {"id": "alpha", "name": "alpha", "source": "fixture"})
                else:
                    self.conn.close()
                    self.db = Path(self.temp.name) / ("schema-%d.sqlite3" % version)
                    self.conn = memory.connect(self.db, create=True)
                    self.addCleanup(self.conn.close)
                    if version == 2:
                        memory.execute_script(self.conn, (BASE / 'schema.sql').read_text())
                        memory.execute_script(self.conn, (BASE / 'graph_schema.sql').read_text())
                        memory.execute_script(self.conn, memory.FTS_SQL)
                        self.conn.execute("INSERT INTO metadata VALUES ('search_backend','fts5')")
                        self.conn.execute('PRAGMA user_version=2')
                    else:
                        memory.initialize(self.conn, self.db)
                    memory.project_put(self.conn, {"id": "alpha", "name": "alpha", "source": "fixture"})
                    self.entity("concept:detail", summary="Long entity context")
                    self.edge("scope", "entity:concept:detail", "project:alpha", kind="used_in")
                record = self.entry("task:detail", summary="First detailed finding", work_state="implemented")
                self.entry(record["id"], summary="Updated detailed finding", work_state="merged")
                before = list(self.conn.iterdump())
                with mock.patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                        mock.patch("subprocess.run", side_effect=AssertionError("Git forbidden")), \
                        mock.patch("subprocess.Popen", side_effect=AssertionError("Git forbidden")), \
                        mock.patch("builtins.open", side_effect=AssertionError("source I/O forbidden")), \
                        mock.patch("pathlib.Path.open", side_effect=AssertionError("source I/O forbidden")), \
                        mock.patch("pathlib.Path.exists", side_effect=AssertionError("source I/O forbidden")):
                    first = self.report(summary_warn_chars=1)
                    second = self.report(summary_warn_chars=1)
                self.assertEqual(first, second)
                self.assertEqual(first["schema_version"], version)
                self.assertEqual(first["finding_counts"]["long_summary"], 1 if version == 1 else 2)
                self.assertEqual(first["health"]["status"], "ok")
                self.assertEqual(list(self.conn.iterdump()), before)
                self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], version)

    def test_long_summary_cli_budget_omits_whole_findings_and_preserves_exact_ids(self):
        identities = ["task:000"] + ["task:%03d-" % index + "д" * 191 for index in range(1, 20)]
        expected = {}
        for identity in identities:
            summary = "Подробные условия " + identity + " 👩\u200d🔬" * 10
            self.entry(identity, kind="task", summary=summary)
            expected["entry:" + identity] = len(summary)
        for budget in (2000, 4096, 16000):
            with self.subTest(budget=budget):
                process = subprocess.run(
                    [sys.executable, "-m", "richi", "--db", str(self.db), "punisher", "--project", "alpha",
                     "--as-of", "2026-10-01", "--summary-warn-chars", "10", "--limit", "200", "--max-chars", str(budget)],
                    capture_output=True, text=True, timeout=20, env=cli_environment(self.db.parent))
                self.assertEqual(process.returncode, 0, process.stderr)
                report = json.loads(process.stdout)
                self.assertEqual(report["summary_warn_chars"], 10)
                self.assertEqual(report["budget"]["output_chars"], len(process.stdout))
                self.assertLessEqual(len(process.stdout), budget)
                self.assertEqual(report["candidate_count"], len(identities))
                self.assertEqual(report["finding_counts"], {"long_summary": len(identities)})
                self.assertEqual(report["budget"]["omitted_findings"], len(identities) - len(report["findings"]))
                self.assertTrue(report["coverage"]["complete"])
                if budget == 2000:
                    self.assertTrue(report["truncated"])
                    self.assertGreater(report["budget"]["omitted_findings"], 0)
                else:
                    self.assertTrue(report["findings"])
                for item in report["findings"]:
                    ref = item["refs"][0]
                    self.assertIn(ref, expected)
                    self.assertEqual(item["details"], {
                        "summary_chars": expected[ref], "threshold_chars": 10, "kind": "task",
                        "knowledge_state": "confirmed", "read_arguments": ["entry", "get", ref[len("entry:"):]],
                    })

    def test_long_summary_read_arguments_keep_shell_characters_literal(self):
        identity = "task:literal 'quoted'; $(touch never) `uname`"
        self.entry(identity)
        item = self.findings(self.report(summary_warn_chars=1), "long_summary")[0]
        self.assertEqual(item["details"]["read_arguments"], ["entry", "get", identity])
        self.assertEqual(memory.parser().parse_args(item["details"]["read_arguments"]).id, identity)

    def test_long_summary_read_arguments_allow_ids_starting_with_dash(self):
        for family, create in (("entry", self.entry), ("entity", self.entity)):
            for identity in ("-x", "--history", "--"):
                create(identity)
        for item in self.findings(self.report(summary_warn_chars=1), "long_summary"):
            family, _, identity = item["refs"][0].partition(":")
            arguments = item["details"]["read_arguments"]
            self.assertEqual(arguments, [family, "get", "--", identity])
            self.assertEqual(memory.parser().parse_args(arguments).id, identity)
            process = subprocess.run(
                [sys.executable, "-m", "richi", "--db", str(self.db), *arguments],
                capture_output=True, text=True, timeout=20, env=cli_environment(self.db.parent))
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(json.loads(process.stdout)["id"], identity)

    def test_summary_threshold_cli_rejects_invalid_values(self):
        for value in ("0", "-1", "1000001", "1.5", "true", "invalid"):
            with self.subTest(value=value):
                process = subprocess.run(
                    [sys.executable, "-m", "richi", "--db", str(self.db), "punisher", "--summary-warn-chars", value],
                    capture_output=True, text=True, timeout=20, env=cli_environment(self.db.parent))
                self.assertNotEqual(process.returncode, 0)
                self.assertIn("summary", json.loads(process.stderr)["error"])


class PunisherWorkspaceTests(unittest.TestCase):
    def test_bound_workspace_and_project_path_isolate_same_record_ids(self):
        # The existing launcher fixture uses only copied packages and a temporary
        # registry/home; its synthetic chat IDs never select the caller's store.
        from test_chat_workspaces import ChatCliTests
        fixture = ChatCliTests("test_two_chats_in_same_directory_remain_isolated_after_cli_default_changes")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        repositories = {}
        for name, summary in (("alpha", "Long synthetic summary"), ("beta", "Short")):
            fixture.command("workspace", "create", name)
            fixture.command("chat", "bind", name, chat=name)
            fixture.command("init", chat=name)
            repo = fixture.root / (name + "-repo")
            repo.mkdir()
            repositories[name] = repo
            fixture.command("project", "add", repo, "--id", "shared", chat=name)
            fixture.command("--project-path", repo, "entry", "put", "--json", "-", chat=name, payload={
                "id": "task:shared", "kind": "task", "title": "Same ID in two workspaces", "summary": summary,
                "project_ids": ["shared"], "sources": [{"reference": "test://" + name}],
                "knowledge_state": "confirmed", "verified_at": "2026-10-01"})
        fixture.command("use", "beta")
        for name, expected in (("alpha", 1), ("beta", 0)):
            report = fixture.command("--project-path", repositories[name], "punisher", "--project", "shared",
                                     "--summary-warn-chars", "10", "--as-of", "2026-10-01", chat=name)
            self.assertEqual(report["finding_counts"].get("long_summary", 0), expected)
            self.assertEqual(report["coverage"]["inspected"]["entries"], 1)
            self.assertEqual(fixture.command("chat", "current", chat=name)["workspace"], name)
        rejected = fixture.command("-w", "beta", "punisher", chat="alpha", ok=False)
        self.assertEqual(rejected["code"], "chat_workspace_conflict")
        fixture.command("--project-path", repositories["beta"], "punisher", "--project", "shared", chat="alpha", ok=False)


if __name__ == "__main__":
    unittest.main()
