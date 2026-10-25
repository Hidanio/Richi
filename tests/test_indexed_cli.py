"""Installed CLI contracts for explicit indexing and composed read filters."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from cli_environment import cli_environment


class IndexedCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-indexed-cli-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "synthetic.sqlite3"
        self.cli("init")
        self.cli("project", "upsert", "--json", "-", payload=[{"id": "alpha", "name": "Alpha"},
                                                                  {"id": "beta", "name": "Beta"}])

    def process(self, *arguments, payload=None, ok=True):
        result = subprocess.run([sys.executable, "-m", "richi", "--db", str(self.db), "--compact-json", *arguments],
                                input=json.dumps(payload) if payload is not None else None,
                                capture_output=True, text=True, timeout=30,
                                env=cli_environment(self.root, PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        if ok:
            self.assertEqual(result.stderr, "")
        else:
            self.assertEqual(result.stdout, "")
            self.assertIsInstance(json.loads(result.stderr)["error"], str)
        return result

    def cli(self, *arguments, **options):
        result = self.process(*arguments, **options)
        return json.loads(result.stdout if options.get("ok", True) else result.stderr)

    def entry(self, identity, **changes):
        value = dict(id=identity, kind="note", title="Needle evidence", summary="Synthetic observation.",
                     sources=[{"reference": "test://" + identity}], aliases=["routingneedle"],
                     tags=[], project_ids=["alpha"], knowledge_state="confirmed", verified_at="2026-10-01")
        value.update(changes)
        self.cli("entry", "put", "--json", "-", payload=value)

    def authoritative_rows(self):
        with sqlite3.connect(self.db) as conn:
            names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                     if row[0] in {"entries", "entry_projects", "entry_history", "entry_events", "projects", "relations",
                                   "entities", "entity_history", "graph_edges", "edge_history", "graph_nodes"}]
            return {name: conn.execute("SELECT * FROM " + name + " ORDER BY 1").fetchall() for name in names}

    def test_status_and_unindexed_filtered_reads_are_byte_readonly(self):
        self.entry("task:one")
        before = self.db.read_bytes()
        status = self.cli("search-index", "status")
        self.assertFalse(status["available"])
        self.assertEqual(status["status"], "absent")
        result = self.cli("recall", "routingneedle", "--where", "family='entry' AND project='alpha'")
        self.assertEqual([row["ref"] for row in result["results"]], ["entry:task:one"])
        self.assertEqual(self.db.read_bytes(), before)

    def test_explicit_build_is_repeatable_without_changing_knowledge_or_history(self):
        self.entry("task:one")
        self.entry("task:one", summary="A later preserved observation.")
        before = self.authoritative_rows()
        for _ in range(2):
            status = self.cli("search-index", "build")
            self.assertTrue(status["available"])
            self.assertGreater(status["keys"]["entry"], 0)
            self.assertEqual(self.authoritative_rows(), before)
        indexed_bytes = self.db.read_bytes()
        self.assertTrue(self.cli("search-index", "status")["available"])
        self.cli("recall", "routingneedle", "--where", "kind='note'")
        self.assertEqual(self.db.read_bytes(), indexed_bytes)
        self.assertEqual(self.cli("check")["schema_version"], 2)

    def test_filter_applies_before_limit_and_does_not_override_state_policy(self):
        self.entry("a-noise", project_ids=["beta"])
        self.entry("z-target", kind="decision", tags=["speed"])
        self.entry("y-hypothesis", kind="decision", knowledge_state="hypothesis")
        self.cli("search-index", "build")
        expression = "project='alpha' AND (kind IN ('decision','experiment') OR tags HAS 'speed')"
        selected = self.cli("search", "routingneedle", "--where", expression, "--limit", "1")
        self.assertIn(selected["entries"][0]["id"], {"z-target", "y-hypothesis"})
        recalled = self.cli("recall", "routingneedle", "--where", expression, "--limit", "1")
        self.assertEqual([row["id"] for row in recalled["results"]], ["z-target"])
        self.assertTrue(recalled["selection"]["filter_applied"])
        hypothesis = self.cli("recall", "routingneedle", "--where", "knowledge_state='hypothesis'")
        self.assertTrue(hypothesis["no_match"])
        hypothesis = self.cli("recall", "routingneedle", "--where", "knowledge_state='hypothesis'", "--include-hypotheses")
        self.assertEqual([row["id"] for row in hypothesis["results"]], ["y-hypothesis"])

    def test_invalid_filter_and_project_guard_cannot_mutate_store(self):
        self.entry("task:one")
        before = self.db.read_bytes()
        for expression in ("id='task:one'; DELETE FROM entries", "id='x' OR", "summary='x'", "id IN ()"):
            with self.subTest(expression=expression):
                self.cli("recall", "routingneedle", "--where", expression, ok=False)
                self.assertEqual(self.db.read_bytes(), before)
        self.cli("--project-path", str(self.root / "unregistered-repository"), "search-index", "build", ok=False)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse(self.cli("search-index", "status")["available"])

    def test_timestamp_filters_keep_equal_instants_and_supported_stored_iso_variants(self):
        self.entry("utc", verified_at="2026-10-25T08:00:00Z")
        self.entry("offset", verified_at="2026-10-25T10:00:00+02:00")
        # These forms are accepted by the existing public write API. Their
        # equivalent instants must not silently disappear behind SQLite NULL.
        self.entry("separator", verified_at="2026-10-25X08:00:00+00:00")
        self.entry("offset-seconds", verified_at="2026-10-25T10:00:00+02:00:00")
        self.entry("wide-offset", verified_at="2026-10-26T07:00:00+23:00")
        expressions = ["verified_at = '2026-10-25T09:00:00+01:00'",
                       "verified_at = '2026-10-26T07:00:00+23:00'"]
        expected = {"utc", "offset", "separator", "offset-seconds", "wide-offset"}
        for indexed in (False, True):
            if indexed:
                self.cli("search-index", "build")
            before = self.db.read_bytes()
            for expression in expressions:
                result = self.cli("recall", "routingneedle", "--where", expression)
                self.assertEqual({row["id"] for row in result["results"]}, expected)
            self.assertEqual(self.db.read_bytes(), before)
            self.assertEqual(self.cli("entry", "get", "offset")["verified_at"], "2026-10-25T10:00:00+02:00")


if __name__ == "__main__":
    unittest.main()
