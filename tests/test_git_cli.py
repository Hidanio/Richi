"""Public Git workflows, source manifests, recall and maintenance integration."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest



class GitCliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="richi-git-cli-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / "memory.sqlite3"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / "contract.py").write_text("version = 1\n")
        self.git("add", "contract.py")
        self.git("commit", "-qm", "Initial contract")
        self.commit = self.git("rev-parse", "HEAD").strip()
        self.cli("init")
        self.put("project", {"id": "fixture", "name": "Fixture", "repo_path": str(self.repo)}, action="upsert")
        self.put("entry", {"id": "task:contract", "kind": "task", "title": "Version contract",
                           "summary": "Measured contract with preserved history.", "project_ids": ["fixture"],
                           "work_state": "implemented", "knowledge_state": "confirmed",
                           "sources": [{"reference": "fixture://original"}], "tags": ["contract"],
                           "aliases": [], "verified_at": "2026-10-01", "expected_updated_at": None})

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, text=True,
                              capture_output=True, timeout=20).stdout

    def cli(self, *args, ok=True):
        result = subprocess.run([sys.executable, "-B", "-m", "richi", "--db", str(self.db), *args],
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode == 0, ok, result.stderr + result.stdout)
        return json.loads(result.stdout if ok else result.stderr)

    def put(self, family, value, action="put"):
        path = self.root / "input.json"
        path.write_text(json.dumps(value))
        return self.cli(family, action, "--json", str(path))

    def capture_attach(self, *mode):
        path = self.root / "source.json"
        captured = self.cli("sources", "capture", "--project", "fixture", "--path", "contract.py",
                            *mode, "--output", str(path))
        before = self.cli("entry", "get", "task:contract")
        attached = self.cli("sources", "attach", "--ref", "entry:task:contract", "--json", str(path),
                            "--expected-updated-at", before["updated_at"])
        return captured, before, attached, path

    def test_commit_attach_history_recall_manifest_and_punisher(self):
        captured, before, attached, path = self.capture_attach("--rev", "HEAD")
        self.assertEqual(attached["source_index"], 2)
        after = self.cli("entry", "get", "task:contract", "--history")
        for field in ("summary", "verified_at", "project_ids", "work_state", "knowledge_state", "tags"):
            self.assertEqual(before[field], after[field])
        self.assertEqual(after["sources"][-1]["git"]["commit"], self.commit)
        self.assertTrue(after["history"])
        stale = self.cli("sources", "attach", "--ref", "entry:task:contract", "--json", str(path),
                         "--expected-updated-at", before["updated_at"], ok=False)
        self.assertIn("conflict", stale["error"].lower())
        self.assertEqual(self.cli("sources", "attach", "--ref", "entry:task:contract", "--json", str(path),
                                 "--expected-updated-at", after["updated_at"])["status"], "unchanged")
        recalled = self.cli("recall", "task:contract")
        self.assertIn(self.commit, json.dumps(recalled))
        report = self.cli("punisher", "--project", "fixture")
        finding = next(item for item in report["findings"] if item["code"] == "git_evidence_available")
        self.assertEqual(finding["details"]["source_indices"], [2])
        manifest = self.root / "manifest.json"
        self.cli("sources", "snapshot", "--ref", "entry:task:contract", "--output", str(manifest))
        self.assertIn(self.commit, manifest.read_text())
        checked = self.cli("sources", "check", "--manifest", str(manifest))
        self.assertFalse(checked["has_changes"])
        immutable = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.assertEqual(self.cli("sources", "git-check", "--project", "fixture")["status"], "unchanged")
        related = self.cli("sources", "related", "--project", "fixture", "--path", "contract.py", "--commit", self.commit)
        self.assertIn("entry:task:contract", json.dumps(related))
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), immutable)

    def test_working_snapshot_survives_later_edits_and_navigation_is_readonly(self):
        (self.repo / "contract.py").write_text("version = 2\n")
        captured, _, _, path = self.capture_attach()
        self.assertEqual(captured["source"]["git"]["mode"], "worktree")
        self.assertTrue(captured["source"]["git"]["dirty"])
        (self.repo / "contract.py").write_text("version = 3\n")
        self.git("add", "contract.py")
        self.git("commit", "-qm", "New contract")
        state = (self.git("rev-parse", "HEAD"), self.git("status", "--porcelain"))
        shown = self.cli("sources", "show", "--ref", "entry:task:contract", "--source", "2")
        self.assertIn("version = 2", json.dumps(shown))
        changes = self.cli("sources", "diff", "--json", str(path), "--target", "HEAD")
        self.assertIn("version = 3", json.dumps(changes))
        checked = self.cli("sources", "git-check", "--ref", "entry:task:contract", "--target", "HEAD")
        self.assertEqual(checked["status"], "changed")
        historic = self.cli("sources", "history", "--json", str(path))
        self.assertIn("Initial contract", json.dumps(historic))
        self.assertNotIn("New contract", json.dumps(historic))
        current = self.cli("sources", "history", "--json", str(path), "--target", "HEAD")
        self.assertIn("New contract", json.dumps(current))
        self.assertEqual((self.git("rev-parse", "HEAD"), self.git("status", "--porcelain")), state)
        small = self.cli("sources", "git-check", "--project", "fixture", "--max-chars", "2000")
        size = len(json.dumps(small, ensure_ascii=False, indent=2)) + 1
        self.assertLessEqual(size, 2000)
        self.assertEqual(small["budget"]["output_chars"], size)


if __name__ == "__main__":
    unittest.main()
