"""CLI/map navigation shares exact legacy scope, selection and read-only behavior."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from urllib.parse import urlencode

from richi import git_evidence
from richi import git_legacy
from richi import git_sources
from richi import graph
from richi import memory
from richi import serve


class GitCliLegacyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="git-cli-legacy-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / "code.txt").write_text("original\n")
        (self.repo / "untouched.txt").write_text("constant\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Original")
        self.first = self.git("rev-parse", "HEAD").strip()
        self.structured = git_evidence.capture(self.repo, "repo", "code.txt", revision=self.first)
        (self.repo / "code.txt").write_text("next\n")
        self.git("commit", "-qam", "Next")
        self.head = self.git("rev-parse", "HEAD").strip()
        self.db = self.root / "memory.sqlite3"
        self.conn = memory.connect(self.db, create=True)
        self.addCleanup(self.conn.close)
        memory.initialize(self.conn, self.db)
        memory.project_put(self.conn, {"id": "repo", "name": "Repo", "repo_path": str(self.repo)})
        self.sources = [{"reference": "report://first"},
                        {"reference": "git:" + self.first + ":code.txt", "observed_at": "2026-09-30"},
                        {"reference": "git:" + self.head}, self.structured]
        memory.entry_put(self.conn, {"id": "task:cli", "kind": "experiment", "title": "CLI parity",
            "summary": "Keep original evidence", "project_ids": ["repo"], "sources": self.sources,
            "knowledge_state": "confirmed", "work_state": "implemented", "verified_at": "2026-09-30"})
        self.ref = "entry:task:cli"

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def cli(self, action, *flags, success=True, ref=None, source=2):
        command = [sys.executable, "-B", "-m", "richi", "--db", str(self.db),
                   "sources", action, "--ref", ref or self.ref, "--source", str(source), "--max-chars", "32000"]
        command.extend(flags)
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        return result.stderr

    def web(self, action, ref=None, source=2, **extra):
        ref = ref or self.ref
        _, record = git_sources._record(self.conn, ref, memory)
        options = {"action": action, "ref": ref, "source": str(source),
                   "expected_updated_at": record["updated_at"], "max_chars": "32000"}
        options.update(extra)
        return serve._git_response(self.db, serve._git_options(urlencode(options)))

    def same(self, left, right):
        left, right = copy.deepcopy(left), copy.deepcopy(right)
        # A legacy hash is calculated during each response, not retroactively dated.
        for value in (left, right):
            if "resolved_source" in value:
                self.assertRegex(value["resolved_source"]["observed_at"], r"^\d{4}-\d\d-\d\dT")
                value["resolved_source"].pop("observed_at")
        self.assertEqual(left, right)

    def replace(self, **changes):
        record = memory.entry_get(self.conn, "task:cli")
        payload = {key: value for key, value in record.items() if key not in {"created_at", "updated_at", "history"}}
        payload.update(changes, expected_updated_at=record["updated_at"])
        memory.entry_put(self.conn, payload)

    def test_legacy_file_cli_map_parity_and_no_state_changes(self):
        before = list(self.conn.iterdump())
        index = (self.repo / ".git" / "index").read_bytes()
        for action in ("show", "history", "diff"):
            self.same(self.cli(action), self.web(action))
        self.same(self.cli("history", "--target", "HEAD"), self.web("history", target="HEAD"))
        (self.repo / "code.txt").write_text("dirty bytes\n")
        self.same(self.cli("diff", "--worktree"), self.web("diff", worktree="1"))
        self.assertEqual(self.cli("show")["result"]["content"], "original\n")
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.head)
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), index)
        self.assertEqual(list(self.conn.iterdump()), before)
        self.assertFalse(git_sources._artifacts(self.db).exists())

    def test_bare_commit_file_list_selection_and_errors_match(self):
        self.same(self.cli("commit", source=3), self.web("commit", source=3))
        for action in ("show", "history", "diff"):
            self.same(self.cli(action, "--path", "code.txt", source=3),
                      self.web(action, source=3, path="code.txt"))
        self.assertIn("Select a changed file", self.cli("show", source=3, success=False))
        self.assertIn("available regular file", self.cli("show", "--path", "untouched.txt", source=3, success=False))
        self.assertIn("cannot select another path", self.cli("show", "--path", "untouched.txt", success=False))
        self.assertIn("bare legacy commit", self.cli("commit", source=2, success=False))

    def test_selected_git_check_matches_map_without_overloading_manifest_check(self):
        self.same(self.cli("git-check"), self.web("check"))
        self.same(self.cli("git-check", "--path", "code.txt", source=3),
                  self.web("check", source=3, path="code.txt"))
        self.same(self.cli("git-check", "--worktree", source=4), self.web("check", source=4, worktree="1"))
        self.assertIn("exactly one --ref", self.cli("git-check", "--ref", self.ref, success=False))
        args = SimpleNamespace(action="git-check", refs=[self.ref], project=None,
                               source=None, path="code.txt", max_chars=32000)
        with self.assertRaisesRegex(memory.MemoryError, "--source"):
            git_sources.run(self.conn, args, memory, self.db)
        args = SimpleNamespace(action="git-check", refs=None, project="repo", source=1, path=None)
        with self.assertRaisesRegex(memory.MemoryError, "--project"):
            git_sources.run(self.conn, args, memory, self.db)
        result = subprocess.run([sys.executable, "-B", "-m", "richi", "sources", "check", "--help"],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertIn("--manifest", result.stdout)

    def test_structured_sources_keep_navigation_and_json_input(self):
        for action in ("show", "history", "diff"):
            self.same(self.cli(action, source=4), self.web(action, source=4))
        self.assertIn("cannot select another path", self.cli("show", "--path", "code.txt", source=4, success=False))
        self.assertIn("bare legacy commit", self.cli("commit", source=4, success=False))
        source_json = self.root / "source.json"
        source_json.write_text(json.dumps(self.structured))
        result = subprocess.run([sys.executable, "-B", "-m", "richi", "--db", str(self.db),
                                 "sources", "show", "--json", str(source_json)], text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["result"]["content"], "original\n")

    def test_public_resolver_scoping_and_deadline(self):
        self.assertEqual(git_legacy.parse_reference("git:" + self.first + ":code.txt"), (self.first, "code.txt"))
        resolved = git_legacy.resolve_repository(self.conn, self.ref, self.first)
        self.assertEqual((resolved["repo_id"], resolved["repo"], resolved["commit"], resolved["path"]),
                         ("repo", str(self.repo), self.first, None))
        with self.assertRaises(git_legacy.LegacyGitError):
            git_legacy.resolve_repository(self.conn, self.ref, "HEAD")
        with self.assertRaisesRegex(TimeoutError, "time limit"):
            git_legacy.resolve_repository(self.conn, self.ref, self.first, deadline=time.monotonic() - 1)

    def test_ambiguous_unavailable_and_abbreviated_commits_do_not_fall_back(self):
        clone = self.root / "clone"
        self.git("clone", "-q", str(self.repo), str(clone))
        memory.project_put(self.conn, {"id": "clone", "name": "Clone", "repo_path": str(clone)})
        self.replace(project_ids=["repo", "clone"])
        before = list(self.conn.iterdump())
        self.assertIn("multiple scoped repositories", self.cli("show", success=False))
        with self.assertRaises(serve.GitRequestError) as error:
            self.web("show")
        self.assertEqual((error.exception.status, error.exception.code), (409, "legacy_repository_ambiguous"))
        self.assertEqual(list(self.conn.iterdump()), before)
        self.replace(project_ids=["repo"], sources=[{"reference": "git:" + "a" * 40 + ":code.txt"}])
        self.assertIn("unavailable", self.cli("show", source=1, success=False))
        self.replace(sources=[{"reference": "git:" + self.first[:8] + ":code.txt"}])
        self.assertIn("full lowercase", self.cli("show", source=1, success=False))

    def test_entity_and_explicit_edge_use_same_scoped_resolver(self):
        graph.put(self.conn, {"id": "term", "kind": "concept", "title": "Term", "summary": "Meaning",
                             "knowledge_state": "confirmed", "sources": self.sources}, "entity", memory)
        graph.put(self.conn, {"id": "scope", "from_ref": "entity:term", "to_ref": "project:repo",
                             "kind": "used_in", "knowledge_state": "confirmed",
                             "sources": [{"reference": "report://scope"}]}, "edge", memory)
        graph.put(self.conn, {"id": "evidence", "from_ref": self.ref, "to_ref": "entity:term",
                             "kind": "supports", "knowledge_state": "confirmed", "sources": self.sources}, "edge", memory)
        for ref in ("entity:term", "edge:evidence"):
            self.same(self.cli("show", ref=ref), self.web("show", ref=ref))

    def test_map_cas_still_prevents_changed_source_index(self):
        _, record = git_sources._record(self.conn, self.ref, memory)
        options = serve._git_options(urlencode({"action": "show", "ref": self.ref, "source": "2",
                                               "expected_updated_at": record["updated_at"]}))
        self.replace(sources=[self.structured] + self.sources)
        with self.assertRaises(serve.GitRequestError) as error:
            serve._git_response(self.db, options)
        self.assertEqual((error.exception.status, error.exception.code), (409, "stale_record"))


if __name__ == "__main__":
    unittest.main()
