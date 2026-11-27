"""Scenario runs: scheduler, leases, matrix aggregation, report validation, secrets, C3/C4/K1/C2 and task entries."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

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
                raw_inputs=["branch=auth-boundary", "angles=security,tests"],
                author=self.chat_a, now_override=self.t0,
            )
        run_id = run["run_id"]
        self.assertEqual(run["status"], "running")
        # Richi already ran `context` (memory.brief) and expanded `angle` into shell-safe keys angle.0 and angle.1!
        keys = [step["key"] for step in run["steps"]]
        self.assertEqual(keys, ["context", "angle.0", "angle.1", "merge", "approve"])
        self.assertEqual(run["steps"][0]["status"], "done")
        # R2-J01: memory.brief actually reads res["results"] and finds the seeded decision:auth-boundary!
        self.assertIn("Auth boundary", run["steps"][0]["summary"])
        ctx_row = self.conn.execute(
            "SELECT result_json FROM scenario_steps WHERE run_id = ? AND step_key = 'context'", (run_id,)
        ).fetchone()
        self.assertIn("entry:decision:auth-boundary", json.loads(ctx_row["result_json"])["data"]["refs"])
        # Task entry created in "scenario-run" category.
        task = memory.entry_get(self.conn, run["task_entry_id"])
        self.assertEqual(task["kind"], "task")
        self.assertEqual(task["title"], "Review: auth-boundary")
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
        # R2-J02: next_step returns blocked_step JSON instead of raising (so blocked state commits!).
        with memory.transaction(self.conn):
            blocked_res = scenario_runs.next_step(self.conn, memory, self.db, r_bad["run_id"], claim=True,
                                                  project_path=self.repo, author=self.chat_a)
        self.assertIsNone(blocked_res["step"])
        self.assertEqual(blocked_res["blocked_step"]["key"], "run_cmd")
        st_blocked = scenario_runs.run_status(self.conn, memory, r_bad["run_id"])
        self.assertEqual(st_blocked["status"], "waiting")
        self.assertEqual(st_blocked["steps"][0]["status"], "blocked")
        events = [e["action"] for e in scenarios.log(self.conn, run_id=r_bad["run_id"], entries=20)["entries"]]
        self.assertIn("step_blocked", events)
        # Retry can recover the persisted blocked step!
        with memory.transaction(self.conn):
            scenario_runs.retry_step(self.conn, memory, self.db, r_bad["run_id"], "run_cmd", author=self.chat_a)
        self.assertEqual(scenario_runs.run_status(self.conn, memory, r_bad["run_id"])["status"], "running")

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

    def test_memory_recall_returns_results_r2_j01(self):
        self.add_scenario({
            "format": "richi-scenario", "format_version": 1, "id": "recall-scn", "title": "Recall",
            "steps": [
                {"id": "rec", "type": "memory.recall",
                 "params": {"query": "session tokens", "kinds": ["decision"], "limit": 5}},
                {"id": "use", "type": "instruction", "params": {"text": "Found: {{steps.rec.summary}}"}},
            ],
        })
        with memory.transaction(self.conn):
            run = scenario_runs.start_run(self.conn, memory, self.db, "recall-scn",
                                          project_id="demo", project_path=self.repo, author=self.chat_a)
        self.assertIn("Auth boundary", run["steps"][0]["summary"])
        rec_row = self.conn.execute(
            "SELECT result_json FROM scenario_steps WHERE run_id = ? AND step_key = 'rec'", (run["run_id"],)
        ).fetchone()
        self.assertIn("entry:decision:auth-boundary", json.loads(rec_row["result_json"])["data"]["refs"])

    def test_evidence_git_hardening_and_cwd_without_project_path_r2_j03_j09_j30(self):
        # Create a second repo whose HEAD should NOT spoof demo repo via GIT_DIR.
        other_repo = self.root / "other_repo"
        other_repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=other_repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.test"], cwd=other_repo, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=other_repo, check=True)
        (other_repo / "other.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "other.txt"], cwd=other_repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "other"], cwd=other_repo, check=True)
        other_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=other_repo, text=True).strip()

        self.add_scenario({
            "format": "richi-scenario", "format_version": 1, "id": "git-hard", "title": "Git hard",
            "inputs": {"dir": {"type": "string", "default": "sub"}},
            "steps": [{
                "id": "s1", "type": "command",
                "params": {"command": "echo ok", "cwd": "{{inputs.dir}}", "expect_exit": 0},
                "checks": [{"kind": "exit_code", "expect": 0}, {"kind": "evidence_git"}],
            }],
        })
        # R2-J09: runtime cwd="../outside" blocks even when project_path is None!
        with memory.transaction(self.conn):
            r_trav = scenario_runs.start_run(self.conn, memory, self.db, "git-hard",
                                             raw_inputs=["dir=../outside"], author=self.chat_a)
            blocked = scenario_runs.next_step(self.conn, memory, self.db, r_trav["run_id"], claim=True,
                                              project_path=None, author=self.chat_a)
        self.assertEqual(blocked["blocked_step"]["key"], "s1")

        # R2-J03 & R2-J30: evidence_git without project_path rejected; second item checked; GIT_DIR ignored; no dup exit_code reason.
        with memory.transaction(self.conn):
            r = scenario_runs.start_run(self.conn, memory, self.db, "git-hard",
                                        project_id="demo", raw_inputs=["dir=sub"], author=self.chat_a)
            scenario_runs.next_step(self.conn, memory, self.db, r["run_id"], claim=True,
                                    project_path=None, author=self.chat_a)
            no_path = scenario_runs.report_step(
                self.conn, memory, self.db, r["run_id"], "s1",
                {"status": "done", "summary": "ok", "exit_code": 1,
                 "evidence": [{"type": "git", "revision": self.head_sha}]},
                project_path=None, author=self.chat_a,
            )
        self.assertEqual(no_path["status"], "rejected")
        self.assertTrue(any("--project-path" in reason for reason in no_path["reasons"]))
        # R2-J30: only ONE exit_code failure reason despite both expect_exit=0 and checks[exit_code=0].
        exit_reasons = [reason for reason in no_path["reasons"] if "exit_code" in reason]
        self.assertEqual(len(exit_reasons), 1)

        # Second git evidence item invalid + NUL byte rejected without crashing; GIT_DIR spoofing blocked.
        with mock.patch.dict(os.environ, {"GIT_DIR": str(other_repo / ".git")}):
            with memory.transaction(self.conn):
                multi_bad = scenario_runs.report_step(
                    self.conn, memory, self.db, r["run_id"], "s1",
                    {"status": "done", "summary": "ok", "exit_code": 0,
                     "evidence": [{"type": "git", "revision": self.head_sha},
                                  {"type": "git", "revision": other_sha},
                                  {"type": "git", "revision": "HEAD\x00bad"}]},
                    project_path=self.repo, author=self.chat_a,
                )
        self.assertEqual(multi_bad["status"], "rejected")
        self.assertTrue(any(other_sha in reason for reason in multi_bad["reasons"]))
        self.assertTrue(any("invalid git revision syntax" in reason for reason in multi_bad["reasons"]))

    def test_lease_isolation_anonymous_vs_chat_and_expiry_on_report_r2_j05(self):
        anon = rules.Author(None, None)
        self.add_scenario({
            "format": "richi-scenario", "format_version": 1, "id": "lease-iso", "title": "Lease",
            "steps": [{"id": "s1", "type": "instruction", "params": {"text": "work"}}],
        })
        with memory.transaction(self.conn):
            r = scenario_runs.start_run(self.conn, memory, self.db, "lease-iso", author=self.chat_a, now_override=self.t0)
            rid = r["run_id"]
            scenario_runs.next_step(self.conn, memory, self.db, rid, claim=True, author=self.chat_a, now_override=self.t0)
        # Anonymous caller CANNOT report or release chat_a's claimed step!
        with self.assertRaises(memory.MemoryError) as c1:
            with memory.transaction(self.conn):
                scenario_runs.report_step(self.conn, memory, self.db, rid, "s1",
                                          {"status": "done", "summary": "hijack"}, author=anon, now_override=self.t0)
        self.assertEqual(c1.exception.code, "conflict")
        with self.assertRaises(memory.MemoryError) as c2:
            with memory.transaction(self.conn):
                scenario_runs.release_step(self.conn, memory, rid, "s1", author=anon, now_override=self.t0)
        self.assertEqual(c2.exception.code, "conflict")
        # Rejected report renews lease_until!
        with memory.transaction(self.conn):
            rej = scenario_runs.report_step(self.conn, memory, self.db, rid, "s1",
                                            {"status": "done", "summary": ""}, author=self.chat_a,
                                            now_override=self.t0 + timedelta(minutes=20))
        self.assertEqual(rej["status"], "rejected")
        self.assertGreater(rej["lease_until"], (self.t0 + timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M:%S"))
        # After lease expires (31m after renewal), report_step rejects with invalid_state!
        with self.assertRaises(memory.MemoryError) as c3:
            with memory.transaction(self.conn):
                scenario_runs.report_step(self.conn, memory, self.db, rid, "s1",
                                          {"status": "done", "summary": "too late"}, author=self.chat_a,
                                          now_override=self.t0 + timedelta(minutes=55))
        self.assertEqual(c3.exception.code, "invalid_state")
        # Now anonymous can claim, and chat_a cannot steal anonymous's claim!
        with memory.transaction(self.conn):
            scenario_runs.claim_step(self.conn, memory, self.db, rid, "s1",
                                     author=anon, now_override=self.t0 + timedelta(minutes=56))
        with self.assertRaises(memory.MemoryError) as c4:
            with memory.transaction(self.conn):
                scenario_runs.report_step(self.conn, memory, self.db, rid, "s1",
                                          {"status": "done", "summary": "steal"}, author=self.chat_a,
                                          now_override=self.t0 + timedelta(minutes=57))
        self.assertEqual(c4.exception.code, "conflict")

    def test_lifecycle_guards_checklist_retry_max_cas_and_deleted_task_r2_j11_j12_j13_j22_j25(self):
        self.add_scenario({
            "format": "richi-scenario", "format_version": 1, "id": "life", "title": "Life",
            "steps": [
                {"id": "par", "type": "instruction", "matrix": {"k": ["one", "two"]},
                 "params": {"text": "work {{matrix.k}}"}, "retry": {"max": 1}},
                {"id": "gate_nc", "type": "gate",
                 "params": {"question": "Ok?", "options": ["yes", "no"], "allow_comment": False}},
                {"id": "chk", "type": "checklist",
                 "params": {"items": [{"text": "Req item", "required": True}, {"text": "Opt item"}]}},
            ],
        })
        with memory.transaction(self.conn):
            r = scenario_runs.start_run(self.conn, memory, self.db, "life",
                                        project_id="demo", author=self.chat_a, now_override=self.t0)
            rid = r["run_id"]
        # R2-J22: wrong expected_revision rejected with conflict.
        with self.assertRaises(memory.MemoryError) as c_rev:
            with memory.transaction(self.conn):
                scenario_runs.claim_step(self.conn, memory, self.db, rid, "par.0",
                                         author=self.chat_a, expected_revision=999)
        self.assertEqual(c_rev.exception.code, "conflict")
        # R2-J11: single chat claims BOTH parallel matrix instances and gets full rendered payloads!
        with memory.transaction(self.conn):
            c0 = scenario_runs.claim_step(self.conn, memory, self.db, rid, "par.0", author=self.chat_a)
            c1 = scenario_runs.claim_step(self.conn, memory, self.db, rid, "par.1", author=self.chat_a)
        self.assertEqual(c0["step"]["text"], "work one")
        self.assertEqual(c1["step"]["text"], "work two")
        # R2-J11: next_step with step_key selects par.1 directly.
        with memory.transaction(self.conn):
            n1 = scenario_runs.next_step(self.conn, memory, self.db, rid, step_key="par.1", author=self.chat_a)
        self.assertEqual(n1["step"]["key"], "par.1")
        # R2-J13: pause rejects release, skip, retry.
        with memory.transaction(self.conn):
            scenario_runs.set_run_state(self.conn, memory, self.db, rid, "pause", author=self.chat_a)
        for fn in (
            lambda: scenario_runs.release_step(self.conn, memory, rid, "par.0", author=self.chat_a),
            lambda: scenario_runs.skip_step(self.conn, memory, self.db, rid, "par.0", "skip", author=self.chat_a),
            lambda: scenario_runs.retry_step(self.conn, memory, self.db, rid, "par.0", author=self.chat_a),
        ):
            with self.assertRaises(memory.MemoryError) as caught_p:
                with memory.transaction(self.conn):
                    fn()
            self.assertEqual(caught_p.exception.code, "invalid_state")
        with memory.transaction(self.conn):
            scenario_runs.set_run_state(self.conn, memory, self.db, rid, "resume", author=self.chat_a)
        # R2-J13: failing par.0 marks run failed AND clears par.1's claimed lease!
        with memory.transaction(self.conn):
            scenario_runs.report_step(self.conn, memory, self.db, rid, "par.0",
                                      {"status": "failed", "summary": "err1"}, author=self.chat_a)
        st_fail = scenario_runs.run_status(self.conn, memory, rid)
        self.assertEqual(st_fail["status"], "failed")
        par1_st = next(s for s in st_fail["steps"] if s["key"] == "par.1")
        self.assertEqual(par1_st["status"], "ready")
        self.assertIsNone(par1_st.get("claimed_by"))
        # Retry 1 succeeds (retry.max == 1 allows 2 total attempts); failing again blocks 2nd retry (retry_limit)!
        with memory.transaction(self.conn):
            scenario_runs.retry_step(self.conn, memory, self.db, rid, "par.0", author=self.chat_a)
            scenario_runs.claim_step(self.conn, memory, self.db, rid, "par.0", author=self.chat_a)
            scenario_runs.report_step(self.conn, memory, self.db, rid, "par.0",
                                      {"status": "failed", "summary": "err2"}, author=self.chat_a)
        with self.assertRaises(memory.MemoryError) as c_lim:
            with memory.transaction(self.conn):
                scenario_runs.retry_step(self.conn, memory, self.db, rid, "par.0", author=self.chat_a)
        self.assertEqual(c_lim.exception.code, "retry_limit")
        # Skip par.0 recovers failed run and syncs task entry back to in_progress!
        with memory.transaction(self.conn):
            scenario_runs.skip_step(self.conn, memory, self.db, rid, "par.0", "waived", author=self.chat_a)
        self.assertEqual(memory.entry_get(self.conn, r["task_entry_id"])["work_state"], "in_progress")
        # Complete par.1 -> gate_nc waiting_human.
        with memory.transaction(self.conn):
            scenario_runs.claim_step(self.conn, memory, self.db, rid, "par.1", author=self.chat_a)
            scenario_runs.report_step(self.conn, memory, self.db, rid, "par.1",
                                      {"status": "done", "summary": "done"}, author=self.chat_a)
        # R2-J25: allow_comment: False rejects comment.
        with self.assertRaises(memory.MemoryError) as c_cmt:
            with memory.transaction(self.conn):
                scenario_runs.answer_step(self.conn, memory, self.db, rid, "gate_nc", "yes",
                                          comment="nope", author=self.chat_a)
        self.assertEqual(c_cmt.exception.code, "invalid_answer")
        with memory.transaction(self.conn):
            scenario_runs.answer_step(self.conn, memory, self.db, rid, "gate_nc", "yes", author=self.chat_a)
        # R2-J13: checklist rejects missing required item and unknown item; accepts required item by index or text.
        for bad_ans in ("1", "unknown", ""):
            with self.assertRaises(memory.MemoryError) as c_chk:
                with memory.transaction(self.conn):
                    scenario_runs.answer_step(self.conn, memory, self.db, rid, "chk", bad_ans, author=self.chat_a)
            self.assertEqual(c_chk.exception.code, "invalid_answer")
        # R2-J12: missing/deleted task entry must not crash final transition!
        with memory.transaction(self.conn):
            self.conn.execute("UPDATE scenario_runs SET task_entry_id = 'task:missing-entry' WHERE id = ?", (rid,))
        with memory.transaction(self.conn):
            fin = scenario_runs.answer_step(self.conn, memory, self.db, rid, "chk", "Req item,1", author=self.chat_a)
        self.assertEqual(fin["run_status"], "done")

    def test_continue_on_failure_context_reason_nul_secret_edge_cases_and_run_id_r2_j14_j16_j17_j19_j24_j28(self):
        # R2-J16: non-matrix continue_on_failure preserves result_json in downstream steps context!
        # R2-J19: literal '<secret>' in normal input works in run_title; empty secret is falsey; unknown --secret fails!
        self.add_scenario({
            "format": "richi-scenario", "format_version": 1, "id": "a_b", "title": "AB",
            "run_title": "Tag {{inputs.label}}",
            "inputs": {
                "label": {"type": "string", "default": "<secret>"},
                "tok": {"type": "secret"},
            },
            "steps": [
                {"id": "fail_cmd", "type": "command", "continue_on_failure": True,
                 "params": {"command": "false", "expect_exit": 0}},
                {"id": "skip_if_empty_sec", "type": "instruction", "if": "inputs.tok",
                 "params": {"text": "secret was set"}},
                {"id": "after", "type": "skill",
                 "params": {"skill": {"default": "nonexistent-skill-xyz"},
                            "instruction": "Prev={{steps.fail_cmd.summary}} code={{steps.fail_cmd.exit_code}}"}},
            ],
        })
        self.add_scenario({
            "format": "richi-scenario", "format_version": 1, "id": "azb", "title": "AZB",
            "steps": [{"id": "s1", "type": "instruction", "params": {"text": "ok"}}],
        })
        with memory.transaction(self.conn):
            r1 = scenario_runs.start_run(self.conn, memory, self.db, "a_b",
                                         raw_inputs=["label=<secret>", "tok="], author=self.chat_a, now_override=self.t0)
            r2 = scenario_runs.start_run(self.conn, memory, self.db, "azb",
                                         author=self.chat_a, now_override=self.t0)
        # R2-J14: a_b and azb both get -0001 cleanly without LIKE wildcard collision!
        self.assertTrue(r1["run_id"].endswith("-0001"))
        self.assertTrue(r2["run_id"].endswith("-0001"))
        # R2-J19: literal "<secret>" preserved in run_title!
        self.assertEqual(memory.entry_get(self.conn, r1["task_entry_id"])["title"], "Tag <secret>")
        # R2-J17 & R2-J24: non-string reason and NUL byte in report rejected cleanly!
        with memory.transaction(self.conn):
            scenario_runs.next_step(self.conn, memory, self.db, r1["run_id"], claim=True, author=self.chat_a)
            bad_reason = scenario_runs.report_step(
                self.conn, memory, self.db, r1["run_id"], "fail_cmd",
                {"status": "failed", "summary": "err", "reason": {"nested": 1}}, author=self.chat_a,
            )
            bad_nul = scenario_runs.report_step(
                self.conn, memory, self.db, r1["run_id"], "fail_cmd",
                {"status": "failed", "summary": "bad\x00sum", "reason": "ok"}, author=self.chat_a,
            )
        self.assertEqual(bad_reason["status"], "rejected")
        self.assertEqual(bad_nul["status"], "rejected")
        # Accept failed step -> skip_if_empty_sec skipped (empty secret falsey!) -> after renders fail_cmd result + skill_warning!
        with memory.transaction(self.conn):
            scenario_runs.report_step(
                self.conn, memory, self.db, r1["run_id"], "fail_cmd",
                {"status": "failed", "summary": "cmd failed", "exit_code": 2}, author=self.chat_a,
            )
            with mock.patch("richi.scenarios.installed_skills", return_value=set()):
                nxt = scenario_runs.next_step(self.conn, memory, self.db, r1["run_id"], claim=True, author=self.chat_a)
        self.assertEqual(nxt["step"]["key"], "after")
        self.assertEqual(nxt["step"]["instruction"], "Prev=cmd failed code=2")
        self.assertIn("skill_warning", nxt["step"])
        # Unknown --secret key rejected with invalid_input!
        with self.assertRaises(memory.MemoryError) as c_sec:
            with memory.transaction(self.conn):
                scenario_runs.next_step(self.conn, memory, self.db, r1["run_id"],
                                        author=self.chat_a, secrets_runtime={"bogus": "val"})
        self.assertEqual(c_sec.exception.code, "invalid_input")
        # R2-J14 & R2-J31: sequence rollover past 9999 and pre-existing entry guard.
        with memory.transaction(self.conn):
            day = self.t0.strftime("%Y%m%d")
            self.conn.execute(
                "INSERT INTO scenario_runs(id, scenario_id, scope, scenario_version, scenario_sha256, definition_json, "
                "inputs_json, status, created_at, updated_at) VALUES (?, 'azb', 'global', 1, 'abc', '{}', '{}', 'done', ?, ?)",
                ("run-azb-%s-9999" % day, "2026-11-27T10:00:00Z", "2026-11-27T10:00:00Z"),
            )
            memory.entry_put(self.conn, {
                "id": "task:scenario-run-azb-%s-10000" % day, "kind": "task", "title": "Pre-existing",
                "summary": "occupies 10000", "project_ids": ["demo"], "knowledge_state": "confirmed",
                "sources": [{"reference": "synthetic:occ"}], "expected_updated_at": None,
            })
            r3 = scenario_runs.start_run(self.conn, memory, self.db, "azb", author=self.chat_a, now_override=self.t0)
        self.assertEqual(r3["run_id"], "run-azb-%s-10001" % day)
        # R2-J27: list_runs rejects unknown project_id.
        with self.assertRaisesRegex(memory.MemoryError, "Unknown project"):
            scenario_runs.list_runs(self.conn, memory, project_id="nonexistent-proj")


if __name__ == "__main__":
    unittest.main()
