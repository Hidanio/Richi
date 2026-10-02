"""Impact uses disposable repositories and databases; never live project data."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import shlex
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from richi import git_evidence
from richi import git_impact
from richi import git_legacy
from richi import graph
from richi import memory


class GitImpactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="git-impact-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo with spaces"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        for name in ("code.txt", "delete.txt", "old.txt"):
            (self.repo / name).write_text(name + " original\n")
        self.commit("Initial")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.db = self.root / "memory.sqlite3"
        self.conn = memory.connect(self.db, create=True)
        self.addCleanup(self.conn.close)
        memory.initialize(self.conn, self.db)
        memory.project_put(self.conn, {"id": "repo", "name": "Repo", "repo_path": str(self.repo)})

    def git(self, *args, cwd=None):
        completed = subprocess.run(["git", "-C", str(cwd or self.repo), *args],
                                   capture_output=True, text=True, timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-qm", message)

    def anchor(self, path="code.txt", project="repo", repo=None):
        return git_evidence.capture(str(repo or self.repo), project, path,
                                    revision=self.base, worktree=False)

    def entry(self, identity="one", sources=None, projects=None, state="confirmed", **fields):
        payload = {"id": identity, "kind": "experiment", "title": "Observed result " + identity,
                   "summary": "The experiment failed. Only verified for the historical input.",
                   "project_ids": ["repo"] if projects is None else projects,
                   "sources": [self.anchor()] if sources is None else sources,
                   "knowledge_state": state, "work_state": "done", "verified_at": "2026-10-01"}
        payload.update(fields)
        return memory.entry_put(self.conn, payload)

    def impact(self, **changes):
        args = {"project": "repo", "repo": None, "base": self.base, "target": "HEAD",
                "worktree": False, "limit": 12, "max_chars": 16000}
        args.update(changes)
        return git_impact.impact(self.conn, SimpleNamespace(**args), memory, self.db)

    def change(self):
        (self.repo / "code.txt").write_text("Changed implementation\n")
        self.commit("Change")

    def test_exact_endpoint_matches_all_record_types_and_knowledge_states(self):
        source = self.anchor()
        self.entry(sources=[source])
        graph.put(self.conn, {"id": "term", "kind": "concept", "title": "Meaning",
            "summary": "Historical contract.", "knowledge_state": "hypothesis", "sources": [source]},
            "entity", memory)
        graph.put(self.conn, {"id": "edge", "kind": "supports", "from_ref": "entry:one",
            "to_ref": "entity:term", "description": "Observed association.",
            "knowledge_state": "superseded", "sources": [source]}, "edge", memory)
        self.change()
        result = self.impact()
        self.assertEqual({card["ref"] for card in result["results"]}, {"entry:one", "entity:term", "edge:edge"})
        self.assertTrue(result["coverage"]["complete"])
        self.assertFalse(result["no_matches"])
        self.assertEqual(result["comparison"]["base_commit"], self.base)
        self.assertEqual(result["comparison"]["target_commit"], self.git("rev-parse", "HEAD").strip())
        edge = next(card for card in result["results"] if card["ref"] == "edge:edge")
        self.assertEqual(edge["summary_excerpt"], "Observed association.")
        self.assertIn("Historical", edge["state_note"])
        self.assertEqual(edge["read_more"], {"command": "edge get", "id": "edge"})

    def test_rename_delete_old_and_new_paths_and_unmatched_paths(self):
        self.entry("old", sources=[self.anchor("old.txt")])
        self.entry("deleted", sources=[self.anchor("delete.txt")])
        self.git("mv", "old.txt", "new.txt")
        (self.repo / "delete.txt").unlink()
        self.commit("Rename and remove")
        result = self.impact()
        self.assertEqual({item["path"]: item["status"] for item in result["changes"]},
                         {"old.txt": "D", "new.txt": "A", "delete.txt": "D"})
        self.assertEqual({card["ref"] for card in result["results"]}, {"entry:old", "entry:deleted"})
        self.assertEqual(result["unmatched_paths"], ["new.txt"])
        renamed_commit = self.git("rev-parse", "HEAD").strip()
        self.entry("new", sources=[{"reference": "git:" + renamed_commit + ":new.txt"}])
        self.assertEqual(self.impact()["unmatched_paths"], [])

    def test_worktree_includes_tracked_and_untracked_but_excludes_ignored(self):
        self.entry()
        (self.repo / "code.txt").write_text("Uncommitted\n")
        (self.repo / "new.txt").write_text("Untracked\n")
        (self.repo / ".gitignore").write_text("ignored.txt\n")
        (self.repo / "ignored.txt").write_text("Ignored\n")
        result = self.impact(worktree=True)
        paths = {item["path"]: item["status"] for item in result["changes"]}
        self.assertEqual(paths["code.txt"], "M")
        self.assertEqual(paths["new.txt"], "?")
        self.assertNotIn("ignored.txt", paths)
        self.assertIsNone(result["comparison"]["target_commit"])
        self.assertIn("sequential", result["comparison"]["semantics"])
        self.assertEqual(self.impact()["results"], [])

    def test_same_path_other_repository_does_not_match(self):
        other = self.root / "other"
        self.git("clone", "-q", str(self.repo), str(other))
        memory.project_put(self.conn, {"id": "other", "name": "Other", "repo_path": str(other)})
        self.entry("foreign", sources=[self.anchor(project="other", repo=other)], projects=["other"])
        self.change()
        result = self.impact()
        self.assertTrue(result["no_matches"])
        self.assertEqual(result["results"], [])
        with self.assertRaisesRegex(memory.MemoryError, "separate clone"):
            self.impact(repo=str(other))

    def test_legacy_file_and_bare_commit_and_scope_cache(self):
        self.entry("bare", sources=[{"reference": "git:" + self.base}])
        self.entry("file", sources=[{"reference": "git:" + self.base + ":code.txt"}])
        self.change()
        with patch.object(git_legacy, "resolve", wraps=git_legacy.resolve) as resolve:
            result = self.impact()
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual({card["ref"] for card in result["results"]}, {"entry:bare", "entry:file"})
        self.assertEqual({card["matches"][0]["match_reason"] for card in result["results"]},
                         {"legacy_commit_changed_file", "legacy_file"})

    def test_ambiguous_legacy_commit_is_error_and_not_no_match(self):
        other = self.root / "clone"
        self.git("clone", "-q", str(self.repo), str(other))
        memory.project_put(self.conn, {"id": "other", "name": "Other", "repo_path": str(other)})
        self.entry("ambiguous", sources=[{"reference": "git:" + self.base}], projects=["repo", "other"])
        self.change()
        result = self.impact()
        self.assertFalse(result["coverage"]["complete"])
        self.assertFalse(result["no_matches"])
        self.assertEqual(result["results"], [])
        self.assertEqual(result["errors"][0]["reason"], "legacy_repository_ambiguous")

    def test_legacy_foreign_scope_and_unrelated_path_do_not_probe(self):
        memory.project_put(self.conn, {"id": "other", "name": "Other"})
        self.entry("foreign", sources=[{"reference": "git:" + self.base}], projects=["other"])
        self.entry("unrelated", sources=[{"reference": "git:" + self.base + ":old.txt"}])
        self.change()
        with patch.object(git_legacy, "resolve", side_effect=AssertionError("No probe expected")):
            result = self.impact()
        self.assertEqual(result["results"], [])
        self.assertTrue(result["coverage"]["complete"])

    def test_refs_are_fixed_before_diff_even_if_branch_moves(self):
        self.entry()
        self.change()
        fixed = self.git("rev-parse", "HEAD").strip()
        original = git_impact._changes
        seen = []
        def move_branch(repo, base, target, worktree):
            seen.append((base, target))
            (self.repo / "old.txt").write_text("Branch moved\n")
            self.commit("Concurrent branch movement")
            return original(repo, base, target, worktree)
        with patch.object(git_impact, "_changes", side_effect=move_branch):
            result = self.impact(base="HEAD~1", target="HEAD")
        self.assertEqual(seen, [(self.base, fixed)])
        self.assertEqual(result["changes"], [{"path": "code.txt", "status": "M"}])
        self.assertEqual(result["comparison"]["target_commit"], fixed)

    def test_limits_and_budget_preserve_coverage_and_no_match_distinction(self):
        for index in range(12):
            self.entry("row" + str(index), title="Quoted \" title " * 30)
        self.change()
        result = self.impact(max_chars=2000, limit=3)
        raw = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        self.assertLessEqual(len(raw), 2000)
        self.assertEqual(result["budget"]["output_chars"], len(raw))
        self.assertEqual(result["matching_records"], 12)
        self.assertEqual(result["limit_omitted"], 9)
        self.assertFalse(result["no_matches"])
        self.assertTrue(result["truncated"])
        with patch.object(git_impact, "MAX_RECORDS", 1):
            limited = self.impact()
        self.assertFalse(limited["coverage"]["complete"])
        self.assertFalse(limited["coverage"]["inventory_complete"])

    def test_path_and_legacy_probe_limits_are_explicit(self):
        self.entry("bare", sources=[{"reference": "git:" + self.base}])
        self.change()
        with patch.object(git_impact, "MAX_LEGACY_RESOLUTIONS", 0):
            result = self.impact()
        self.assertEqual(result["coverage"]["legacy_skipped"], 1)
        self.assertFalse(result["no_matches"])
        self.assertFalse(result["coverage"]["complete"])
        (self.repo / "second.txt").write_text("new\n")
        with patch.object(git_impact, "MAX_PATHS", 1):
            result = self.impact(worktree=True)
        self.assertEqual(result["changed_path_count"], 2)
        self.assertEqual(result["coverage"]["changed_paths_omitted"], 1)

    def test_readonly_database_and_repo_remain_unchanged(self):
        self.entry("legacy", sources=[{"reference": "git:" + self.base}])
        self.change()
        before = list(self.conn.iterdump())
        index_before = (self.repo / ".git" / "index").read_bytes()
        files_before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        readonly = memory.connect(self.db, readonly=True)
        self.addCleanup(readonly.close)
        with patch.object(self, "conn", readonly):
            self.impact()
        self.assertEqual(before, list(self.conn.iterdump()))
        self.assertEqual(index_before, (self.repo / ".git" / "index").read_bytes())
        self.assertEqual(files_before, sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*")))

    def test_no_changed_paths_needs_no_inventory_or_legacy_probes(self):
        self.entry("bare", sources=[{"reference": "git:" + self.base}])
        with patch.object(git_impact, "MAX_SECONDS", 0):
            result = self.impact(worktree=True)
        self.assertTrue(result["no_matches"])
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(result["coverage"]["records_scanned"], 0)

    def test_linked_worktree_keeps_repository_identity(self):
        self.entry()
        linked = self.root / "linked"
        self.git("worktree", "add", "-q", "-b", "parallel", str(linked))
        (linked / "code.txt").write_text("Changed linked checkout\n")
        result = self.impact(repo=str(linked), worktree=True)
        self.assertEqual(result["comparison"]["repo_root"], str(linked.resolve()))
        self.assertEqual(result["results"][0]["matches"][0]["repo_id"], "repo")

    def test_error_and_match_details_are_bounded_without_losing_counts(self):
        self.entry("invalid", sources=[{"reference": "git:invalid" + str(i)} for i in range(6)])
        self.entry("valid", sources=[self.anchor()] * 6)
        self.change()
        with patch.object(git_impact, "MAX_ERROR_DETAILS", 2):
            result = self.impact()
        self.assertEqual(len(result["errors"]), 0)
        self.assertEqual(result["coverage"]["source_errors"], 0)
        self.assertEqual(result["coverage"]["unsupported_legacy_sources"], 6)
        self.assertFalse(result["coverage"]["complete"])
        self.assertEqual(result["matching_records"], 1)
        self.assertEqual(result["results"][0]["matches_omitted"], 0)
        self.assertEqual(len(result["results"][0]["matches"]), 6)

    def test_nonoverlapping_unscoped_legacy_path_does_not_poison_coverage(self):
        self.entry("unscoped", sources=[{"reference": "git:" + self.base + ":old.txt"}], projects=[])
        self.change()
        result = self.impact()
        self.assertTrue(result["coverage"]["complete"])
        self.assertTrue(result["no_matches"])
        self.assertEqual(result["coverage"]["unscoped_legacy_sources"], 0)

    def test_overlapping_unscoped_legacy_is_counted_without_repeated_errors(self):
        self.entry("unscoped", sources=[{"reference": "git:" + self.base + ":code.txt"}], projects=[])
        self.change()
        result = self.impact()
        self.assertFalse(result["coverage"]["complete"])
        self.assertFalse(result["no_matches"])
        self.assertEqual(result["coverage"]["unscoped_legacy_sources"], 1)
        self.assertEqual(result["errors"], [])

    def test_worktree_refuses_filters_without_executing_and_commits_still_work(self):
        marker = self.root / "filter-was-run"
        script = self.root / "clean.sh"
        script.write_text("#!/bin/sh\nprintf executed > " + shlex.quote(str(marker)) + "\ncat\n")
        (self.repo / ".gitattributes").write_text("code.txt filter=audit\n")
        self.git("config", "filter.audit.clean", "sh " + shlex.quote(str(script)))
        (self.repo / "code.txt").write_text("Uncommitted\n")
        with self.assertRaisesRegex(git_evidence.GitEvidenceError, "clean/process filters"):
            self.impact(worktree=True)
        self.assertFalse(marker.exists())
        self.impact()
        self.assertFalse(marker.exists())

    def test_large_budget_keeps_more_than_eight_matching_paths(self):
        for index in range(12):
            (self.repo / ("file-%02d.txt" % index)).write_text("old\n")
        self.commit("Many paths")
        many_base = self.git("rev-parse", "HEAD").strip()
        self.entry("many", sources=[{"reference": "git:" + many_base}])
        for index in range(12):
            (self.repo / ("file-%02d.txt" % index)).write_text("new\n")
        self.commit("Change many paths")
        result = self.impact(base=many_base, max_chars=100000)
        match = result["results"][0]["matches"][0]
        self.assertEqual(len(match["changed_paths"]), 12)
        self.assertEqual(match["changed_paths_omitted"], 0)

    def test_direct_file_and_exact_target_rank_before_alphabetic_bare_commits(self):
        for index in range(6):
            self.entry("a-broad-" + str(index), sources=[{"reference": "git:" + self.base}])
        self.change()
        target = self.git("rev-parse", "HEAD").strip()
        self.entry("z-direct", sources=[{"reference": "git:" + target + ":code.txt"}])
        result = self.impact(limit=1, max_chars=6000)
        self.assertEqual(result["results"][0]["ref"], "entry:z-direct")
        self.assertIn("direct_file_source", result["results"][0]["match_reasons"])
        self.assertIn("source_at_target_commit", result["results"][0]["match_reasons"])

    def test_record_rank_combines_direct_and_target_sources(self):
        direct = {"reference": "git:" + self.base + ":code.txt"}
        self.entry("a-direct-only", sources=[direct])
        self.change()
        target = self.git("rev-parse", "HEAD").strip()
        self.entry("z-combined", sources=[direct, {"reference": "git:" + target}])
        result = self.impact(limit=1)
        self.assertEqual(result["results"][0]["ref"], "entry:z-combined")
        self.assertEqual(set(result["results"][0]["match_reasons"]),
                         {"direct_file_source", "source_at_target_commit"})

    def test_budget_preserves_card_breadth_before_secondary_sources(self):
        direct = self.anchor()
        for index in range(5):
            self.entry("record-" + str(index), sources=[direct] * 5)
        self.change()
        result = self.impact(max_chars=9000)
        self.assertEqual(len(result["results"]), 5)
        self.assertEqual(result["budget"]["omitted_results"], 0)
        self.assertTrue(any(card["matches_omitted"] for card in result["results"]))
        self.assertTrue(all(card["matches"] for card in result["results"]))


if __name__ == "__main__":
    unittest.main()
