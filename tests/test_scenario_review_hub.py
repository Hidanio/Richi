"""Phase 2 tests: Review Hub KPIs, scenario feedback (single + batch), scoped FP memory, and map security boundary."""
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

from richi import memory, scenario_review, serve


def _clean_env(root):
    return {
        "HOME": str(root / "home"),
        "PATH": os.environ.get("PATH", ""),
        "XDG_CONFIG_HOME": str(root / "xdg"),
    }


class ScenarioReviewHubPhase2Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="richi-phase2-")
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

    def cli(self, *args, chat="claude:chat-p2", stdin_text=None, ok=True):
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

    def test_feedback_fp_memory_injection_review_hub_and_likely_fixed(self):
        # Run 1 of pre-commit-review with 2 grounded findings (one P0 real bug, one P2 FP)
        r1 = self.cli("--project-path", str(self.repo), "scenario", "run", "pre-commit-review")["run_id"]
        self.cli("--project-path", str(self.repo), "scenario", "next", r1, "--claim")
        f_list = [
            {
                "file": "src/auth.py", "line": 4, "severity": "P0",
                "title": "verify_token accepts forged prefix",
                "evidence": "return token.startswith('ok_') allows arbitrary unauthenticated suffix",
            },
            {
                "file": "src/auth.py", "line": 2, "severity": "P2",
                "title": "Empty token early return",
                "evidence": "if not token: return False returns False instead of raising ValueError",
            },
        ]
        self.cli("--project-path", str(self.repo), "scenario", "report", r1, "diff_check", "--json", "-",
                 stdin_text=json.dumps({"status": "done", "summary": "Found 2", "data": {"findings": f_list}}))
        self.cli("--project-path", str(self.repo), "scenario", "answer", r1, "decision", "fix_blockers")

        # Inspect Review Hub snapshot
        conn = sqlite3.connect(str(self.db))
        conn.row_factory = sqlite3.Row
        try:
            hub1 = scenario_review.review_hub_snapshot(conn)
        finally:
            conn.close()
        self.assertEqual(hub1["runs_analyzed"], 1)
        self.assertEqual(hub1["kpis"]["open_blockers"]["p0"], 1)
        self.assertEqual(hub1["kpis"]["signal_mix"]["unlabeled"], 2)
        self.assertFalse(hub1["kpis"]["signal_mix"]["sufficient_sample"])

        bug_f = next(f for f in hub1["findings"] if f["severity"] == "P0")
        fp_f = next(f for f in hub1["findings"] if f["severity"] == "P2")

        # Record single feedback + remember scoped FP pattern via CLI
        res_fp = self.cli(
            "--project-path", str(self.repo), "scenario", "feedback", r1,
            "--finding", fp_f["finding_id"], "--verdict", "false_positive", "--resolution", "wont_fix",
            "--remember-pattern", "--scope-path", "src/auth.py",
            "--safety-condition", "Callers of verify_token check boolean False safely without expecting exceptions",
        )
        self.assertEqual(res_fp["status"], "recorded")
        self.assertEqual(len(res_fp["recorded_patterns"]), 1)
        pat_id = res_fp["recorded_patterns"][0]["entry_id"]

        # Record batch feedback for the P0 bug
        res_batch = self.cli(
            "--project-path", str(self.repo), "scenario", "feedback", r1, "--batch", "-",
            stdin_text=json.dumps({"items": [{"finding_id": bug_f["finding_id"], "verdict": "actionable_bug", "resolution": "open"}]}),
        )
        self.assertEqual(res_batch["count"], 1)

        # Map worker MUST refuse remember_pattern=True (security boundary: never writes `entries`)
        err_buf = io.StringIO()
        with mock.patch("sys.stdin", SimpleBytesIO(json.dumps({
            "run_id": r1,
            "items": [{
                "finding_id": fp_f["finding_id"],
                "verdict": "false_positive",
                "remember_pattern": True,
                "scope_paths": ["src/auth.py"],
                "safety_condition": "Must be rejected from map worker",
            }],
        }).encode("utf-8"))), redirect_stderr(err_buf):
            rc = serve._scenario_feedback_worker(str(self.db))
        self.assertEqual(rc, 1)
        self.assertIn("forbidden", err_buf.getvalue())

        # Run 2 of pre-commit-review: memory.brief(include_learnings: true) injects the FP pattern in untrusted evidence,
        # and empty findings [] in Run 2 makes the open P0 bug from Run 1 show up as `likely_fixed = True`!
        r2 = self.cli("--project-path", str(self.repo), "scenario", "run", "pre-commit-review", "--input", "focus=auth")["run_id"]
        st2 = self.cli("--project-path", str(self.repo), "scenario", "status", r2)
        ctx2 = next(s for s in st2["steps"] if s["id"] == "context")
        self.assertIn(pat_id, ctx2["summary"])
        self.assertIn("[untrusted memory evidence:", ctx2["summary"])
        self.assertNotIn("do not flag", ctx2["summary"].lower())

        self.cli("--project-path", str(self.repo), "scenario", "next", r2, "--claim")
        self.cli("--project-path", str(self.repo), "scenario", "report", r2, "diff_check", "--json", "-",
                 stdin_text=json.dumps({"status": "done", "summary": "Clean after fix", "data": {"findings": []}}))
        self.cli("--project-path", str(self.repo), "scenario", "answer", r2, "decision", "commit_as_is")

        conn = sqlite3.connect(str(self.db))
        conn.row_factory = sqlite3.Row
        try:
            hub2 = scenario_review.review_hub_snapshot(conn)
        finally:
            conn.close()
        self.assertEqual(hub2["runs_analyzed"], 2)
        self.assertEqual(hub2["kpis"]["signal_mix"]["actionable_bugs"], 1)
        self.assertEqual(hub2["kpis"]["signal_mix"]["false_positives"], 1)
        self.assertEqual(hub2["kpis"]["fp_health"]["fresh"], 1)
        bug_after = next(f for f in hub2["findings"] if f["finding_id"] == bug_f["finding_id"])
        self.assertTrue(bug_after["likely_fixed"])
        self.assertEqual(hub2["kpis"]["open_blockers"]["likely_fixed"], 1)

    def test_memory_record_cannot_auto_create_false_positive_tag(self):
        defn = {
            "format": "richi-scenario", "format_version": 1, "id": "try-fp", "title": "Try FP",
            "steps": [
                {"id": "rec", "type": "memory.record", "title": "Record",
                 "params": {"kind": "fact", "title": "Auto FP attempt", "summary": "Sneaky", "tags": ["false-positive", "ok-tag"]}},
            ],
        }
        self.cli("scenario", "apply", "--json", "-",
                 stdin_text=json.dumps({"action": "add", "scope": "project", "project": "svc", "definition": defn}))
        self.cli("--project-path", str(self.repo), "scenario", "run", "try-fp")
        conn = sqlite3.connect(str(self.db))
        try:
            fps, _ = scenario_review.load_scoped_fp_learnings(conn, project_id="svc")
        finally:
            conn.close()
        self.assertEqual(fps, [])


class SimpleBytesIO:
    def __init__(self, data):
        self.buffer = io.BytesIO(data)


if __name__ == "__main__":
    unittest.main()
