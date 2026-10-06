"""Explicit source watches detect drift without treating it as semantic truth."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1] / "src" / "richi"
from richi import graph
from richi import memory
from richi import source_watch as watch

from cli_environment import cli_environment


class SourceWatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="source-watch-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "memory with spaces.sqlite3"
        self.file = self.root / "checked source.txt"
        self.file.write_text("measured prototype, no rollout claim\n", encoding="utf-8")
        self.conn = memory.connect(self.db, create=True)
        self.addCleanup(self.conn.close)
        memory.initialize(self.conn, self.db)
        for identity in ("a", "b"):
            memory.project_put(self.conn, {"id": identity, "name": identity})
        self.put_entry("one")
        self.put_entry("two", project_ids=["b"])
        self.manifest = self.root / "baseline.json"

    def put_entry(self, identity, **changes):
        payload = dict(id=identity, kind="experiment", title="Experiment " + identity,
                       summary="Observed result with known limitations", project_ids=["a"],
                       sources=[{"reference": str(self.file)}], knowledge_state="confirmed",
                       verified_at="2026-09-30T10:00:00Z")
        payload.update(changes)
        memory.entry_put(self.conn, payload)

    def put_edge(self, identity="one-two", **changes):
        payload = dict(id=identity, from_ref="entry:one", to_ref="entry:two", kind="informs",
                       description="Measurement informed the next experiment",
                       sources=[{"reference": str(self.file)}], knowledge_state="confirmed")
        payload.update(changes)
        graph.put(self.conn, payload, "edge", memory)

    def snap(self, **kwargs):
        options = dict(refs=["entry:one"], output=self.manifest)
        options.update(kwargs)
        return watch.snapshot(self.conn, db_path=self.db, **options)

    def check(self, **kwargs):
        return watch.check(self.conn, db_path=self.db, manifest=self.manifest, **kwargs)

    def data(self):
        return json.loads(self.manifest.read_text())

    def test_readonly_snapshot_preserves_full_sources_and_database(self):
        declared = dict(reference=str(self.file), label="Authoritative local report",
                        type="measurement", observed_at="2026-09-30T10:00:00Z",
                        revision="git:abc123", locator="paragraph two (metadata only)",
                        sha256=hashlib.sha256(self.file.read_bytes()).hexdigest())
        self.put_entry("one", sources=[declared])
        before = list(self.conn.iterdump())
        readonly = memory.connect(self.db, readonly=True)
        self.addCleanup(readonly.close)
        result = watch.snapshot(readonly, db_path=self.db, refs=["entry:one"], output=self.manifest)
        self.assertEqual(result["status"], "created")
        source = self.data()["sources"][0]
        self.assertEqual(source["evidence"], [{"owner": "entry:one", "source": declared}])
        self.assertEqual(source["locator"], "")
        checked = watch.check(readonly, db_path=self.db, manifest=self.manifest)
        self.assertEqual(checked["status"], "unchanged")
        self.assertIn("do not establish", checked["notice"])
        self.assertEqual(before, list(self.conn.iterdump()))
        self.assertFalse(readonly.in_transaction)

    def test_hash_detects_file_change_then_missing_source_is_unavailable(self):
        self.snap()
        self.file.write_text("another observation\n")
        changed = self.check()
        self.assertTrue(changed["has_changes"])
        self.assertEqual(changed["records"]["changed"], [])
        self.assertEqual(changed["sources"][0]["status"], "changed")
        self.file.unlink()
        missing = self.check()
        self.assertEqual(missing["status"], "unavailable")
        self.assertFalse(missing["coverage"]["all_current_local_sources_hashed"])

    def test_unavailable_baseline_never_proves_unchanged(self):
        self.file.unlink()
        result = self.snap()
        self.assertFalse(result["all_sources_hashed"])
        self.assertEqual(self.check()["status"], "unavailable")
        self.file.write_text("now exists")
        self.assertEqual(self.check()["sources"][0]["reason"], "source_now_available")

    def test_nullable_observed_at_roundtrips_from_supported_source_schema(self):
        self.put_entry("one", sources=[{"reference": str(self.file), "observed_at": None}])
        self.snap()
        self.assertIsNone(self.data()["sources"][0]["evidence"][0]["source"]["observed_at"])
        self.assertEqual(self.check()["status"], "unchanged")

    def test_urls_relative_paths_and_ids_are_not_fetched_or_resolved(self):
        references = ["https://example.invalid/report", "relative/report.txt", "entry:other"]
        self.put_entry("one", sources=[{"reference": ref} for ref in references])
        with patch.object(watch.os, "open", wraps=os.open) as opened:
            self.snap()
        opened_paths = [str(call.args[0]) for call in opened.call_args_list]
        self.assertFalse(set(references) & set(opened_paths))
        checked = self.check()
        self.assertEqual(checked["status"], "unsupported")
        self.assertEqual(checked["counts"]["unsupported"], 3)

    def test_locators_preserve_exact_reference_and_literal_filename_wins(self):
        literal = self.root / "literal:12"
        literal.write_text("this is a filename")
        references = [str(self.file) + ":4:2", str(self.file) + "#L2-L8", str(literal)]
        self.put_entry("one", sources=[{"reference": ref} for ref in references])
        self.snap()
        observations = {source["reference"]: source for source in self.data()["sources"]}
        self.assertEqual(observations[references[0]]["path"], str(self.file))
        self.assertEqual(observations[references[0]]["locator"], ":4:2")
        self.assertEqual(observations[references[1]]["locator"], "#L2-L8")
        self.assertEqual(observations[str(literal)]["path"], str(literal))
        self.assertEqual(observations[str(literal)]["locator"], "")
        self.assertEqual(self.check()["status"], "unchanged")

    def test_nonregular_sources_are_bounded_and_unavailable(self):
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        self.put_entry("one", sources=[{"reference": str(fifo)}, {"reference": str(self.root)}])
        self.snap()
        self.assertEqual({source["reason"] for source in self.data()["sources"]}, {"not_regular_file"})
        self.assertEqual(self.check()["status"], "unavailable")

    def test_per_file_and_total_read_limits_do_not_claim_complete_hashes(self):
        self.snap(max_file_bytes=1)
        self.assertEqual(self.data()["sources"][0]["reason"], "file_size_limit")
        self.manifest.unlink()
        second = self.root / "second.txt"
        second.write_bytes(b"b" * 40)
        self.file.write_bytes(b"a" * 40)
        self.put_entry("one", sources=[{"reference": str(self.file)}, {"reference": str(second)}])
        with patch.object(watch, "MAX_TOTAL_BYTES", 50):
            self.snap(max_file_bytes=50)
            result = self.check()
        self.assertLessEqual(result["bytes_read"], 50)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["counts"]["unchanged"], 1)
        self.assertEqual(result["counts"]["unavailable"], 1)

    def test_path_replacement_during_hash_does_not_create_false_baseline(self):
        actual_read = os.read
        replaced = False
        def replace_once(fd, size):
            nonlocal replaced
            data = actual_read(fd, size)
            if not replaced:
                replacement = self.root / "replacement"
                replacement.write_text("different inode")
                replacement.replace(self.file)
                replaced = True
            return data
        with patch.object(watch.os, "read", side_effect=replace_once):
            self.snap()
        self.assertEqual(self.data()["sources"][0]["reason"], "file_changed_during_read")

    def test_project_scope_detects_added_entries_removed_memberships_and_used_in(self):
        graph.put(self.conn, dict(id="term", kind="concept", title="Scoped meaning",
                                 summary="Definition in a", knowledge_state="confirmed",
                                 sources=[{"reference": str(self.file)}]), "entity", memory)
        self.put_edge("scope", from_ref="entity:term", to_ref="project:a", kind="used_in")
        self.put_edge()
        self.snap(refs=None, project="a")
        self.assertEqual({item["ref"] for item in self.data()["records"]},
                         {"project:a", "entry:one", "entity:term"})
        self.put_entry("new")
        self.put_entry("one", project_ids=["b"])
        changed = self.check()
        self.assertEqual(changed["records"]["added"], ["entry:new"])
        self.assertEqual(changed["records"]["removed"], ["entry:one"])
        self.assertEqual(changed["status"], "changed")

    def test_full_cards_and_incident_edge_set_detect_all_revisions(self):
        self.put_edge()
        self.snap()
        self.put_entry("one", project_ids=["b"], aliases=["new alias"])
        self.put_edge(description="Revised explanation, preserving source")
        self.put_edge("incoming", from_ref="entry:two", to_ref="entry:one")
        changed = self.check()
        self.assertEqual(changed["records"]["changed"], ["entry:one"])
        self.assertEqual(changed["incident_edges"]["changed"], ["one-two"])
        self.assertEqual(changed["incident_edges"]["added"], ["incoming"])
        # Emulate a row disappearing outside the supported append-only CLI.
        # This corruption is deliberately confined to the disposable fixture.
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("DELETE FROM graph_edges WHERE 1=1 AND id = ?", ("one-two",))
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.assertEqual(self.check()["incident_edges"]["removed"], ["one-two"])

    def test_unselected_cards_and_sources_are_not_read(self):
        missing = self.root / "do-not-read"
        self.put_entry("two", project_ids=["b"], sources=[{"reference": str(missing)}])
        self.put_edge()
        self.snap()
        self.assertEqual([source["reference"] for source in self.data()["sources"]], [str(self.file)])
        self.put_entry("two", project_ids=["b"], summary="Changed neighbor, not selected")
        self.assertEqual(self.check()["status"], "unchanged")

    def test_output_no_clobber_db_existing_symlinks_and_creation_race(self):
        self.snap()
        original = self.manifest.read_bytes()
        for target in (self.db, self.manifest):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.snap(output=target)
        broken = self.root / "broken.json"
        broken.symlink_to(self.root / "absent")
        with self.assertRaises(ValueError):
            self.snap(output=broken)
        racing = self.root / "racing.json"
        real_link = os.link
        def create_competitor(source, target):
            Path(target).write_text("concurrent output")
            return real_link(source, target)
        with patch.object(watch.os, "link", side_effect=create_competitor), self.assertRaises(ValueError):
            self.snap(output=racing)
        self.assertEqual(racing.read_text(), "concurrent output")
        self.assertEqual(self.manifest.read_bytes(), original)
        self.assertEqual(list(self.root.glob(".source-watch-*")), [])

    def test_selection_limits_refuse_incomplete_snapshot(self):
        with patch.object(watch, "MAX_REFS", 1), self.assertRaises(ValueError):
            self.snap(refs=None, project="a")
        self.put_edge()
        with patch.object(watch, "MAX_EDGES", 0), self.assertRaises(ValueError):
            self.snap()
        with patch.object(watch, "MAX_SOURCES", 0), self.assertRaises(ValueError):
            self.snap()
        self.assertFalse(self.manifest.exists())
        with self.assertRaises(ValueError):
            self.snap(refs=["entry:missing"])
        with self.assertRaises(ValueError):
            self.snap(project="a")

    def test_scope_growth_over_limit_refuses_incomplete_check(self):
        self.snap(refs=None, project="a")
        self.put_entry("new")
        with patch.object(watch, "MAX_REFS", 2), self.assertRaises(ValueError):
            self.check()

    def test_schema_one_is_observed_without_migration(self):
        legacy = self.root / "legacy.sqlite3"
        with sqlite3.connect(legacy) as conn:
            conn.executescript((BASE / "schema.sql").read_text())
            conn.execute("PRAGMA user_version = 1")
        conn = memory.connect(legacy)
        self.addCleanup(conn.close)
        memory.project_put(conn, {"id": "legacy", "name": "Legacy"})
        before = list(conn.iterdump())
        watch.snapshot(conn, db_path=legacy, refs=["project:legacy"], output=self.manifest)
        self.assertEqual(watch.check(conn, db_path=legacy, manifest=self.manifest)["status"], "unchanged")
        self.assertEqual(before, list(conn.iterdump()))
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_caller_transaction_is_not_committed_or_rolled_back(self):
        self.conn.execute("BEGIN")
        self.put_entry("one", summary="Transaction-owned change")
        self.snap()
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.check()["status"], "unchanged")
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual(self.check()["records"]["changed"], ["entry:one"])

    def test_malformed_and_oversized_manifests_fail_closed(self):
        self.snap()
        original = self.data()
        malformed = [[], {}, dict(original, version=True), dict(original, sources="bad"),
                     dict(original, records=[]), dict(original, database_schema_version=99)]
        bad_source = copy.deepcopy(original)
        bad_source["sources"][0]["sha256"] = "invalid"
        malformed.append(bad_source)
        bad_owner = copy.deepcopy(original)
        bad_owner["sources"][0]["owners"] = ["entry:unselected"]
        malformed.append(bad_owner)
        bad_locator = copy.deepcopy(original)
        bad_locator["sources"][0]["locator"] = "#L5"
        malformed.append(bad_locator)
        bad_evidence = copy.deepcopy(original)
        bad_evidence["sources"][0]["evidence"] = []
        malformed.append(bad_evidence)
        for value in malformed:
            with self.subTest(value=str(value)[:80]):
                self.manifest.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    self.check()
        for raw in (b'{"version":1,"version":2}', b'{"x":NaN}', b'not JSON', b'\xff'):
            self.manifest.write_bytes(raw)
            with self.assertRaises(ValueError):
                self.check()
        self.manifest.write_text(json.dumps(original))
        with patch.object(watch, "MAX_MANIFEST_BYTES", 100), self.assertRaises(ValueError):
            self.check()
        self.manifest.unlink()
        os.mkfifo(self.manifest)
        with self.assertRaises(ValueError):
            self.check()

    def test_report_budget_preserves_counts_and_explicit_omissions(self):
        self.put_entry("one", sources=[{"reference": str(self.file), "label": "доказательство " * 1000}])
        self.snap()
        self.file.write_text("changed")
        full = self.check()
        bounded = self.check(max_chars=2000)
        self.assertLessEqual(len(json.dumps(bounded, ensure_ascii=False, indent=2) + "\n"), 2000)
        self.assertEqual(bounded["status"], full["status"])
        self.assertEqual(bounded["counts"], full["counts"])
        self.assertTrue(bounded["budget"]["truncated"])
        self.assertEqual(bounded["budget"]["sources_omitted"], 1)
        self.assertTrue(bounded["truncated"])
        self.assertEqual(bounded["budget"]["output_chars"], len(json.dumps(bounded, ensure_ascii=False, indent=2) + "\n"))

    def test_cli_snapshot_and_check_are_readonly_and_bounded(self):
        for command in (("snapshot", "--ref", "entry:one", "--output", str(self.manifest)),
                        ("check", "--manifest", str(self.manifest), "--max-chars", "2000")):
            proc = subprocess.run([sys.executable, "-m", "richi", "--db", str(self.db),
                                   "sources", *command], capture_output=True, text=True, timeout=10, env=cli_environment(self.db.parent))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
        self.assertEqual(result["status"], "unchanged")
        self.assertLessEqual(len(proc.stdout), 2000)


if __name__ == "__main__":
    unittest.main()
