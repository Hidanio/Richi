"""Phase 3 tests: 5 daily templates, DAG fork (pin data), Git export/import with --trust-sha256, and Golden Eval."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

from richi import memory, scenario_format, scenarios


def _clean_env(root):
    return {
        "HOME": str(root / "home"),
        "PATH": os.environ.get("PATH", ""),
        "XDG_CONFIG_HOME": str(root / "xdg"),
    }


class ScenarioPhase3Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="richi-phase3-")
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
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=self.repo, check=True)
        self.base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repo, text=True).strip()

        (self.repo / "src" / "pay.py").write_text(
            "def charge_wallet(balance, amount):\n"
            "    if amount < 0:\n"
            "        return balance\n"
            "    return balance - amount\n"
            "\n\n\n\n\n\n\n\n\n\n"
            "def format_currency(amount):\n"
            "    return f'${amount}'\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "head"], cwd=self.repo, check=True)
        self.head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repo, text=True).strip()

        self.cli("init")
        self.cli("project", "add", str(self.repo), "--id", "svc", "--name", "Svc")

    def cli(self, *args, chat="claude:chat-p3", stdin_text=None, ok=True):
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

    def test_all_builtin_templates_validate(self):
        templates = scenarios.builtin_templates()
        expected = {
            "review", "deep-review", "pre-commit-review", "bug-hunt",
            "pr-ready", "spec-feature", "incident-triage", "baseline-check", "release-notes",
        }
        self.assertTrue(expected.issubset(set(templates)))
        for tid in expected:
            norm = scenario_format.normalize(templates[tid]["definition"])
            self.assertEqual(norm["id"], tid)

    def test_dag_fork_freezes_ancestors_and_isolates_fork_run(self):
        defn = {
            "format": "richi-scenario", "format_version": 1, "id": "fork-demo", "title": "Fork Demo",
            "steps": [
                {"id": "step_a", "type": "instruction", "title": "Step A", "needs": [], "params": {"text": "A"}},
                {"id": "step_b", "type": "instruction", "title": "Step B", "needs": ["step_a"], "params": {"text": "B"}},
                {"id": "step_c", "type": "memory.record", "title": "Record C", "needs": ["step_b"],
                 "params": {"kind": "decision", "title": "Result {{run.id}}", "summary": "{{steps.step_b.summary}}", "knowledge_state": "confirmed"}},
            ],
        }
        self.cli("scenario", "apply", "--json", "-",
                 stdin_text=json.dumps({"action": "add", "scope": "project", "project": "svc", "definition": defn}))

        r1 = self.cli("--project-path", str(self.repo), "scenario", "run", "fork-demo")["run_id"]
        self.cli("--project-path", str(self.repo), "scenario", "next", r1, "--claim")
        self.cli("--project-path", str(self.repo), "scenario", "report", r1, "step_a", "--json", "-",
                 stdin_text=json.dumps({"status": "done", "summary": "A-done"}))
        self.cli("--project-path", str(self.repo), "scenario", "next", r1, "--claim")
        self.cli("--project-path", str(self.repo), "scenario", "report", r1, "step_b", "--json", "-",
                 stdin_text=json.dumps({"status": "done", "summary": "B-v1"}))

        # Dry-run preview
        prev = self.cli("--project-path", str(self.repo), "scenario", "fork", r1, "--from", "step_b", "--dry-run")
        self.assertEqual(prev["frozen_steps"], ["step_a"])
        self.assertEqual(prev["recompute_steps"], ["step_b", "step_c"])

        # Actual fork -> step_a is frozen done, step_b is ready, run_kind='fork'
        forked = self.cli("--project-path", str(self.repo), "scenario", "fork", r1, "--from", "step_b")
        f_id = forked["run_id"]
        self.assertEqual(forked["run_kind"], "fork")
        by_id = {s["id"]: s for s in forked["steps"]}
        self.assertEqual(by_id["step_a"]["status"], "done")
        self.assertEqual(by_id["step_a"]["summary"], "A-done")
        self.assertEqual(by_id["step_b"]["status"], "ready")

        # Complete fork: memory.record MUST be downgraded to hypothesis!
        self.cli("--project-path", str(self.repo), "scenario", "next", f_id, "--claim")
        self.cli("--project-path", str(self.repo), "scenario", "report", f_id, "step_b", "--json", "-",
                 stdin_text=json.dumps({"status": "done", "summary": "B-v2"}))
        entry_id = f"decision:scenario-{f_id}-step_c"
        got = self.cli("entry", "get", entry_id)
        self.assertEqual(got["knowledge_state"], "hypothesis")

        # Fork run is excluded from prod stats and refused as baseline
        self.cli("--project-path", str(self.repo), "scenario", "baseline", f_id, ok=False)
        prod_stats = self.cli("--project-path", str(self.repo), "scenario", "stats", "--scenario", "fork-demo")
        self.assertEqual(prod_stats["total_runs"], 1)

    def test_export_and_trust_bound_import(self):
        # Export a built-in template with memory.record (requires trust!)
        out_file = self.root / "spec-feature.json"
        exp = self.cli("scenario", "export", "spec-feature", "--builtin", "--out", str(out_file))
        sha = exp["sha256"]
        self.assertTrue(exp["security_summary"]["requires_trust"])
        self.assertTrue(out_file.is_file())

        # First import without --trust-sha256 -> returns trust_required without saving
        imp1 = self.cli("--project-path", str(self.repo), "scenario", "import", str(out_file))
        self.assertEqual(imp1["status"], "trust_required")
        self.assertEqual(imp1["sha256"], sha)

        # Wrong digest -> rejected
        self.cli("--project-path", str(self.repo), "scenario", "import", str(out_file), "--trust-sha256", "deadbeef", ok=False)

        # Exact digest -> accepted and saved!
        imp2 = self.cli("--project-path", str(self.repo), "scenario", "import", str(out_file), "--trust-sha256", sha)
        self.assertEqual(imp2["status"], "created")
        self.assertEqual(imp2["sha256"], sha)

    def test_golden_eval_memory_isolation_and_paired_bug_diff(self):
        # Seed a project memory fact to verify memory_mode=clean hides it!
        self.cli("entry", "put", "--json", "-", stdin_text=json.dumps({
            "id": "fact:leak-check", "kind": "fact", "title": "Auth token secret hint",
            "summary": "verify_token has a prefix bug", "project_ids": ["svc"],
            "knowledge_state": "confirmed", "sources": [{"reference": "src/auth.py"}],
            "expected_updated_at": None,
        }))

        # Add a fixed-commit golden case with 2 expected bugs and 1 known trap
        case = {
            "id": "auth-pay-golden",
            "title": "Auth + Pay golden benchmark",
            "project_id": "svc",
            "base_rev": self.base_sha,
            "head_rev": self.head_sha,
            "expected_bugs": [
                {"id": "bug-auth", "file": "src/auth.py", "line_range": [3, 4], "symbols": ["verify_token"], "description": "Weak prefix check"},
                {"id": "bug-pay", "file": "src/pay.py", "line_range": [2, 4], "symbols": ["charge_wallet"], "description": "Allows overdraft"},
            ],
            "known_traps": [
                {"id": "trap-empty", "file": "src/auth.py", "line_range": [2, 2], "symbols": ["token"], "description": "Intentional empty return"},
            ],
        }
        added = self.cli("scenario", "golden", "add", "--json", "-", stdin_text=json.dumps(case))
        self.assertEqual(added["status"], "saved")
        listed = self.cli("scenario", "golden", "list", "--project", "svc")
        self.assertEqual(len(listed["cases"]), 1)

        # Eval Run 1 (clean mode): catches 1 bug + triggers 1 trap + 1 extra_unjudged
        ev1 = self.cli("--project-path", str(self.repo), "scenario", "eval", "pre-commit-review",
                       "--case", "auth-pay-golden", "--memory-mode", "clean")
        r1 = ev1["run_id"]
        self.assertEqual(ev1["run_kind"], "eval")
        st1 = self.cli("--project-path", str(self.repo), "scenario", "status", r1)
        ctx1 = next(s for s in st1["steps"] if s["id"] == "context")
        self.assertIn("memory_mode=clean", ctx1["summary"])
        self.assertNotIn("fact:leak-check", ctx1["summary"])

        self.cli("--project-path", str(self.repo), "scenario", "next", r1, "--claim")
        self.cli("--project-path", str(self.repo), "scenario", "report", r1, "diff_check", "--json", "-",
                 stdin_text=json.dumps({
                     "status": "done", "summary": "Eval 1",
                     "data": {"findings": [
                         {"file": "src/auth.py", "line": 4, "severity": "P2", "title": "verify_token prefix", "evidence": "return token.startswith('ok_') is insecure"},
                         {"file": "src/auth.py", "line": 2, "severity": "nit", "title": "empty token check", "evidence": "if not token returns False"},
                         {"file": "src/pay.py", "line": 15, "severity": "nit", "title": "Missing docstring", "evidence": "def format_currency has no docstring at all"},
                     ]},
                 }))
        self.cli("--project-path", str(self.repo), "scenario", "answer", r1, "decision", "fix_blockers")

        score1 = self.cli("--project-path", str(self.repo), "scenario", "eval", "pre-commit-review",
                          "--case", "auth-pay-golden", "--score", r1)
        self.assertEqual(score1["result"]["recall"], 0.5)
        self.assertEqual(score1["result"]["traps_triggered_count"], 1)
        self.assertEqual(score1["result"]["extra_unjudged_count"], 1)

        # Eval Run 2: catches BOTH bugs and 0 traps -> Paired Bug Diff shows newly_caught=['bug-pay'], delta_recall=+0.5!
        ev2 = self.cli("--project-path", str(self.repo), "scenario", "eval", "pre-commit-review",
                       "--case", "auth-pay-golden", "--memory-mode", "clean")
        r2 = ev2["run_id"]
        self.cli("--project-path", str(self.repo), "scenario", "next", r2, "--claim")
        self.cli("--project-path", str(self.repo), "scenario", "report", r2, "diff_check", "--json", "-",
                 stdin_text=json.dumps({
                     "status": "done", "summary": "Eval 2",
                     "data": {"findings": [
                         {"file": "src/auth.py", "line": 4, "severity": "P0", "title": "verify_token bypass", "evidence": "return token.startswith('ok_') without hmac"},
                         {"file": "src/pay.py", "line": 4, "severity": "P1", "title": "Overdraft in charge_wallet", "evidence": "return balance - amount allows negative balance"},
                     ]},
                 }))
        self.cli("--project-path", str(self.repo), "scenario", "answer", r2, "decision", "fix_blockers")

        score2 = self.cli("--project-path", str(self.repo), "scenario", "eval", "pre-commit-review",
                          "--case", "auth-pay-golden", "--score", r2, "--compare-run", r1)
        self.assertEqual(score2["result"]["recall"], 1.0)
        diff = score2["paired_bug_diff"]
        self.assertEqual(diff["newly_caught"], ["bug-pay"])
        self.assertEqual(diff["regressed_missed"], [])
        self.assertEqual(diff["still_caught"], ["bug-auth"])
        self.assertEqual(diff["delta_recall"], 0.5)
        self.assertEqual(diff["delta_traps"], -1)

        # Remove golden case
        rm = self.cli("scenario", "golden", "remove", "auth-pay-golden")
        self.assertEqual(rm["status"], "removed")


if __name__ == "__main__":
    unittest.main()
