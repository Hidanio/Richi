"""Workspace-local registration must not copy knowledge or overwrite another project."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest

from richi import memory


class ProjectAddTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-project-add-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "shared repository"
        self.repo.mkdir()
        (self.repo / "code.txt").write_text("source stays here\n")
        self.configs = []
        for name in ("alpha", "beta"):
            directory = self.root / name
            directory.mkdir()
            config = directory / "config.json"
            config.write_text(json.dumps({"database": "memory.sqlite3"}))
            self.configs.append(config)
            self.command(config, "init")

    def command(self, config, *argv, success=True):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = memory.main(["--config", str(config), *map(str, argv)])
        self.assertEqual(status == 0, success, out.getvalue() + err.getvalue())
        return json.loads(out.getvalue() if success else err.getvalue())

    def test_same_folder_and_project_id_are_independent_between_stores(self):
        for config in self.configs:
            result = self.command(config, "project", "add", self.repo, "--id", "shared")
            self.assertEqual(result["status"], "created")
            self.assertEqual(result["project"]["repo_path"], str(self.repo))
            self.assertEqual(result["database"], str(config.parent / "memory.sqlite3"))
        with memory.connect(self.configs[0].parent / "memory.sqlite3") as connection:
            memory.entry_put(connection, {"id": "note:alpha", "kind": "note", "title": "Only alpha",
                "summary": "Workspace-specific knowledge", "project_ids": ["shared"],
                "knowledge_state": "confirmed"})
        self.assertEqual(self.command(self.configs[1], "recall", "Only alpha")["results"], [])
        self.assertTrue(self.command(self.configs[0], "recall", "Only alpha")["results"])
        self.assertEqual(list(self.repo.iterdir()), [self.repo / "code.txt"])

    def test_repeated_registration_preserves_existing_metadata(self):
        config = self.configs[0]
        first = self.command(config, "project", "add", self.repo, "--id", "shared", "--name", "Curated name", "--description", "Original description")
        alias = self.root / "alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        again = self.command(config, "project", "add", alias, "--id", "shared", "--name", "Ignored update")
        self.assertEqual(again["status"], "unchanged")
        self.assertEqual(again["project"], first["project"])

    def test_unavailable_historical_path_does_not_block_new_registration(self):
        config = self.configs[0]
        loop = self.root / "old-loop"
        loop.symlink_to(loop)
        with memory.connect(config.parent / "memory.sqlite3") as connection:
            memory.project_put(connection, {"id": "historical", "name": "Historical",
                                            "repo_path": str(loop)})
        result = self.command(config, "project", "add", self.repo, "--id", "new-project")
        self.assertEqual(result["status"], "created")
        self.command(config, "project", "add", self.repo, "--id", "historical", success=False)
        self.assertEqual(len(self.command(config, "project", "list")["projects"]), 2)

    def test_collision_and_invalid_paths_leave_projects_unchanged(self):
        config = self.configs[0]
        self.command(config, "project", "add", self.repo, "--id", "shared")
        before = self.command(config, "project", "list")
        other = self.root / "other"
        other.mkdir()
        for args in [(other, "--id", "shared"), (self.repo, "--id", "duplicate"),
                     (self.root / "missing",), (self.repo / "code.txt",)]:
            self.assertIn("error", self.command(config, "project", "add", *args, success=False))
        self.assertEqual(self.command(config, "project", "list"), before)


if __name__ == "__main__":
    unittest.main()
