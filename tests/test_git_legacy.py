"""Legacy source resolution never mutates memory or invents repository identity."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import urlencode
from unittest.mock import patch

from richi import git_evidence
from richi import git_legacy
from richi import git_sources
from richi import graph
from richi import memory
from richi import serve


class LegacyGitTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="git-legacy-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / "code.txt").write_text("original\n")
        (self.repo / "untouched.txt").write_text("unchanged\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Initial code")
        self.first = self.git("rev-parse", "HEAD").strip()
        (self.repo / "code.txt").write_text("second\n")
        self.git("commit", "-qam", "Second code")
        self.head = self.git("rev-parse", "HEAD").strip()
        self.db = self.root / "memory.sqlite3"
        self.conn = memory.connect(self.db, create=True)
        self.addCleanup(self.conn.close)
        memory.initialize(self.conn, self.db)
        memory.project_put(self.conn, {"id": "repo", "name": "Repo", "repo_path": str(self.repo)})
        self.sources = [{"reference": "report://before"},
            {"reference": "git:" + self.first + ":code.txt", "type": "repository_code", "observed_at": "2026-09-30"},
            {"reference": "git:" + self.head, "type": "commit"}]
        memory.entry_put(self.conn, {"id": "task:legacy", "kind": "experiment", "title": "Legacy",
            "summary": "Prior evidence", "project_ids": ["repo"], "sources": self.sources,
            "knowledge_state": "confirmed", "work_state": "implemented", "verified_at": "2026-09-30"})

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def record(self, ref="entry:task:legacy"):
        return git_sources._record(self.conn, ref, memory)[1]

    def options(self, action="show", ref="entry:task:legacy", **extra):
        options = {"action": action, "ref": ref, "source": "2", "expected_updated_at": self.record(ref)["updated_at"]}
        options.update(extra)
        return serve._git_options(urlencode(options))

    def response(self, action="show", ref="entry:task:legacy", **extra):
        return serve._git_response(self.db, self.options(action, ref, **extra))

    def replace(self, **changes):
        record = self.record()
        payload = {key: value for key, value in record.items() if key not in {"created_at", "updated_at", "history"}}
        payload.update(changes, expected_updated_at=record["updated_at"])
        memory.entry_put(self.conn, payload)

    def assert_error(self, code, **options):
        with self.assertRaises(serve.GitRequestError) as caught:
            self.response(**options)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_legacy_file_all_actions_use_exact_commit_without_backfill(self):
        before_db = list(self.conn.iterdump())
        before_index = (self.repo / ".git" / "index").read_bytes()
        shown = self.response()
        self.assertEqual(shown["result"]["content"], "original\n")
        self.assertEqual(shown["captured_commit"], self.first)
        self.assertEqual(shown["resolved_source"]["git"]["repo_id"], "repo")
        self.assertEqual(shown["legacy_resolution"]["original_observed_at"], "2026-09-30")
        self.assertEqual(self.response("history")["result"]["target"], self.first)
        self.assertEqual(self.response("check")["result"]["status"], "changed")
        self.assertIn("+second", self.response("diff")["result"]["patch"])
        (self.repo / "code.txt").write_text("dirty\n")
        self.assertIn("+dirty", self.response("diff", worktree="1")["result"]["patch"])
        self.assertEqual(list(self.conn.iterdump()), before_db)
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), before_index)
        self.assertFalse(git_sources._artifacts(self.db).exists())

    def test_bare_commit_lists_changes_then_supports_selected_file_actions(self):
        result = self.response("commit", source="3")
        self.assertEqual(result["result"]["subject"], "Second code")
        self.assertEqual(result["result"]["parents"], [self.first])
        self.assertEqual(result["result"]["files"], [{"path": "code.txt", "status": "M", "available": True}])
        self.assertEqual(self.response(source="3", path="code.txt")["result"]["content"], "second\n")
        self.assertEqual(self.response("history", source="3", path="code.txt")["result"]["target"], self.head)
        self.assertEqual(self.response("check", source="3", path="code.txt")["result"]["status"], "unchanged")
        self.assertEqual(self.response("diff", source="3", path="code.txt")["result"]["status"], "unchanged")
        self.assert_error("legacy_path_required", source="3")
        self.assert_error("legacy_path_unavailable", source="3", path="untouched.txt")
        self.assert_error("invalid_source", action="commit", source="2")
        self.assert_error("invalid_source", source="2", path="untouched.txt")

    def test_root_commit_and_deleted_file(self):
        sources = self.sources + [{"reference": "git:" + self.first}]
        self.replace(sources=sources)
        result = self.response("commit", source="4")["result"]
        self.assertEqual(result["parents"], [])
        self.assertEqual({item["path"] for item in result["files"]}, {"code.txt", "untouched.txt"})
        self.git("rm", "-q", "code.txt")
        self.git("commit", "-qm", "Deleted code")
        removed = self.git("rev-parse", "HEAD").strip()
        self.replace(sources=sources + [{"reference": "git:" + removed}])
        result = self.response("commit", source="5")["result"]
        self.assertFalse(result["files"][0]["available"])
        self.assert_error("legacy_path_unavailable", source="5", path="code.txt")
        self.assertEqual(self.response()["result"]["content"], "original\n")

    def test_exact_membership_rejects_unscoped_or_ambiguous_repository(self):
        clone = self.root / "clone"
        self.git("clone", "-q", str(self.repo), str(clone))
        memory.project_put(self.conn, {"id": "clone", "name": "Clone", "repo_path": str(clone)})
        self.assertEqual(self.response()["repo_id"], "repo")
        self.replace(project_ids=["repo", "clone"])
        self.assertEqual(self.assert_error("legacy_repository_ambiguous").status, 409)
        self.replace(project_ids=[])
        self.assert_error("legacy_repository_unavailable")

    def test_missing_commit_never_falls_back_to_head(self):
        self.replace(sources=[{"reference": "git:" + "a" * 40 + ":code.txt"}])
        self.assert_error("legacy_repository_unavailable", source="1")
        self.replace(sources=[{"reference": "git:" + self.first[:8] + ":code.txt"}])
        self.assert_error("invalid_source", source="1")

    def test_unavailable_scoped_candidate_never_implies_unique_match(self):
        memory.project_put(self.conn, {"id": "missing", "name": "Missing", "repo_path": str(self.root / "missing")})
        self.replace(project_ids=["repo", "missing"])
        self.assert_error("legacy_repository_unavailable")
        memory.project_put(self.conn, {"id": "missing", "name": "No local checkout"})
        self.assertEqual(self.response()["repo_id"], "repo")

    def test_candidate_resolution_does_not_swallow_worker_deadline(self):
        with patch.object(git_evidence, "identify", side_effect=TimeoutError("deadline")):
            with self.assertRaises(TimeoutError):
                self.response()

    def test_entity_scope_requires_direct_sourced_confirmed_used_in(self):
        graph.put(self.conn, {"id": "term", "kind": "concept", "title": "Term", "summary": "Meaning",
            "sources": self.sources, "knowledge_state": "confirmed"}, "entity", memory)
        self.assert_error("legacy_repository_unavailable", ref="entity:term")
        edge = {"id": "scope", "from_ref": "entity:term", "to_ref": "project:repo", "kind": "used_in",
            "sources": [{"reference": "report://scope"}], "knowledge_state": "hypothesis"}
        graph.put(self.conn, edge, "edge", memory)
        self.assert_error("legacy_repository_unavailable", ref="entity:term")
        edge["knowledge_state"] = "confirmed"
        graph.put(self.conn, edge, "edge", memory)
        self.assertEqual(self.response(ref="entity:term")["repo_id"], "repo")
        edge["sources"] = self.sources
        graph.put(self.conn, edge, "edge", memory)
        self.assertEqual(self.response(ref="edge:scope")["repo_id"], "repo")

    def test_edge_scope_can_use_entry_endpoint_membership(self):
        graph.put(self.conn, {"id": "edge", "from_ref": "entry:task:legacy", "to_ref": "project:repo",
            "kind": "supports", "sources": self.sources, "knowledge_state": "hypothesis"}, "edge", memory)
        self.assertEqual(self.response(ref="edge:edge")["repo_id"], "repo")

    def test_stale_source_index_still_rejected_before_resolution(self):
        options = self.options()
        self.replace(sources=list(reversed(self.sources)))
        with self.assertRaises(serve.GitRequestError) as caught:
            serve._git_response(self.db, options)
        self.assertEqual(caught.exception.code, "stale_record")

    def test_unsafe_legacy_paths_and_structured_override_rejected(self):
        for path in ("../code.txt", "/tmp/code.txt", ".git/config", "a//b", "a\\b"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                git_legacy.parse({"reference": "git:" + self.first + ":" + path})
            with self.subTest(path=path), self.assertRaises(serve.GitRequestError):
                self.options(source="3", path=path)
        source = git_evidence.capture(self.repo, "repo", "code.txt", revision=self.first)
        self.replace(sources=[source])
        self.assert_error("invalid_source", source="1", path="code.txt")
        self.assert_error("invalid_source", source="1", action="commit")

    def test_commit_list_budget_and_type_limits(self):
        for number in range(40):
            (self.repo / ("long-file-name-" + str(number) + ".txt")).write_text("x")
        (self.repo / "link").symlink_to("code.txt")
        self.git("add", ".")
        self.git("commit", "-qm", "Many files")
        commit = self.git("rev-parse", "HEAD").strip()
        self.replace(sources=[{"reference": "git:" + commit}])
        result = self.response("commit", source="1", max_chars="3000")
        self.assertTrue(result["result"]["truncated"])
        self.assertTrue(result["budget"]["truncated"])
        self.assertLessEqual(result["budget"]["output_chars"], 3000)
        self.assertEqual(result["budget"]["output_chars"], len(git_sources._render(result)))
        self.assertNotIn("link", {item["path"] for item in result["result"]["files"]})
        self.assert_error("legacy_path_unavailable", source="1", path="link")

    def test_merge_commit_lists_first_parent_changes_explicitly(self):
        branch = self.git("symbolic-ref", "--short", "HEAD").strip()
        self.git("checkout", "-qb", "side", self.first)
        (self.repo / "side.txt").write_text("side")
        self.git("add", ".")
        self.git("commit", "-qm", "Side")
        self.git("checkout", "-q", branch)
        self.git("merge", "--no-ff", "-qm", "Merge side", "side")
        commit = self.git("rev-parse", "HEAD").strip()
        self.replace(sources=[{"reference": "git:" + commit}])
        result = self.response("commit", source="1")["result"]
        self.assertTrue(result["first_parent"])
        self.assertEqual(len(result["parents"]), 2)
        self.assertEqual([item["path"] for item in result["files"]], ["side.txt"])

    def test_worker_supports_legacy_and_all_runtime_capabilities_are_required(self):
        status, result = serve._run_git_request(self.db, self.options())
        self.assertEqual(status, 200, result)
        self.assertEqual(result["result"]["content"], "original\n")
        capabilities = ["git_source_viewer", "legacy_git_source_viewer", "standalone_runtime",
                        "runtime_selection", "runtime_reload", "workspace_selection"]
        health = {"application": "project-memory-map", "database": str(self.db),
                  "api_version": 4, "capabilities": capabilities,
                  "runtime": serve.current_runtime().as_dict(), "config_file": None}
        self.assertTrue(serve.compatible_health(health, self.db))
        for required in capabilities:
            with self.subTest(missing=required):
                incomplete = dict(health, capabilities=[item for item in capabilities if item != required])
                self.assertFalse(serve.compatible_health(incomplete, self.db))


if __name__ == "__main__":
    unittest.main()
