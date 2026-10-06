"""CLI project guards validate identities in the selected store before mutations."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from richi import memory


class ProjectWorkspaceGuardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-project-guard-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.configs = []
        self.repos = []
        for name in ("alpha", "beta"):
            store = self.root / name
            store.mkdir()
            config = store / "config.json"
            config.write_text(json.dumps({"database": "memory.sqlite3"}))
            repo = self.root / (name + "-repo")
            repo.mkdir()
            (repo / "src").mkdir()
            self.configs.append(config)
            self.repos.append(repo)
            self.command(config, "init")
            self.command(config, "project", "add", repo, "--id", "shared")

    def command(self, config, *argv, ok=True):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = memory.main(["--config", str(config), *map(str, argv)])
        self.assertEqual(result == 0, ok, out.getvalue() + err.getvalue())
        return json.loads(out.getvalue() if ok else err.getvalue())

    def payload(self, name="task:example", projects=None):
        return {"id": name, "kind": "task", "title": "Scoped result", "summary": "Checked result",
                "project_ids": ["shared"] if projects is None else projects,
                "knowledge_state": "confirmed", "work_state": "implemented"}

    def write_payload(self, payload):
        path = self.root / "payload.json"
        path.write_text(json.dumps(payload))
        return path

    def test_same_project_id_different_checkout_is_rejected_before_write(self):
        a, b = self.configs
        repo = self.repos[0]
        payload = self.write_payload(self.payload())
        result = self.command(a, "project", "check", repo / "src", "--id", "shared")
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["database"], str(a.parent / "memory.sqlite3"))
        self.command(b, "--project-path", repo, "entry", "put", "--json", payload, ok=False)
        self.command(b, "entry", "get", "task:example", ok=False)
        self.command(a, "--project-path", repo / "src", "entry", "put", "--json", payload)
        self.assertEqual(self.command(a, "entry", "get", "task:example")["project_ids"], ["shared"])
        self.command(b, "entry", "get", "task:example", ok=False)

    def test_explicit_project_and_entry_payload_must_agree_with_guard(self):
        config, repo = self.configs[0], self.repos[0]
        other = self.root / "another"
        other.mkdir()
        self.command(config, "project", "add", other, "--id", "other")
        self.command(config, "--project-path", repo, "recall", "result", "--project", "other", ok=False)
        payload = self.write_payload([self.payload("task:first"), self.payload("task:wrong", ["other"])])
        self.command(config, "--project-path", repo, "entry", "put", "--json", payload, ok=False)
        self.command(config, "entry", "get", "task:first", ok=False)
        payload = self.write_payload(self.payload(projects=["shared", "other"]))
        self.command(config, "--project-path", repo, "entry", "put", "--json", payload)
        self.assertEqual(self.command(config, "entry", "get", "task:example")["project_ids"], ["other", "shared"])

    def test_nested_unregistered_git_checkout_is_not_the_outer_project(self):
        repo, config = self.repos[0], self.configs[0]
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        nested = repo / "nested"
        subprocess.run(["git", "init", "-q", str(nested)], check=True)
        result = self.command(config, "project", "check", nested)
        self.assertEqual(result["status"], "unregistered")
        self.command(config, "--project-path", nested, "entry", "put",
                     "--json", self.write_payload(self.payload()), ok=False)
        self.command(config, "entry", "get", "task:example", ok=False)

    def test_backup_cannot_silently_ignore_project_guard(self):
        output = self.root / "guarded-backup.sqlite3"
        self.command(self.configs[0], "--project-path", self.root / "missing",
                     "backup", "--output", output, ok=False)
        self.assertFalse(output.exists())

    def test_scan_depth_zero_checks_root_and_rejects_incomplete_apply(self):
        root = self.root / "scan-root"
        subprocess.run(["git", "init", "-q", str(root / "child")], check=True)
        preview = self.command(self.configs[0], "project", "scan", root, "--max-depth", "0")
        self.assertTrue(preview["truncated"])
        self.command(self.configs[0], "project", "scan", root, "--max-depth", "0", "--apply", ok=False)
        self.assertEqual(len(self.command(self.configs[0], "project", "list")["projects"]), 1)
        self.command(self.configs[0], "project", "scan", root, "--max-depth", "21", ok=False)

    def test_scan_cli_preview_and_apply_are_workspace_local(self):
        roots = self.root / "repositories"
        roots.mkdir()
        for name in ("first", "second"):
            subprocess.run(["git", "init", "-q", str(roots / name)], check=True)
        a, b = self.configs
        preview = self.command(a, "project", "scan", roots)
        self.assertFalse(preview["applied"])
        self.assertEqual(len(self.command(a, "project", "list")["projects"]), 1)
        applied = self.command(a, "project", "scan", roots, "--apply")
        self.assertTrue(applied["applied"])
        self.assertEqual(len(self.command(a, "project", "list")["projects"]), 3)
        self.assertEqual(len(self.command(b, "project", "list")["projects"]), 1)


if __name__ == "__main__":
    unittest.main()
