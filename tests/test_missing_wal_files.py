"""A WAL store without its -wal and -shm files, as after copying only the
database file, stays readable. Synthetic stores only."""
from contextlib import closing, contextmanager
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from cli_environment import cli_environment
from richi import memory, serve


CANNOT_OPEN = "unable to open database file"


class MissingWalFilesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-missing-wal-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.db = self.root / "store" / "memory.sqlite3"
        with closing(memory.connect(self.db, create=True)) as conn:
            memory.initialize(conn, self.db)
            with memory.transaction(conn):
                memory.project_put(conn, {"id": "fixture", "name": "Fixture"})
            # With every frame checkpointed, the database file alone is the whole store.
            self.assertEqual(tuple(conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()), (0, 0, 0))
        for suffix in ("-wal", "-shm"):
            Path(str(self.db) + suffix).unlink(missing_ok=True)
        self.content = self.db.read_bytes()

    def cli(self, *arguments, db=None):
        result = subprocess.run([sys.executable, "-m", "richi", "--db", str(db or self.db), *map(str, arguments)],
                                capture_output=True, text=True, timeout=20, env=cli_environment(self.root))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(result.stdout)

    @contextmanager
    def macos_system_sqlite(self, read_write_error=None):
        """Make sqlite3.connect behave as the system SQLite of macOS, whose
        read-only connections do not create a missing -wal file; with
        read_write_error, the store cannot be opened for writing either.
        Yields the modes of the connections opened meanwhile."""
        connect, wal, opened = sqlite3.connect, Path(str(self.db) + "-wal"), []

        class ReadOnly(sqlite3.Connection):
            def execute(self, sql, *parameters):
                # Setting a pragma does not open the files; reading does.
                if "=" not in sql and not wal.exists():
                    raise sqlite3.OperationalError(CANNOT_OPEN)
                return super().execute(sql, *parameters)

        def patched(database, **options):
            mode = database.rpartition("?mode=")[2]
            opened.append(mode)
            if mode == "rw" and read_write_error is not None:
                raise read_write_error
            return connect(database, factory=ReadOnly if mode == "ro" else sqlite3.Connection, **options)

        with mock.patch.object(sqlite3, "connect", patched):
            yield opened

    def test_read_command_reads_the_store_and_leaves_the_database_unchanged(self):
        # Without the WAL files the system SQLite of macOS refuses a read-only
        # connection; other builds create the files for it.
        self.assertEqual(self.cli("check")["status"], "ok")
        self.assertEqual(self.db.read_bytes(), self.content)

    def test_backup_copied_as_a_single_file_passes_check(self):
        # "Check the copy" of richi help backup, for a copy moved without its WAL files.
        backup = self.root / "backups" / "memory-backup.sqlite3"
        self.cli("backup", "--output", backup)
        moved = self.root / "moved" / "memory-backup.sqlite3"
        moved.parent.mkdir()
        shutil.copyfile(backup, moved)
        self.assertEqual(self.cli("check", db=moved)["status"], "ok")
        self.assertEqual([project["id"] for project in self.cli("project", "list", db=moved)["projects"]],
                         ["fixture"])

    def test_map_reads_the_schema_and_none_for_an_unreadable_file(self):
        self.assertEqual(serve.database_schema(self.db), memory.SCHEMA)
        other = self.root / "other.sqlite3"
        self.assertIsNone(serve.database_schema(other))
        other.write_bytes(b"not a database " * 100)
        self.assertIsNone(serve.database_schema(other))

    def test_store_with_wal_files_is_opened_once_and_read_only(self):
        with closing(memory.connect(self.db)) as writer:
            writer.execute("SELECT id FROM projects").fetchone()  # the files exist while it is open
            with mock.patch.object(sqlite3, "connect", wraps=sqlite3.connect) as opened:
                with closing(memory.connect(self.db, readonly=True)) as reader:
                    self.assertEqual(reader.execute("SELECT id FROM projects").fetchone()[0], "fixture")
        self.assertEqual([call.args[0].rpartition("?mode=")[2] for call in opened.call_args_list], ["ro"])

    def test_one_read_write_open_creates_the_files_a_read_only_connection_cannot(self):
        with self.macos_system_sqlite() as opened:
            with closing(memory.connect(self.db, readonly=True)) as reader:
                self.assertEqual(reader.execute("SELECT id FROM projects").fetchone()[0], "fixture")
                with self.assertRaisesRegex(sqlite3.OperationalError, "readonly database"):
                    reader.execute("DELETE FROM projects")
        self.assertEqual(opened, ["ro", "rw"])
        self.assertEqual(self.db.read_bytes(), self.content)

    def test_error_is_unchanged_when_the_files_cannot_be_created(self):
        refused = sqlite3.OperationalError("attempt to write a readonly database")
        with self.macos_system_sqlite(read_write_error=refused) as opened:
            with self.assertRaisesRegex(sqlite3.OperationalError, "^" + CANNOT_OPEN + "$"):
                memory.connect(self.db, readonly=True)
        self.assertEqual(opened, ["ro", "rw"])

    def test_other_errors_do_not_open_the_store_for_writing(self):
        self.db.write_bytes(b"not a database " * 100)
        with mock.patch.object(sqlite3, "connect", wraps=sqlite3.connect) as opened:
            with self.assertRaisesRegex(sqlite3.DatabaseError, "^file is not a database$"):
                memory.connect(self.db, readonly=True)
        self.assertEqual(opened.call_count, 1)


if __name__ == "__main__":
    unittest.main()
