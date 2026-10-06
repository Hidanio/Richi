"""Discovery operates on disposable repositories and one supplied store only."""
from pathlib import Path
import os
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from richi import memory, projects


class ProjectDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-discovery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.sources = self.root / "sources"
        self.sources.mkdir()
        self.conn = self.store("alpha")

    def store(self, name):
        path = self.root / (name + ".sqlite3")
        conn = memory.connect(path, create=True)
        memory.initialize(conn, path)
        self.addCleanup(conn.close)
        return conn

    def git(self, path, *arguments):
        result = subprocess.run(["git", "-c", "init.defaultBranch=main", *arguments],
                                cwd=path, capture_output=True, text=True, timeout=10,
                                env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")})
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def repo(self, relative, commit=False):
        path = self.sources / relative
        path.mkdir(parents=True)
        self.git(path, "init")
        if commit:
            self.git(path, "-c", "user.email=synthetic@example.test", "-c", "user.name=Synthetic", "commit", "--allow-empty", "-m", "Initial")
        return path

    def rows(self, conn=None):
        return [dict(row) for row in (conn or self.conn).execute("SELECT * FROM projects ORDER BY id")]

    def test_preview_is_read_only_apply_preserves_metadata_and_parent_transaction(self):
        first = self.repo("first")
        second = self.repo("second")
        memory.project_add(self.conn, first, project_id="curated", name="Curated name", description="Keep description")
        before = self.rows()
        report = projects.project_scan(self.conn, self.sources)
        self.assertEqual(report["status"], "preview")
        self.assertEqual([row["status"] for row in report["projects"]], ["unchanged", "would_create"])
        self.assertEqual(self.rows(), before)
        with memory.transaction(self.conn):
            report = projects.project_scan(self.conn, self.sources, apply=True)
            self.assertTrue(self.conn.in_transaction)
        self.assertEqual(report["status"], "applied")
        self.assertEqual(self.rows()[0], before[0])
        self.assertEqual(self.rows()[1]["repo_path"], str(second))
        again = projects.project_scan(self.conn, self.sources, apply=True)
        self.assertEqual([row["status"] for row in again["projects"]], ["unchanged", "unchanged"])

    def test_same_repository_in_two_stores_does_not_share_knowledge(self):
        path = self.repo("shared")
        other = self.store("beta")
        for conn in (self.conn, other):
            projects.project_scan(conn, self.sources, apply=True)
            self.assertEqual(projects.ensure_project_context(conn, path)["id"], "shared")
        memory.entry_put(self.conn, {"id": "note:alpha", "kind": "note", "title": "Only alpha", "summary": "Separate workspace", "project_ids": ["shared"], "knowledge_state": "confirmed"})
        self.assertEqual(other.execute("SELECT count(*) FROM entries").fetchone()[0], 0)
        self.assertEqual(len(self.rows(other)), 1)

    def test_same_named_repositories_abort_entire_import(self):
        self.repo("a/shared")
        self.repo("b/shared")
        self.repo("c/unique")
        report = projects.project_scan(self.conn, self.sources)
        self.assertEqual(report["status"], "conflict")
        self.assertEqual(report["conflicts"][0]["reason"], "project_id_collision")
        with self.assertRaisesRegex(memory.MemoryError, "conflict"):
            projects.project_scan(self.conn, self.sources, apply=True)
        self.assertEqual(self.rows(), [])

    def test_collision_with_stored_id_preserves_old_path(self):
        self.repo("new")
        old_path = self.root / "unavailable"
        memory.project_put(self.conn, {"id": "new", "name": "Old", "repo_path": str(old_path)})
        before = self.rows()
        with self.assertRaisesRegex(memory.MemoryError, "conflict"):
            projects.project_scan(self.conn, self.sources, apply=True)
        self.assertEqual(self.rows(), before)

    def test_failed_insert_rolls_back_scan_but_not_caller_writes(self):
        self.repo("a")
        self.repo("b")
        original = memory.project_add

        def fail_second(conn, path, **kwargs):
            if Path(path).name == "b":
                raise memory.MemoryError("Synthetic insertion failure")
            return original(conn, path, **kwargs)

        with memory.transaction(self.conn):
            memory.project_put(self.conn, {"id": "prior", "name": "Caller write"})
            with patch.object(memory, "project_add", side_effect=fail_second):
                with self.assertRaisesRegex(memory.MemoryError, "Synthetic"):
                    projects.project_scan(self.conn, self.sources, apply=True)
            self.assertEqual([row["id"] for row in self.rows()], ["prior"])

    def test_worktrees_collapse_to_main_and_resolve_existing_project(self):
        main = self.repo("z-main", commit=True)
        linked = self.sources / "a-linked"
        self.git(main, "worktree", "add", "-b", "linked", str(linked))
        report = projects.discover(self.sources)
        self.assertEqual(len(report["repositories"]), 1)
        repository = report["repositories"][0]
        self.assertEqual(repository["repo_path"], str(main))
        self.assertEqual(repository["discovered_paths"], [str(linked), str(main)])
        memory.project_add(self.conn, main, project_id="curated-main", description="Keep this")
        context = projects.project_check(self.conn, linked)
        self.assertEqual(context["status"], "matched")
        self.assertEqual(context["match"], "git_common_dir")
        self.assertEqual(context["project"]["id"], "curated-main")
        imported = projects.project_scan(self.conn, self.sources, apply=True)
        self.assertEqual(imported["projects"][0]["status"], "unchanged")
        self.assertEqual(len(self.rows()), 1)

    def test_external_main_is_metadata_only_and_existing_linked_path_preserved(self):
        main = self.repo("main", commit=True)
        external = self.root / "external-linked"
        self.git(main, "worktree", "add", "-b", "external", str(external))
        report = projects.discover(external)
        self.assertEqual(report["repositories"][0]["repo_path"], str(external))
        self.assertEqual(report["repositories"][0]["discovered_paths"], [str(external)])
        memory.project_add(self.conn, external, project_id="external")
        before = self.rows()
        projects.project_scan(self.conn, self.sources, apply=True)
        self.assertEqual(self.rows(), before)

    def test_duplicate_git_registrations_are_ambiguous_even_with_expected_id(self):
        main = self.repo("main", commit=True)
        linked = self.sources / "linked"
        self.git(main, "worktree", "add", "-b", "linked", str(linked))
        memory.project_add(self.conn, main, project_id="one")
        memory.project_add(self.conn, linked, project_id="two")
        self.assertEqual(projects.project_check(self.conn, main, "one")["status"], "ambiguous")
        with self.assertRaisesRegex(memory.MemoryError, "ambiguous"):
            projects.ensure_project_context(self.conn, main, "one")

    def test_nested_git_repository_does_not_match_outer_or_plain_parent(self):
        outer = self.repo("outer")
        nested = self.repo("outer/nested")
        memory.project_add(self.conn, outer, project_id="outer")
        memory.project_add(self.conn, self.sources, project_id="container")
        report = projects.project_check(self.conn, nested)
        self.assertEqual(report["status"], "unregistered")
        self.assertEqual(report["candidates"], [])
        memory.project_add(self.conn, nested, project_id="nested")
        child = nested / "code.py"
        child.write_text("pass\n")
        self.assertEqual(projects.ensure_project_context(self.conn, child)["id"], "nested")
        with self.assertRaisesRegex(memory.MemoryError, "mismatch"):
            projects.ensure_project_context(self.conn, child, "outer")

    def test_plain_directories_use_longest_containment_and_canonical_path(self):
        parent = self.sources / "plain"
        child = parent / "child"
        child.mkdir(parents=True)
        source = child / "code.py"
        source.write_text("pass\n")
        memory.project_add(self.conn, parent, project_id="parent")
        memory.project_add(self.conn, child, project_id="child")
        alias = self.root / "alias"
        alias.symlink_to(child, target_is_directory=True)
        self.assertEqual(projects.ensure_project_context(self.conn, source)["id"], "child")
        self.assertEqual(projects.project_check(self.conn, alias)["match"], "exact_path")

    def test_registered_git_subdirectory_matches_files_and_longest_folder(self):
        repository = self.repo("monorepo")
        source = repository / "src"
        component = source / "component"
        component.mkdir(parents=True)
        code = component / "code.py"
        code.write_text("pass\n")
        sibling = repository / "README.md"
        sibling.write_text("Synthetic repository\n")
        memory.project_add(self.conn, source, project_id="source")
        self.assertEqual(projects.ensure_project_context(self.conn, source)["id"], "source")
        self.assertEqual(projects.ensure_project_context(self.conn, code)["id"], "source")
        self.assertEqual(projects.project_check(self.conn, code)["match"], "directory")
        self.assertEqual(projects.project_check(self.conn, sibling)["status"], "unregistered")
        memory.project_add(self.conn, repository, project_id="whole-repo")
        self.assertEqual(projects.ensure_project_context(self.conn, source)["id"], "source")
        self.assertEqual(projects.ensure_project_context(self.conn, code)["id"], "source")
        self.assertEqual(projects.ensure_project_context(self.conn, sibling)["id"], "whole-repo")
        memory.project_add(self.conn, component, project_id="component")
        self.assertEqual(projects.ensure_project_context(self.conn, code)["id"], "component")
        with self.assertRaisesRegex(memory.MemoryError, "mismatch"):
            projects.ensure_project_context(self.conn, code, "source")

    def test_registered_git_subdirectory_does_not_cross_nested_git_boundary(self):
        repository = self.repo("outer")
        source = repository / "src"
        source.mkdir()
        nested = self.repo("outer/src/nested")
        code = nested / "code.py"
        code.write_text("pass\n")
        memory.project_add(self.conn, source, project_id="source")
        self.assertEqual(projects.project_check(self.conn, nested)["status"], "unregistered")
        self.assertEqual(projects.project_check(self.conn, code)["status"], "unregistered")
        memory.project_add(self.conn, nested, project_id="nested")
        self.assertEqual(projects.ensure_project_context(self.conn, code)["id"], "nested")
        with self.assertRaisesRegex(memory.MemoryError, "mismatch"):
            projects.ensure_project_context(self.conn, code, "source")

    def test_symlinks_and_vendor_directories_are_not_scanned(self):
        valid = self.repo("valid")
        for prefix in ("node_modules", "vendor", "venv", ".hidden"):
            self.repo(prefix + "/excluded")
        escape = self.root / "outside"
        escape.mkdir()
        self.git(escape, "init")
        (self.sources / "escape").symlink_to(escape, target_is_directory=True)
        (self.sources / "loop").symlink_to(self.sources, target_is_directory=True)
        report = projects.discover(self.sources)
        self.assertEqual([repo["repo_path"] for repo in report["repositories"]], [str(valid)])
        self.assertFalse(report["truncated"])
        self.assertFalse(report["errors"])
        self.assertEqual({item["reason"] for item in report["skipped"]}, {"symlink"})

    def test_malformed_or_symlink_git_marker_blocks_apply_and_outer_fallback(self):
        self.repo("valid")
        bad = self.sources / "bad"
        bad.mkdir()
        (bad / ".git").write_text("gitdir: /unavailable-synthetic-git\n")
        alias = self.sources / "git-symlink"
        alias.mkdir()
        (alias / ".git").symlink_to(self.sources / "valid" / ".git", target_is_directory=True)
        report = projects.project_scan(self.conn, self.sources)
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(len(report["errors"]), 2)
        with self.assertRaisesRegex(memory.MemoryError, "incomplete"):
            projects.project_scan(self.conn, self.sources, apply=True)
        self.assertEqual(self.rows(), [])
        outer = self.repo("outer")
        memory.project_add(self.conn, outer)
        broken = outer / "nested"
        broken.mkdir()
        (broken / ".git").write_text("broken git marker")
        self.assertTrue(projects.project_check(self.conn, broken)["errors"])
        with self.assertRaises(memory.MemoryError):
            projects.ensure_project_context(self.conn, broken)

    def test_limits_report_truncation_and_prevent_apply(self):
        self.repo("a/nested/repo")
        report = projects.discover(self.sources, max_depth=1)
        self.assertTrue(report["truncated"])
        self.assertEqual(report["skipped"][0]["reason"], "depth_limit")
        with self.assertRaisesRegex(memory.MemoryError, "incomplete"):
            projects.project_scan(self.conn, self.sources, apply=True, max_depth=1)
        self.repo("b")
        report = projects.discover(self.sources, max_repositories=1)
        self.assertTrue(report["truncated"])
        self.assertEqual(len(report["repositories"]), 1)
        with patch.object(projects, "MAX_DIRECTORIES", 1):
            self.assertTrue(projects.discover(self.sources)["truncated"])
        with patch.object(projects, "MAX_DIRECTORY_ENTRIES", 1):
            self.assertTrue(projects.discover(self.sources)["truncated"])

    def test_project_check_reads_no_knowledge_tables(self):
        path = self.repo("registered")
        memory.project_add(self.conn, path)
        reads = set()

        def authorize(action, table, column, database, origin):
            if action == sqlite3.SQLITE_READ:
                reads.add(table)
                return sqlite3.SQLITE_OK if table == "projects" else sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.conn.set_authorizer(authorize)
        try:
            self.assertEqual(projects.ensure_project_context(self.conn, path)["id"], "registered")
        finally:
            self.conn.set_authorizer(None)
        self.assertEqual(reads, {"projects"})

    def test_git_environment_cannot_redirect_identity_and_timeouts_are_reported(self):
        actual = self.repo("actual")
        other = self.repo("other")
        memory.project_add(self.conn, actual)
        with patch.dict(os.environ, {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other)}):
            self.assertEqual(projects.ensure_project_context(self.conn, actual)["id"], "actual")
        with patch.object(projects.git_evidence, "_git", side_effect=projects.git_evidence.GitEvidenceError("Local Git operation exceeded the time limit")):
            report = projects.discover(self.sources)
        self.assertTrue(report["errors"])


if __name__ == "__main__":
    unittest.main()
