"""End-to-end CLI tests for `richi scenario ...` on synthetic stores and repositories."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from cli_environment import cli_environment
from richi import memory


class ScenarioCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="richi-scn-cli-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "memory.sqlite3"
        self.repo = self.root / "service"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.test"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.repo, check=True)
        (self.repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
        subprocess.run(["git", "add", "app.py"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self.repo, check=True)

        self.cli("init")
        self.cli("project", "add", str(self.repo), "--id", "service", "--name", "Service")

    def cli(self, *args, chat="claude:chat-1", stdin_text=None, ok=True):
        out, err = io.StringIO(), io.StringIO()
        env = cli_environment(self.root)
        env["RICHI_CHAT_ID"] = chat
        argv = ["--db", str(self.db), "--compact-json"] + list(args)
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(out), redirect_stderr(err):
            if stdin_text is not None:
                with mock.patch("sys.stdin", io.StringIO(stdin_text)):
                    code = memory.main(argv)
            else:
                code = memory.main(argv)
        raw = out.getvalue() if code == 0 else err.getvalue()
        payload = json.loads(raw)
        if ok:
            self.assertEqual(code, 0, raw)
        else:
            self.assertNotEqual(code, 0, raw)
        return payload

    def test_e2e_review_cli_walkthrough_and_chat_handoff(self):
        # Discover templates, copy, plan, run.
        templates = self.cli("scenario", "templates")
        self.assertEqual([item["id"] for item in templates["templates"]], ["review"])
        copied = self.cli("--project-path", str(self.repo), "scenario", "copy", "review",
                          "--project", "service", "--id", "svc-review")
        self.assertEqual(copied["status"], "created")
        listed = self.cli("scenario", "list", "--project", "service")
        self.assertEqual([item["id"] for item in listed["scenarios"]], ["svc-review"])
        shown = self.cli("--project-path", str(self.repo), "scenario", "show", "svc-review", "--agent", "codex")
        self.assertEqual(shown["resolved_skills"]["angle"], "code-review")
        planned = self.cli("--project-path", str(self.repo), "scenario", "plan", "svc-review",
                           "--input", "branch=feature_x", "--input", "angles=security,tests", "--agent", "codex")
        self.assertTrue(planned["ready_to_run"])
        self.assertEqual(planned["run_title"], "Review: feature_x")

        # Start run in chat-1.
        started = self.cli("--project-path", str(self.repo), "scenario", "run", "svc-review",
                           "--input", "branch=feature_x", "--input", "angles=security,tests", chat="claude:chat-1")
        run_id = started["run_id"]
        self.assertEqual(started["status"], "running")

        # Claim angle.0, get rejected once, then report valid findings.
        step0 = self.cli("--project-path", str(self.repo), "scenario", "next", run_id, "--claim", chat="claude:chat-1")
        self.assertEqual(step0["step"]["key"], "angle.0")
        rejected = self.cli("--project-path", str(self.repo), "scenario", "report", run_id, "angle.0",
                            "--json", "-", stdin_text=json.dumps({"status": "done", "summary": "missing findings", "data": {}}),
                            chat="claude:chat-1")
        self.assertEqual(rejected["status"], "rejected")
        accepted = self.cli("--project-path", str(self.repo), "scenario", "report", run_id, "angle.0",
                            "--json", "-", stdin_text=json.dumps({"status": "done", "summary": "1 issue",
                                                                  "data": {"findings": ["S1"]}}),
                            chat="claude:chat-1")
        self.assertEqual(accepted["status"], "accepted")

        # Handoff to chat-2: discover active run, claim angle.1, release, re-claim, report.
        active = self.cli("--project-path", str(self.repo), "scenario", "runs", "--active", chat="codex:chat-2")
        self.assertEqual([item["id"] for item in active["runs"]], [run_id])
        self.cli("--project-path", str(self.repo), "scenario", "claim", run_id, "angle.1", chat="codex:chat-2")
        self.cli("--project-path", str(self.repo), "scenario", "release", run_id, "angle.1", chat="codex:chat-2")
        step1 = self.cli("--project-path", str(self.repo), "scenario", "next", run_id, "--claim", chat="codex:chat-2")
        self.assertEqual(step1["step"]["key"], "angle.1")
        self.cli("--project-path", str(self.repo), "scenario", "report", run_id, "angle.1",
                 "--json", "-", stdin_text=json.dumps({"status": "done", "summary": "0 issues",
                                                       "data": {"findings": []}}),
                 chat="codex:chat-2")

        # Merge step, then human gate answer.
        merge = self.cli("--project-path", str(self.repo), "scenario", "next", run_id, "--claim", chat="codex:chat-2")
        self.assertEqual(merge["step"]["key"], "merge")
        self.assertIn("S1", merge["step"]["text"])
        self.cli("--project-path", str(self.repo), "scenario", "report", run_id, "merge",
                 "--json", "-", stdin_text=json.dumps({"status": "done", "summary": "Merged 1 issue",
                                                       "data": {"findings": ["S1"]}}),
                 chat="codex:chat-2")
        gate = self.cli("--project-path", str(self.repo), "scenario", "next", run_id, chat="codex:chat-2")
        self.assertEqual(gate["step"]["type"], "gate")
        answered = self.cli("--project-path", str(self.repo), "scenario", "answer", run_id, "approve", "all",
                            "--comment", "Ship after fixing S1", chat="codex:chat-2")
        self.assertEqual(answered["run_status"], "done")

        # Verify final status, run task entry, and unified journal.
        status = self.cli("scenario", "status", run_id)
        self.assertEqual(status["status"], "done")
        task = self.cli("entry", "get", status["task_entry_id"])
        self.assertEqual(task["work_state"], "done")
        self.assertIn("scenario-run", task["tags"])
        journal = self.cli("scenario", "log", "--run", run_id, "--entries", "50")
        self.assertTrue(len(journal["entries"]) >= 8)

    def test_apply_and_control_commands_via_cli(self):
        definition = {
            "format": "richi-scenario", "format_version": 1, "id": "mini", "title": "Mini",
            "steps": [{"id": "s1", "type": "instruction", "params": {"text": "do work"}}],
        }
        added = self.cli("scenario", "apply", "--json", "-",
                         stdin_text=json.dumps({"action": "add", "scope": "global", "definition": definition}))
        self.assertEqual(added["status"], "created")
        started = self.cli("scenario", "run", "mini")
        rid = started["run_id"]
        self.cli("scenario", "pause", rid)
        self.cli("scenario", "resume", rid)
        self.cli("scenario", "next", rid, "--claim")
        self.cli("scenario", "report", rid, "s1", "--json", "-",
                 stdin_text=json.dumps({"status": "failed", "summary": "err"}))
        self.cli("scenario", "retry", rid, "s1")
        self.cli("scenario", "skip", rid, "s1", "--reason", "not applicable")
        self.assertEqual(self.cli("scenario", "status", rid)["status"], "done")
        started2 = self.cli("scenario", "run", "mini")
        self.cli("scenario", "cancel", started2["run_id"])
        self.assertEqual(self.cli("scenario", "status", started2["run_id"])["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
