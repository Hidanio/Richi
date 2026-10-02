"""Git source workflows use temporary repositories and isolated SQLite databases."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from richi import git_sources
from richi import graph
from richi import memory


class GitSourceWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="git-sources-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repository with spaces"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.file = self.repo / "code.txt"
        self.file.write_text("measured implementation\n", encoding="utf-8")
        self.git("add", "code.txt")
        self.git("commit", "-qm", "Initial code")
        self.head = self.git("rev-parse", "HEAD").strip()
        self.db = self.root / "memory.sqlite3"
        self.conn = memory.connect(self.db, create=True)
        self.addCleanup(self.conn.close)
        memory.initialize(self.conn, self.db)
        memory.project_put(self.conn, {"id": "repo", "name": "Repo", "repo_path": str(self.repo)})
        memory.entry_put(self.conn, {"id": "task:one", "kind": "experiment", "title": "Known experiment",
            "summary": "Preserve limitations and failed attempts", "project_ids": ["repo"],
            "sources": [{"reference": "report://before"}], "tags": ["measured"], "aliases": ["first"],
            "work_state": "implemented", "knowledge_state": "confirmed", "verified_at": "2026-09-30"})
        graph.put(self.conn, {"id": "concept", "kind": "concept", "title": "Term", "summary": "Meaning",
            "sources": [{"reference": "report://term"}], "knowledge_state": "hypothesis"}, "entity", memory)
        graph.put(self.conn, {"id": "supports", "from_ref": "entry:task:one", "to_ref": "entity:concept",
            "kind": "supports", "description": "Evidence", "knowledge_state": "superseded",
            "sources": [{"reference": "report://edge"}]}, "edge", memory)

    def git(self, *args, cwd=None):
        result = subprocess.run(["git", "-C", str(cwd or self.repo), *args], capture_output=True,
                                text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def run_source(self, action, **changes):
        options = dict(action=action, project=None, refs=None, ref=None, path=None, rev=None,
                       worktree=False, repo=None, output=None, json_file=None, source=None if action == "git-check" else 1,
                       target=None, max_chars=16000, limit=20, commit=None, expected_updated_at=None)
        options.update(changes)
        return git_sources.run(self.conn, SimpleNamespace(**options), memory, self.db)

    def capture(self, **changes):
        options = dict(project="repo", path="code.txt")
        options.update(changes)
        return self.run_source("capture", **options)["source"]

    def source_file(self, source, name="source.json"):
        path = self.root / name
        path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def attach(self, source, ref="entry:task:one", **changes):
        _, record = git_sources._record(self.conn, ref, memory)
        options = dict(ref=ref, json_file=self.source_file(source), expected_updated_at=record["updated_at"])
        options.update(changes)
        return self.run_source("attach", **options)

    def test_capture_defaults_to_actual_worktree_and_preserves_sqlite(self):
        self.file.write_text("uncommitted experiment\n")
        before = list(self.conn.iterdump())
        source = self.capture()
        self.assertTrue(source["git"]["dirty"])
        self.assertIn("snapshot", source["git"])
        self.assertEqual(source["git"]["commit"], self.head)
        self.assertEqual(list(self.conn.iterdump()), before)
        shown = self.run_source("show", json_file=self.source_file(source))
        self.assertEqual(shown["result"]["content"], "uncommitted experiment\n")

    def test_explicit_revision_capture_does_not_record_uncommitted_bytes(self):
        self.file.write_text("uncommitted\n")
        source = self.capture(rev="HEAD")
        self.assertFalse(source["git"]["dirty"])
        self.assertNotIn("snapshot", source["git"])
        shown = self.run_source("show", json_file=self.source_file(source))
        self.assertEqual(shown["result"]["content"], "measured implementation\n")

    def test_capture_output_is_direct_source_and_never_overwrites(self):
        output = self.root / "captured.json"
        result = self.run_source("capture", project="repo", path="code.txt", output=str(output))
        self.assertEqual(json.loads(output.read_text()), result["source"])
        before = output.read_bytes()
        with self.assertRaises(FileExistsError):
            self.run_source("capture", project="repo", path="code.txt", output=str(output))
        self.assertEqual(output.read_bytes(), before)

    def test_entry_attach_preserves_fields_sources_and_history(self):
        before = memory.entry_get(self.conn, "task:one", history=True)
        source = self.capture()
        result = self.attach(source)
        self.assertEqual(result["source_index"], 2)
        after = memory.entry_get(self.conn, "task:one", history=True)
        for key in set(before) - {"sources", "updated_at", "history"}:
            self.assertEqual(before[key], after[key], key)
        self.assertEqual(after["sources"], before["sources"] + [source])
        self.assertEqual(len(after["history"]), len(before["history"]) + 1)
        self.assertEqual(after["history"][-1]["before"]["sources"], before["sources"])
        self.assertEqual(self.attach(source)["status"], "unchanged")
        self.assertEqual(after, memory.entry_get(self.conn, "task:one", history=True))

    def test_attach_stale_revision_rejected_even_for_idempotent_source(self):
        before = memory.entry_get(self.conn, "task:one")
        source = self.capture()
        self.attach(source)
        with self.assertRaisesRegex(memory.MemoryError, "conflict"):
            self.attach(source, expected_updated_at=before["updated_at"])

    def test_attach_verifies_observed_hash_before_writing(self):
        source = self.capture(rev="HEAD")
        source["sha256"] = "0" * 64
        before = list(self.conn.iterdump())
        with self.assertRaisesRegex(ValueError, "hash"):
            self.attach(source)
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_attach_requires_retained_worktree_snapshot(self):
        source = self.capture()
        (git_sources._artifacts(self.db) / source["git"]["snapshot"]).unlink()
        before = list(self.conn.iterdump())
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.attach(source)
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_entity_and_edge_attach_preserve_state_and_history(self):
        source = self.capture()
        for family, identity in (("entity", "concept"), ("edge", "supports")):
            before = graph.get(self.conn, family, identity, memory)
            self.attach(source, ref=family + ":" + identity)
            after = graph.get(self.conn, family, identity, memory)
            self.assertEqual(after["knowledge_state"], before["knowledge_state"])
            self.assertEqual(after["verified_at"], before["verified_at"])
            self.assertEqual(after["history"][-1]["before"]["sources"], before["sources"])
            self.assertEqual(len(after["history"]), len(before["history"]) + 1)

    def test_git_check_compares_selected_target_without_mutating_cards(self):
        self.attach(self.capture(rev="HEAD"))
        before = list(self.conn.iterdump())
        self.file.write_text("changed\n")
        current = self.run_source("git-check", refs=["entry:task:one"])
        self.assertEqual(current["status"], "unchanged")
        worktree = self.run_source("git-check", refs=["entry:task:one"], worktree=True)
        self.assertEqual(worktree["status"], "changed")
        self.assertEqual(list(self.conn.iterdump()), before)
        self.git("add", "code.txt")
        self.git("commit", "-qm", "Changed code")
        self.assertEqual(self.run_source("git-check", project="repo")["status"], "changed")
        historical = self.run_source("git-check", project="repo", target=self.head)
        self.assertEqual(historical["status"], "unchanged")
        self.assertEqual(historical["comparison"]["target"], self.head)

    def test_project_check_includes_cross_scope_entities_and_edges_in_all_states(self):
        source = self.capture()
        for ref in ("entry:task:one", "entity:concept", "edge:supports"):
            self.attach(source, ref=ref)
        result = self.run_source("git-check", project="repo")
        self.assertEqual({item["ref"] for item in result["results"]},
                         {"entry:task:one", "entity:concept", "edge:supports"})
        self.assertEqual(result["counts"]["unchanged"], 3)
        self.assertTrue(result["coverage"]["scan_complete"])

    def test_multi_source_check_pins_target_commit_after_first_comparison(self):
        second = self.repo / "second.txt"
        second.write_text("old second implementation\n")
        self.git("add", "second.txt")
        self.git("commit", "-qm", "Second file")
        target = self.git("rev-parse", "HEAD").strip()
        self.attach(self.capture(rev="HEAD"))
        self.attach(self.capture(path="second.txt", rev="HEAD"))
        original = git_sources.git_evidence.check
        calls = []

        def move_branch_after_first_comparison(*args, **kwargs):
            calls.append(kwargs["target"])
            result = original(*args, **kwargs)
            if len(calls) == 1:
                second.write_text("new implementation while report is running\n")
                self.git("add", "second.txt")
                self.git("commit", "-qm", "Concurrent update")
            return result

        with patch.object(git_sources.git_evidence, "check", side_effect=move_branch_after_first_comparison):
            result = self.run_source("git-check", project="repo")
        self.assertEqual(calls, ["HEAD", target])
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(result["comparison"]["resolved_targets"], {"repo": target})
        self.assertEqual({item["target"]["commit"] for item in result["results"]}, {target})

    def test_navigate_source_ordinal_uses_entire_sources_array(self):
        self.attach(self.capture())
        with self.assertRaises(ValueError):
            self.run_source("show", ref="entry:task:one")
        self.assertEqual(self.run_source("show", ref="entry:task:one", source=2)["result"]["content"],
                         self.file.read_text())
        with self.assertRaisesRegex(memory.MemoryError, "1-based"):
            self.run_source("show", ref="entry:task:one", source=3)

    def test_history_and_diff_are_available_from_attached_record(self):
        self.attach(self.capture())
        self.file.write_text("new experiment\n")
        diff = self.run_source("diff", ref="entry:task:one", source=2, worktree=True)
        self.assertIn("new experiment", json.dumps(diff["result"]))
        history = self.run_source("history", ref="entry:task:one", source=2)
        self.assertIn(self.head, json.dumps(history["result"]))

    def test_linked_worktree_override_accepted_but_separate_clone_rejected(self):
        worktree = self.root / "linked"
        self.git("worktree", "add", "-q", "-b", "another", str(worktree))
        source = self.capture(repo=str(worktree))
        self.assertEqual(source["git"]["repo_id"], "repo")
        clone = self.root / "clone"
        self.git("clone", "-q", str(self.repo), str(clone))
        with self.assertRaisesRegex(memory.MemoryError, "separate clone"):
            self.capture(repo=str(clone))

    def test_related_exact_path_and_commit_including_edge_declarations(self):
        source = self.capture()
        for ref in ("entry:task:one", "entity:concept", "edge:supports"):
            self.attach(source, ref=ref)
        matches = self.run_source("related", project="repo", path="code.txt", commit=self.head, limit=2)
        self.assertEqual(matches["matching_declarations"], 3)
        self.assertEqual(matches["limit_omitted"], 1)
        self.assertEqual(self.run_source("related", project="repo", path="other.txt")["results"], [])
        with self.assertRaisesRegex(memory.MemoryError, "full lowercase"):
            self.run_source("related", project="repo", path="code.txt", commit="HEAD")

    def test_no_sources_is_distinct_from_unchanged(self):
        result = self.run_source("git-check", refs=["entry:task:one"])
        self.assertEqual(result["status"], "no_git_sources")
        self.assertEqual(result["counts"]["unchanged"], 0)

    def test_missing_registered_repository_reported_unavailable(self):
        self.attach(self.capture())
        self.repo.rename(self.root / "temporarily absent")
        result = self.run_source("git-check", project="repo")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["results"][0]["reason"], "repository_unavailable")

    def test_scan_limit_never_claims_complete_unchanged_result(self):
        self.attach(self.capture())
        with patch.object(git_sources, "MAX_RECORDS", 1):
            result = self.run_source("git-check", project="repo")
        self.assertFalse(result["coverage"]["scan_complete"])
        self.assertEqual(result["status"], "unavailable")

    def test_bounds_account_for_json_escaping_and_removed_results(self):
        value = {"result": {"content": '"\\\n' * 5000}, "notice": git_sources.NOTICE}
        result = git_sources._bound(value, 2000)
        rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        self.assertLessEqual(len(rendered), 2000)
        self.assertEqual(result["budget"]["output_chars"], len(rendered))
        self.assertTrue(result["budget"]["truncated"])
        self.assertGreater(result["budget"]["content_chars_omitted"], 0)
        cards = git_sources._bound({"results": [{"title": "Я" * 200} for _ in range(200)]}, 2000)
        self.assertEqual(len(cards["results"]) + cards["budget"]["items_omitted"], 200)

    def test_readonly_database_supports_capture_navigation_and_checks(self):
        self.attach(self.capture())
        readonly = memory.connect(self.db, readonly=True)
        self.addCleanup(readonly.close)
        before = list(self.conn.iterdump())
        with patch.object(self, "conn", readonly):
            self.run_source("show", ref="entry:task:one", source=2)
            self.run_source("git-check", project="repo")
            self.run_source("related", project="repo", path="code.txt")
            self.capture()
        self.assertEqual(list(self.conn.iterdump()), before)


if __name__ == "__main__":
    unittest.main()
