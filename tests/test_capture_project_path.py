"""Capture respects the checked checkout without weakening workspace guards."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cli_environment import cli_environment
from richi import memory


class CaptureProjectPathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-capture-context-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.environment = patch.dict(os.environ, cli_environment(self.root), clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"database": "memory.sqlite3"}), encoding="utf-8")
        self.database = self.root / "memory.sqlite3"
        self.artifacts = self.root / "artifacts" / "git"
        self.repo = self.root / "main checkout"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "code.txt").write_text("main committed bytes\n", encoding="utf-8")
        self.git("add", "src/code.txt")
        self.git("commit", "-qm", "Main implementation")
        self.main_head = self.git("rev-parse", "HEAD").strip()
        self.linked = self.root / "linked checkout"
        self.git("worktree", "add", "-q", "-b", "feature", str(self.linked))
        (self.linked / "src" / "code.txt").write_text("linked committed bytes\n", encoding="utf-8")
        self.git("add", "src/code.txt", cwd=self.linked)
        self.git("commit", "-qm", "Linked implementation", cwd=self.linked)
        self.linked_head = self.git("rev-parse", "HEAD", cwd=self.linked).strip()
        self.assertNotEqual(self.main_head, self.linked_head)
        self.command("init")
        self.command("project", "add", self.repo, "--id", "fixture")

    def git(self, *args, cwd=None):
        result = subprocess.run(["git", "-C", str(cwd or self.repo), *args],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def command(self, *args, ok=True):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            status = memory.main(["--config", str(self.config), *map(str, args)])
        self.assertEqual(status, 0 if ok else 1, output.getvalue() + error.getvalue())
        if not ok:
            self.assertEqual(output.getvalue(), "")
        return json.loads(output.getvalue() if ok else error.getvalue())

    def capture(self, *args, project_path=None, project="fixture", ok=True):
        prefix = [] if project_path is None else ["--project-path", project_path]
        return self.command(*prefix, "sources", "capture", "--project", project,
                            "--path", "src/code.txt", *args, ok=ok)

    def assert_context(self, result, repo, selected_by, commit):
        self.assertEqual(result["source"]["git"]["commit"], commit)
        self.assertEqual(result["source"]["reference"], str(repo / "src" / "code.txt"))
        self.assertEqual(result["capture_context"], {
            "repo_root": str(repo.resolve()), "selected_by": selected_by,
            "resolved_commit": commit,
        })

    def assert_rejected_without_side_effects(self, *args, project_path=None, project="fixture"):
        connection = memory.connect(self.database, readonly=True)
        try:
            before = list(connection.iterdump())
        finally:
            connection.close()
        output = self.root / "must-not-exist.json"
        result = self.capture(*args, "--output", output,
                              project_path=project_path, project=project, ok=False)
        self.assertFalse(output.exists())
        self.assertFalse(self.artifacts.exists())
        connection = memory.connect(self.database, readonly=True)
        try:
            self.assertEqual(list(connection.iterdump()), before)
        finally:
            connection.close()
        return result

    def test_head_revision_uses_linked_project_path_and_preserves_source_file_format(self):
        output = self.root / "source.json"
        result = self.capture("--rev", "HEAD", "--output", output, project_path=self.linked)
        self.assert_context(result, self.linked, "project_path", self.linked_head)
        source = result["source"]
        self.assertEqual(source["git"]["mode"], "commit")
        self.assertEqual(source["sha256"], hashlib.sha256(b"linked committed bytes\n").hexdigest())
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), source)
        self.assertNotIn("capture_context", source)
        self.assertNotIn("capture_context", source["git"])
        shown = self.command("sources", "show", "--json", output)
        self.assertEqual(shown["result"]["content"], "linked committed bytes\n")

    def test_default_working_copy_capture_uses_linked_dirty_bytes_and_base_commit(self):
        (self.repo / "src" / "code.txt").write_text("main dirty bytes\n", encoding="utf-8")
        (self.linked / "src" / "code.txt").write_text("linked dirty bytes\n", encoding="utf-8")
        result = self.capture(project_path=self.linked)
        self.assert_context(result, self.linked, "project_path", self.linked_head)
        source = result["source"]
        self.assertEqual(source["git"]["mode"], "worktree")
        self.assertTrue(source["git"]["dirty"])
        self.assertEqual(source["sha256"], hashlib.sha256(b"linked dirty bytes\n").hexdigest())
        self.assertEqual((self.artifacts / source["git"]["snapshot"]).read_bytes(), b"linked dirty bytes\n")

    def test_project_subdirectory_resolves_checkout_root_without_rebasing_file_path(self):
        result = self.capture("--rev", "HEAD", project_path=self.linked / "src")
        self.assert_context(result, self.linked, "project_path", self.linked_head)
        self.assertEqual(result["source"]["git"]["path"], "src/code.txt")

    def test_project_file_resolves_its_containing_checkout_root(self):
        result = self.capture("--rev", "HEAD", project_path=self.linked / "src" / "code.txt")
        self.assert_context(result, self.linked, "project_path", self.linked_head)

    def test_capture_metadata_over_budget_does_not_create_output_file(self):
        # Each component and the complete path also fit macOS filesystem limits.
        parent = self.root / ("a" * 180) / ("b" * 180) / ("c" * 180)
        parent.mkdir(parents=True)
        checkout = parent / "budget-checkout"
        self.git("worktree", "add", "-q", "--detach", str(checkout), self.linked_head)
        output = parent / "capture.json"
        result = self.capture("--rev", "HEAD", "--max-chars", "2000", "--output", output,
                              project_path=checkout, ok=False)
        self.assertIn("metadata exceeds the output budget", result["error"])
        self.assertFalse(output.exists())
        self.assertFalse(self.artifacts.exists())

    def test_explicit_repo_wins_over_checked_linked_project_path(self):
        result = self.capture("--rev", "HEAD", "--repo", self.repo, project_path=self.linked)
        self.assert_context(result, self.repo, "repo", self.main_head)

    def test_explicit_linked_repo_works_without_project_path(self):
        result = self.capture("--rev", "HEAD", "--repo", self.linked)
        self.assert_context(result, self.linked, "repo", self.linked_head)

    def test_no_selector_uses_registered_checkout_from_unrelated_cwd(self):
        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        previous = Path.cwd()
        try:
            os.chdir(unrelated)
            result = self.capture("--rev", "HEAD")
        finally:
            os.chdir(previous)
        self.assert_context(result, self.repo, "registered", self.main_head)

    def test_unregistered_separate_clone_project_path_rejected_even_with_valid_repo(self):
        clone = self.root / "separate clone"
        self.git("clone", "-q", str(self.repo), str(clone))
        result = self.assert_rejected_without_side_effects("--repo", self.repo, project_path=clone)
        self.assertIn("unregistered", result["error"].lower())

    def test_separate_clone_repo_override_rejected_with_valid_project_path(self):
        clone = self.root / "separate clone"
        self.git("clone", "-q", str(self.repo), str(clone))
        result = self.assert_rejected_without_side_effects("--repo", clone, project_path=self.linked)
        self.assertIn("separate clone", result["error"].lower())

    def test_unregistered_nested_repository_rejected_even_with_valid_repo(self):
        nested = self.repo / "nested"
        self.git("init", "-q", str(nested))
        result = self.assert_rejected_without_side_effects("--repo", self.repo, project_path=nested)
        self.assertIn("unregistered", result["error"].lower())

    def test_checked_project_cannot_be_overridden_by_repo_or_project_option(self):
        other = self.root / "other repo"
        self.git("init", "-q", str(other))
        self.command("project", "add", other, "--id", "other")
        result = self.assert_rejected_without_side_effects("--repo", self.repo, project_path=other)
        self.assertIn("mismatch", result["error"].lower())


if __name__ == "__main__":
    unittest.main()
