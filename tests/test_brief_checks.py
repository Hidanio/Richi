"""Opt-in brief source checks on disposable repositories and databases only."""
import copy
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

import test_task_brief as fixtures

from richi import git_evidence
from richi import git_legacy
from richi import git_sources
from richi import memory
from richi import task_brief


class BriefChecksTests(unittest.TestCase):
    # Borrow helpers, without inheriting unrelated test methods.
    setUp = fixtures.TaskBriefTests.setUp
    git = fixtures.TaskBriefTests.git
    commit = fixtures.TaskBriefTests.commit
    anchor = fixtures.TaskBriefTests.anchor
    entry = fixtures.TaskBriefTests.entry
    change = fixtures.TaskBriefTests.change
    brief = fixtures.TaskBriefTests.brief
    cli = fixtures.TaskBriefTests.cli
    entity = fixtures.TaskBriefTests.entity
    edge = fixtures.TaskBriefTests.edge
    by_ref = staticmethod(fixtures.TaskBriefTests.by_ref)
    assert_budget = fixtures.TaskBriefTests.assert_budget

    def args(self, **changes):
        values = dict(check_sources=False, check_limit=None)
        values.update(changes)
        return fixtures.TaskBriefTests.args(self, **values)

    def seed(self, sources, identity="seed", **changes):
        return self.entry(identity, sources=sources, aliases=["needleword"], **changes)

    def source_results(self, value, ref="entry:seed"):
        return {result["source_index"]: result
                for result in self.by_ref(value)[ref]["source_checks"]["results"]}

    def assert_check_counts(self, value):
        coverage = value["source_check_coverage"]
        self.assertTrue(coverage["requested"])
        self.assertEqual(coverage["git_sources"], coverage["checked"] + coverage["not_checked"])
        self.assertEqual(coverage["checked"], coverage["results_returned"] + coverage["results_omitted"])
        self.assertEqual(sum(coverage["counts"].values()), coverage["checked"])
        returned = 0
        for card in value["results"]:
            checks = card.get("source_checks")
            if checks is None:
                continue
            self.assertEqual(checks["source_count"], checks["git_source_count"] + checks["non_git_source_count"])
            self.assertEqual(checks["git_source_count"], checks["checked_count"] + checks["not_checked_count"])
            self.assertEqual(sum(checks["not_checked_reasons"].values()), checks["not_checked_count"])
            self.assertEqual(checks["checked_count"], len(checks["results"]) + checks["results_omitted"])
            returned += len(checks["results"])
        self.assertEqual(coverage["results_returned"], returned)

    def test_default_keeps_old_namespace_and_performs_no_git(self):
        self.seed([self.anchor()])
        old_args = fixtures.TaskBriefTests.args(self)
        with patch.object(git_evidence, "_git", side_effect=AssertionError("Unexpected Git call")):
            actual = task_brief.brief(self.conn, old_args, memory, self.db)
        self.assertNotIn("source_check_coverage", actual)
        self.assertNotIn("source_checks", actual["results"][0])

    def test_unchanged_changed_deleted_and_unavailable_are_distinct(self):
        missing_blob = copy.deepcopy(self.anchor("old.txt"))
        missing_blob["git"]["blob"] = "0" * 40
        sources = [self.anchor("old.txt"), self.anchor(), self.anchor("delete.txt"), missing_blob]
        for index, source in enumerate(sources, 1):
            source["label"] = "Evidence " + str(index)
        self.seed(sources)
        (self.repo / "code.txt").write_text("Changed implementation\n")
        (self.repo / "delete.txt").unlink()
        self.commit("Change and delete")
        target = self.git("rev-parse", "HEAD").strip()
        actual = self.brief(check_sources=True, max_chars=100000)
        results = self.source_results(actual)
        self.assertEqual({i: r["status"] for i, r in results.items()},
                         {1: "unchanged", 2: "changed", 3: "deleted", 4: "unavailable"})
        for index in (1, 2, 3):
            result = results[index]
            self.assertEqual(result["captured_commit"], self.base)
            self.assertEqual(result["captured_mode"], "commit")
            self.assertEqual(result["target_commit"], target)
            self.assertEqual(result["target_mode"], "commit")
            self.assertEqual(result["repo_id"], "repo")
            self.assertEqual(result["label"], sources[index - 1]["label"])
            self.assertEqual(result["observed_at"], sources[index - 1]["observed_at"])
            self.assertEqual(result["ancestry"], "ahead")
        self.assertFalse(actual["source_check_coverage"]["complete"])
        self.assertTrue(actual["results"][0]["needs_recheck"])
        self.assertTrue(actual["results"][0]["recheck_reasons"])
        self.assert_check_counts(actual)

    def test_full_record_source_indices_extend_beyond_two_compact_sources(self):
        source = self.anchor()
        self.seed([{"reference": "https://example.invalid/report/" + str(i)} for i in range(3)] + [source])
        actual = self.brief(check_sources=True, max_chars=100000)
        card = actual["results"][0]
        self.assertEqual(len(card["sources"]), 2)
        self.assertEqual(set(self.source_results(actual)), {4})
        checks = card["source_checks"]
        self.assertEqual((checks["source_count"], checks["git_source_count"], checks["non_git_source_count"]), (4, 1, 3))
        self.assertEqual(self.source_results(actual)[4]["status"], "unchanged")
        self.assertEqual(actual["source_check_coverage"]["source_limit"], 12)
        self.assert_check_counts(actual)

    def test_unchanged_does_not_clear_current_warning_or_modify_records(self):
        self.seed([self.anchor()], work_state="merged", verified_at="2020-01-01")
        before = list(self.conn.iterdump())
        index = (self.repo / ".git" / "index").read_bytes()
        paths = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        actual = self.brief(query="needleword сейчас", check_sources=True, max_chars=100000)
        card = self.by_ref(actual)["entry:seed"]
        self.assertEqual(self.source_results(actual)[1]["status"], "unchanged")
        self.assertTrue(card["needs_recheck"])
        self.assertTrue(card["recheck_reasons"])
        self.assertEqual(card["verified_at"], "2020-01-01")
        self.assertEqual(card["work_state"], "merged")
        self.assertEqual(list(self.conn.iterdump()), before)
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), index)
        self.assertEqual(sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")), paths)

    def test_limit_uses_round_robin_cards_and_structured_before_legacy(self):
        self.seed([{"reference": "git:" + self.base + ":code.txt"},
                   self.anchor("old.txt"), self.anchor("delete.txt")], identity="a")
        self.seed([self.anchor()], identity="b")
        actual = self.brief(check_sources=True, check_limit=2, max_chars=100000)
        self.assertEqual(set(self.source_results(actual, "entry:a")), {2})
        self.assertEqual(set(self.source_results(actual, "entry:b")), {1})
        coverage = actual["source_check_coverage"]
        self.assertEqual((coverage["git_sources"], coverage["checked"], coverage["not_checked"]), (4, 2, 2))
        self.assertFalse(coverage["complete"])
        self.assert_check_counts(actual)

    def test_legacy_file_checked_bare_skipped_and_malformed_unavailable(self):
        self.seed([{"reference": "https://example.invalid/report"},
                   {"reference": "git:" + self.base + ":code.txt"},
                   {"reference": "git:" + self.base},
                   {"reference": "git:bad-format"}])
        with patch.object(git_legacy, "commit_files", side_effect=AssertionError("Bare expansion is forbidden")):
            actual = self.brief(check_sources=True, max_chars=100000)
        checks = actual["results"][0]["source_checks"]
        results = self.source_results(actual)
        self.assertEqual(results[2]["status"], "unchanged")
        self.assertEqual(results[4]["status"], "unavailable")
        self.assertNotIn(3, results)
        self.assertEqual(checks["not_checked_reasons"].get("bare_commit_requires_path"), 1)
        self.assertEqual((checks["checked_count"], checks["not_checked_count"]), (2, 1))
        self.assertFalse(actual["source_check_coverage"]["complete"])
        self.assert_check_counts(actual)

    def test_legacy_stored_hash_is_validated_even_after_same_file_was_checked(self):
        reference = "git:" + self.base + ":code.txt"
        recorded_hash = self.anchor()["sha256"]
        wrong_hash = "0" * 64
        self.assertNotEqual(recorded_hash, wrong_hash)
        # Matching/absent hashes populate the same commit:path observation
        # cache first; neither may hide a later source's conflicting evidence.
        self.seed([{"reference": reference, "sha256": recorded_hash},
                   {"reference": reference},
                   {"reference": reference, "sha256": wrong_hash}])
        before = list(self.conn.iterdump())
        actual = self.brief(check_sources=True, max_chars=100000)
        results = self.source_results(actual)
        self.assertEqual({i: result["status"] for i, result in results.items()},
                         {1: "unchanged", 2: "unchanged", 3: "unavailable"})
        self.assertIn("stored observation hash matches", results[1]["legacy_note"].lower())
        self.assertIn("no historical observation hash", results[2]["legacy_note"].lower())
        self.assertNotEqual(results[1]["legacy_note"], results[2]["legacy_note"])
        self.assertIn("legacy_hash_mismatch", results[3]["reason"])
        self.assertFalse(actual["source_check_coverage"]["complete"])
        self.assertTrue(actual["results"][0]["needs_recheck"])
        self.assertEqual(list(self.conn.iterdump()), before)
        self.assert_check_counts(actual)

    def foreign_source(self):
        other = self.root / "foreign"
        self.git("clone", "-q", str(self.repo), str(other))
        self.git("config", "user.name", "Fixture", cwd=other)
        self.git("config", "user.email", "fixture@example.invalid", cwd=other)
        memory.project_put(self.conn, {"id": "other", "name": "Other", "repo_path": str(other)})
        source = self.anchor(project="other", repo=other)
        (other / "code.txt").write_text("Foreign change\n")
        self.git("add", "code.txt", cwd=other)
        self.git("commit", "-qm", "Foreign change", cwd=other)
        return other, source, self.git("rev-parse", "HEAD", cwd=other).strip()

    def test_explicit_project_target_does_not_check_foreign_repository(self):
        other, foreign, _ = self.foreign_source()
        self.seed([self.anchor(), foreign], projects=["repo", "other"])
        with patch.object(git_evidence, "check", wraps=git_evidence.check) as checked:
            actual = self.brief(check_sources=True, project="repo", target=self.base, max_chars=100000)
        results = self.source_results(actual)
        self.assertEqual(set(results), {1})
        self.assertEqual(results[1]["target_commit"], self.base)
        self.assertEqual(results[1]["status"], "unchanged")
        self.assertEqual(actual["results"][0]["source_checks"]["not_checked_count"], 1)
        self.assertTrue(actual["results"][0]["source_checks"]["not_checked_reasons"])
        self.assertTrue(checked.call_args_list)
        self.assertFalse(any(str(other) in str(call) for call in checked.call_args_list))
        self.assert_check_counts(actual)

    def test_without_project_each_repository_uses_its_own_head(self):
        _, foreign, foreign_head = self.foreign_source()
        self.seed([self.anchor(), foreign], projects=["repo", "other"])
        actual = self.brief(check_sources=True, max_chars=100000)
        results = self.source_results(actual)
        self.assertEqual(results[1]["target_commit"], self.base)
        self.assertEqual(results[1]["status"], "unchanged")
        self.assertEqual(results[2]["target_commit"], foreign_head)
        self.assertEqual(results[2]["status"], "changed")
        self.assertTrue(actual["source_check_coverage"]["complete"])
        self.assert_check_counts(actual)

    def test_committed_target_stays_pinned_across_multiple_sources_and_head_move(self):
        self.seed([self.anchor(), self.anchor("old.txt"), self.anchor("delete.txt")])
        self.change()
        head = self.git("rev-parse", "HEAD").strip()
        original = git_evidence.check
        targets = []

        def move_after_target_pinned(*args, **kwargs):
            targets.append(kwargs["target"])
            if len(targets) == 1:
                (self.repo / "old.txt").write_text("New HEAD must not become this batch's target\n")
                self.commit("Move HEAD after pinning committed comparison")
            return original(*args, **kwargs)

        with patch.object(git_evidence, "check", side_effect=move_after_target_pinned):
            actual = self.brief(check_sources=True, max_chars=100000)
        self.assertNotEqual(self.git("rev-parse", "HEAD").strip(), head)
        self.assertEqual(targets, [head] * 3)
        self.assertEqual({r["target_commit"] for r in self.source_results(actual).values()}, {head})
        self.assertEqual(self.source_results(actual)[2]["status"], "unchanged")

    def test_combined_diff_reuses_its_exact_target_sha_for_source_checks(self):
        self.seed([self.anchor()])
        self.change()
        head = self.git("rev-parse", "HEAD").strip()
        original = git_evidence.check
        targets = []

        def move_between_diff_and_source_check(*args, **kwargs):
            targets.append(kwargs["target"])
            # Restore the captured bytes at a newer HEAD: a mutable comparison
            # would incorrectly report unchanged instead of the diff's changed.
            captured = self.git("show", self.base + ":code.txt")
            (self.repo / "code.txt").write_text(captured)
            self.commit("Move HEAD after impact comparison")
            return original(*args, **kwargs)

        with patch.object(git_evidence, "check", side_effect=move_between_diff_and_source_check):
            actual = self.brief(check_sources=True, project="repo", base=self.base,
                                target="HEAD", max_chars=100000)
        self.assertNotEqual(self.git("rev-parse", "HEAD").strip(), head)
        self.assertEqual(targets, [head])
        self.assertEqual(actual["comparison"]["target_commit"], head)
        self.assertEqual(self.source_results(actual)[1]["target_commit"], actual["comparison"]["target_commit"])
        self.assertEqual(self.source_results(actual)[1]["status"], "changed")
        self.assertTrue(self.by_ref(actual)["entry:seed"]["needs_recheck"])
        self.assertTrue(self.by_ref(actual)["entry:seed"]["recheck_reasons"])

    def test_linked_worktree_checks_actual_bytes_with_explicit_repository(self):
        self.seed([self.anchor()])
        linked = self.root / "linked"
        self.git("worktree", "add", "-q", "-b", "check-worktree", str(linked))
        (linked / "code.txt").write_text("Uncommitted linked worktree bytes\n")
        actual = self.brief(check_sources=True, project="repo", repo=str(linked),
                            worktree=True, max_chars=100000)
        result = self.source_results(actual)[1]
        self.assertEqual(result["status"], "changed")
        self.assertEqual(result["target_mode"], "worktree")
        self.assertEqual(result["target_commit"], self.base)
        self.assertEqual(result["captured_mode"], "commit")
        self.assertEqual(result["repo_id"], "repo")

    def test_worktree_head_change_invalidates_earlier_results_in_the_same_repo(self):
        self.seed([self.anchor(), self.anchor("old.txt")])
        original = git_evidence.check
        moved = []

        def move_after_first_check(*args, **kwargs):
            result = original(*args, **kwargs)
            if not moved:
                (self.repo / "new-head.txt").write_text("Concurrent commit in disposable fixture\n")
                self.commit("Move HEAD during check batch")
                moved.append(True)
            return result

        with patch.object(git_evidence, "check", side_effect=move_after_first_check):
            actual = self.brief(check_sources=True, project="repo", worktree=True, max_chars=100000)
        self.assertTrue(moved)
        self.assertEqual({r["status"] for r in self.source_results(actual).values()}, {"unavailable"})
        self.assertEqual(actual["source_check_coverage"]["counts"].get("unavailable"), 2)
        self.assertFalse(actual["source_check_coverage"]["complete"])
        self.assert_check_counts(actual)

    def test_missing_repository_and_missing_worktree_snapshot_are_unavailable(self):
        missing_repo = self.anchor()
        missing_repo["git"]["repo_id"] = "unregistered"
        (self.repo / "code.txt").write_text("Captured dirty content\n")
        artifacts = git_sources._artifacts(self.db)
        missing_snapshot = git_evidence.capture(str(self.repo), "repo", "code.txt",
                                                worktree=True, artifact_dir=artifacts)
        (artifacts / missing_snapshot["git"]["snapshot"]).unlink()
        self.seed([missing_repo, missing_snapshot])
        actual = self.brief(check_sources=True, max_chars=100000)
        self.assertEqual({r["status"] for r in self.source_results(actual).values()}, {"unavailable"})
        self.assertEqual(actual["source_check_coverage"]["checked"], 2)
        self.assertFalse(actual["source_check_coverage"]["complete"])
        self.assert_check_counts(actual)

    def test_entry_entity_and_explicit_edge_cards_all_use_full_source_records(self):
        source = self.anchor()
        self.seed([source])
        self.entity("meaning", aliases=["needleword"], sources=[self.anchor("old.txt")])
        self.edge("proof", "entry:seed", "entity:meaning", sources=[source])
        self.change()
        actual = self.brief(check_sources=True, project="repo", base=self.base, max_chars=100000)
        cards = self.by_ref(actual)
        self.assertEqual(set(cards), {"entry:seed", "entity:meaning", "edge:proof"})
        for ref in cards:
            results = self.source_results(actual, ref)
            self.assertEqual(set(results), {1})
            self.assertEqual(results[1]["status"], "unchanged" if ref == "entity:meaning" else "changed")
        self.assert_check_counts(actual)

    def test_check_details_share_json_budget_and_keep_honest_omission_counts(self):
        sources = [self.anchor() for _ in range(8)]
        for index, source in enumerate(sources):
            source["label"] = "Long evidence label " + str(index) + " detail" * 40
        self.seed(sources)
        actual = self.brief(check_sources=True, check_limit=8, max_chars=6000)
        self.assert_budget(actual, 6000)
        self.assertFalse(actual["no_match"])
        coverage = actual["source_check_coverage"]
        self.assertEqual(coverage["checked"], 8)
        self.assertGreater(coverage["results_omitted"], 0)
        self.assertFalse(coverage["complete"])
        self.assertTrue(actual["truncated"])
        self.assert_check_counts(actual)

    def test_second_budget_pass_preserves_first_pass_omitted_record_count(self):
        for index in range(12):
            self.seed([], identity="item-{:02d}".format(index))
        initial = self.brief(limit=12, max_chars=4000)
        self.assertGreater(initial["budget"]["omitted_results"], 0)
        self.assertEqual(len(initial["results"]) + initial["budget"]["omitted_results"], 12)
        checked = self.brief(check_sources=True, limit=12, max_chars=4000)
        self.assertEqual(checked["source_check_coverage"]["scanned_cards"], len(initial["results"]))
        self.assertGreaterEqual(checked["budget"]["omitted_results"], initial["budget"]["omitted_results"])
        self.assertEqual(len(checked["results"]) + checked["budget"]["omitted_results"], 12)
        self.assertFalse(checked["no_match"])
        self.assert_budget(checked, 4000)

    def test_time_limit_is_reported_without_false_full_coverage(self):
        from richi import brief_checks
        self.seed([self.anchor(), self.anchor("old.txt")])
        with patch.object(brief_checks, "CHECK_SECONDS", 0):
            actual = self.brief(check_sources=True, max_chars=100000)
        coverage = actual["source_check_coverage"]
        self.assertTrue(coverage["time_limit_reached"])
        self.assertFalse(coverage["complete"])
        self.assertEqual(coverage["checked"], 0)
        self.assertEqual(coverage["not_checked"], 2)
        self.assert_check_counts(actual)

    def test_expired_operation_deadline_prevents_popen_and_resets_after_error(self):
        with patch.object(git_evidence.subprocess, "Popen",
                          side_effect=AssertionError("Expired deadline must not launch Git")) as process:
            with self.assertRaisesRegex(git_evidence.GitEvidenceError, "time limit"):
                with git_evidence.operation_deadline(time.monotonic() - 1):
                    # A nested caller cannot extend an already expired budget.
                    with git_evidence.operation_deadline(time.monotonic() + 30):
                        git_evidence._git(self.repo, ["rev-parse", "HEAD"])
            process.assert_not_called()
        output, status = git_evidence._git(self.repo, ["rev-parse", "HEAD"])
        self.assertEqual(status, 0)
        self.assertEqual(output.decode().strip(), self.base)

    def test_cli_flag_validation_and_check_only_target(self):
        self.seed([self.anchor()])
        for flags in (("--check-limit", "1"),
                      ("--check-sources", "--check-limit", "0"),
                      ("--check-sources", "--check-limit", "51"),
                      ("--check-sources", "--target", "HEAD"),
                      ("--check-sources", "--worktree"),
                      ("--check-sources", "--repo", str(self.repo))):
            with self.subTest(flags=flags):
                self.cli("needleword", *flags, ok=False)
        process = self.cli("needleword", "--check-sources", "--check-limit", "1",
                           "--project", "repo", "--target", self.base)
        actual = json.loads(process.stdout)
        self.assertEqual(self.source_results(actual)[1]["status"], "unchanged")
        self.assertEqual(actual["source_check_coverage"]["source_limit"], 1)
        self.assertEqual(actual["budget"]["output_chars"], len(process.stdout))
        self.assert_check_counts(actual)


if __name__ == "__main__":
    unittest.main()
