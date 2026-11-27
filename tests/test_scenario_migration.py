"""Schema 4 migration tests: upgrading from v1, v2 and v3 with pre-migration backups."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from richi import graph, memory, scenario_runs


class ScenarioMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="richi-scn-migrate-")
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "memory.sqlite3"
        self.conn = memory.connect(self.db, create=True)
        self.addCleanup(self.conn.close)

    def seed_schema_3(self):
        memory.execute_script(self.conn, (memory.BASE / "schema.sql").read_text(encoding="utf-8"))
        memory.execute_script(self.conn, (memory.BASE / "graph_schema.sql").read_text(encoding="utf-8"))
        self.conn.execute("INSERT INTO metadata(key, value) VALUES ('search_backend', 'scan')")
        self.conn.execute("PRAGMA user_version = 3")
        with memory.transaction(self.conn):
            memory.project_put(self.conn, {"id": "demo", "name": "Demo"})
            memory.entry_put(self.conn, {
                "id": "note:keep-me", "kind": "note", "title": "Keep me", "summary": "Preserved across v3->v4.",
                "project_ids": ["demo"], "knowledge_state": "confirmed",
                "sources": [{"reference": "synthetic:1"}], "expected_updated_at": None,
            })

    def test_v3_migrates_to_v4_with_backup_and_is_idempotent(self):
        self.seed_schema_3()
        before = list(self.conn.iterdump())
        # Scenario runs refuse v3 with a clear upgrade code.
        with self.assertRaises(memory.MemoryError) as caught:
            scenario_runs.require_v4(self.conn, memory)
        self.assertEqual(caught.exception.code, "schema_upgrade_required")
        # Explicit migrate backs up v3 and creates v4 tables.
        result = graph.migrate(self.conn, self.db, memory)
        self.assertEqual(result["status"], "migrated")
        self.assertEqual(result["from_version"], 3)
        self.assertEqual(result["schema_version"], 4)
        backup = Path(result["backup"])
        self.assertTrue(backup.is_file())
        with closing(sqlite3.connect(str(backup))) as saved:
            self.assertEqual(saved.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(list(saved.iterdump()), before)
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 4)
        tables = {row[0] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"scenario_runs", "scenario_steps", "scenario_events"}.issubset(tables))
        self.assertEqual(memory.entry_get(self.conn, "note:keep-me")["title"], "Keep me")
        # Repeat migrate is a no-op with no second backup.
        again = graph.migrate(self.conn, self.db, memory)
        self.assertEqual(again, {"status": "existing", "schema_version": 4, "database": str(self.db)})

    def test_fresh_initialize_creates_schema_4(self):
        res = memory.initialize(self.conn, self.db)
        self.assertEqual(res["schema_version"], 4)
        scenario_runs.require_v4(self.conn, memory)


if __name__ == "__main__":
    unittest.main()
