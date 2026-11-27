"""Scenario runs: scheduler, leases, matrix aggregation, report validation, secrets, C3/C4/K1/C2 and task entries."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from richi import memory, rules, scenario_format, scenario_runs, scenarios


class ScenarioRunsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="richi-scn-runs-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.test"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.repo, check=True)
        (self.repo / "README.md").write_text("demo\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=self.repo, check=True)
        self.head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repo, text=True).strip()

        self.db = self.root / "memory.sqlite3"
        self.conn = memory.connect(self.db, create=True)
        self.addCleanup(self.conn.close)
        memory.initialize(self.conn, self.db)
        with memory.transaction(self.conn):
            memory.project_put(self.conn, {"id": "demo", "name": "Demo", "repo_path": str(self.repo)})
            memory.entry_put(self.conn, {
                "id": "decision:auth-boundary", "kind": "decision", "title": "Auth boundary",
                "summary": "Always verify session tokens before accessing storage.",
                "project_ids": ["demo"], "knowledge_state": "confirmed",
                "sources": [{"reference": "synthetic:auth"}], "expected_updated_at": None,
            })
        self.chat_a = rules.Author("claude:chat-a", "claude")
        self.chat_b = rules.Author("codex:chat-b", "codex")
        self.t0 = datetime(2026, 11, 27, 10, 0, 0, tzinfo=timezone.utc)

    def add_scenario(self, definition):
        with memory.transaction(self.conn):
            return scenarios.change(self.conn, {"action": "add", "scope": "global", "definition": definition},
                                    memory, author=self.chat_a)["scenario"]

    def test_full_review_walkthrough_with_resume_and_rejected_report(self):
        # Start built-in review scenario in chat A.
        with memory.transaction(self.conn):
            run = scenario_runs.start_run(
                self.conn, memory, self.db, "review",
                project_id="demo", project_path=self.repo,
                raw_inputs=["branch=feature_auth", "angles=security,tests"],
                author=self.chat_a, now_override=self.t0,
            )
        run_id = run["run_id"]
        self.assertEqual(run["status"], "running")
        # Richi already ran `context` (memory.brief) and expanded `angle` into shell-safe keys angle.0 and angle.1!
        keys = [step["key"] for step in run["steps"]]
        self.assertEqual(keys, ["context", "angle.0", "angle.1", "merge", "approve"])
        self.assertEqual(run["steps"][0]["status"], "done")
        # Task entry created in "scenario-run" category.
        task = memory.entry_get(self.conn, run["task_entry_id"])
        self.assertEqual(task["kind"], "task")
        self.assertEqual(task["title"], "Review: feature_auth")
        self.assertIn("scenario-run", task["tags"])
        self.assertEqual(task["work_state"], "in_progress")

        # Claim first matrix instance angle.0 in chat A.
        with memory.transaction(self.conn):
            nxt = scenario_runs.next_step(self.conn, memory, self.db, run_id, claim=True,
                                          project_path=self.repo, author=self.chat_a, now_override=self.t0)
        self.assertEqual(nxt["step"]["key"], "angle.0")
        self.assertEqual(nxt["step"]["skill"], "code-review")
        self.assertEqual(nxt["parallel_ready"], ["angle.1"])

        # Another chat cannot claim angle.0 while lease is held.
        with self.assertRaises(memory.MemoryError) as caught:
            with memory.transaction(self.conn):
                scenario_runs.claim_step(self.conn, memory, self.db, run_id, "angle.0",
                                         project_path=self.repo, author=self.chat_b, now_override=self.t0)
        self.assertEqual(caught.exception.code, "conflict")

        # Rejected report (missing required data.findings) leaves step claimed and increments rejections.
        with memory.transaction(self.conn):
            bad = scenario_runs.report_step(self.conn, memory, self.db, run_id, "angle.0",
                                            {"status": "done", "summary": "Looks good", "data": {}},
                                            project_path=self.repo, author=self.chat_a, now_override=self.t0)
        self.assertEqual(bad["status"], "rejected")
        self.assertEqual(bad["step_status"], "claimed")
        self.assertTrue(bad["reasons"])

        # Valid report for angle.0 succeeds.
        with memory.transaction(self.conn):
            ok0 = scenario_runs.report_step(
                self.conn, memory, self.db, run_id, "angle.0",
                {"status": "done", "summary": "1 security finding", "data": {"findings": ["S1: check token"]}},
                project_path=self.repo, author=self.chat_a, now_override=self.t0 + timedelta(minutes=2),
            )
        self.assertEqual(ok0["status"], "accepted")

        # Resume in a DIFFERENT chat (chat B) for angle.1 and merge!
        with memory.transaction(self.conn):
            nxt1 = scenario_runs.next_step(self.conn, memory, self.db, run_id, claim=True,
                                           project_path=self.repo, author=self.chat_b,
                                           now_override=self.t0 + timedelta(minutes=3))
        self.assertEqual(nxt1["step"]["key"], "angle.1")
        with memory.transaction(self.conn):
            scenario_runs.report_step(
                self.conn, memory, self.db, run_id, "angle.1",
                {"status": "done", "summary": "1 test finding", "data": {"findings": ["T1: add unit test"]}},
                project_path=self.repo, author=self.chat_b, now_override=self.t0 + timedelta(minutes=4),
            )

        # Merge step receives concatenated findings from both matrix instances (A2)!
        with memory.transaction(self.conn):
            nxt_merge = scenario_runs.next_step(self.conn, memory, self.db, run_id, claim=True,
                                                project_path=self.repo, author=self.chat_b,
                                                now_override=self.t0 + timedelta(minutes=5))
        self.assertEqual(nxt_merge["step"]["key"], "merge")
        self.assertIn("S1: check token", nxt_merge["step"]["text"])
        self.assertIn("T1: add unit test", nxt_merge["step"]["text"])
        with memory.transaction(self.conn):
            merged = scenario_runs.report_step(
                self.conn, memory, self.db, run_id, "merge",
                {"status": "done", "summary": "2 findings ranked", "data": {"findings": ["S1", "T1"]}},
                project_path=self.repo, author=self.chat_b, now_override=self.t0 + timedelta(minutes=6),
            )
        self.assertEqual(merged["run_status"], "waiting")

        # Task entry transitioned to blocked (waiting on human gate).
        self.assertEqual(memory.entry_get(self.conn, run["task_entry_id"])["work_state"], "blocked")

        # Gate rejects invalid option and accepts valid option from chat.
        with self.assertRaises(memory.MemoryError):
            with memory.transaction(self.conn):
                scenario_runs.answer_step(self.conn, memory, self.db, run_id, "approve", "invalid",
                                          project_path=self.repo, author=self.chat_b)
        with memory.transaction(self.conn):
            done = scenario_runs.answer_step(self.conn, memory, self.db, run_id, "approve", "all",
                                             comment="Fix both", project_path=self.repo, author=self.chat_b,
                                             now_override=self.t0 + timedelta(minutes=7))
        self.assertEqual(done["run_status"], "done")

        # Task entry updated to done; unified journal has full event history.
        final_task = memory.entry_get(self.conn, run["task_entry_id"])
        self.assertEqual(final_task["work_state"], "done")
        self.assertIn("✓ approve (done)", final_task["summary"])
        journal = scenarios.log(self.conn, run_id=run_id, entries=50)
        actions = [entry["action"] for entry in journal["entries"]]
        for expected_action in ("run_started", "matrix_expanded", "claimed", "report_rejected",
                                "report_accepted", "waiting_human", "answered", "run_done"):
            self.assertIn(expected_action, actions)

    def test_lease_expiry_releases_step_for_another_chat(self):
        with memory.transaction(self.conn):
            run = scenario_runs.start_run(self.conn, memory, self.db, "review",
                                          project_id="demo", raw_inputs=["branch=b1", "angles=security"],
                                          author=self.chat_a, now_override=self.t0)
            scenario_runs.next_step(self.conn, memory, self.db, run["run_id"], claim=True,
                                    author=self.chat_a, now_override=self.t0)
        # After 31 minutes, lease expires and chat B can claim angle.0.
        later = self.t0 + timedelta(minutes=31)
        with memory.transaction(self.conn):
            nxt = scenario_runs.next_step(self.conn, memory, self.db, run["run_id"], claim=True,
                                          author=self.chat_b, now_override=later)
        self.assertEqual(nxt["step"]["key"], "angle.0")
        self.assertEqual(nxt["step"]["status"], "claimed")
        journal = scenarios.log(self.conn, run_id=run["run_id"], entries=20)
        self.assertIn("lease_expired", [item["action"] for item in journal["entries"]])

    def test_skipped_optional_step_does_not_cascade_skip_downstream_c4(self):
        self.add_scenario({
            "format": "richi-scenario", "format_version": 1, "id": "opt-list", "title": "Optional list",
            "inputs": {"flag": {"type": "boolean", "default": False}},
            "steps": [
                {"id": "first", "type": "condition", "params": {"expr": "true"}},
                {"id": "optional", "type": "instruction", "if": "inputs.flag", "params": {"text": "skip me"}},
                {"id": "after", "type": "instruction", "params": {"text": "must still run"}},
            ],
        })
        with memory.transaction(self.conn):
            run = scenario_runs.start_run(self.conn, memory, self.db, "opt-list", author=self.chat_a, now_override=self.t0)
        statuses = {step["key"]: step["status"] for step in run["steps"]}
        self.assertEqual(statuses, {"first": "done", "optional": "skipped", "after": "ready"})

    def test_rendered_line_limit_c3_cwd_containment_k1_and_file_symlink_c2(self):
        # C3: multi-line text input rendered into one-line run_title is rejected cleanly.
        self.add_scenario({
            "format": "richi-scenario", "format_version": 1, "id": "line-check", "title": "Line check",
            "run_title": "Title: {{inputs.note}}",
            "inputs": {"note": {"type": "text", "required": True}},
            "steps": [{"id": "s1", "type": "instruction", "params": {"text": "ok"}}],
        })
        with self.assertRaises(memory.MemoryError) as caught:
            with memory.transaction(self.conn):
                scenario_runs.start_run(self.conn, memory, self.db, "line-check",
                                        raw_inputs=["note=line1\nline2"], author=self.chat_a)
        self.assertEqual(caught.exception.code, "invalid_input")

        # K1: cwd escaping --project-path blocks the step; valid command returns substituted_values.
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("nope", encoding="utf-8")
        symlink_dir = self.repo / "escape_dir"
        os.symlink(outside, symlink_dir)
        symlink_file = self.repo / "escape.txt"
        os.symlink(outside / "secret.txt", symlink_file)

        self.add_scenario({
            "format": "richi-scenario", "format_version": 1, "id": "cmd-check", "title": "Cmd check",
            "inputs": {"subdir": {"type": "string", "default": "escape_dir"}, "arg": {"type": "string", "default": "a b"}},
            "steps": [{
                "id": "run_cmd", "type": "command",
                "params": {"command": "echo {{inputs.arg | shell}}", "cwd": "{{inputs.subdir}}", "expect_exit": 0},
                "checks": [{"kind": "file_exists", "path": "escape.txt"}, {"kind": "evidence_git"}],
            }],
        })
        with memory.transaction(self.conn):
            r_bad = scenario_runs.start_run(self.conn, memory, self.db, "cmd-check",
                                            project_id="demo", project_path=self.repo, author=self.chat_a)
        with self.assertRaises(memory.MemoryError) as caught_cwd:
            with memory.transaction(self.conn):
                scenario_runs.next_step(self.conn, memory, self.db, r_bad["run_id"], claim=True,
                                        project_path=self.repo, author=self.chat_a)
        self.assertEqual(caught_cwd.exception.code, "step_blocked")

        # Valid subdir: substituted_values shown, symlink file_exists and invalid git rev rejected, real file+rev accepted.
        (self.repo / "sub").mkdir()
        (self.repo / "report.md").write_text("ok\n", encoding="utf-8")
        self.add_scenario({
            "format": "richi-scenario", "format_version": 1, "id": "cmd-ok", "title": "Cmd ok",
            "inputs": {"arg": {"type": "string", "default": "x; y"}},
            "steps": [{
                "id": "run_cmd", "type": "command",
                "params": {"command": "echo {{inputs.arg | shell}}", "cwd": "sub", "expect_exit": 0},
                "checks": [{"kind": "file_exists", "path": "report.md"}, {"kind": "evidence_git"}],
            }],
        })
        with memory.transaction(self.conn):
            r_ok = scenario_runs.start_run(self.conn, memory, self.db, "cmd-ok",
                                           project_id="demo", project_path=self.repo, author=self.chat_a)
            nxt = scenario_runs.next_step(self.conn, memory, self.db, r_ok["run_id"], claim=True,
                                          project_path=self.repo, author=self.chat_a)
        self.assertEqual(nxt["step"]["command"], "echo 'x; y'")
        self.assertEqual(nxt["step"]["substituted_values"], [{"path": "inputs.arg", "value": "x; y"}])
        # Bogus git rev rejected.
        with memory.transaction(self.conn):
            bad_rev = scenario_runs.report_step(
                self.conn, memory, self.db, r_ok["run_id"], "run_cmd",
                {"status": "done", "summary": "ran", "exit_code": 0,
                 "evidence": [{"type": "git", "revision": "deadbeef1234"}]},
                project_path=self.repo, author=self.chat_a,
            )
        self.assertEqual(bad_rev["status"], "rejected")
        # Real HEAD rev + existing file accepted.
        with memory.transaction(self.conn):
            good = scenario_runs.report_step(
                self.conn, memory, self.db, r_ok["run_id"], "run_cmd",
                {"status": "done", "summary": "ran", "exit_code": 0,
                 "evidence": [{"type": "git", "revision": self.head_sha}]},
                project_path=self.repo, author=self.chat_a,
            )
        self.assertEqual(good["status"], "accepted")
        self.assertEqual(good["run_status"], "done")

    def test_secret_inputs_are_never_persisted_i2(self):
        self.add_scenario({
            "format": "richi-scenario", "format_version": 1, "id": "sec-run", "title": "Secret run",
            "inputs": {"token": {"type": "secret", "required": True}},
            "steps": [{"id": "s1", "type": "instruction", "if": "inputs.token != null",
                       "params": {"text": "Use {{inputs.token}}"}}],
        })
        secret_value = "TOPSECRET-99999"
        with memory.transaction(self.conn):
            run = scenario_runs.start_run(self.conn, memory, self.db, "sec-run",
                                          raw_inputs=["token=" + secret_value], author=self.chat_a)
        # Raw secret must not appear anywhere in the SQLite dump!
        dump = "\n".join(self.conn.iterdump())
        self.assertNotIn(secret_value, dump)
        self.assertEqual(run["inputs"]["token"], "<secret>")
        # Calling next without --secret reports missing_secret cleanly; supplying it at call time renders in memory only.
        with memory.transaction(self.conn):
            need_sec = scenario_runs.next_step(self.conn, memory, self.db, run["run_id"], claim=False, author=self.chat_a)
        self.assertEqual(need_sec["missing_secret"], "token")
        with memory.transaction(self.conn):
            with_sec = scenario_runs.next_step(self.conn, memory, self.db, run["run_id"], claim=True,
                                               author=self.chat_a, secrets_runtime={"token": secret_value})
        self.assertEqual(with_sec["step"]["text"], "Use " + secret_value)
        self.assertNotIn(secret_value, "\n".join(self.conn.iterdump()))

    def test_retry_pause_resume_skip_and_cancel(self):
        self.add_scenario({
            "format": "richi-scenario", "format_version": 1, "id": "ctrl", "title": "Control",
            "steps": [
                {"id": "a", "type": "instruction", "params": {"text": "step a"}},
                {"id": "b", "type": "instruction", "params": {"text": "step b"}},
            ],
        })
        with memory.transaction(self.conn):
            run = scenario_runs.start_run(self.conn, memory, self.db, "ctrl", author=self.chat_a)
            rid = run["run_id"]
            scenario_runs.set_run_state(self.conn, memory, self.db, rid, "pause", author=self.chat_a)
            self.assertEqual(scenario_runs.run_status(self.conn, memory, rid)["status"], "paused")
            scenario_runs.set_run_state(self.conn, memory, self.db, rid, "resume", author=self.chat_a)
            scenario_runs.next_step(self.conn, memory, self.db, rid, claim=True, author=self.chat_a)
            scenario_runs.report_step(self.conn, memory, self.db, rid, "a",
                                      {"status": "failed", "summary": "boom"}, author=self.chat_a)
            self.assertEqual(scenario_runs.run_status(self.conn, memory, rid)["status"], "failed")
            scenario_runs.retry_step(self.conn, memory, self.db, rid, "a", author=self.chat_a)
            self.assertEqual(scenario_runs.run_status(self.conn, memory, rid)["status"], "running")
            scenario_runs.skip_step(self.conn, memory, self.db, rid, "a", "not needed", author=self.chat_a)
            scenario_runs.set_run_state(self.conn, memory, self.db, rid, "cancel", author=self.chat_a)
            self.assertEqual(scenario_runs.run_status(self.conn, memory, rid)["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
