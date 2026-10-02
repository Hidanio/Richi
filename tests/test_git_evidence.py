"""Git evidence preserves exact observed bytes without changing a checkout."""
import copy
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from richi import git_evidence as evidence


class GitEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="git-evidence-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo with spaces"
        self.repo.mkdir()
        self.artifacts = self.root / "artifacts" / "git"
        self.git("init", "-q")
        self.git("config", "user.name", "Evidence Test")
        self.git("config", "user.email", "evidence@example.invalid")
        self.file = self.repo / "code.py"
        self.file.write_bytes(b"answer = 1\n")
        self.first = self.commit("Initial evidence")

    def git(self, *args, cwd=None):
        result = subprocess.run(["git"] + list(args), cwd=str(cwd or self.repo), check=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return result.stdout.decode("utf-8").strip()

    def commit(self, message):
        self.git("add", "--all")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def capture(self, **kwargs):
        options = {"path": "code.py", "artifact_dir": self.artifacts}
        options.update(kwargs)
        return evidence.capture(self.repo, "project:engine", **options)

    def test_commit_capture_uses_committed_bytes_and_does_not_touch_checkout(self):
        self.file.write_bytes(b"answer = 900\n")
        index_before = (self.repo / ".git" / "index").read_bytes()
        source = self.capture()
        self.assertEqual(source["git"]["commit"], self.first)
        self.assertFalse(source["git"]["dirty"])
        self.assertEqual(source["sha256"], hashlib.sha256(b"answer = 1\n").hexdigest())
        self.assertEqual(evidence.show(source, self.repo)["content"], "answer = 1\n")
        self.assertEqual(self.file.read_bytes(), b"answer = 900\n")
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), index_before)
        self.assertFalse(self.artifacts.exists())

    def test_dirty_capture_retains_exact_bytes_after_checkout_changes(self):
        self.file.write_bytes(b"answer = 2\n")
        source = self.capture(worktree=True)
        self.assertTrue(source["git"]["dirty"])
        self.assertEqual(source["git"]["mode"], "worktree")
        self.file.write_bytes(b"answer = 3\n")
        shown = evidence.show(source, self.repo, artifact_dir=self.artifacts)
        self.assertEqual(shown["content"], "answer = 2\n")
        self.assertEqual(evidence.check(source, self.repo, artifact_dir=self.artifacts)["status"], "changed")
        checked = evidence.check(source, self.repo, worktree=True, artifact_dir=self.artifacts)
        self.assertEqual(checked["status"], "changed")
        self.assertEqual(checked["ancestry"], "same")
        self.assertTrue(checked["target"]["dirty"])

    def test_clean_worktree_keeps_snapshot_and_detects_uncommitted_change(self):
        source = self.capture(worktree=True)
        self.assertFalse(source["git"]["dirty"])
        self.assertTrue((self.artifacts / source["git"]["snapshot"]).is_file())
        self.assertEqual(evidence.check(source, self.repo, worktree=True, artifact_dir=self.artifacts)["status"], "unchanged")
        self.file.write_bytes(b"answer = 2\n")
        self.assertEqual(evidence.check(source, self.repo, artifact_dir=self.artifacts)["status"], "unchanged")
        self.assertEqual(evidence.check(source, self.repo, worktree=True, artifact_dir=self.artifacts)["status"], "changed")

    def test_untracked_worktree_has_no_baseline_blob_and_remains_viewable(self):
        (self.repo / "new.py").write_bytes(b"new implementation\n")
        source = self.capture(path="new.py", worktree=True)
        self.assertIsNone(source["git"]["blob"])
        self.assertTrue(source["git"]["dirty"])
        self.assertEqual(evidence.check(source, self.repo, artifact_dir=self.artifacts)["status"], "deleted")
        (self.repo / "new.py").unlink()
        self.assertEqual(evidence.show(source, self.repo, artifact_dir=self.artifacts)["content"], "new implementation\n")

    def test_commit_ancestry_separate_from_content_change(self):
        source = self.capture()
        (self.repo / "other.py").write_bytes(b"another file\n")
        second = self.commit("Unrelated file")
        checked = evidence.check(source, self.repo)
        self.assertEqual(checked["status"], "unchanged")
        self.assertEqual(checked["ancestry"], "ahead")
        self.assertEqual(checked["target"]["commit"], second)
        latest = self.capture()
        checked = evidence.check(latest, self.repo, target=self.first)
        self.assertEqual(checked["ancestry"], "behind")
        self.assertEqual(checked["status"], "unchanged")

    def test_diverged_history_is_not_inferred_from_file_drift(self):
        self.file.write_bytes(b"answer = 2\n")
        self.commit("First branch")
        source = self.capture()
        self.git("checkout", "-q", "-b", "parallel", self.first)
        self.file.write_bytes(b"answer = 3\n")
        self.commit("Parallel branch")
        result = evidence.check(source, self.repo)
        self.assertEqual(result["ancestry"], "diverged")
        self.assertEqual(result["status"], "changed")

    def test_deleted_source_offers_rename_candidate_without_following_it(self):
        source = self.capture()
        self.git("mv", "code.py", "renamed.py")
        self.commit("Rename implementation")
        result = evidence.check(source, self.repo)
        self.assertEqual(result["status"], "deleted")
        self.assertEqual(result["target"]["path"], "code.py")
        self.assertEqual(result["rename_candidates"], [{"path": "renamed.py", "similarity": 100}])
        self.assertIn("not automatically", result["rename_detection"])

    def test_missing_source_commit_is_unavailable(self):
        source = self.capture()
        source["git"]["commit"] = "a" * 40
        source["revision"] = "git:" + "a" * 40
        result = evidence.check(source, self.repo)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["ancestry"], "unknown")

    def test_missing_snapshot_is_unavailable_not_changed(self):
        source = self.capture(worktree=True)
        (self.artifacts / source["git"]["snapshot"]).unlink()
        result = evidence.check(source, self.repo, artifact_dir=self.artifacts)
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("snapshot is unavailable", result["reason"])

    def test_corrupt_snapshot_is_not_trusted_or_overwritten(self):
        source = self.capture(worktree=True)
        snapshot = self.artifacts / source["git"]["snapshot"]
        snapshot.chmod(0o644)
        snapshot.write_bytes(b"corrupt\n")
        result = evidence.check(source, self.repo, artifact_dir=self.artifacts)
        self.assertEqual(result["status"], "unavailable")
        with self.assertRaisesRegex(ValueError, "corrupt"):
            self.capture(worktree=True)
        self.assertEqual(snapshot.read_bytes(), b"corrupt\n")
        self.assertEqual(list(self.artifacts.glob(".pending-*")), [])

    def test_repeated_snapshot_is_content_addressed_and_immutable(self):
        first = self.capture(worktree=True)
        snapshot = self.artifacts / first["git"]["snapshot"]
        before = snapshot.stat().st_mtime_ns
        second = self.capture(worktree=True)
        self.assertEqual(first["git"]["snapshot"], second["git"]["snapshot"])
        self.assertEqual(snapshot.stat().st_mtime_ns, before)
        self.assertEqual(len(list(self.artifacts.iterdir())), 1)

    def test_history_defaults_to_captured_commit_and_can_target_head(self):
        source = self.capture()
        self.file.write_bytes(b"answer = 2\n")
        second = self.commit("New evidence")
        history = evidence.history(source, self.repo)
        self.assertEqual([item["commit"] for item in history["commits"]], [self.first])
        history = evidence.history(source, self.repo, target="HEAD", limit=1)
        self.assertEqual([item["commit"] for item in history["commits"]], [second])
        self.assertTrue(history["truncated"])

    def test_history_follows_rename_backwards(self):
        self.git("mv", "code.py", "renamed.py")
        self.commit("Rename implementation")
        source = self.capture(path="renamed.py")
        history = evidence.history(source, self.repo)
        self.assertEqual(len(history["commits"]), 2)
        self.assertEqual(history["commits"][-1]["commit"], self.first)

    def test_diff_compares_observed_dirty_bytes_not_just_commit(self):
        self.file.write_bytes(b"answer = 2\n")
        source = self.capture(worktree=True)
        self.file.write_bytes(b"answer = 3\n")
        result = evidence.diff(source, self.repo, worktree=True, artifact_dir=self.artifacts)
        self.assertIn("-answer = 2\n", result["patch"])
        self.assertIn("+answer = 3\n", result["patch"])
        self.assertNotIn("-answer = 1\n", result["patch"])
        self.assertFalse(result["binary"])

    def test_diff_bounded_and_binary_honest(self):
        source = self.capture()
        self.file.write_bytes((b"a long replacement line\n") * 50)
        result = evidence.diff(source, self.repo, worktree=True, max_chars=100)
        self.assertLessEqual(len(result["patch"]), 100)
        self.assertTrue(result["truncated"])
        self.file.write_bytes(b"\x00\x01\x02")
        result = evidence.diff(source, self.repo, worktree=True)
        self.assertTrue(result["binary"])
        self.assertIsNone(result["patch"])

    def test_diff_renders_missing_final_newline(self):
        source = self.capture()
        self.file.write_bytes(b"answer = 2")
        result = evidence.diff(source, self.repo, worktree=True)
        self.assertIn("+answer = 2\n\\ No newline at end of file\n", result["patch"])

    def test_show_bounds_output_and_reports_binary(self):
        source = self.capture()
        result = evidence.show(source, self.repo, max_bytes=3)
        self.assertEqual(result["content"], "ans")
        self.assertTrue(result["truncated"])
        self.file.write_bytes(b"\x00\xff\x01")
        source = self.capture(worktree=True)
        result = evidence.show(source, self.repo, artifact_dir=self.artifacts)
        self.assertEqual(result["encoding"], "base64")
        self.assertEqual(result["content"], "AP8B")

    def test_malformed_anchors_cannot_escape_or_reinterpret_evidence(self):
        source = self.capture(worktree=True)
        changes = [dict(version=2), dict(version=True), dict(path="../outside"), dict(path="/absolute"),
                   dict(path="a/../code.py"), dict(path="./code.py"), dict(path="a//b"), dict(path=".git/config"),
                   dict(path="a\\b"), dict(snapshot="../../outside"), dict(snapshot="a" * 64 + ".blob"),
                   dict(commit="HEAD"), dict(blob="short"), dict(dirty=1), dict(mode="commit"), dict(unknown=True)]
        for change in changes:
            with self.subTest(change=change):
                invalid = copy.deepcopy(source)
                invalid["git"].update(change)
                with self.assertRaises(ValueError):
                    evidence.validate_anchor(invalid)

    def test_dirty_snapshot_checks_base_blob_and_dirty_flag_claims(self):
        source = self.capture(worktree=True)
        source["git"]["dirty"] = True
        self.assertEqual(evidence.check(source, self.repo, artifact_dir=self.artifacts)["status"], "unavailable")
        with self.assertRaisesRegex(ValueError, "dirty flag"):
            evidence.show(source, self.repo, artifact_dir=self.artifacts)
        source["git"]["dirty"] = False
        source["git"]["blob"] = "a" * 40
        with self.assertRaisesRegex(ValueError, "commit/path/blob"):
            evidence.show(source, self.repo, artifact_dir=self.artifacts)

    def test_head_race_during_worktree_capture_is_rejected(self):
        original_read = evidence._file_bytes

        def moving_head(root, path):
            data = original_read(root, path)
            (self.repo / "parallel.txt").write_text("simulated concurrent commit\n")
            self.commit("Concurrent checkout activity")
            return data

        with patch.object(evidence, "_file_bytes", side_effect=moving_head):
            with self.assertRaisesRegex(ValueError, "HEAD changed"):
                self.capture(worktree=True)
        self.assertFalse(self.artifacts.exists())

    def test_symlink_directory_and_submodule_are_rejected(self):
        (self.repo / "link.py").symlink_to(self.file)
        (self.repo / "directory").mkdir()
        for path in ("link.py", "directory"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.capture(path=path, worktree=True)
        self.commit("Add a symlink")
        with self.assertRaises(ValueError):
            self.capture(path="link.py")
        self.git("update-index", "--add", "--cacheinfo", "160000," + self.first + ",nested")
        self.git("commit", "-qm", "A gitlink")
        (self.repo / "nested").mkdir()
        (self.repo / "nested" / "inside.py").write_bytes(b"nested source")
        with self.assertRaisesRegex(ValueError, "submodules"):
            self.capture(path="nested/inside.py", worktree=True)

    def test_worktree_does_not_follow_symlink_parent(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "x.py").write_bytes(b"outside")
        (self.repo / "directory").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.capture(path="directory/x.py", worktree=True)

    def test_symlink_artifact_and_snapshot_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        linked = self.root / "linked"
        linked.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            self.capture(worktree=True, artifact_dir=linked)
        source = self.capture(worktree=True)
        snapshot = self.artifacts / source["git"]["snapshot"]
        snapshot.unlink()
        snapshot.symlink_to(self.file)
        self.assertEqual(evidence.check(source, self.repo, artifact_dir=self.artifacts)["status"], "unavailable")

    def test_linked_worktree_shares_common_dir_and_preserves_own_checkout(self):
        linked = self.root / "linked"
        self.git("worktree", "add", "-q", "--detach", str(linked), self.first)
        identity = evidence.identify(self.repo)
        other = evidence.identify(linked)
        self.assertEqual(identity["common_dir"], other["common_dir"])
        self.assertNotEqual(identity["git_dir"], other["git_dir"])
        source = self.capture()
        self.assertEqual(evidence.check(source, linked)["status"], "unchanged")

    def test_unsafe_git_environment_is_ignored(self):
        with patch.dict(os.environ, {"GIT_DIR": str(self.root / "missing"), "GIT_CONFIG_COUNT": "1",
                                    "GIT_CONFIG_KEY_0": "core.fsmonitor", "GIT_CONFIG_VALUE_0": "false"}):
            self.assertEqual(self.capture()["git"]["commit"], self.first)

    def test_promisor_repositories_are_rejected_without_fetching(self):
        self.git("config", "remote.origin.promisor", "true")
        self.git("config", "remote.origin.url", "https://invalid.example/no-network")
        with self.assertRaisesRegex(ValueError, "Partial/promisor"):
            evidence.identify(self.repo)

    def test_external_diff_textconv_and_fsmonitor_are_never_executed(self):
        marker = self.root / "must-not-exist"
        command = self.root / "unsafe.sh"
        command.write_text("#!/bin/sh\ntouch '" + str(marker) + "'\n", encoding="utf-8")
        command.chmod(0o755)
        self.git("config", "core.fsmonitor", str(command))
        self.git("config", "diff.external", str(command))
        self.git("config", "diff.custom.textconv", str(command))
        (self.repo / ".gitattributes").write_text("*.py diff=custom\n", encoding="utf-8")
        source = self.capture()
        self.file.write_bytes(b"new bytes\n")
        self.assertEqual(evidence.diff(source, self.repo, worktree=True)["status"], "changed")
        self.assertFalse(marker.exists())

    def test_source_size_limit_and_git_output_limit(self):
        with patch.object(evidence, "MAX_FILE_BYTES", 4):
            with self.assertRaisesRegex(ValueError, "size limit"):
                self.capture()
            with self.assertRaisesRegex(ValueError, "size limit"):
                self.capture(worktree=True)
        with self.assertRaisesRegex(ValueError, "output exceeded"):
            evidence._git(self.repo, ["show", "HEAD:code.py"], maximum=4)

    def test_git_execution_deadline_is_enforced(self):
        with patch.object(evidence, "TIMEOUT", 0.000001):
            with self.assertRaisesRegex(ValueError, "time limit"):
                evidence._git(self.repo, ["log", "--oneline"])

    def test_option_like_revision_rejected(self):
        for revision in ("--help", "HEAD\nother"):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                self.capture(revision=revision)

    def test_worktree_cannot_claim_a_different_base_revision(self):
        self.file.write_bytes(b"new code\n")
        self.commit("Second")
        with self.assertRaisesRegex(ValueError, "current HEAD"):
            self.capture(worktree=True, revision=self.first)

    def test_shallow_clone_reports_missing_history_honestly(self):
        source = self.capture()
        self.file.write_bytes(b"new code\n")
        self.commit("Second")
        clone = self.root / "shallow"
        self.git("clone", "--quiet", "--depth=1", self.repo.as_uri(), str(clone))
        self.assertTrue(evidence.identify(clone)["shallow"])
        self.assertEqual(evidence.check(source, clone)["status"], "unavailable")
        latest = evidence.capture(clone, "engine", "code.py")
        self.assertTrue(evidence.history(latest, clone)["shallow"])

    def test_sha256_repository_when_supported(self):
        repo = self.root / "sha256"
        result = subprocess.run(["git", "init", "-q", "--object-format=sha256", str(repo)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            self.skipTest("Installed Git does not support SHA-256 repositories")
        self.git("config", "user.name", "Evidence", cwd=repo)
        self.git("config", "user.email", "evidence@example.invalid", cwd=repo)
        (repo / "code.py").write_bytes(b"code\n")
        self.git("add", "code.py", cwd=repo)
        self.git("commit", "-qm", "First", cwd=repo)
        source = evidence.capture(repo, "engine", "code.py")
        self.assertEqual(len(source["git"]["commit"]), 64)
        self.assertEqual(len(source["git"]["blob"]), 64)
        self.assertEqual(evidence.identify(repo)["object_format"], "sha256")
        self.assertEqual(evidence.check(source, repo)["status"], "unchanged")


if __name__ == "__main__":
    unittest.main()
