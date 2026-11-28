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
                                             project_id="demo", raw_inputs=["dir=../outside"], author=self.chat_a)
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

    def test_completed_task_confirmed_recalled_and_cancelled_superseded_r3_a1(self):
        from types import SimpleNamespace
        from richi import punisher, recall
        self.add_scenario({
            "format": "richi-scenario", "format_version": 1, "id": "richi-only", "title": "Richi only",
            "run_title": "UniqueMarkerRun {{inputs.tag}}",
            "inputs": {"tag": {"type": "string", "required": True}},
            "steps": [{"id": "c1", "type": "condition", "params": {"expr": "true"}}],
        })
        self.add_scenario({
            "format": "richi-scenario", "format_version": 1, "id": "cancellable", "title": "Cancellable",
            "run_title": "CancelledMarkerRun",
            "steps": [{"id": "s1", "type": "instruction", "params": {"text": "work"}}],
        })
        # Richi-only scenario reaches done inside start_run() -> confirmed + verified_at!
        with memory.transaction(self.conn):
            done_run = scenario_runs.start_run(self.conn, memory, self.db, "richi-only",
                                               project_id="demo", raw_inputs=["tag=NeedleAlpha"],
                                               author=self.chat_a, now_override=self.t0)
            c_run = scenario_runs.start_run(self.conn, memory, self.db, "cancellable",
                                            project_id="demo", author=self.chat_a, now_override=self.t0)
            scenario_runs.set_run_state(self.conn, memory, self.db, c_run["run_id"], "cancel",
                                        author=self.chat_a, now_override=self.t0 + timedelta(minutes=1))
        done_task = memory.entry_get(self.conn, done_run["task_entry_id"])
        self.assertEqual(done_task["work_state"], "done")
        self.assertEqual(done_task["knowledge_state"], "hypothesis")
        cancel_task = memory.entry_get(self.conn, c_run["task_entry_id"])
        self.assertEqual(cancel_task["knowledge_state"], "superseded")
        # Default recall stays clean (routine scenario runs do not pollute confirmed memory),
        # while include_hypotheses=True still finds completed runs and omits cancelled runs!
        rec_default = recall.recall(self.conn, SimpleNamespace(
            query="NeedleAlpha", project="demo", where=None, limit=10, max_chars=8000,
            include_hypotheses=False, include_superseded=False, explain=False, expect=None), memory)
        self.assertEqual(rec_default["results"], [])
        rec_done = recall.recall(self.conn, SimpleNamespace(
            query="NeedleAlpha", project="demo", where=None, limit=10, max_chars=8000,
            include_hypotheses=True, include_superseded=False, explain=False, expect=None), memory)
        self.assertIn("entry:" + done_run["task_entry_id"], [r["ref"] for r in rec_done["results"]])
        rec_cancel = recall.recall(self.conn, SimpleNamespace(
            query="CancelledMarkerRun", project="demo", where=None, limit=10, max_chars=8000,
            include_hypotheses=False, include_superseded=False, explain=False, expect=None), memory)
        self.assertEqual(rec_cancel["results"], [])
        # Punisher does NOT emit verification_date_missing on completed run task entry!
        audit = punisher.punisher(self.conn, SimpleNamespace(
            project="demo", where=None, include_hypotheses=False, include_superseded=False,
            stale_days=90, limit=50, max_chars=20000, summary_threshold=1500,
            findings_only=False, kind=None), memory)
        vdm = [f for f in audit.get("findings") or []
               if f.get("code") == "verification_date_missing" and done_run["task_entry_id"] in str(f)]
        self.assertEqual(vdm, [])

    def test_r3_dag_skip_retry_revision_leases_checklist_preflight_and_limits(self):
        # R3-I3: skipping pending step rejected; R3-I5/A2: revision bumps on parallel/rejection/renew;
        # R3-S3/I9: retry on continue_on_failure with progressed descendant rejected;
        # R3-I10: skip expires stale lease and avoids duplicate run_failed;
        # R3-X1: start_run fails fast on workspace-wide run with file_exists;
        # R3-S1/I6 & R3-X4: matrix checklist text match + duplicate item text ambiguity;
        # R3-I7, R3-I11, R3-X3, R3-C1, R3-C2, R3-C3, R3-A5, R3-S5, R3-S6.
        self.add_scenario({
            "format": "richi-scenario", "format_version": 1, "id": "r3-dag", "title": "R3 DAG",
            "steps": [
                {"id": "a", "type": "instruction", "matrix": {"k": ["x", "y"]}, "params": {"text": "do {{matrix.k}}"}},
                {"id": "b", "type": "instruction", "continue_on_failure": True, "params": {"text": "mid"}},
                {"id": "c", "type": "instruction", "params": {"text": "end"}},
            ],
        })
        with memory.transaction(self.conn):
            r = scenario_runs.start_run(self.conn, memory, self.db, "r3-dag", author=self.chat_a, now_override=self.t0)
            rid = r["run_id"]
        # R3-S5: list_runs includes revision.
        listed = scenario_runs.list_runs(self.conn, memory, scenario_id="r3-dag")["runs"]
        self.assertIn("revision", listed[0])
        # R3-I3: skipping pending `b` while `a` is unfinished rejected!
        with self.assertRaises(memory.MemoryError) as c_pend:
            with memory.transaction(self.conn):
                scenario_runs.skip_step(self.conn, memory, self.db, rid, "b", "too early", author=self.chat_a)
        self.assertEqual(c_pend.exception.code, "invalid_state")
        # Claim a.0 and a.1; bump stop_continuations manually to verify R3-A5 resets it!
        with memory.transaction(self.conn):
            scenario_runs.claim_step(self.conn, memory, self.db, rid, "a.0", author=self.chat_a, now_override=self.t0)
            scenario_runs.claim_step(self.conn, memory, self.db, rid, "a.1", author=self.chat_a, now_override=self.t0)
            self.conn.execute("UPDATE scenario_steps SET stop_continuations = 5 WHERE run_id = ? AND step_key = 'a.0'", (rid,))
            # R3-C2: prepare_report_preflight returns None for wrong chat, expired lease, or ready step.
            self.assertIsNone(scenario_runs.prepare_report_preflight(
                self.conn, memory, rid, "a.0", {"status": "done", "summary": "ok"}, author=self.chat_b, now_dt=self.t0))
            self.assertIsNone(scenario_runs.prepare_report_preflight(
                self.conn, memory, rid, "a.0", {"status": "done", "summary": "ok"}, author=self.chat_a,
                now_dt=self.t0 + timedelta(minutes=31)))
        # R3-I5/A2: report rejection + parallel completion bump revision!
        rev0 = scenario_runs.run_status(self.conn, memory, rid)["revision"]
        with memory.transaction(self.conn):
            rej = scenario_runs.report_step(self.conn, memory, self.db, rid, "a.0",
                                            {"status": "done", "summary": ""}, author=self.chat_a, now_override=self.t0)
        self.assertEqual(rej["status"], "rejected")
        rev1 = scenario_runs.run_status(self.conn, memory, rid)["revision"]
        self.assertGreater(rev1, rev0)
        # R3-X3 & R3-C3: deep nesting rejected; float('nan') rejected; ~960 KB compact report passes size!
        deep = []
        cur = deep
        for _ in range(40):
            nxt = []
            cur.append(nxt)
            cur = nxt
        with memory.transaction(self.conn):
            rej_deep = scenario_runs.report_step(self.conn, memory, self.db, rid, "a.0",
                                                 {"status": "done", "summary": "d", "data": {"x": deep}},
                                                 author=self.chat_a, now_override=self.t0)
            rej_nan = scenario_runs.report_step(self.conn, memory, self.db, rid, "a.0",
                                                {"status": "done", "summary": "n", "data": {"x": float("nan")}},
                                                author=self.chat_a, now_override=self.t0)
        self.assertEqual(rej_deep["status"], "rejected")
        self.assertEqual(rej_nan["status"], "rejected")
        # R3-I5/A2: completing a.0 while a.1 is still claimed bumps revision!
        rev_before_a0 = scenario_runs.run_status(self.conn, memory, rid)["revision"]
        with memory.transaction(self.conn):
            scenario_runs.report_step(self.conn, memory, self.db, rid, "a.0",
                                      {"status": "done", "summary": "ok0", "exit_code": None, "data": {"exit_code": 0}},
                                      author=self.chat_a, now_override=self.t0 + timedelta(minutes=1))
        self.assertGreater(scenario_runs.run_status(self.conn, memory, rid)["revision"], rev_before_a0)
        # R3-I11: exit_code=None fell back to data.exit_code == 0!
        a0_res = json.loads(self.conn.execute(
            "SELECT result_json FROM scenario_steps WHERE run_id = ? AND step_key = 'a.0'", (rid,)
        ).fetchone()[0])
        self.assertEqual(a0_res["exit_code"], 0)
        # R3-I10 & R3-A5: after 31 min, chat B can skip a.1 (expired lease) and stop_continuations resets to 0!
        with memory.transaction(self.conn):
            self.conn.execute("UPDATE scenario_steps SET stop_continuations = 4 WHERE run_id = ? AND step_key = 'a.1'", (rid,))
            scenario_runs.skip_step(self.conn, memory, self.db, rid, "a.1", "abandoned",
                                    author=self.chat_b, now_override=self.t0 + timedelta(minutes=32))
        self.assertEqual(self.conn.execute(
            "SELECT stop_continuations FROM scenario_steps WHERE run_id = ? AND step_key = 'a.1'", (rid,)
        ).fetchone()[0], 0)
        # Now b (continue_on_failure) fails -> c unlocks to ready -> retrying b is REFUSED (R3-S3/I9)!
        with memory.transaction(self.conn):
            scenario_runs.claim_step(self.conn, memory, self.db, rid, "b", author=self.chat_a)
            scenario_runs.report_step(self.conn, memory, self.db, rid, "b",
                                      {"status": "failed", "summary": "soft fail"}, author=self.chat_a)
        with self.assertRaises(memory.MemoryError) as c_ret:
            with memory.transaction(self.conn):
                scenario_runs.retry_step(self.conn, memory, self.db, rid, "b", author=self.chat_a)
        self.assertEqual(c_ret.exception.code, "invalid_state")

        # R3-X1: workspace-wide run with file_exists fails fast at start_run!
        self.add_scenario({
            "format": "richi-scenario", "format_version": 1, "id": "needs-proj", "title": "Needs proj",
            "steps": [{"id": "s1", "type": "instruction", "params": {"text": "x"},
                       "checks": [{"kind": "file_exists", "path": "README.md"}]}],
        })
        with self.assertRaises(memory.MemoryError) as c_np:
            with memory.transaction(self.conn):
                scenario_runs.start_run(self.conn, memory, self.db, "needs-proj", author=self.chat_a)
        self.assertEqual(c_np.exception.code, "invalid_input")

        # R3-S1/I6, R3-X4, R3-I11, R3-S6: matrix checklist text match, ambiguous duplicate text, blank gate, no null comment.
        self.add_scenario({
            "format": "richi-scenario", "format_version": 1, "id": "chk-gate", "title": "Chk Gate",
            "steps": [
                {"id": "g", "type": "gate", "params": {"question": "Say something?"}},
                {"id": "mchk", "type": "checklist", "matrix": {"env": ["staging"]},
                 "params": {"items": [{"text": "Deploy {{matrix.env}}", "required": True},
                                      {"text": "Dup"}, {"text": "Dup"}]}},
            ],
        })
        with memory.transaction(self.conn):
            rg = scenario_runs.start_run(self.conn, memory, self.db, "chk-gate", author=self.chat_a)
            rg_id = rg["run_id"]
        with self.assertRaises(memory.MemoryError) as c_blank:
            with memory.transaction(self.conn):
                scenario_runs.answer_step(self.conn, memory, self.db, rg_id, "g", "   ", author=self.chat_a)
        self.assertEqual(c_blank.exception.code, "invalid_answer")
        with memory.transaction(self.conn):
            scenario_runs.answer_step(self.conn, memory, self.db, rg_id, "g", "go", author=self.chat_a)
        # R3-S6: answered event omits comment when None.
        ans_ev = [e for e in scenarios.log(self.conn, run_id=rg_id, entries=10)["entries"] if e["action"] == "answered"][0]
        self.assertNotIn("comment", ans_ev["detail"])
        # R3-X4: duplicate checklist text rejected as ambiguous.
        with self.assertRaises(memory.MemoryError) as c_dup:
            with memory.transaction(self.conn):
                scenario_runs.answer_step(self.conn, memory, self.db, rg_id, "mchk.0", "Deploy staging,Dup", author=self.chat_a)
        self.assertEqual(c_dup.exception.code, "invalid_answer")
        # R3-S1/I6: rendered matrix text "Deploy staging" matches cleanly!
        with memory.transaction(self.conn):
            fin_chk = scenario_runs.answer_step(self.conn, memory, self.db, rg_id, "mchk.0", "Deploy staging", author=self.chat_a)
        self.assertEqual(fin_chk["run_status"], "done")

        # R3-I7: >20 git evidence items rejected and duplicate revs deduplicated.
        too_many = [{"type": "git", "revision": self.head_sha}] * 21
        res_many = scenario_runs._verify_git_evidence_io(too_many, self.repo)
        self.assertTrue(any("at most 20" in r for r in res_many))
        with mock.patch("richi.git_evidence._git", wraps=scenario_runs.git_evidence._git) as spy_git:
            res_dup = scenario_runs._verify_git_evidence_io([{"type": "git", "revision": self.head_sha}] * 5, self.repo)
            self.assertEqual(res_dup, [])
            self.assertEqual(spy_git.call_count, 1)

    def test_product_review_must_fixes_project_path_failed_reason_evidence_and_secret_env(self):
        from richi import scenario_commands
        self.add_scenario({
            "format": "richi-scenario", "format_version": 1, "id": "pr-fixes", "title": "PR Fixes",
            "inputs": {"tok": {"type": "secret"}},
            "steps": [
                {"id": "rec", "type": "memory.recall", "title": "Recall", "params": {"query": "Existing"}},
                {"id": "work", "type": "instruction", "title": "Work", "needs": ["rec"],
                 "params": {"text": "Use {{steps.rec.summary}} with {{inputs.tok}}"}},
            ],
        })
        with memory.transaction(self.conn):
            memory.entry_put(self.conn, {
                "id": "fact:existing-note", "kind": "fact", "title": "Existing Architecture Fact",
                "summary": "Keep boundaries clean.", "project_ids": ["demo"],
                "knowledge_state": "confirmed", "sources": [{"reference": "docs/arch.md"}],
                "expected_updated_at": None,
            })
            started = scenario_runs.start_run(self.conn, memory, self.db, "pr-fixes",
                                              project_id="demo", project_path=self.repo, author=self.chat_a)
            run_id = started["run_id"]
            # Memory step summary is explicitly framed as untrusted evidence.
            st = scenario_runs.run_status(self.conn, memory, run_id)
            rec_step = next(s for s in st["steps"] if s["id"] == "rec")
            self.assertIn("[untrusted memory evidence:", rec_step["summary"])
            self.assertIn("Existing Architecture Fact", rec_step["summary"])
            # --secret-env resolves from environment without placing secret in argv.
            with mock.patch.dict(os.environ, {"RICHI_TEST_TOK": "s3cr3t-env"}):
                parsed_sec = scenario_commands._parse_secrets(None, memory, ["tok=RICHI_TEST_TOK"])
            self.assertEqual(parsed_sec, {"tok": "s3cr3t-env"})
            with self.assertRaises(memory.MemoryError) as missing_env:
                scenario_commands._parse_secrets(None, memory, ["tok=RICHI_MISSING_VAR_XYZ"])
            self.assertEqual(missing_env.exception.code, "invalid_input")
            # next_step includes --project-path in report_command and recommends --secret-env when missing.
            miss = scenario_runs.next_step(self.conn, memory, self.db, run_id, claim=True,
                                           checked_project="demo", project_path=self.repo, author=self.chat_a)
            self.assertEqual(miss["missing_secret"], "tok")
            self.assertIn("--secret-env tok=ENV_VAR", miss["note"])
            claimed = scenario_runs.next_step(self.conn, memory, self.db, run_id, claim=True,
                                              checked_project="demo", project_path=self.repo,
                                              secrets_runtime=parsed_sec, author=self.chat_a)
            self.assertIn("--project-path", claimed["step"]["report_command"])
            self.assertIn(str(self.repo), claimed["step"]["report_command"])
            # step_failed event persists reason even after retry_step clears scenario_steps.reason.
            scenario_runs.report_step(self.conn, memory, self.db, run_id, "work",
                                      {"status": "failed", "summary": "failed build", "reason": "pytest timeout 60s"},
                                      checked_project="demo", project_path=self.repo, author=self.chat_a)
            scenario_runs.retry_step(self.conn, memory, self.db, run_id, "work",
                                     checked_project="demo", author=self.chat_a)
            fail_ev = [e for e in scenarios.log(self.conn, run_id=run_id, entries=20)["entries"]
                       if e["action"] == "step_failed"][0]
            self.assertEqual(fail_ev["detail"].get("reason"), "pytest timeout 60s")

    def test_round2_exit_code_validation_run_secrets_and_evidence_closing_bracket(self):
        # F1: invalid exit_code rejected even on failed status; valid exit_code persisted to step_failed detail.
        self.add_scenario({
            "format": "richi-scenario", "format_version": 1, "id": "r2-checks", "title": "R2 Checks",
            "inputs": {"tok": {"type": "secret", "required": True}},
            "steps": [
                {"id": "rec", "type": "memory.recall", "title": "Recall", "params": {"query": "LongEntry"}},
                {"id": "cmd", "type": "command", "title": "Cmd", "needs": ["rec"],
                 "if": "inputs.tok != null", "params": {"command": "echo ok"}},
            ],
        })
        with memory.transaction(self.conn):
            for i in range(6):
                memory.entry_put(self.conn, {
                    "id": f"fact:long-entry-{i}", "kind": "fact",
                    "title": f"LongEntry {i} " + ("x" * 180),
                    "summary": "Long summary " + ("y" * 180),
                    "project_ids": ["demo"],
                    "knowledge_state": "confirmed", "sources": [{"reference": "docs/long.md"}],
                    "expected_updated_at": None,
                })
            # F2: required secret accepted via secrets_runtime at start_run without storing plaintext.
            started = scenario_runs.start_run(
                self.conn, memory, self.db, "r2-checks",
                project_id="demo", project_path=self.repo,
                secrets_runtime={"tok": "top-secret-r2"}, author=self.chat_a,
            )
            run_id = started["run_id"]
            st = scenario_runs.run_status(self.conn, memory, run_id)
            self.assertEqual(st["inputs"]["tok"], "<secret>")
            self.assertNotIn("top-secret-r2", json.dumps(st))
            # F7: untrusted memory evidence framing preserves closing ']' even when truncated.
            rec_step = next(s for s in st["steps"] if s["id"] == "rec")
            self.assertTrue(rec_step["summary"].startswith("[untrusted memory evidence: "))
            self.assertTrue(rec_step["summary"].endswith("]"))
            # F2: conditional step `if: inputs.tok != null` is ready (not skipped!).
            cmd_step = next(s for s in st["steps"] if s["id"] == "cmd")
            self.assertEqual(cmd_step["status"], "ready")
            scenario_runs.next_step(self.conn, memory, self.db, run_id, claim=True,
                                    checked_project="demo", project_path=self.repo, author=self.chat_a)
            for bad_code in ("bad", True, 999999, -300):
                rej = scenario_runs.report_step(
                    self.conn, memory, self.db, run_id, "cmd",
                    {"status": "failed", "summary": "boom", "exit_code": bad_code},
                    checked_project="demo", project_path=self.repo, author=self.chat_a,
                )
                self.assertEqual(rej["status"], "rejected", f"expected rejected for exit_code={bad_code!r}")
                self.assertTrue(any("exit_code" in r for r in rej["reasons"]))
            rep = scenario_runs.report_step(
                self.conn, memory, self.db, run_id, "cmd",
                {"status": "failed", "summary": "oom killed", "reason": "SIGKILL", "exit_code": 137},
                checked_project="demo", project_path=self.repo, author=self.chat_a,
            )
            self.assertEqual(rep["status"], "accepted")
            self.assertEqual(rep["step_status"], "failed")
            fail_ev = [e for e in scenarios.log(self.conn, run_id=run_id, entries=20)["entries"]
                       if e["action"] == "step_failed"][0]
            self.assertEqual(fail_ev["detail"].get("exit_code"), 137)
            self.assertEqual(fail_ev["detail"].get("reason"), "SIGKILL")


if __name__ == "__main__":
    unittest.main()
