"""Schema 4 migration tests: upgrading from v1, v2 and v3 with pre-migration backups."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from richi import graph, memory, rules, scenario_runs, scenarios


class ScenarioMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="richi-scn-migrate-")
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "memory.sqlite3"
        self.conn = memory.connect(self.db, create=True)
        self.addCleanup(self.conn.close)

    def _seed_base(self, version):
        memory.execute_script(self.conn, (memory.BASE / "schema.sql").read_text(encoding="utf-8"))
        if version >= 2:
            graph_sql = (memory.BASE / "graph_schema.sql").read_text(encoding="utf-8")
            if version == 2:
                graph_sql = graph_sql.replace(",'tool'", "")
            memory.execute_script(self.conn, graph_sql)
        self.conn.execute("INSERT INTO metadata(key, value) VALUES ('search_backend', 'scan')")
        self.conn.execute("PRAGMA user_version = %d" % version)
        with memory.transaction(self.conn):
            memory.project_put(self.conn, {"id": "demo", "name": "Demo"})
            memory.entry_put(self.conn, {
                "id": "note:keep-me", "kind": "note", "title": "Keep me",
                "summary": "Preserved across v%d->v4." % version,
                "project_ids": ["demo"], "knowledge_state": "confirmed",
                "sources": [{"reference": "synthetic:1"}], "expected_updated_at": None,
            })
            # Write a scenario definition BEFORE migration so scenario-log:* metadata rows exist (R2-J15).
            scenarios.copy_template(self.conn, memory, "review", new_id="pre-migrate-scn",
                                    scope="global", author=rules.Author("codex:pre", "codex"))

    def seed_schema_3(self):
        self._seed_base(3)

    def test_v1_v2_v3_migrate_to_v4_preserving_data_and_backfilling_scenario_logs(self):
        for start_version in (1, 2, 3):
            with self.subTest(from_version=start_version):
                sub_dir = Path(self.temp.name) / ("v%d" % start_version)
                sub_dir.mkdir()
                self.db = sub_dir / "memory.sqlite3"
                self.conn.close()
                self.conn = memory.connect(self.db, create=True)
                self._seed_base(start_version)
                pre_log = scenarios.log(self.conn, scenario_id="pre-migrate-scn", entries=10)["entries"]
                self.assertEqual(len(pre_log), 1)
                self.assertEqual(pre_log[0]["action"], "scenario_created")
                before = list(self.conn.iterdump())
                # Scenario runs refuse pre-v4 with a clear upgrade code.
                with self.assertRaises(memory.MemoryError) as caught:
                    scenario_runs.require_v4(self.conn, memory)
                self.assertEqual(caught.exception.code, "schema_upgrade_required")
                # Explicit migrate backs up and creates v4 tables + backfills scenario-log:*.
                result = graph.migrate(self.conn, self.db, memory)
                self.assertEqual(result["status"], "migrated")
                self.assertEqual(result["from_version"], start_version)
                self.assertEqual(result["schema_version"], 4)
                backup = Path(result["backup"])
                self.assertTrue(backup.is_file())
                with closing(sqlite3.connect(str(backup))) as saved:
                    self.assertEqual(saved.execute("PRAGMA user_version").fetchone()[0], start_version)
                    self.assertEqual(list(saved.iterdump()), before)
                self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 4)
                tables = {row[0] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                self.assertTrue({"scenario_runs", "scenario_steps", "scenario_events"}.issubset(tables))
                self.assertEqual(memory.entry_get(self.conn, "note:keep-me")["title"], "Keep me")
                # Pre-migration scenario-log history was backfilled into scenario_events!
                post_log = scenarios.log(self.conn, scenario_id="pre-migrate-scn", entries=10)["entries"]
                self.assertEqual(len(post_log), 1)
                self.assertEqual(post_log[0]["action"], "scenario_created")
                self.assertEqual(post_log[0]["detail"]["after"]["id"], "pre-migrate-scn")
                # Repeat migrate is a no-op with no second backup.
                again = graph.migrate(self.conn, self.db, memory)
                self.assertEqual(again, {"status": "existing", "schema_version": 4, "database": str(self.db)})

    def test_fresh_initialize_creates_schema_4(self):
        res = memory.initialize(self.conn, self.db)
        self.assertEqual(res["schema_version"], 4)
        scenario_runs.require_v4(self.conn, memory)


if __name__ == "__main__":
    unittest.main()
