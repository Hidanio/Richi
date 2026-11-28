"""Phase 1 tests: no_dirty_git worktree fingerprint, findings_grounded I/O check, dedup/consensus, and review templates."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from richi import memory, scenario_findings, scenario_format, scenarios


def _clean_env(root):
    return {
        "HOME": str(root / "home"),
        "PATH": os.environ.get("PATH", ""),
        "XDG_CONFIG_HOME": str(root / "xdg"),
    }


class ScenarioGroundedReviewPhase1Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="richi-phase1-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        (self.root / "home").mkdir()
        self.db = self.root / "store.sqlite3"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.test"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=self.repo, check=True)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "auth.py").write_text(
            "def verify_token(token):\n"
            "    if not token:\n"
            "        return False\n"
            "    return token.startswith('ok_')\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self.repo, check=True)
        self.cli("init")
        self.cli("project", "add", str(self.repo), "--id", "svc", "--name", "Svc")

    def cli(self, *args, chat="claude:chat-p1", stdin_text=None, ok=True):
        out, err = io.StringIO(), io.StringIO()
        env = _clean_env(self.root)
        if chat is not None:
            env["RICHI_CHAT_ID"] = chat
        argv = ["--db", str(self.db), "--compact-json"] + list(args)
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(out), redirect_stderr(err):
            if stdin_text is not None:
                with mock.patch("sys.stdin", io.StringIO(stdin_text)):
                    code = memory.main(argv)
            else:
                code = memory.main(argv)
        raw = out.getvalue() if code == 0 else err.getvalue()
        payload = json.loads(raw) if raw.strip().startswith("{") else {"raw": raw}
        if ok:
            self.assertEqual(code, 0, raw)
        else:
            self.assertNotEqual(code, 0, raw)
        return payload

    def test_builtin_review_templates_validate_and_load(self):
        templates = scenarios.builtin_templates()
        self.assertIn("pre-commit-review", templates)
        self.assertIn("bug-hunt", templates)
        for tid in ("pre-commit-review", "bug-hunt", "review", "deep-review"):
            norm = scenario_format.normalize(templates[tid]["definition"])
            self.assertEqual(norm["id"], tid)
        self.assertEqual(templates["bug-hunt"]["definition"]["steps"][1]["matrix_mode"], "replicated")

    def test_no_dirty_git_unchanged_and_clean_modes(self):
        # Start with a pre-existing unstaged edit (common for pre-commit-review!)
        (self.repo / "src" / "auth.py").write_text(
            "def verify_token(token):\n"
            "    if not token:\n"
            "        return False\n"
            "    return token.startswith('ok_')  # edited\n",
            encoding="utf-8",
        )
        run = self.cli("--project-path", str(self.repo), "scenario", "run", "pre-commit-review")
        rid = run["run_id"]
        self.cli("--project-path", str(self.repo), "scenario", "next", rid, "--claim")

        # Mutate the file DURING the read-only step -> no_dirty_git(unchanged) rejects!
        (self.repo / "src" / "auth.py").write_text(
            "def verify_token(token):\n"
            "    return True  # sneaky mutation\n",
            encoding="utf-8",
        )
        # Lease renewal must NOT overwrite original claimed fingerprint!
        self.cli("--project-path", str(self.repo), "scenario", "next", rid, "--claim")
        rej = self.cli("--project-path", str(self.repo), "scenario", "report", rid, "diff_check", "--json", "-",
                       stdin_text=json.dumps({"status": "done", "summary": "Clean", "data": {"findings": []}}))
        self.assertEqual(rej["status"], "rejected")
        self.assertIn("no_dirty_git (unchanged)", json.dumps(rej["reasons"]))

        # Restore original pre-existing dirty content -> accepted (even with empty findings []!)
        (self.repo / "src" / "auth.py").write_text(
            "def verify_token(token):\n"
            "    if not token:\n"
            "        return False\n"
            "    return token.startswith('ok_')  # edited\n",
            encoding="utf-8",
        )
        acc = self.cli("--project-path", str(self.repo), "scenario", "report", rid, "diff_check", "--json", "-",
                       stdin_text=json.dumps({"status": "done", "summary": "No issues found", "data": {"findings": []}}))
        self.assertEqual(acc["status"], "accepted")

        # Also test clean mode rejects dirty worktree
        clean_def = {
            "format": "richi-scenario", "format_version": 1, "id": "clean-only", "title": "Clean Only",
            "steps": [
                {"id": "s1", "type": "instruction", "title": "Check", "params": {"text": "Check"},
                 "checks": [{"kind": "no_dirty_git", "mode": "clean"}]},
            ],
        }
        self.cli("scenario", "apply", "--json", "-",
                 stdin_text=json.dumps({"action": "add", "scope": "project", "project": "svc", "definition": clean_def}))
        r2 = self.cli("--project-path", str(self.repo), "scenario", "run", "clean-only")["run_id"]
        self.cli("--project-path", str(self.repo), "scenario", "next", r2, "--claim")
        rej2 = self.cli("--project-path", str(self.repo), "scenario", "report", r2, "s1", "--json", "-",
                        stdin_text=json.dumps({"status": "done", "summary": "Tried"}))
        self.assertEqual(rej2["status"], "rejected")
        self.assertIn("no_dirty_git (clean)", json.dumps(rej2["reasons"]))

    def test_findings_grounded_rejects_hallucinations_and_accepts_valid(self):
        # Create an escaping symlink to test symlink boundary
        outside = self.root / "outside.txt"
        outside.write_text("secret\nline2\n", encoding="utf-8")
        (self.repo / "escape.py").symlink_to(outside)

        run = self.cli("--project-path", str(self.repo), "scenario", "run", "pre-commit-review")
        rid = run["run_id"]
        self.cli("--project-path", str(self.repo), "scenario", "next", rid, "--claim")

        # 1. Hallucinated line out of range + .git target + symlink escape + bad severity
        bad_findings = [
            {"file": "src/auth.py", "line": 999, "severity": "P0", "title": "Out of range", "evidence": "return token.startswith('ok_')"},
            {"file": ".git/HEAD", "line": 1, "severity": "P1", "title": "Git internals", "evidence": "ref: refs/heads/main"},
            {"file": "escape.py", "line": 1, "severity": "P1", "title": "Symlink escape", "evidence": "secret line content here"},
            {"file": "src/auth.py", "line": 2, "severity": "CRITICAL", "title": "Bad sev", "evidence": "if not token: return False"},
            {"file": "src/auth.py", "line": 2, "severity": "P2", "title": "Short ev", "evidence": "too short"},
        ]
        rej = self.cli("--project-path", str(self.repo), "scenario", "report", rid, "diff_check", "--json", "-",
                       stdin_text=json.dumps({"status": "done", "summary": "Found 5", "data": {"findings": bad_findings}}))
        self.assertEqual(rej["status"], "rejected")
        joined = "\n".join(rej["reasons"])
        self.assertIn("out of range", joined)
        self.assertIn(".git internals", joined)
        self.assertIn("escapes project root", joined)
        self.assertIn("severity must be one of", joined)
        self.assertIn("evidence must be", joined)

        # 2. Valid grounded finding accepted
        good_findings = [
            {
                "file": "src/auth.py",
                "line": 4,
                "severity": "P1",
                "title": "Weak prefix check in verify_token",
                "evidence": "return token.startswith('ok_') accepts any arbitrary suffix without HMAC verification",
            }
        ]
        acc = self.cli("--project-path", str(self.repo), "scenario", "report", rid, "diff_check", "--json", "-",
                       stdin_text=json.dumps({"status": "done", "summary": "Found 1 P1", "data": {"findings": good_findings}}))
        self.assertEqual(acc["status"], "accepted")

    def test_dedupe_findings_complete_linkage_disproved_boundary_and_consensus(self):
        # 1. Replicated passes finding same bug within 12 lines with overlapping identifiers -> consensus_ratio 2/3
        raw = [
            {
                "file": "src/auth.py", "line": 10, "severity": "P1",
                "title": "verify_token skips signature check",
                "evidence": "verify_token only checks token.startswith('ok_') without hmac_compare",
                "status": "confirmed", "step_id": "sweep", "step_key": "sweep.0",
                "matrix_label": "pass-1", "matrix_mode": "replicated",
            },
            {
                "file": "src/auth.py", "line": 14, "severity": "P0",
                "title": "Missing HMAC validation in verify_token",
                "evidence": "token.startswith('ok_') in verify_token allows forged tokens",
                "status": "confirmed", "step_id": "sweep", "step_key": "sweep.1",
                "matrix_label": "pass-2", "matrix_mode": "replicated",
            },
            # Disproved item on the exact same line must NEVER merge with confirmed findings!
            {
                "file": "src/auth.py", "line": 11, "severity": "P1",
                "title": "verify_token skips signature check",
                "evidence": "Disproved: verify_token is called after gateway hmac_compare",
                "status": "disproved", "step_id": "disprove", "step_key": "disprove",
                "matrix_label": "disprove", "matrix_mode": "orthogonal",
            },
            # Orthogonal corroboration (security + api) -> corroborated_by without consensus_ratio
            {
                "file": "src/pay.py", "line": 40, "severity": "P1",
                "title": "Race condition in charge_wallet balance update",
                "evidence": "charge_wallet reads balance before UPDATE without transaction lock",
                "status": "confirmed", "step_id": "angle", "step_key": "angle.0",
                "matrix_label": "security", "matrix_mode": "orthogonal",
            },
            {
                "file": "src/pay.py", "line": 42, "severity": "P2",
                "title": "Non-atomic balance update in charge_wallet",
                "evidence": "charge_wallet updates balance outside BEGIN IMMEDIATE lock",
                "status": "confirmed", "step_id": "angle", "step_key": "angle.1",
                "matrix_label": "api", "matrix_mode": "orthogonal",
            },
        ]
        deduped = scenario_findings.dedupe_findings(raw, replicated_totals={"sweep": 3})
        self.assertEqual(len(deduped), 3)

        auth_confirmed = next(f for f in deduped if f["file"] == "src/auth.py" and f["status"] == "confirmed")
        self.assertEqual(auth_confirmed["severity"], "P0")
        self.assertEqual(auth_confirmed["severity_votes"], {"P1": 1, "P0": 1})
        self.assertEqual(auth_confirmed["consensus_ratio"], {"votes": 2, "total": 3, "step_id": "sweep"})

        auth_disproved = next(f for f in deduped if f["file"] == "src/auth.py" and f["status"] == "disproved")
        self.assertEqual(auth_disproved["occurrences"], 1)

        pay = next(f for f in deduped if f["file"] == "src/pay.py")
        self.assertIsNone(pay["consensus_ratio"])
        self.assertEqual(pay["corroborated_by"], ["security", "api"])
        self.assertEqual(pay["severity"], "P1")
        self.assertEqual(pay["severity_votes"], {"P1": 1, "P2": 1})


if __name__ == "__main__":
    unittest.main()
