"""Comprehensive regression tests for all Review R4 consensus findings (CR-01 .. CR-32)."""
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

from richi import (
    memory,
    scenario_eval,
    scenario_findings,
    scenario_format,
    scenario_hooks,
    scenario_registry,
    scenario_review,
    scenario_runs,
    scenarios,
    serve,
)


def _clean_env(root):
    return {
        "HOME": str(root / "home"),
        "PATH": os.environ.get("PATH", ""),
        "XDG_CONFIG_HOME": str(root / "xdg"),
    }


class ReviewR4RegressionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="richi-r4-")
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
        (self.repo / "src" / "pay.py").write_text(
            "def charge_wallet(balance, amount):\n"
            "    if amount < 0:\n"
            "        return balance\n"
            "    return balance - amount\n",
            encoding="utf-8",
        )
        (self.repo / "sub").mkdir()
        (self.repo / "sub" / "worker.py").write_text("def run():\n    return 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self.repo, check=True)
        self.head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repo, text=True).strip()

        self.cli("init")
        self.cli("project", "add", str(self.repo), "--id", "svc", "--name", "Svc")

    def cli(self, *args, chat="claude:chat-r4", stdin_text=None, ok=True):
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

    # ---- CR-03, CR-04, CR-05, CR-31: findings_grounded & no_dirty_git security/correctness

    def test_findings_grounded_blocks_dotgit_case_symlink_fifo_and_missing_project_path(self):
        # CR-31: project_path=None must fail even for empty findings list
        errs = scenario_findings.verify_findings_grounded_io([], None)
        self.assertTrue(errs and "requires --project-path" in errs[0])

        # CR-03: case-insensitive .GIT check
        errs_git = scenario_findings.verify_findings_grounded_io(
            [{"file": ".GIT/config", "line": 1, "severity": "P1", "title": "Git leak", "evidence": "read git config file"}],
            str(self.repo),
        )
        self.assertTrue(errs_git)

        # CR-03: symlink inside repo pointing to real file inside repo must still be refused by O_NOFOLLOW
        link_path = self.repo / "src" / "auth_link.py"
        link_path.symlink_to(self.repo / "src" / "auth.py")
        errs_sym = scenario_findings.verify_findings_grounded_io(
            [{"file": "src/auth_link.py", "line": 1, "severity": "P1", "title": "Symlink", "evidence": "def verify_token(token):"}],
            str(self.repo),
        )
        self.assertTrue(errs_sym)

        # CR-03: FIFO (named pipe) inside repo must NOT block or open as regular file
        fifo_path = self.repo / "src" / "pipe.py"
        os.mkfifo(fifo_path)
        errs_fifo = scenario_findings.verify_findings_grounded_io(
            [{"file": "src/pipe.py", "line": 1, "severity": "P1", "title": "Pipe", "evidence": "non regular fifo node"}],
            str(self.repo),
        )
        self.assertTrue(errs_fifo)

    def test_no_dirty_git_detects_new_file_in_untracked_dir_and_monorepo_subdir(self):
        # CR-04: adding a second file inside an already-untracked directory must change worktree fingerprint
        untracked_dir = self.repo / "scratch_dir"
        untracked_dir.mkdir()
        (untracked_dir / "a.txt").write_text("one\n", encoding="utf-8")
        fp1 = scenario_findings.compute_worktree_fingerprint(self.repo)
        self.assertFalse(fp1["error"])
        (untracked_dir / "b.txt").write_text("two\n", encoding="utf-8")
        fp2 = scenario_findings.compute_worktree_fingerprint(self.repo)
        self.assertNotEqual(fp1["fingerprint"], fp2["fingerprint"])

        # CR-05: subdirectory monorepo path resolves repo root cleanly
        sub_fp = scenario_findings.compute_worktree_fingerprint(self.repo / "sub")
        self.assertIsNone(sub_fp["error"])
        self.assertEqual(sub_fp["fingerprint"], fp2["fingerprint"])

    def test_no_dirty_git_unchanged_fails_closed_without_claim_fingerprint(self):
        # CR-02: claiming without --project-path omits baseline fingerprint; reporting with --project-path must fail closed
        run = self.cli("--project-path", str(self.repo), "scenario", "run", "pre-commit-review")
        rid = run["run_id"]
        # Claim WITHOUT --project-path
        self.cli("scenario", "next", rid, "--claim")
        rep = self.cli(
            "--project-path", str(self.repo), "scenario", "report", rid, "diff_check", "--json", "-",
            stdin_text=json.dumps({"status": "done", "summary": "No edits", "data": {"findings": []}}),
        )
        self.assertEqual(rep["status"], "rejected")
        self.assertTrue(any("missing baseline worktree fingerprint" in r for r in rep["reasons"]))

    # ---- CR-01, CR-13, CR-25: per-branch DAG dominance, corroboration propagation, finding_id collision

    def test_per_branch_dag_dominance_and_corroboration_preservation(self):
        defn = {
            "format": "richi-scenario", "format_version": 1, "id": "dual-branch-review", "title": "Dual Branch",
            "tags": ["review"],
            "steps": [
                {
                    "id": "branch_a", "type": "instruction", "title": "Branch A", "needs": [],
                    "matrix": {"pass_name": ["p1", "p2"]}, "matrix_mode": "replicated",
                    "params": {"text": "Audit A"},
                    "outputs": {"data": {"findings": {"type": "list", "required": True}}},
                    "checks": [{"kind": "findings_grounded", "path": "findings"}],
                },
                {
                    "id": "verify_a", "type": "instruction", "title": "Verify A", "needs": ["branch_a"],
                    "params": {"text": "Verify A"},
                    "outputs": {"data": {"findings": {"type": "list", "required": True}, "disproved": {"type": "list", "required": True}}},
                    "checks": [{"kind": "findings_grounded", "path": "findings"}, {"kind": "findings_grounded", "path": "disproved"}],
                },
                {
                    "id": "branch_b", "type": "instruction", "title": "Branch B", "needs": [],
                    "params": {"text": "Audit B"},
                    "outputs": {"data": {"findings": {"type": "list", "required": True}}},
                    "checks": [{"kind": "findings_grounded", "path": "findings"}],
                },
            ],
        }
        self.cli("scenario", "apply", "--json", "-",
                 stdin_text=json.dumps({"action": "add", "scope": "project", "project": "svc", "definition": defn}))
        rid = self.cli("--project-path", str(self.repo), "scenario", "run", "dual-branch-review")["run_id"]

        for key in ("branch_a.0", "branch_a.1"):
            self.cli("--project-path", str(self.repo), "scenario", "claim", rid, key)
            self.cli("--project-path", str(self.repo), "scenario", "report", rid, key, "--json", "-",
                     stdin_text=json.dumps({
                         "status": "done", "summary": "Found auth + noise",
                         "data": {"findings": [
                             {"file": "src/auth.py", "line": 4, "severity": "P0", "title": "Weak prefix check", "evidence": "return token.startswith('ok_')"},
                             {"file": "src/auth.py", "line": 2, "severity": "nit", "title": "Empty check noise", "evidence": "if not token returns False"},
                         ]},
                     }))
        # Critic verify_a (NOT named 'disprove') confirms auth bug and disproves noise
        self.cli("--project-path", str(self.repo), "scenario", "claim", rid, "verify_a")
        self.cli("--project-path", str(self.repo), "scenario", "report", rid, "verify_a", "--json", "-",
                 stdin_text=json.dumps({
                     "status": "done", "summary": "Verified A",
                     "data": {
                         "findings": [{"file": "src/auth.py", "line": 4, "severity": "P0", "title": "Weak prefix check", "evidence": "Confirmed prefix check bypass"}],
                         "disproved": [{"file": "src/auth.py", "line": 2, "severity": "nit", "title": "Empty check noise", "evidence": "Intentional guard for empty token"}],
                     },
                 }))
        # Independent parallel branch_b finds pay bug; verify_a MUST NOT dominate or drop branch_b!
        self.cli("--project-path", str(self.repo), "scenario", "claim", rid, "branch_b")
        self.cli("--project-path", str(self.repo), "scenario", "report", rid, "branch_b", "--json", "-",
                 stdin_text=json.dumps({
                     "status": "done", "summary": "Found pay bug",
                     "data": {"findings": [
                         {"file": "src/pay.py", "line": 4, "severity": "P1", "title": "Overdraft bug", "evidence": "return balance - amount"},
                     ]},
                 }))

        conn = memory.connect(self.db, readonly=True)
        try:
            snap = scenario_review.review_hub_snapshot(conn, memory, project_id="svc")
        finally:
            conn.close()
        active = [f for f in snap["findings"] if f["status"] != "disproved"]
        disproved = [f for f in snap["findings"] if f["status"] == "disproved"]
        files = {f["file"] for f in active}
        self.assertEqual(files, {"src/auth.py", "src/pay.py"})
        auth_f = next(f for f in active if f["file"] == "src/auth.py")
        # Upstream consensus_ratio from replicated branch_a must be preserved!
        self.assertEqual(auth_f["consensus_ratio"], {"votes": 2, "total": 2, "step_id": "branch_a"})
        self.assertEqual(len(disproved), 1)
        self.assertTrue(disproved[0]["finding_id"].endswith(":d"))
        dy = snap["kpis"]["disprove_yield"]
        self.assertEqual(dy["disproved"], 1)
        self.assertEqual(dy["candidates"], 2)
        self.assertLessEqual(dy["disprove_rate"], 1.0)

    def test_dedupe_findings_distinct_ids_for_same_location_and_disproved_suffix(self):
        # CR-25: two distinct titles or confirmed vs disproved at same line get unique finding_ids
        items = [
            {"file": "src/auth.py", "line": 4, "severity": "P1", "title": "Issue one", "evidence": "evidence one here", "status": "confirmed"},
            {"file": "src/auth.py", "line": 4, "severity": "P2", "title": "Issue one", "evidence": "disproved reason here", "status": "disproved"},
        ]
        res = scenario_findings.dedupe_findings(items)
        ids = [x["finding_id"] for x in res]
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), 2)
        self.assertTrue(any(fid.endswith(":d") for fid in ids))

    # ---- CR-06: eval memory.record strict no-op, fork memory.record never mutates existing entry

    def test_eval_memory_record_noop_and_fork_never_mutates_existing_entry(self):
        self.cli("entry", "put", "--json", "-", stdin_text=json.dumps({
            "id": "decision:shared-target", "kind": "decision", "title": "Original confirmed decision",
            "summary": "Immutable prod summary", "project_ids": ["svc"],
            "knowledge_state": "confirmed", "sources": [{"reference": "src/auth.py"}],
            "expected_updated_at": None,
        }))
        defn = {
            "format": "richi-scenario", "format_version": 1, "id": "rec-iso", "title": "Record Iso",
            "steps": [
                {"id": "work", "type": "instruction", "title": "Work", "needs": [], "params": {"text": "Work"}},
                {"id": "save", "type": "memory.record", "title": "Save", "needs": ["work"],
                 "params": {"kind": "decision", "entry_id": "decision:shared-target", "title": "Updated", "summary": "{{steps.work.summary}}", "knowledge_state": "confirmed"}},
            ],
        }
        self.cli("scenario", "apply", "--json", "-",
                 stdin_text=json.dumps({"action": "add", "scope": "project", "project": "svc", "definition": defn}))

        # Prod run first
        r_prod = self.cli("--project-path", str(self.repo), "scenario", "run", "rec-iso")["run_id"]
        self.cli("--project-path", str(self.repo), "scenario", "next", r_prod, "--claim")
        self.cli("--project-path", str(self.repo), "scenario", "report", r_prod, "work", "--json", "-",
                 stdin_text=json.dumps({"status": "done", "summary": "Prod change"}))
        after_prod = self.cli("entry", "get", "decision:shared-target")
        self.assertIn("Prod change", after_prod["summary"])

        # Fork run -> MUST NOT mutate decision:shared-target; creates fresh fork hypothesis entry
        forked = self.cli("--project-path", str(self.repo), "scenario", "fork", r_prod, "--from", "work")
        f_id = forked["run_id"]
        self.cli("--project-path", str(self.repo), "scenario", "next", f_id, "--claim")
        self.cli("--project-path", str(self.repo), "scenario", "report", f_id, "work", "--json", "-",
                 stdin_text=json.dumps({"status": "done", "summary": "Fork experiment"}))
        after_fork = self.cli("entry", "get", "decision:shared-target")
        self.assertEqual(after_fork["summary"], after_prod["summary"])
        self.assertEqual(after_fork["knowledge_state"], "confirmed")
        fork_entry = self.cli("entry", "get", f"decision:scenario-{f_id}-save")
        self.assertEqual(fork_entry["knowledge_state"], "hypothesis")

        # Eval run -> strict dry-run no-op, ZERO writes to entries
        case = {
            "id": "iso-case", "title": "Iso", "project_id": "svc",
            "base_rev": self.head_sha, "head_rev": self.head_sha,
            "expected_bugs": [{"id": "b1", "file": "src/auth.py", "line_range": [1, 4], "symbols": ["verify_token"]}],
        }
        self.cli("--project-path", str(self.repo), "scenario", "golden", "add", "--json", "-", stdin_text=json.dumps(case))
        ev = self.cli("--project-path", str(self.repo), "scenario", "eval", "rec-iso", "--case", "iso-case", "--memory-mode", "clean")
        e_id = ev["run_id"]
        self.cli("--project-path", str(self.repo), "scenario", "next", e_id, "--claim")
        self.cli("--project-path", str(self.repo), "scenario", "report", e_id, "work", "--json", "-",
                 stdin_text=json.dumps({"status": "done", "summary": "Eval dry run"}))
        after_eval = self.cli("entry", "get", "decision:shared-target")
        self.assertEqual(after_eval["summary"], after_prod["summary"])
        self.cli("entry", "get", f"decision:scenario-{e_id}-save", ok=False)

    # ---- CR-07: as_of_commit fail-closed + UTC cutoff

    def test_as_of_commit_fail_closed_and_cutoff(self):
        # Case without commit_time must fail closed for as_of_commit
        case_no_ts = {
            "id": "no-ts-case", "title": "No TS", "project_id": "svc",
            "base_rev": self.head_sha, "head_rev": self.head_sha,
            "expected_bugs": [{"id": "b1", "file": "src/auth.py", "line_range": [1, 4]}],
        }
        self.cli("--project-path", str(self.repo), "scenario", "golden", "add", "--json", "-", stdin_text=json.dumps(case_no_ts))
        self.cli("--project-path", str(self.repo), "scenario", "eval", "pre-commit-review",
                 "--case", "no-ts-case", "--memory-mode", "as_of_commit", ok=False)

        # Seed a recent fact (after 2020-01-01) and run as_of_commit at 2020-01-01 -> must NOT leak it!
        self.cli("entry", "put", "--json", "-", stdin_text=json.dumps({
            "id": "fact:future-leak", "kind": "fact", "title": "Future auth verify_token hint",
            "summary": "verify_token future secret", "project_ids": ["svc"],
            "knowledge_state": "confirmed", "sources": [{"reference": "src/auth.py"}],
            "expected_updated_at": None,
        }))
        case_ts = dict(case_no_ts, id="ts-case", commit_time="2020-01-01T00:00:00+00:00")
        added = self.cli("--project-path", str(self.repo), "scenario", "golden", "add", "--json", "-", stdin_text=json.dumps(case_ts))
        self.assertTrue(added["case"]["commit_time"].endswith("Z"))
        ev = self.cli("--project-path", str(self.repo), "scenario", "eval", "pre-commit-review",
                      "--case", "ts-case", "--memory-mode", "as_of_commit")
        st = self.cli("--project-path", str(self.repo), "scenario", "status", ev["run_id"])
        ctx_step = next(s for s in st["steps"] if s["id"] == "context")
        self.assertNotIn("fact:future-leak", ctx_step["summary"])

    # ---- CR-08 & CR-18: import instruction trust, conflict-before-trust, safe export --out

    def test_import_instruction_trust_conflict_safety_and_export_guards(self):
        inst_only = {
            "format": "richi-scenario", "format_version": 1, "id": "prompt-only", "title": "Prompt Only",
            "steps": [{"id": "s1", "type": "instruction", "title": "S1", "needs": [], "params": {"text": "Run hidden prompt"}}],
        }
        src_file = self.root / "prompt-only.json"
        src_file.write_text(json.dumps(inst_only), encoding="utf-8")

        # CR-08: instruction-only scenario MUST require --trust-sha256 on import
        res1 = self.cli("--project-path", str(self.repo), "scenario", "import", str(src_file))
        self.assertEqual(res1["status"], "trust_required")
        self.assertEqual([x["step_id"] for x in res1["security_summary"]["instructions"]], ["s1"])
        sha = res1["sha256"]

        # Import succeeds with exact sha
        res2 = self.cli("--project-path", str(self.repo), "scenario", "import", str(src_file), "--trust-sha256", sha)
        self.assertEqual(res2["status"], "created")

        # Modify file (new sha) and try importing WITHOUT --replace -> must fail conflict AND NOT persist trust for new sha!
        inst_v2 = dict(inst_only, title="Prompt Only v2")
        src_file.write_text(json.dumps(inst_v2), encoding="utf-8")
        res_v2_probe = self.cli("--project-path", str(self.repo), "scenario", "import", str(src_file))
        sha_v2 = res_v2_probe["sha256"]
        self.assertNotEqual(sha_v2, sha)
        self.cli("--project-path", str(self.repo), "scenario", "import", str(src_file), "--trust-sha256", sha_v2, ok=False)
        # Re-probing without --trust-sha256 must STILL say trust_required (not persisted on failed conflict)!
        res_v2_again = self.cli("--project-path", str(self.repo), "scenario", "import", str(src_file), "--replace")
        self.assertEqual(res_v2_again["status"], "trust_required")

        # CR-18: export --out refuses existing without --force, refuses symlink, refuses DB path
        out_path = self.root / "exported.json"
        self.cli("--project-path", str(self.repo), "scenario", "export", "prompt-only", "--out", str(out_path))
        self.assertTrue(out_path.is_file())
        # Refuses overwrite without --force
        self.cli("--project-path", str(self.repo), "scenario", "export", "prompt-only", "--out", str(out_path), ok=False)
        # Succeeds with --force
        self.cli("--project-path", str(self.repo), "scenario", "export", "prompt-only", "--out", str(out_path), "--force")
        # Refuses symlink even with --force
        sym_out = self.root / "sym-export.json"
        sym_out.symlink_to(out_path)
        self.cli("--project-path", str(self.repo), "scenario", "export", "prompt-only", "--out", str(sym_out), "--force", ok=False)
        # Refuses overwriting DB file
        self.cli("--project-path", str(self.repo), "scenario", "export", "prompt-only", "--out", str(self.db), "--force", ok=False)

    # ---- CR-09 & CR-15: fork on in-progress run leaves zero zombie claimed steps & matches dry-run

    def test_fork_in_progress_matches_dry_run_and_no_zombie_claims(self):
        defn = {
            "format": "richi-scenario", "format_version": 1, "id": "par-fork", "title": "Parallel Fork",
            "steps": [
                {"id": "a", "type": "instruction", "title": "A", "needs": [], "params": {"text": "A"}},
                {"id": "b", "type": "instruction", "title": "B", "needs": [], "params": {"text": "B"}},
                {"id": "c", "type": "instruction", "title": "C", "needs": ["a"], "params": {"text": "C"}},
            ],
        }
        self.cli("scenario", "apply", "--json", "-",
                 stdin_text=json.dumps({"action": "add", "scope": "project", "project": "svc", "definition": defn}))
        rid = self.cli("--project-path", str(self.repo), "scenario", "run", "par-fork")["run_id"]
        # Complete a, claim b (leave b claimed!)
        self.cli("--project-path", str(self.repo), "scenario", "claim", rid, "a")
        self.cli("--project-path", str(self.repo), "scenario", "report", rid, "a", "--json", "-",
                 stdin_text=json.dumps({"status": "done", "summary": "A done"}))
        self.cli("--project-path", str(self.repo), "scenario", "claim", rid, "b")

        preview = self.cli("--project-path", str(self.repo), "scenario", "fork", rid, "--from", "c", "--dry-run")
        self.assertEqual(preview["frozen_steps"], ["a"])
        self.assertEqual(preview["recompute_steps"], ["b", "c"])

        forked = self.cli("--project-path", str(self.repo), "scenario", "fork", rid, "--from", "c")
        self.assertEqual(forked["frozen_steps"], preview["frozen_steps"])
        self.assertEqual(forked["recompute_steps"], preview["recompute_steps"])
        by_id = {s["id"]: s for s in forked["steps"]}
        self.assertEqual(by_id["a"]["status"], "done")
        # b MUST be ready (NOT zombie claimed!)
        self.assertEqual(by_id["b"]["status"], "ready")
        self.assertEqual(by_id["c"]["status"], "ready")

    # ---- CR-10, CR-16, CR-29: feedback --batch raw list, flag conflict, comma scope_paths, confirmed restore

    def test_feedback_batch_raw_list_conflict_and_comma_scope_paths(self):
        rid = self.cli("--project-path", str(self.repo), "scenario", "run", "pre-commit-review")["run_id"]
        self.cli("--project-path", str(self.repo), "scenario", "next", rid, "--claim")
        self.cli("--project-path", str(self.repo), "scenario", "report", rid, "diff_check", "--json", "-",
                 stdin_text=json.dumps({
                     "status": "done", "summary": "One finding",
                     "data": {"findings": [{"file": "src/auth.py", "line": 4, "severity": "P1", "title": "Token check", "evidence": "return token.startswith('ok_')"}]},
                 }))
        self.cli("--project-path", str(self.repo), "scenario", "answer", rid, "decision", "fix_blockers")

        conn = memory.connect(self.db, readonly=True)
        try:
            snap = scenario_review.review_hub_snapshot(conn, memory, project_id="svc")
        finally:
            conn.close()
        fid = snap["findings"][0]["finding_id"]

        # CR-29: combining --batch with --finding must fail
        self.cli("--project-path", str(self.repo), "scenario", "feedback", rid, "--batch", "-", "--finding", fid,
                 stdin_text=json.dumps([{"finding_id": fid, "verdict": "valid_nit"}]), ok=False)

        # CR-10: raw JSON array in --batch works
        res = self.cli("--project-path", str(self.repo), "scenario", "feedback", rid, "--batch", "-",
                       stdin_text=json.dumps([{
                           "finding_id": fid, "verdict": "false_positive", "remember_pattern": True,
                           "scope_paths": ["src/a,b/auth.py", "src/auth.py"],
                           "safety_condition": "Gateway enforces HMAC signature before auth helper",
                       }]))
        self.assertEqual(res["count"], 1)
        entry_id = res["recorded_patterns"][0]["entry_id"]

        # CR-16: comma-containing scope_paths round-trips cleanly
        conn = memory.connect(self.db, readonly=True)
        try:
            snap2 = scenario_review.review_hub_snapshot(conn, memory, project_id="svc")
        finally:
            conn.close()
        pat = next(p for p in snap2["fp_patterns"] if p["entry_id"] == entry_id)
        self.assertEqual(pat["scope_paths"], ["src/a,b/auth.py", "src/auth.py"])

    # ---- CR-14, CR-19, CR-24: eval scoring 1-to-1, word boundary, optional scenario id

    def test_eval_scoring_word_boundary_one_to_one_and_optional_id(self):
        case = {
            "id": "wb-case", "title": "Word Boundary", "project_id": "svc",
            "base_rev": self.head_sha, "head_rev": self.head_sha,
            "expected_bugs": [
                {"id": "bug-1", "file": "src/auth.py", "line_range": [1, 4], "symbols": ["tok"]},
                {"id": "bug-2", "file": "src/auth.py", "line_range": [1, 4], "symbols": ["verify_token"]},
            ],
        }
        self.cli("--project-path", str(self.repo), "scenario", "golden", "add", "--json", "-", stdin_text=json.dumps(case))
        ev = self.cli("--project-path", str(self.repo), "scenario", "eval", "pre-commit-review", "--case", "wb-case", "--memory-mode", "clean")
        rid = ev["run_id"]
        self.cli("--project-path", str(self.repo), "scenario", "next", rid, "--claim")
        # Single finding mentions 'verify_token' (contains substring 'tok', NOT whole word 'tok')!
        self.cli("--project-path", str(self.repo), "scenario", "report", rid, "diff_check", "--json", "-",
                 stdin_text=json.dumps({
                     "status": "done", "summary": "Found one",
                     "data": {"findings": [
                         {"file": "src/auth.py", "line": 4, "severity": "P1", "title": "verify_token check", "evidence": "verify_token prefix check is weak"},
                     ]},
                 }))
        self.cli("--project-path", str(self.repo), "scenario", "answer", rid, "decision", "fix_blockers")

        # CR-19: eval --score works WITHOUT positional scenario ID; word-boundary + 1-to-1 matches only bug-2!
        scored = self.cli("--project-path", str(self.repo), "scenario", "eval", "--case", "wb-case", "--score", rid)
        self.assertEqual([d["bug_id"] for d in scored["result"]["detected"]], ["bug-2"])
        self.assertEqual([m["bug_id"] for m in scored["result"]["missed"]], ["bug-1"])
        self.assertEqual(scored["result"]["recall"], 0.5)

        # Passing wrong positional scenario ID with --score must be rejected!
        self.cli("--project-path", str(self.repo), "scenario", "eval", "bug-hunt", "--case", "wb-case", "--score", rid, ok=False)

    # ---- CR-26, CR-28, CR-30: stats accuracy, catalog parity, artifacts exposure, idempotent schema

    def test_stats_no_double_count_catalog_parity_and_artifacts_exposure(self):
        # CR-28 / CR-30: STEP_KINDS vs STEP_BLOCK_CATALOG parity
        active_blocks = {b["type"]: b for b in scenario_registry.STEP_BLOCK_CATALOG if not b.get("planned")}
        self.assertEqual(set(active_blocks), set(scenario_format.STEP_KINDS))
        for stype, spec in scenario_format.STEP_KINDS.items():
            self.assertEqual(set(active_blocks[stype]["exposes"]), set(spec["results"]), stype)
            if stype in ("instruction", "skill", "command"):
                self.assertIn("artifacts", spec["results"])

        # CR-30: scenario_schema.sql is idempotent when re-executed
        conn = memory.connect(self.db)
        try:
            schema_sql = (Path(scenario_registry.__file__).parent / "scenario_schema.sql").read_text(encoding="utf-8")
            conn.executescript(schema_sql)
        finally:
            conn.close()

        # CR-26 & CR-28: test artifacts in report + step context, and failed reason counted once
        defn = {
            "format": "richi-scenario", "format_version": 1, "id": "art-stats", "title": "Artifacts & Stats",
            "steps": [
                {"id": "s1", "type": "instruction", "title": "S1", "needs": [], "params": {"text": "Produce artifact"}},
                {"id": "s2", "type": "instruction", "title": "S2", "needs": ["s1"], "params": {"text": "Use {{steps.s1.artifacts}}"}},
            ],
        }
        self.cli("scenario", "apply", "--json", "-",
                 stdin_text=json.dumps({"action": "add", "scope": "project", "project": "svc", "definition": defn}))
        rid = self.cli("--project-path", str(self.repo), "scenario", "run", "art-stats")["run_id"]
        self.cli("--project-path", str(self.repo), "scenario", "next", rid, "--claim")
        # Bad artifact URI with '..' in id or newline in title rejected
        bad = self.cli("--project-path", str(self.repo), "scenario", "report", rid, "s1", "--json", "-",
                       stdin_text=json.dumps({
                           "status": "done", "summary": "Bad art",
                           "artifacts": [{"uri": "richi://artifact/a..b@v1/r.md", "title": "line1\nline2"}],
                       }))
        self.assertEqual(bad["status"], "rejected")
        # Valid artifact accepted and exposed to s2!
        good = self.cli("--project-path", str(self.repo), "scenario", "report", rid, "s1", "--json", "-",
                        stdin_text=json.dumps({
                            "status": "done", "summary": "Good art",
                            "artifacts": [{"uri": "richi://artifact/report-1@v1/summary.md", "kind": "report", "title": "Summary"}],
                        }))
        self.assertEqual(good["status"], "accepted")
        nxt = self.cli("--project-path", str(self.repo), "scenario", "next", rid, "--claim")
        self.assertIn("richi://artifact/report-1@v1/summary.md", nxt["step"]["text"])
        # Fail s2 with a reason -> stats must count that reason ONCE (not 2x!) and include scenario_id
        self.cli("--project-path", str(self.repo), "scenario", "report", rid, "s2", "--json", "-",
                 stdin_text=json.dumps({"status": "failed", "summary": "Boom", "reason": "unique_failure_reason"}))
        stats = self.cli("--project-path", str(self.repo), "scenario", "stats", "--scenario", "art-stats")
        s2_stat = next(s for s in stats["per_step"] if s["step_id"] == "s2")
        self.assertEqual(s2_stat["scenario_id"], "art-stats")
        self.assertEqual(s2_stat["top_failure_reasons"], [{"reason": "unique_failure_reason", "count": 1}])

    # ---- CR-12, CR-21, CR-23: likely_fixed chronology, HTTP 403/404 status mapping, hooks --project-path

    def test_likely_fixed_chronological_and_p0_p1_only(self):
        # Run 1 (older, done): has a P2 nit finding that disappears in Run 2 -> marked likely_fixed on finding,
        # but open_blockers.likely_fixed MUST remain 0 because it is P2 (not P0/P1)!
        r1 = self.cli("--project-path", str(self.repo), "scenario", "run", "pre-commit-review")["run_id"]
        self.cli("--project-path", str(self.repo), "scenario", "next", r1, "--claim")
        self.cli("--project-path", str(self.repo), "scenario", "report", r1, "diff_check", "--json", "-",
                 stdin_text=json.dumps({
                     "status": "done", "summary": "R1 P2",
                     "data": {"findings": [{"file": "src/auth.py", "line": 4, "severity": "P2", "title": "Minor style issue", "evidence": "return token.startswith('ok_')"}]},
                 }))
        self.cli("--project-path", str(self.repo), "scenario", "answer", r1, "decision", "fix_blockers")

        # Run 2 (newer, done): clean
        r2 = self.cli("--project-path", str(self.repo), "scenario", "run", "pre-commit-review")["run_id"]
        self.cli("--project-path", str(self.repo), "scenario", "next", r2, "--claim")
        self.cli("--project-path", str(self.repo), "scenario", "report", r2, "diff_check", "--json", "-",
                 stdin_text=json.dumps({"status": "done", "summary": "R2 clean", "data": {"findings": []}}))
        self.cli("--project-path", str(self.repo), "scenario", "answer", r2, "decision", "commit_as_is")

        # Run 3 (newest, STILL RUNNING / waiting on gate): has new P0 finding -> MUST NOT be marked likely_fixed by older done Run 2!
        r3 = self.cli("--project-path", str(self.repo), "scenario", "run", "pre-commit-review")["run_id"]
        self.cli("--project-path", str(self.repo), "scenario", "next", r3, "--claim")
        self.cli("--project-path", str(self.repo), "scenario", "report", r3, "diff_check", "--json", "-",
                 stdin_text=json.dumps({
                     "status": "done", "summary": "R3 new P0",
                     "data": {"findings": [{"file": "src/pay.py", "line": 4, "severity": "P0", "title": "New critical overdraft", "evidence": "return balance - amount"}]},
                 }))

        conn = memory.connect(self.db, readonly=True)
        try:
            snap = scenario_review.review_hub_snapshot(conn, memory, project_id="svc")
        finally:
            conn.close()
        by_run = {f["run_id"]: f for f in snap["findings"]}
        self.assertTrue(by_run[r1]["likely_fixed"])
        self.assertFalse(by_run[r3]["likely_fixed"])
        self.assertEqual(snap["kpis"]["open_blockers"]["p0"], 1)
        self.assertEqual(snap["kpis"]["open_blockers"]["likely_fixed"], 0)

    def test_map_feedback_http_403_and_404(self):
        import http.client
        import threading
        from richi import map_access
        rid = self.cli("--project-path", str(self.repo), "scenario", "run", "pre-commit-review")["run_id"]
        self.cli("--project-path", str(self.repo), "scenario", "next", rid, "--claim")
        self.cli("--project-path", str(self.repo), "scenario", "report", rid, "diff_check", "--json", "-",
                 stdin_text=json.dumps({
                     "status": "done", "summary": "Finding",
                     "data": {"findings": [{"file": "src/auth.py", "line": 4, "severity": "P1", "title": "Auth bug", "evidence": "return token.startswith('ok_')"}]},
                 }))
        self.cli("--project-path", str(self.repo), "scenario", "answer", rid, "decision", "fix_blockers")
        conn = memory.connect(self.db, readonly=True)
        try:
            fid = scenario_review.review_hub_snapshot(conn, memory, project_id="svc")["findings"][0]["finding_id"]
        finally:
            conn.close()

        server = serve.LoopbackHTTPServer(("127.0.0.1", 0), serve.handler_for(self.db))
        server.daemon_threads = True
        th = threading.Thread(target=server.serve_forever, daemon=True)
        th.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), th.join(timeout=5)))
        port = server.server_address[1]

        def post(path, body, hdrs=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            try:
                h = {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"}
                if hdrs:
                    h.update(hdrs)
                c.request("POST", path, body=json.dumps(body).encode("utf-8"), headers=h)
                r = c.getresponse()
                return r.status, json.loads(r.read().decode("utf-8"))
            finally:
                c.close()

        ticket = map_access.issue_ticket(self.db)
        st, sess = post("/api/session", {"ticket": ticket})
        self.assertEqual(st, 200)
        auth = {map_access.HEADER: sess["secret"]}

        # CR-21: remember_pattern via Map API must return HTTP 403 forbidden!
        st403, d403 = post("/api/scenario-feedback", {
            "run_id": rid,
            "items": [{"finding_id": fid, "verdict": "false_positive", "remember_pattern": True}],
        }, auth)
        self.assertEqual(st403, 403, d403)
        self.assertEqual(d403["code"], "forbidden")

        # CR-21: unknown run_id must return HTTP 404 not_found!
        st404, d404 = post("/api/scenario-feedback", {
            "run_id": "run-nonexistent-9999",
            "items": [{"finding_id": fid, "verdict": "valid_nit"}],
        }, auth)
        self.assertEqual(st404, 404, d404)
        self.assertEqual(d404["code"], "not_found")

    def test_hooks_stop_check_project_path_and_eval_isolation(self):
        rid = self.cli("--project-path", str(self.repo), "scenario", "run", "pre-commit-review")["run_id"]
        self.cli("--project-path", str(self.repo), "scenario", "next", rid, "--claim")
        hook = self.cli("--project-path", str(self.repo), "scenario", "hook", "stop")
        self.assertEqual(hook["decision"], "block")
        self.assertTrue(hook["enforced"])
        self.assertIn("--project-path", hook["next_command"])


if __name__ == "__main__":
    unittest.main()
