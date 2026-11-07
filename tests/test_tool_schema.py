"""Tool schema upgrades use disposable databases, including legacy indexes."""

from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from richi import graph, memory, search_index
from richi_launcher import installation


class ToolSchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="richi-tool-schema-")
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "legacy.sqlite3"
        self.conn = memory.connect(self.db, create=True)
        self.addCleanup(self.conn.close)

    def legacy(self, version=2):
        memory.execute_script(self.conn, (memory.BASE / "schema.sql").read_text())
        memory.execute_script(self.conn, memory.FTS_SQL)
        self.conn.execute("INSERT INTO metadata VALUES ('search_backend', 'fts5')")
        if version == 2:
            legacy_sql = (memory.BASE / "graph_schema.sql").read_text().replace(", 'tool'", "")
            memory.execute_script(self.conn, legacy_sql)
        self.conn.execute("PRAGMA user_version = " + str(version))
        with memory.transaction(self.conn):
            memory.project_put(self.conn, {"id": "demo", "name": "Demo"})
            memory.entry_put(self.conn, {"id": "task:demo", "kind": "task", "title": "Check formatting",
                "summary": "Synthetic task", "project_ids": ["demo"], "knowledge_state": "confirmed"})

    @staticmethod
    def entity(**changes):
        return dict({"id": "tool:lint", "kind": "tool", "title": "Lint Demo",
                     "summary": "Checks synthetic sources", "knowledge_state": "confirmed",
                     "sources": [{"reference": "test://lint"}], "expected_updated_at": None}, **changes)

    def put_entity(self, **changes):
        with memory.transaction(self.conn):
            return graph.put(self.conn, self.entity(**changes), "entity", memory)

    def test_new_store_supports_tool_with_graph_and_immutable_history(self):
        self.assertEqual(memory.initialize(self.conn, self.db)["schema_version"], 3)
        self.put_entity()
        record = graph.get(self.conn, "entity", "tool:lint", memory)
        self.assertEqual(record["kind"], "tool")
        self.assertEqual(record["history"][0]["after"]["kind"], "tool")
        self.assertEqual(self.conn.execute("SELECT ref FROM graph_nodes").fetchone()[0], "entity:tool:lint")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE entity_history SET changed_at = 'changed'")

    def test_old_store_reads_and_old_writes_do_not_implicitly_migrate(self):
        self.legacy()
        self.put_entity(id="concept:lint", kind="concept")
        before = list(self.conn.iterdump())
        self.assertEqual(memory.backend(self.conn), "fts5")
        graph.require_v2(self.conn, memory)
        self.assertEqual(graph.get(self.conn, "entity", "concept:lint", memory)["kind"], "concept")
        with self.assertRaisesRegex(memory.MemoryError, "schema 3.*migrate"):
            self.put_entity()
        with self.assertRaisesRegex(memory.MemoryError, "explicit migrate"):
            memory.initialize(self.conn, self.db)
        self.assertEqual(list(self.conn.iterdump()), before)
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_v2_migration_preserves_rows_histories_custom_objects_and_search_index(self):
        self.legacy()
        self.put_entity(id="concept:lint", kind="concept")
        initial = graph.get(self.conn, "entity", "concept:lint", memory, history=False)
        self.put_entity(id="concept:lint", kind="concept", summary="New stable explanation",
                        expected_updated_at=initial["updated_at"])
        with memory.transaction(self.conn):
            graph.put(self.conn, {"id": "used", "kind": "uses", "from_ref": "entry:task:demo",
                "to_ref": "entity:concept:lint", "knowledge_state": "confirmed",
                "sources": [{"reference": "test://run"}]}, "edge", memory)
            search_index.build(self.conn, memory)
        self.conn.execute("CREATE INDEX custom_entity_title ON entities(title)")
        self.conn.execute("CREATE TRIGGER custom_entity_guard BEFORE UPDATE ON entities WHEN new.title = '' BEGIN SELECT RAISE(ABORT, 'empty title'); END")
        tables = [row[0] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        before = {name: self.conn.execute('SELECT * FROM "' + name + '"').fetchall() for name in tables}
        history = graph.get(self.conn, "entity", "concept:lint", memory)
        index_status = search_index.status(self.conn)
        self.assertEqual(graph.migrate(self.conn, self.db, memory)["from_version"], 2)
        self.assertEqual(graph.migrate(self.conn, self.db, memory)["status"], "existing")
        after = {name: self.conn.execute('SELECT * FROM "' + name + '"').fetchall() for name in tables}
        self.assertEqual(before, after)
        self.assertEqual(graph.get(self.conn, "entity", "concept:lint", memory), history)
        self.assertEqual(search_index.status(self.conn), index_status)
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.put_entity()
        self.assertEqual(search_index.status(self.conn)["dirty"]["entity"], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM graph_nodes WHERE ref = 'entity:tool:lint'").fetchone()[0], 1)
        self.assertTrue(self.conn.execute("SELECT sql FROM sqlite_master WHERE name = 'custom_entity_title'").fetchone())
        with self.assertRaisesRegex(sqlite3.IntegrityError, "empty title"):
            self.conn.execute("UPDATE entities SET title = '' WHERE id = 'tool:lint'")
        with self.assertRaisesRegex(memory.MemoryError, "update conflict"):
            self.put_entity(id="concept:lint", kind="concept", expected_updated_at=initial["updated_at"])

    def test_failed_rebuild_rolls_back_schema_data_and_foreign_keys(self):
        self.legacy()
        self.put_entity(id="concept:lint", kind="concept")
        before = list(self.conn.iterdump())
        migrate = graph._migrate_entities_v3
        def fail_after_rebuild(conn, api):
            migrate(conn, api)
            raise RuntimeError("synthetic interruption")
        with mock.patch.object(graph, "_migrate_entities_v3", side_effect=fail_after_rebuild):
            with self.assertRaisesRegex(RuntimeError, "synthetic interruption"):
                graph.migrate(self.conn, self.db, memory)
        self.assertEqual(list(self.conn.iterdump()), before)
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_v1_upgrades_directly_to_three_and_allows_tool(self):
        self.legacy(version=1)
        self.assertEqual(graph.migrate(self.conn, self.db, memory),
                         {"status": "migrated", "from_version": 1, "schema_version": 3, "database": str(self.db)})
        self.put_entity()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM graph_nodes").fetchone()[0], 3)

    def test_release_supports_old_and_new_schemas_without_map_wire_change(self):
        manifest = {"format_version": 1, "updater_protocol": 1, "python_min": [3, 9], "map_schema": 2}
        # Later schemas need no bridge release: any support from 1 to the newest
        # schema is accepted; shrinking support is refused on update/rollback.
        for schemas in ([1, 2], [1, 2, 3], [1, 2, 3, 4], list(range(1, 33))):
            installation._compatible(dict(manifest, database_schemas=schemas))
        # More than the bootstrap's generation limit would brick state.json.
        for schemas in ([2, 3], [1, 3], [1], list(range(1, 34))):
            with self.assertRaises(ValueError):
                installation._compatible(dict(manifest, database_schemas=schemas))
        with self.assertRaises(ValueError):
            installation._compatible(dict(manifest, database_schemas=[1, 2, 3], map_schema=3))


if __name__ == "__main__":
    unittest.main()
