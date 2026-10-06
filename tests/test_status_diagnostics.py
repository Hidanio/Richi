"""Synthetic, read-only workspace status and registered-path diagnostics."""
from pathlib import Path
import hashlib
import os
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from richi import git_evidence, memory, status


class StatusDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-status-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.db = self.store("alpha")

    def store(self, name):
        path = self.root / (name + ".sqlite3")
        conn = memory.connect(path, create=True)
        try:
            memory.initialize(conn, path)
        finally:
            conn.close()
        return path

    def register(self, project_id, path=None, database=None):
        conn = memory.connect(database or self.db)
        try:
            memory.project_put(conn, {"id": project_id, "name": project_id,
                                     "repo_path": str(path) if path else None,
                                     "description": "Private registry context not needed in status"})
        finally:
            conn.close()

    def directory(self, name):
        path = self.root / name
        path.mkdir(parents=True)
        return path

    def git(self, path, *arguments):
        result = subprocess.run(["git", "-c", "init.defaultBranch=main", *arguments], cwd=path,
                                capture_output=True, text=True, timeout=10,
                                env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")})
        self.assertEqual(result.returncode, 0, result.stderr)

    def repo(self, name, commit=False):
        path = self.directory(name)
        self.git(path, "init")
        if commit:
            self.git(path, "-c", "user.email=synthetic@example.test", "-c", "user.name=Synthetic",
                     "commit", "--allow-empty", "-m", "Initial")
        return path

    def test_missing_database_does_not_create_parent_or_guess_membership(self):
        path = self.root / "never-created" / "memory.sqlite3"
        report = status.inspect(path, self.root, all_projects=True)
        self.assertEqual(report["database"]["status"], "missing")
        self.assertEqual(report["project"]["status"], "unknown")
        self.assertEqual(report["projects"]["status"], "not_checked")
        self.assertFalse(path.parent.exists())

    def test_same_path_has_workspace_local_id_and_no_other_store_read(self):
        path = self.repo("shared")
        beta = self.store("beta")
        self.register("alpha-id", path)
        self.register("beta-id", path, beta)
        for database, expected in ((self.db, "alpha-id"), (beta, "beta-id")):
            report = status.inspect(database, path)
            self.assertEqual(report["project"]["project"]["id"], expected)
            self.assertEqual(report["project"]["status"], "matched")
            self.assertNotIn("description", report["project"]["project"])
            self.assertEqual(report["projects"]["status"], "not_checked")
        self.assertEqual(status.inspect(self.db, path, project_id="beta-id")["project"]["status"], "mismatch")

    def test_plain_directories_allow_nested_files_and_longest_membership(self):
        parent = self.directory("plain")
        child = self.directory("plain/child")
        file = child / "example.py"
        file.write_text("pass\n")
        self.register("parent", parent)
        self.register("child", child)
        report = status.inspect(self.db, file, all_projects=True)
        self.assertEqual(report["project"]["project"]["id"], "child")
        self.assertEqual(report["projects"]["counts"], {"ready": 2})
        self.assertTrue(all(item["kind"] == "directory" for item in report["projects"]["items"]))

    def test_nested_git_checkout_is_not_claimed_by_outer_project(self):
        outer = self.repo("outer")
        nested = self.repo("outer/nested")
        self.register("outer", outer)
        self.assertEqual(status.inspect(self.db, nested)["project"]["status"], "unregistered")

    def test_moved_missing_remote_only_and_file_paths_do_not_change_registry(self):
        moved = self.directory("original")
        self.register("moved", moved)
        destination = moved.rename(self.root / "relocated")
        file = self.root / "file"
        file.write_text("not a directory")
        self.register("file", file)
        self.register("remote-only")
        before = self.db.read_bytes()
        report = status.inspect(self.db, destination, all_projects=True)
        self.assertEqual(report["project"]["status"], "unregistered")
        self.assertEqual(report["projects"]["counts"], {"missing": 1, "no_local_path": 1, "not_directory": 1})
        self.assertEqual(before, self.db.read_bytes())
        remote = next(issue for issue in report["issues"] if issue.get("project_id") == "remote-only")
        self.assertEqual(remote["severity"], "info")

    def test_permission_failure_is_unavailable_not_missing_and_other_project_matches(self):
        denied = self.directory("denied")
        valid = self.directory("valid")
        self.register("denied", denied)
        self.register("valid", valid)
        original = Path.resolve

        def resolve(path, *args, **kwargs):
            if path == denied:
                raise PermissionError("Synthetic permission failure")
            return original(path, *args, **kwargs)

        with patch.object(Path, "resolve", resolve):
            report = status.inspect(self.db, valid, all_projects=True)
        self.assertEqual(report["project"]["status"], "matched")
        self.assertEqual(report["projects"]["counts"], {"ready": 1, "unavailable": 1})
        self.assertEqual(report["issues"][0]["severity"], "error")

    def test_broken_git_marker_is_reported_and_target_membership_is_unknown(self):
        path = self.directory("broken")
        (path / ".git").write_text("gitdir: /unavailable/synthetic-git-dir\n")
        self.register("broken", path)
        report = status.inspect(self.db, path, all_projects=True)
        self.assertEqual(report["projects"]["counts"], {"invalid_repository": 1})
        self.assertEqual(report["project"]["status"], "unknown")
        self.assertTrue(any(issue["severity"] == "error" for issue in report["issues"]))

    def test_duplicate_canonical_directories_are_ambiguous_and_candidates_bounded(self):
        path = self.directory("plain")
        alias = self.root / "alias"
        alias.symlink_to(path, target_is_directory=True)
        self.register("first", path)
        self.register("second", alias)
        report = status.inspect(self.db, alias, all_projects=True, limit=1)
        self.assertEqual(report["project"]["status"], "ambiguous")
        self.assertEqual(report["project"]["candidate_count"], 2)
        self.assertEqual(report["project"]["candidates_omitted"], 1)
        self.assertEqual(len(report["project"]["candidates"]), 1)
        self.assertEqual(report["projects"]["counts"], {"ambiguous": 2})

    def test_worktree_identity_is_cached_and_duplicate_git_registrations_are_ambiguous(self):
        main = self.repo("main", commit=True)
        linked = self.root / "linked"
        self.git(main, "worktree", "add", "-b", "linked", str(linked))
        self.register("one", main)
        with patch.object(git_evidence, "_git", wraps=git_evidence._git) as git:
            report = status.inspect(self.db, linked, all_projects=True)
        self.assertEqual(report["project"]["status"], "matched")
        self.assertEqual(report["project"]["match"], "git_common_dir")
        self.assertEqual(git.call_count, 4)
        self.register("two", linked)
        with patch.object(git_evidence, "_git", wraps=git_evidence._git) as git:
            report = status.inspect(self.db, linked, all_projects=True)
        self.assertEqual(report["project"]["status"], "ambiguous")
        self.assertEqual(report["projects"]["counts"], {"ambiguous": 2})
        self.assertEqual(git.call_count, 4)

    def test_output_limit_prioritizes_problems_and_counts_full_audit(self):
        self.register("a-ready", self.directory("ready"))
        self.register("b-remote")
        self.register("c-missing", self.root / "missing")
        report = status.inspect(self.db, all_projects=True, limit=1)
        audit = report["projects"]
        self.assertEqual(audit["status"], "complete")
        self.assertEqual((audit["total"], audit["checked"], audit["omitted"]), (3, 3, 2))
        self.assertEqual(audit["items"][0]["project"]["id"], "c-missing")
        self.assertEqual(audit["counts"], {"missing": 1, "no_local_path": 1, "ready": 1})
        self.assertTrue(any(issue["code"] == "project_issues_omitted" for issue in report["issues"]))

    def test_duplicate_conflict_details_have_bounded_samples(self):
        path = self.directory("duplicates")
        for number in range(30):
            self.register("duplicate-{:02d}".format(number), path)
        report = status.inspect(self.db, all_projects=True, limit=1)
        item = report["projects"]["items"][0]
        self.assertEqual(item["conflict_count"], 29)
        self.assertEqual(len(item["conflicts"]), 20)
        self.assertEqual(item["conflicts_omitted"], 9)

    def test_deadline_returns_partial_coverage_and_unknown_target(self):
        path = self.directory("one")
        self.register("one", path)
        self.register("two", self.directory("two"))
        original = status._Registry.inspect_row

        def bounded(registry, row):
            if row["id"] == "two":
                registry.deadline = 0
            return original(registry, row)

        with patch.object(status._Registry, "inspect_row", bounded):
            report = status.inspect(self.db, path, all_projects=True)
        self.assertEqual(report["database"]["status"], "ready")
        self.assertEqual(report["project"]["status"], "unknown")
        self.assertEqual(report["project"]["reason"], "inspection_incomplete")
        self.assertEqual(report["projects"]["status"], "partial")
        self.assertEqual(report["projects"]["checked"], 1)

    def test_registry_limit_never_reports_false_match(self):
        path = self.directory("one")
        self.register("one", path)
        self.register("two", path)
        with patch.object(status, "MAX_PROJECTS", 1):
            report = status.inspect(self.db, path, all_projects=True)
        self.assertEqual(report["project"]["status"], "unknown")
        self.assertEqual(report["projects"]["status"], "partial")
        self.assertEqual(report["projects"]["total"], 2)

    def test_schema_one_supported_unknown_schema_not_migrated(self):
        for version, expected in ((1, "ready"), (99, "unsupported_schema")):
            with sqlite3.connect(str(self.db)) as conn:
                conn.execute("PRAGMA user_version = {}".format(version))
            digest = hashlib.sha256(self.db.read_bytes()).hexdigest()
            report = status.inspect(self.db, self.root, all_projects=True)
            self.assertEqual(report["database"]["status"], expected)
            self.assertEqual(report["database"]["schema_version"], version)
            self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), digest)

    def test_invalid_database_is_unavailable_not_missing(self):
        path = self.root / "corrupt.sqlite3"
        path.write_bytes(b"not sqlite")
        self.assertEqual(status.inspect(path)["database"]["status"], "unavailable")
        self.assertEqual(status.inspect(self.root)["database"]["status"], "unavailable")

    def test_only_registry_and_metadata_reads_and_no_writes(self):
        path = self.repo("project")
        self.register("project", path)
        original = memory.connect
        reads = []

        def connect(database, **kwargs):
            self.assertTrue(kwargs.get("readonly"))
            conn = original(database, **kwargs)

            def authorize(action, arg1, arg2, db, trigger):
                if action == sqlite3.SQLITE_READ:
                    reads.append(arg1)
                    return sqlite3.SQLITE_OK if arg1 in {"projects", "metadata"} else sqlite3.SQLITE_DENY
                if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                              sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE}:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            conn.set_authorizer(authorize)
            return conn

        before = self.db.read_bytes()
        with patch.object(memory, "connect", connect):
            report = status.inspect(self.db, path, all_projects=True)
        self.assertEqual(report["database"]["status"], "ready")
        self.assertEqual(report["project"]["status"], "matched")
        self.assertEqual(set(reads), {"projects", "metadata"})
        self.assertEqual(self.db.read_bytes(), before)

    def test_limit_rejects_unbounded_or_noninteger_values(self):
        for value in (0, -1, 1001, True, "10"):
            with self.assertRaises(memory.MemoryError):
                status.inspect(self.db, limit=value)


if __name__ == "__main__":
    unittest.main()
