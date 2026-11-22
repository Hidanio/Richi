"""Tool schema upgrades use disposable databases, including legacy indexes."""

from contextlib import closing, redirect_stderr
from pathlib import Path
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

import ast
from cli_environment import cli_environment
import richi_bootstrap
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

    def backups(self):
        folder = self.db.parent / "backups"
        return sorted(folder.iterdir()) if folder.is_dir() else []

    def unchanged_at_schema_2(self, before):
        self.assertEqual(list(self.conn.iterdump()), before)
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

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
        result = graph.migrate(self.conn, self.db, memory)
        self.assertEqual(result, {"status": "migrated", "from_version": 1, "schema_version": 3, "database": str(self.db),
                                  "backup": result["backup"]})
        self.assertRegex(Path(result["backup"]).name, r"^legacy-schema1-")
        self.put_entity()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM graph_nodes").fetchone()[0], 3)

    def test_migration_backs_up_the_old_store_first(self):
        self.legacy()
        self.put_entity(id="concept:lint", kind="concept")
        before = list(self.conn.iterdump())
        copy = Path(graph.migrate(self.conn, self.db, memory)["backup"])
        self.assertEqual(self.backups(), [copy])
        self.assertRegex(copy.name, r"^legacy-schema2-\d{8}T\d{12}Z\.sqlite3$")
        self.assertEqual(copy.stat().st_mode & 0o777, 0o600)
        with closing(sqlite3.connect(str(copy))) as saved:
            self.assertEqual(saved.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(list(saved.iterdump()), before)
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 3)
        # A current store is left alone: no second copy.
        self.assertEqual(graph.migrate(self.conn, self.db, memory),
                         {"status": "existing", "schema_version": 3, "database": str(self.db)})
        self.assertEqual(self.backups(), [copy])

    def test_refused_store_is_not_copied(self):
        self.legacy()
        self.conn.execute("PRAGMA user_version = 9")
        with self.assertRaisesRegex(memory.MemoryError, "unknown schema version"):
            graph.migrate(self.conn, self.db, memory)
        self.conn.execute("PRAGMA user_version = 2")
        self.conn.execute("DROP TABLE edge_history")
        with self.assertRaisesRegex(memory.MemoryError, "missing required tables"):
            graph.migrate(self.conn, self.db, memory)
        self.assertEqual(self.backups(), [])

    def test_migration_does_not_start_without_a_copy(self):
        self.legacy()
        (self.db.parent / "backups").write_text("not a directory", encoding="utf-8")
        before = list(self.conn.iterdump())
        with self.assertRaisesRegex(memory.MemoryError, "migration did not start and nothing changed"):
            graph.migrate(self.conn, self.db, memory)
        self.unchanged_at_schema_2(before)

    def test_failed_migration_keeps_the_store_and_names_the_copy(self):
        self.legacy()
        before = list(self.conn.iterdump())
        with mock.patch.object(graph, "_migrate_entities_v3", side_effect=sqlite3.OperationalError("synthetic failure")):
            with self.assertRaisesRegex(memory.MemoryError, "store is unchanged and its backup is retained at") as caught:
                graph.migrate(self.conn, self.db, memory)
        (copy,) = self.backups()
        self.assertIn(str(copy) + ": synthetic failure", str(caught.exception))
        self.unchanged_at_schema_2(before)
        with closing(sqlite3.connect(str(copy))) as saved:
            self.assertEqual(list(saved.iterdump()), before)

    def test_commit_blocked_by_reader_reports_busy_and_keeps_backup(self):
        self.legacy()
        real_connect = memory.connect

        def connect_without_wait(path, **kwargs):
            return real_connect(path, timeout=0, **kwargs)

        with closing(real_connect(self.db)) as reader, memory.transaction(reader, write=False):
            reader.execute("SELECT id FROM projects").fetchall()
            error = io.StringIO()
            with mock.patch.object(memory, "connect", connect_without_wait), redirect_stderr(error), \
                    mock.patch.dict(os.environ, cli_environment(self.db.parent), clear=True):
                self.assertEqual(memory.main(["--db", str(self.db), "migrate"]), 1)
            result = json.loads(error.getvalue())
            self.assertEqual(result.get("code"), "busy", result)
            backup, = self.backups()
            self.assertIn(str(backup), result["error"])
            self.assertEqual(reader.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(graph.migrate(self.conn, self.db, memory)["status"], "migrated")

    def test_long_database_name_still_gets_a_restorable_pre_migration_copy(self):
        self.legacy()
        self.conn.close()
        long_path = self.db.with_name("x" * 225 + ".sqlite3")
        self.db.rename(long_path)
        with closing(memory.connect(long_path)) as source:
            result = graph.migrate(source, long_path, memory)
        self.assertEqual(result["status"], "migrated")
        copy = Path(result["backup"])
        self.assertIn("-schema2-", copy.name)
        with closing(memory.connect(copy, readonly=True)) as saved:
            self.assertEqual(saved.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(saved.execute("SELECT id FROM projects").fetchone()[0], "demo")

    def check_migration_snapshot(self, journal):
        self.conn.execute("PRAGMA journal_mode = " + journal)
        self.legacy()
        make_copy = memory.backup

        def try_competing_writes(reader, database, output, label):
            result = make_copy(reader, database, output, label=label)
            with closing(memory.connect(self.db, timeout=0)) as other:
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    other.execute("INSERT INTO metadata VALUES ('concurrent', 'write')")
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    graph.migrate(other, self.db, memory)
            return result

        with mock.patch.object(memory, "backup", side_effect=try_competing_writes):
            result = graph.migrate(self.conn, self.db, memory)
        with closing(memory.connect(self.db)) as other:
            self.assertEqual(graph.migrate(other, self.db, memory)["status"], "existing")
            other.execute("INSERT INTO metadata VALUES ('after', 'migration')")
        copy = Path(result["backup"])
        self.assertEqual(list(copy.parent.glob("*.sqlite3")), [copy])
        # The backup database is portable even if SQLite leaves WAL sidecars beside it.
        standalone = self.db.parent / "standalone.sqlite3"
        shutil.copyfile(copy, standalone)
        with closing(sqlite3.connect(str(standalone))) as saved:
            self.assertEqual(saved.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(saved.execute("SELECT id FROM projects").fetchall(), [("demo",)])
            self.assertIsNone(saved.execute("SELECT value FROM metadata WHERE key = 'after'").fetchone())

    def test_snapshot_and_migration_share_writer_lock_in_wal_mode(self):
        self.check_migration_snapshot("WAL")

    def test_snapshot_and_migration_share_writer_lock_in_rollback_mode(self):
        self.check_migration_snapshot("DELETE")

    def test_release_stays_installable_by_1_0_1_updaters(self):
        # 1.0.1 accepts only manifests declaring [1, 2] and requires the release's
        # memory.VERSION to equal map_schema 2; real support is SUPPORTED_VERSIONS.
        self.assertEqual((memory.VERSION, graph.MAP_SCHEMA_VERSION), (2, 2))
        self.assertEqual((installation.PROTOCOL1_DATABASE_SCHEMAS, installation.PROTOCOL1_MAP_SCHEMA), ([1, 2], 2))
        manifest = {"format_version": 1, "updater_protocol": 1, "python_min": [3, 9], "map_schema": 2}
        installation._compatible(dict(manifest, database_schemas=[1, 2]))
        for schemas in ([1, 2, 3], [2, 3], [1]):
            with self.assertRaises(ValueError):
                installation._compatible(dict(manifest, database_schemas=schemas))
        with self.assertRaises(ValueError):
            installation._compatible(dict(manifest, database_schemas=[1, 2], map_schema=3))

    def test_storage_schema_contract_holds_for_any_schema_bump(self):
        # Support is every schema from 1 to SCHEMA, migrate reaches SCHEMA, and
        # bootstrap's source inference (used for generations 1.0.1 installed)
        # agrees with the real package.
        self.assertEqual(memory.SUPPORTED_VERSIONS, set(range(1, memory.SCHEMA + 1)))
        self.assertEqual(graph.MIGRATION_TARGET, memory.SCHEMA)
        site = Path(memory.__file__).resolve().parents[1]
        self.assertEqual(richi_bootstrap.generation_schemas({"site": str(site)}), sorted(memory.SUPPORTED_VERSIONS))
        # The frozen compatibility field is never read by storage code.
        tree = ast.parse(Path(memory.__file__).read_text(encoding="utf-8"))
        reads = [node for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id == "VERSION"
                 and isinstance(node.ctx, ast.Load)]
        self.assertEqual(reads, [])
        for module in Path(memory.__file__).resolve().parent.glob("*.py"):
            for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Attribute) and node.attr == "VERSION" and getattr(node.value, "id", "") in {"memory", "api"}:
                    self.fail(module.name + " reads memory.VERSION; use memory.SCHEMA")

if __name__ == "__main__":
    unittest.main()
