"""Storage tests use synthetic stores only; no private memory or external paths."""
import argparse
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from richi import graph, memory, output, storage


class ConnectionProxy:
    def __init__(self, conn, blocked):
        self.conn, self.blocked = conn, blocked
    def __getattr__(self, name):
        return getattr(self.conn, name)
    def execute(self, statement, *args):
        if self.blocked(statement):
            raise sqlite3.OperationalError("injected failure")
        return self.conn.execute(statement, *args)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="richi-storage-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.db = self.root / "memory.sqlite3"
        self.conn = memory.connect(self.db, create=True)
        self.addCleanup(self.conn.close)
        memory.initialize(self.conn, self.db)
        with memory.transaction(self.conn):
            memory.project_put(self.conn, {"id": "fixture", "name": "Fixture"})
            for i in range(6):
                value = {"id": "entry", "kind": "note", "title": "Private card title",
                         "summary": "confidential evidence needle " + str(i),
                         "project_ids": ["fixture"], "event_id": "event-" + str(i)}
                memory.entry_put(self.conn, value)
            graph.put(self.conn, {"id": "concept", "kind": "concept", "title": "Private concept",
                                  "summary": "test concept"}, "entity", memory)
            graph.put(self.conn, {"id": "link", "from_ref": "entry:entry", "to_ref": "entity:concept",
                                  "kind": "explains", "description": "Private relation"}, "edge", memory)

    def args(self, action="stats", **kwargs):
        return argparse.Namespace(action=action, apply=False, deep=False, files=False,
                                  max_chars=8000, project_path=None, storage_workspace="synthetic", **kwargs)

    def invoke(self, action="stats", conn=None, **kwargs):
        args = self.args(action)
        for key, value in kwargs.items():
            setattr(args, key, value)
        return storage.run(conn or self.conn, args, memory, self.db)

    def knowledge(self, conn=None):
        conn = conn or self.conn
        tables = storage.BASE_TABLES + storage.GRAPH_TABLES
        return {table: [tuple(row) for row in conn.execute("SELECT rowid, * FROM " + table + " ORDER BY rowid")]
                for table in tables}

    def all_tables(self):
        names = [row[0] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {name: [tuple(row) for row in self.conn.execute('SELECT * FROM "' + name + '"')]
                for name in names if name != "entries_fts"}

    def test_stats_and_default_plan_are_readonly_and_omit_content(self):
        readonly = memory.connect(self.db, readonly=True)
        self.addCleanup(readonly.close)
        before, changes = self.knowledge(), readonly.total_changes
        calls = []
        readonly.set_trace_callback(calls.append)
        stats = self.invoke(conn=readonly, deep=True)
        plan = self.invoke("optimize", conn=readonly)
        self.assertEqual(before, self.knowledge())
        self.assertEqual(changes, readonly.total_changes)
        self.assertFalse((self.root / "backups").exists())
        self.assertEqual(plan["status"], "plan")
        self.assertEqual(stats["scope"]["workspace"], "synthetic")
        self.assertEqual(stats["counts"]["entry_history"], 6)
        self.assertNotIn("confidential", output.rendered(stats))
        self.assertNotIn("Private", output.rendered(stats))
        self.assertFalse(any(sql.startswith(("INSERT", "UPDATE", "DELETE", "ANALYZE", "VACUUM")) for sql in calls))
        self.assertFalse(any("checkpoint" in sql.lower() or "pragma optimize" in sql.lower() for sql in calls))

    def test_size_categories_are_separate_and_utf8_bytes_explicit(self):
        for directory, payload in (("backups", b"123"), ("artifacts/git", b"12345"),
                                   ("evaluations", b"1234567"), ("runtime", b"123456789")):
            target = self.root / directory
            target.mkdir(parents=True)
            (target / "file").write_bytes(payload)
        result = self.invoke(deep=True, files=True)
        areas = result["files"]["areas"]
        self.assertEqual([areas[name]["bytes"] for name in storage.FILE_AREAS], [3, 5, 7, 9])
        self.assertEqual(result["database_files"]["main"]["bytes"], self.db.stat().st_size)
        self.assertEqual(result["database_pages"]["allocated_bytes"],
                         result["database_pages"]["page_size"] * result["database_pages"]["page_count"])
        self.assertGreater(result["deep"]["logical_text_bytes"]["entry_history"],
                           result["deep"]["logical_text_bytes"]["entries"])
        self.assertTrue(result["files"]["complete"])

    def test_files_never_follow_nested_or_parent_symlinks(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret").write_bytes(b"secret" * 100)
        (self.root / "backups").symlink_to(outside, target_is_directory=True)
        (self.root / "artifacts").symlink_to(outside, target_is_directory=True)
        evaluations = self.root / "evaluations"
        evaluations.mkdir()
        (evaluations / "nested").symlink_to(outside, target_is_directory=True)
        result = self.invoke(files=True)
        areas = result["files"]["areas"]
        self.assertFalse(result["files"]["complete"])
        self.assertEqual(areas["backups"]["status"], "unavailable_or_link")
        self.assertEqual(areas["artifacts/git"]["status"], "unavailable_or_link")
        self.assertEqual(areas["evaluations"]["skipped_links"], 1)
        self.assertTrue(all(item["bytes"] == 0 for item in areas.values()))

    def test_file_budget_reports_incomplete_instead_of_zero_total(self):
        folder = self.root / "evaluations"
        folder.mkdir()
        for i in range(4):
            (folder / str(i)).write_bytes(b"x")
        with patch.object(storage, "FILE_ENTRIES", 2):
            result = self.invoke(files=True)
        area = result["files"]["areas"]["evaluations"]
        self.assertEqual(area["file_count"], 2)
        self.assertFalse(area["complete"])
        self.assertEqual(area["reason"], "scan_budget")

    def test_dbstat_missing_is_capability_not_zero(self):
        proxy = ConnectionProxy(self.conn, lambda statement: "FROM dbstat" in statement)
        original = proxy.execute
        def missing(statement, *args):
            if "FROM dbstat" in statement:
                raise sqlite3.OperationalError("no such table: dbstat")
            return original(statement, *args)
        proxy.execute = missing
        result = self.invoke(conn=proxy, deep=True)
        self.assertEqual(result["deep"]["physical_pages"]["status"], "unavailable")
        self.assertFalse(result["coverage"]["database_complete"])
        self.assertTrue(result["deep"]["logical_complete"])

    def test_exact_rendered_budget_in_both_modes(self):
        for compact in (False, True):
            for maximum in (2000, 8000):
                with output.json_mode(compact=compact):
                    value = self.invoke(deep=True, files=True, max_chars=maximum)
                    self.assertEqual(value["budget"]["output_chars"], len(output.rendered(value)))
                    self.assertLessEqual(len(output.rendered(value)), maximum)
                    if value["budget"]["omitted_sections"]:
                        self.assertFalse(value["coverage"]["output_complete"])

    def test_apply_backs_up_and_preserves_all_knowledge_history_events_and_rowids(self):
        before = self.knowledge()
        self.conn.execute("PRAGMA analysis_limit=73")
        result = self.invoke("optimize", apply=True)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(before, self.knowledge())
        self.assertEqual(self.conn.execute("PRAGMA analysis_limit").fetchone()[0], 73)
        backup = memory.connect(Path(result["backup"]), readonly=True)
        try:
            self.assertEqual(self.knowledge(backup), before)
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            backup.close()
        if memory.backend(self.conn) == "fts5":
            self.assertEqual(self.conn.execute("SELECT id FROM entries_fts WHERE entries_fts MATCH 'needle'").fetchone()[0], "entry")
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_backup_failure_prevents_any_index_mutation(self):
        before = self.all_tables()
        with patch.object(memory, "backup", side_effect=OSError("backup unavailable")):
            with self.assertRaises(OSError):
                self.invoke("optimize", apply=True)
        self.assertEqual(before, self.all_tables())

    def test_error_after_fts_rolls_back_and_keeps_backup(self):
        before = self.all_tables()
        proxy = ConnectionProxy(self.conn, lambda statement: statement.startswith("PRAGMA optimize"))
        with self.assertRaisesRegex(memory.MemoryError, "rolled back; backup retained"):
            self.invoke("optimize", conn=proxy, apply=True)
        self.assertEqual(before, self.all_tables())
        self.assertEqual(len(list((self.root / "backups").glob("*.sqlite3"))), 1)
        self.assertFalse(self.conn.in_transaction)

    def test_apply_rejects_readonly_project_scope_and_active_transaction(self):
        readonly = memory.connect(self.db, readonly=True)
        try:
            with self.assertRaisesRegex(memory.MemoryError, "writable"):
                self.invoke("optimize", conn=readonly, apply=True)
        finally:
            readonly.close()
        with self.assertRaisesRegex(memory.MemoryError, "workspace-wide"):
            self.invoke("optimize", apply=True, project_path="/fixture")
        with memory.transaction(self.conn, write=False):
            with self.assertRaisesRegex(memory.MemoryError, "active transaction"):
                self.invoke("optimize", apply=True)
        self.assertFalse((self.root / "backups").exists())

    def test_busy_writer_rolls_back_without_knowledge_change(self):
        second = memory.connect(self.db)
        second.execute("PRAGMA busy_timeout=1")
        before = self.knowledge()
        try:
            with memory.transaction(self.conn):
                with self.assertRaisesRegex(memory.MemoryError, "locked"):
                    self.invoke("optimize", conn=second, apply=True)
        finally:
            second.close()
        self.assertEqual(before, self.knowledge())

    def test_old_sqlite_bounded_planner_and_scan_backend(self):
        with memory.transaction(self.conn):
            self.conn.execute("UPDATE metadata SET value='scan' WHERE key='search_backend'")
        with patch.object(storage.sqlite3, "sqlite_version_info", (3, 31, 0)):
            result = self.invoke("optimize", apply=True)
        self.assertEqual([a["status"] for a in result["actions"]], ["skipped", "skipped"])
        self.assertEqual(storage._planner_action((3, 43, 2))["pragma"], "optimize")
        self.assertEqual(storage._planner_action((3, 46, 0))["pragma"], "optimize=0x10002")

    def test_schema_one_stats_and_apply(self):
        path = self.root / "schema1.sqlite3"
        conn = memory.connect(path, create=True)
        try:
            memory.execute_script(conn, (memory.BASE / "schema.sql").read_text())
            conn.execute("INSERT INTO metadata VALUES ('search_backend','scan')")
            conn.execute("PRAGMA user_version=1")
            args = self.args()
            report = storage.run(conn, args, memory, path)
            self.assertEqual(report["schema_version"], 1)
            self.assertNotIn("entities", report["counts"])
            args.action, args.apply = "optimize", True
            self.assertEqual(storage.run(conn, args, memory, path)["status"], "applied")
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        finally:
            conn.close()

    def test_sql_scan_interrupt_is_explicit_and_handler_is_removed(self):
        proxy = ConnectionProxy(self.conn, lambda statement: statement.startswith("SELECT count(*) FROM entry_history"))
        original = proxy.execute
        def interrupted(statement, *args):
            if statement.startswith("SELECT count(*) FROM entry_history"):
                raise sqlite3.OperationalError("interrupted")
            return original(statement, *args)
        proxy.execute = interrupted
        result = self.invoke(conn=proxy)
        self.assertFalse(result["coverage"]["database_complete"])
        self.assertNotIn("entry_history", result["counts"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM entry_history").fetchone()[0], 6)


if __name__ == "__main__":
    unittest.main()
