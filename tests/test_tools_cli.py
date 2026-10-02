"""Public CLI contracts and source metadata compatibility, on isolated stores."""

import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from richi import memory


class ToolsCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="memory-tools-cli-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "memory.sqlite3"
        self.cli("init")

    def cli(self, *args, ok=True):
        result = subprocess.run([sys.executable, "-B", "-m", "richi",
                                 "--db", str(self.db), *args],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode == 0, ok, result.stderr + result.stdout)
        return json.loads(result.stdout if ok else result.stderr)

    def put(self, family, value, ok=True):
        path = self.root / "input.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return self.cli(family, "put", "--json", str(path), ok=ok)

    def record(self, **fields):
        value = dict(id="fact:contract", kind="fact", title="Contract", summary="Measured contract.",
                     project_ids=[], knowledge_state="confirmed", sources=[{"reference": "test://contract"}],
                     tags=[], aliases=[], verified_at="2026-10-01", expected_updated_at=None)
        value.update(fields)
        return value

    def test_source_anchors_roundtrip_history_and_validation(self):
        source = self.root / "contract.py"
        source.write_bytes(b"version = 1\n")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        anchors = {"reference": str(source), "revision": "git:abc123", "locator": "L1 / version",
                   "sha256": digest.upper(), "observed_at": "2026-10-01"}
        self.put("entry", self.record(sources=[anchors]))
        current = self.cli("entry", "get", "fact:contract")
        self.assertEqual(current["sources"], [dict(anchors, sha256=digest)])
        update = self.record(sources=[dict(anchors, revision="git:def456")],
                             expected_updated_at=current["updated_at"])
        self.put("entry", update)
        history = self.cli("entry", "get", "fact:contract", "--history")["history"]
        self.assertIn("git:abc123", json.dumps(history))
        for invalid in (dict(anchors, sha256="not-a-hash"), dict(anchors, locator="x" * 1025),
                        dict(anchors, revision="one\ntwo"), dict(anchors, invented="field")):
            error = self.put("entry", self.record(id="fact:invalid", sources=[invalid]), ok=False)
            self.assertIn("error", error)
        self.assertIn("error", self.cli("entry", "get", "fact:invalid", ok=False))

    def test_metadata_uses_common_entity_and_edge_validator(self):
        source = {"reference": "test://definition", "revision": "v2", "locator": "api/field", "sha256": "a" * 64}
        for ident in ("term-one", "term-two"):
            self.put("entity", dict(id=ident, kind="concept", title=ident, summary="Definition.",
                                    sources=[source], knowledge_state="confirmed", tags=[], aliases=[]))
        self.put("edge", dict(id="terms-related", from_ref="entity:term-one", to_ref="entity:term-two",
                              kind="references", description="Related definitions.",
                              knowledge_state="confirmed", sources=[source]))
        for family, ident in (("entity", "term-one"), ("edge", "terms-related")):
            value = self.cli(family, "get", ident)
            self.assertEqual(value["sources"], [source])

    def test_readonly_connection_blocks_writes_and_backup_still_works(self):
        conn = memory.connect(self.db, readonly=True)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE forbidden (id INTEGER)")
        saved = self.cli("backup", "--output", str(self.root / "copy.sqlite3"))
        self.assertEqual(saved["status"], "created")
        self.assertEqual(self.cli("check")["status"], "ok")

    def test_diagnostic_commands_dispatch_and_errors_are_json(self):
        self.put("entry", self.record())
        report = self.cli("punisher", "--as-of", "2026-10-01", "--max-chars", "2000")
        self.assertIn("health", report)
        self.assertLessEqual(len(json.dumps(report, ensure_ascii=False, indent=2)) + 1, 2000)
        traced = self.cli("recall", "fact:contract", "--expect", "entry:fact:contract")
        self.assertIn("explain", traced)
        for args in (("punisher", "--stale-days", "0"), ("punisher", "--as-of", "yesterday"),
                     ("recall", "Contract", "--expect", "bad-ref"),
                     ("sources", "check", "--manifest", str(self.root / "missing.json"))):
            self.assertIn("error", self.cli(*args, ok=False))


if __name__ == "__main__":
    unittest.main()
