"""Comprehensive regression tests for all Review R5 unanimous consensus findings."""
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
    scenario_hooks,
    scenario_review,
    scenario_runs,
    scenarios,
)


def _clean_env(root):
    return {
        "HOME": str(root / "home"),
        "PATH": os.environ.get("PATH", ""),
        "XDG_CONFIG_HOME": str(root / "xdg"),
    }


def _apply_def(cli_fn, defn, *, scope="project", project="svc"):
    req = {"action": "add", "scope": scope, "definition": defn}
    if scope == "project" and project:
        req["project"] = project
    return cli_fn("scenario", "apply", "--json", "-", stdin_text=json.dumps(req))


class ReviewR5RegressionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="richi-r5-")
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
            "\n".join([f"# line {i}" for i in range(1, 31)]) + "\n"
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
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self.repo, check=True)
        self.head_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, text=True
        ).strip()

        self.repo2 = self.root / "repo2"
        self.repo2.mkdir()
        (self.repo2 / "README.md").write_text("other\n", encoding="utf-8")

        self.cli("init")
        self.cli("project", "add", str(self.repo), "--id", "svc", "--name", "Svc")
        self.cli("project", "add", str(self.repo2), "--id", "other", "--name", "Other")

    def cli(self, *args, chat="claude:chat-r5", stdin_text=None, ok=True):
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

    def save_entry(self, entry_id, *, kind="fact", title="T", summary="S", project_ids=None, tags=None, project_path=None):
        doc = {
            "id": entry_id,
            "kind": kind,
            "title": title,
            "summary": summary,
            "knowledge_state": "confirmed",
            "sources": [{"reference": "src/auth.py"}],
            "expected_updated_at": None,
        }
        if project_ids is not None:
            doc["project_ids"] = project_ids
        if tags:
            doc["tags"] = tags
        prefix = ["--project-path", str(project_path)] if project_path else []
        return self.cli(*prefix, "entry", "put", "--json", "-", stdin_text=json.dumps(doc))

    def step_result(self, run_id, step_key):
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT status, result_json FROM scenario_steps WHERE run_id = ? AND step_key = ?",
                (run_id, step_key),
            ).fetchone()
            self.assertIsNotNone(row, f"Missing step {step_key} in {run_id}")
            return row[0], (json.loads(row[1]) if row[1] else {})
        finally:
            conn.close()

    # ---- R5-IND-01: trust-v2 key & payload verification

    def test_trust_v2_key_and_payload_verification(self):
        defn = {
            "format": "richi-scenario",
            "format_version": 1,
            "id": "cmd-trust-flow",
            "title": "Cmd Trust",
            "steps": [
                {"id": "run_cmd", "type": "command", "title": "Run", "needs": [], "params": {"command": "echo hi"}}
            ],
        }
        imp_path = self.root / "cmd_trust.json"
        imp_path.write_text(json.dumps(defn), encoding="utf-8")

        res1 = self.cli("scenario", "import", str(imp_path), "--project", "svc")
        self.assertEqual(res1["status"], "trust_required")
        sha = res1["sha256"]

        # Spoofed trust key with mismatched project_id must NOT be accepted
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES(?, ?)",
                (
                    scenario_eval._trust_key("svc", sha),
                    json.dumps({"project_id": "other", "sha256": sha}),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        res_spoof = self.cli("scenario", "import", str(imp_path), "--project", "svc")
        self.assertEqual(res_spoof["status"], "trust_required")

        # Explicit --trust-sha256 writes scenario-trust-v2: and succeeds
        res2 = self.cli("scenario", "import", str(imp_path), "--project", "svc", "--trust-sha256", sha)
        self.assertEqual(res2["status"], "created")
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT key, value FROM metadata WHERE key = ?",
                (scenario_eval._trust_key("svc", sha),),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertTrue(row[0].startswith(scenario_eval.TRUST_V2_PREFIX))
            val = json.loads(row[1])
            self.assertEqual(val["project_id"], "svc")
            self.assertEqual(val["sha256"], sha)
        finally:
            conn.close()

        # Different project still requires trust
        res_other = self.cli("scenario", "import", str(imp_path), "--project", "other")
        self.assertEqual(res_other["status"], "trust_required")

    # ---- R5-IND-02 & R5-SELF-06: export --out "" / --force / DB clobber / io_error

    def test_export_out_validation_db_clobber_and_io_error(self):
        err_empty = self.cli("scenario", "export", "pre-commit-review", "--out", "", ok=False)
        self.assertIn(err_empty["code"], ("invalid_input", "usage_error"))

        err_force = self.cli("scenario", "export", "pre-commit-review", "--force", ok=False)
        self.assertIn(err_force["code"], ("invalid_input", "usage_error"))

        # Refuses to overwrite DB or WAL/SHM sidecar even with --force and different casing
        for target in (str(self.db), str(self.db).upper(), f"{self.db}-wal", f"{self.db}-shm"):
            err = self.cli("scenario", "export", "pre-commit-review", "--out", target, "--force", ok=False)
            self.assertIn(err["code"], ("invalid_input", "usage_error"), target)

        # Parent path is a regular file -> produces structured io_error (not unhandled OSError)
        blocker = self.root / "not_a_dir"
        blocker.write_text("file", encoding="utf-8")
        missing_out = blocker / "nested" / "out.json"
        err_io = self.cli("scenario", "export", "pre-commit-review", "--out", str(missing_out), ok=False)
        self.assertEqual(err_io["code"], "io_error")

    # ---- R5-ARCH-01: security_summary completeness

    def test_security_summary_covers_all_executable_and_prompt_surfaces(self):
        long_text = "A" * 350
        defn = {
            "format": "richi-scenario",
            "format_version": 1,
            "id": "sec-sum",
            "title": "Sec Sum",
            "when_to_use": "Use on every PR touching auth",
            "steps": [
                {"id": "s_inst", "type": "instruction", "title": "Inst", "needs": [], "params": {"text": long_text}},
                {
                    "id": "s_cmd",
                    "type": "command",
                    "title": "Cmd",
                    "needs": ["s_inst"],
                    "params": {"command": "pytest -q", "cwd": "sub"},
                },
                {
                    "id": "s_skill",
                    "type": "skill",
                    "title": "Sk",
                    "needs": ["s_cmd"],
                    "params": {"skill": {"default": "audit-skill"}, "instruction": long_text},
                },
                {
                    "id": "s_rec",
                    "type": "memory.record",
                    "title": "Rec",
                    "needs": ["s_skill"],
                    "params": {
                        "kind": "fact",
                        "entry_id": "fact-custom-1",
                        "title": "Recorded note",
                        "summary": long_text,
                        "tags": ["review", "auth"],
                    },
                },
            ],
        }
        sec = scenario_eval.security_summary(defn)
        self.assertEqual(sec["when_to_use"], "Use on every PR touching auth")
        self.assertEqual(sec["instructions"][0]["chars"], 350)
        self.assertTrue(sec["instructions"][0]["truncated"])
        self.assertEqual(sec["commands"][0]["cwd"], "sub")
        self.assertEqual(sec["skills"][0]["instruction_chars"], 350)
        self.assertTrue(sec["skills"][0]["instruction_truncated"])
        self.assertEqual(sec["memory_records"][0]["entry_id"], "fact-custom-1")
        self.assertEqual(sec["memory_records"][0]["tags"], ["review", "auth"])
        self.assertEqual(sec["memory_records"][0]["summary_chars"], 350)
        self.assertTrue(sec["memory_records"][0]["summary_truncated"])

    # ---- R5-CROSS-01: memory.record blocks FP overwrite & cross-project mutation

    def test_memory_record_blocks_fp_overwrite_and_cross_project_mutation(self):
        self.save_entry(
            "fact:fp-guard",
            kind="fact",
            title="FP auth guard",
            summary="Scoped false positive pattern",
            project_ids=["svc"],
            tags=["false-positive"],
            project_path=self.repo,
        )
        self.save_entry(
            "decision:other-only",
            kind="decision",
            title="Other project decision",
            summary="Belongs only to other",
            project_ids=["other"],
            project_path=self.repo2,
        )

        for idx, target_id in enumerate(("fact:fp-guard", "decision:other-only")):
            defn = {
                "format": "richi-scenario",
                "format_version": 1,
                "id": f"rec-guard-{idx}",
                "title": f"Rec Guard {idx}",
                "steps": [
                    {
                        "id": "rec",
                        "type": "memory.record",
                        "title": "Record",
                        "needs": [],
                        "params": {
                            "kind": "fact",
                            "entry_id": target_id,
                            "title": "Tampered",
                            "summary": "Should not overwrite",
                        },
                    }
                ],
            }
            _apply_def(self.cli, defn, scope="project", project="svc")
            run_res = self.cli("--project-path", str(self.repo), "scenario", "run", f"rec-guard-{idx}", "--project", "svc")
            self.assertEqual(run_res["steps"][0]["status"], "blocked")

        # Verify neither entry was mutated
        self.assertEqual(self.cli("entry", "get", "fact:fp-guard")["title"], "FP auth guard")
        self.assertEqual(self.cli("entry", "get", "decision:other-only")["title"], "Other project decision")

    # ---- R5-CROSS-06: baseline-v2 disjoint keys avoid colon collision

    def test_baseline_v2_disjoint_keys_avoid_colon_collision(self):
        proj_colon = self.root / "repo_colon"
        proj_colon.mkdir()
        proj_c = self.root / "repo_c"
        proj_c.mkdir()
        self.cli("project", "add", str(proj_colon), "--id", "c:a", "--name", "ColonProj")
        self.cli("project", "add", str(proj_c), "--id", "c", "--name", "ProjC")

        for sid, pid in (("a:b", "c"), ("b", "c:a")):
            _apply_def(
                self.cli,
                {
                    "format": "richi-scenario",
                    "format_version": 1,
                    "id": sid,
                    "title": sid,
                    "steps": [{"id": "s1", "type": "condition", "title": "C", "needs": [], "params": {"expr": "true"}}],
                },
                scope="project",
                project=pid,
            )

        r1 = self.cli("scenario", "run", "a:b", "--project", "c")["run_id"]
        r2 = self.cli("scenario", "run", "b", "--project", "c:a")["run_id"]
        self.cli("scenario", "baseline", r1)

        conn = memory.connect(self.db)
        try:
            b1 = scenario_runs.get_baseline_run_id(conn, "a:b", "c")
            b2 = scenario_runs.get_baseline_run_id(conn, "b", "c:a")
            self.assertEqual(b1, r1)
            self.assertIsNone(b2)
            keys = [row[0] for row in conn.execute("SELECT key FROM metadata WHERE key LIKE 'scenario-baseline-v2:%'")]
            self.assertEqual(len(keys), 1)
            self.assertNotIn(r2, scenario_runs._all_baseline_ids(conn))
        finally:
            conn.close()

    # ---- R5-IND-03 & R5-ARCH-02 & R5-ARCH-12: as_of_commit memory.brief + FP learnings + TZ + sanitization

    def test_as_of_commit_no_truncation_tz_aware_and_untrusted_sanitization(self):
        self.save_entry("fact:old-auth", title="Old auth invariant", summary="Token auth invariant alpha", project_ids=["svc"], project_path=self.repo)
        self.save_entry("fact:tz-before", title="TZ before auth invariant", summary="Token auth invariant tzbefore", project_ids=["svc"], project_path=self.repo)
        self.save_entry("fact:tz-after", title="TZ after auth invariant", summary="Token auth invariant tzafter", project_ids=["svc"], project_path=self.repo)
        self.save_entry("fact:fp-old", title="FP old auth", summary="Ignore verify_token in tests", project_ids=["svc"], tags=["false-positive"], project_path=self.repo)
        self.save_entry("fact:fp-corrupt", title="FP corrupt ts", summary="Corrupt timestamp entry", project_ids=["svc"], tags=["false-positive"], project_path=self.repo)

        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE entries SET created_at='2025-01-01T00:00:00Z', updated_at='2025-01-01T00:00:00Z' WHERE id='fact:old-auth'")
            # 02:00+03:00 == 2024-12-31T23:00:00Z (strictly BEFORE 2025-01-01T00:00:00Z)
            conn.execute("UPDATE entries SET created_at='2025-01-01T02:00:00+03:00', updated_at='2025-01-01T02:00:00+03:00' WHERE id='fact:tz-before'")
            # 03:30+03:00 == 2025-01-01T00:30:00Z (strictly AFTER 2025-01-01T00:00:00Z)
            conn.execute("UPDATE entries SET created_at='2025-01-01T03:30:00+03:00', updated_at='2025-01-01T03:30:00+03:00' WHERE id='fact:tz-after'")
            conn.execute(
                "UPDATE entries SET created_at='2025-01-01T00:00:00Z', updated_at='2025-01-01T00:00:00Z', summary=? WHERE id='fact:fp-old'",
                ("Scope paths: src/auth.py\nSafety condition: Gateway verifies token\x00safely",),
            )
            conn.execute("UPDATE entries SET created_at='2026-06-01T00:00:00Z', updated_at='2026-06-01T00:00:00Z' WHERE id='fact:fp-corrupt'")

            for i in range(55):
                eid = f"fact:new-{i:03d}"
                conn.execute(
                    "INSERT INTO entries(id, kind, title, summary, knowledge_state, sources, tags, aliases, created_at, updated_at) "
                    "VALUES(?, 'fact', ?, ?, 'confirmed', '[]', '[]', '[]', '2026-06-01T00:00:00Z', '2026-06-01T00:00:00Z')",
                    (eid, f"New auth invariant {i}", f"Token auth invariant newer {i}"),
                )
                conn.execute("INSERT INTO entry_projects(entry_id, project_id) VALUES(?, 'svc')", (eid,))
                conn.execute(
                    "INSERT INTO entries_fts(id, title, summary, jira_key, tags, aliases) VALUES(?, ?, ?, '', '', '')",
                    (eid, f"New auth invariant {i}", f"Token auth invariant newer {i}"),
                )

            for i in range(125):
                fid = f"fact:fp-new-{i:03d}"
                conn.execute(
                    "INSERT INTO entries(id, kind, title, summary, knowledge_state, sources, tags, aliases, created_at, updated_at) "
                    "VALUES(?, 'fact', ?, ?, 'confirmed', '[]', ?, '[]', '2026-06-01T00:00:00Z', '2026-06-01T00:00:00Z')",
                    (fid, f"New FP {i}", "Scope: src/auth.py\nSafety condition: Newer condition here", json.dumps(["false-positive"])),
                )
                conn.execute("INSERT INTO entry_projects(entry_id, project_id) VALUES(?, 'svc')", (fid,))
            conn.commit()
        finally:
            conn.close()

        defn = {
            "format": "richi-scenario",
            "format_version": 1,
            "id": "as-of-brief-flow",
            "title": "As Of Brief",
            "steps": [
                {
                    "id": "brief",
                    "type": "memory.brief",
                    "title": "Brief",
                    "needs": [],
                    "params": {"question": "Token auth invariant", "limit": 10, "include_learnings": True},
                }
            ],
        }
        _apply_def(self.cli, defn, scope="project", project="svc")

        case_doc = {
            "id": "case-asof",
            "title": "AsOf Case",
            "project_id": "svc",
            "base_rev": self.head_sha,
            "head_rev": self.head_sha,
            "commit_time": "2025-01-01T00:00:00Z",
            "expected_bugs": [{"id": "b1", "file": "src/auth.py", "line_range": [31, 34]}],
        }
        self.cli("--project-path", str(self.repo), "scenario", "golden", "add", "--json", "-", stdin_text=json.dumps(case_doc))

        ev = self.cli(
            "--project-path", str(self.repo),
            "scenario", "eval", "as-of-brief-flow",
            "--case", "case-asof", "--project", "svc", "--memory-mode", "as_of_commit",
        )
        _st, res_obj = self.step_result(ev["run_id"], "brief")
        step_data = res_obj["data"]
        self.assertTrue(step_data.get("untrusted_memory"))
        entry_ids = {r.removeprefix("entry:") for r in step_data.get("refs", [])}
        self.assertIn("fact:old-auth", entry_ids)
        self.assertIn("fact:tz-before", entry_ids)
        self.assertNotIn("fact:tz-after", entry_ids)
        self.assertFalse(any(eid.startswith("fact:new-") for eid in entry_ids))

        learning_ids = {l["entry_id"] for l in step_data.get("learnings", [])}
        self.assertEqual(learning_ids, {"fact:fp-old"})
        self.assertNotIn("fact:fp-corrupt", learning_ids)
        self.assertNotIn("\x00", step_data["learnings"][0]["safety_condition"])

        # Corrupt timestamp in load_scoped_fp_learnings fails closed when as_of_dt is set
        conn = memory.connect(self.db)
        try:
            conn.execute("UPDATE entries SET updated_at='not-a-date' WHERE id='fact:fp-corrupt'")
            conn.commit()
            fp_items, _ = scenario_review.load_scoped_fp_learnings(
                conn, project_id="svc", as_of_dt=scenario_review._parse_utc("2025-01-01T00:00:00Z")
            )
            self.assertEqual({x["entry_id"] for x in fp_items}, {"fact:fp-old"})
        finally:
            conn.close()

    # ---- R5-IND-04, R5-IND-05, R5-ARCH-03, R5-ARCH-07, R5-SELF-07: fork skipped matrix, eval isolation, secret change

    def test_fork_skipped_matrix_eval_isolation_and_secret_type_change(self):
        defn = {
            "format": "richi-scenario",
            "format_version": 1,
            "id": "fork-complex",
            "title": "Fork Complex",
            "inputs": {
                "flag": {"type": "boolean", "default": False},
                "token_arg": {"type": "string", "default": "plain-val"},
            },
            "steps": [
                {
                    "id": "mat",
                    "type": "instruction",
                    "title": "Mat {{matrix.ang}}",
                    "needs": [],
                    "if": "inputs.flag == true",
                    "matrix": {"ang": ["a", "b"]},
                    "params": {"text": "Check {{matrix.ang}}"},
                },
                {
                    "id": "rec",
                    "type": "memory.record",
                    "title": "Record",
                    "needs": ["mat"],
                    "params": {"kind": "fact", "title": "Should stay dry in eval", "summary": "Eval isolation"},
                },
                {
                    "id": "tail",
                    "type": "instruction",
                    "title": "Tail",
                    "needs": ["rec"],
                    "params": {"text": "Finish"},
                },
            ],
        }
        _apply_def(self.cli, defn, scope="project", project="svc")

        case_doc = {
            "id": "case-fork",
            "title": "Fork Case",
            "project_id": "svc",
            "base_rev": self.head_sha,
            "head_rev": self.head_sha,
            "expected_bugs": [{"id": "b1", "file": "src/auth.py", "line_range": [31, 34]}],
        }
        self.cli("--project-path", str(self.repo), "scenario", "golden", "add", "--json", "-", stdin_text=json.dumps(case_doc))

        ev = self.cli("--project-path", str(self.repo), "scenario", "eval", "fork-complex", "--case", "case-fork", "--project", "svc")
        src_run_id = ev["run_id"]
        self.assertEqual(ev["steps"][0]["status"], "skipped")
        _st, rec_res = self.step_result(src_run_id, "rec")
        self.assertEqual(rec_res["data"].get("status"), "simulated_eval")

        # Update scenario changing token_arg from string -> secret
        updated = json.loads(json.dumps(defn))
        updated["inputs"]["token_arg"] = {"type": "secret"}
        meta = self.cli("scenario", "show", "fork-complex", "--project", "svc")
        self.cli(
            "scenario", "apply", "--json", "-",
            stdin_text=json.dumps({
                "action": "update", "id": "fork-complex", "scope": "project", "project": "svc",
                "expected_updated_at": meta["updated_at"], "definition": updated,
            }),
        )

        # Fork from `rec` with --use-latest:
        # 1) accepts unexpanded skipped conditional matrix step `mat`
        # 2) drops `token_arg` whose type changed to secret
        # 3) preserves run_kind == "eval" so `rec` remains dry-run and feedback is rejected
        forked = self.cli("--project-path", str(self.repo), "scenario", "fork", src_run_id, "--from", "rec", "--use-latest")
        self.assertEqual(forked["run_kind"], "eval")
        self.assertNotIn("token_arg", forked.get("inputs", {}))
        _fst, frec_res = self.step_result(forked["run_id"], "rec")
        self.assertEqual(frec_res["data"].get("status"), "simulated_eval")

        # Feedback must be rejected on eval and eval-fork runs
        fb_err = self.cli(
            "scenario", "feedback", forked["run_id"],
            "--finding", "f1", "--verdict", "actionable_bug", ok=False,
        )
        self.assertIn(fb_err["code"], ("invalid_input", "invalid_state"))

    # ---- R5-ARCH-03: compare memory_mode isolation & feedback remember-pattern prod guard

    def test_compare_memory_mode_isolation_and_remember_pattern_prod_guard(self):
        defn = {
            "format": "richi-scenario",
            "format_version": 1,
            "id": "cmp-iso",
            "title": "Compare Iso",
            "steps": [
                {"id": "measure", "type": "instruction", "title": "Measure", "needs": [], "params": {"text": "Measure"}},
                {"id": "cmp", "type": "compare", "title": "Compare", "needs": ["measure"], "params": {"step": "measure", "metrics": ["score"]}},
            ],
        }
        _apply_def(self.cli, defn, scope="project", project="svc")

        # Create a prod baseline run dated 2026-06-01
        r_prod = self.cli("--project-path", str(self.repo), "scenario", "run", "cmp-iso", "--project", "svc")["run_id"]
        self.cli("scenario", "claim", r_prod, "measure", "--agent", "claude")
        self.cli("scenario", "report", r_prod, "measure", "--json", "-", stdin_text=json.dumps({"status": "done", "summary": "ok", "data": {"score": 10}}))
        self.cli("scenario", "baseline", r_prod)
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE scenario_runs SET created_at='2026-06-01T00:00:00Z', updated_at='2026-06-01T00:00:00Z' WHERE id=?", (r_prod,))
            conn.commit()
        finally:
            conn.close()

        case_doc = {
            "id": "case-cmp",
            "title": "Compare Case",
            "project_id": "svc",
            "base_rev": self.head_sha,
            "head_rev": self.head_sha,
            "commit_time": "2025-01-01T00:00:00Z",
            "expected_bugs": [{"id": "b1", "file": "src/auth.py", "line_range": [31, 34]}],
        }
        self.cli("--project-path", str(self.repo), "scenario", "golden", "add", "--json", "-", stdin_text=json.dumps(case_doc))

        # clean, with_fp_only, and as_of_commit (2025-01-01 < 2026-06-01) must omit the newer prod baseline
        for mode in ("clean", "with_fp_only", "as_of_commit"):
            ev = self.cli("--project-path", str(self.repo), "scenario", "eval", "cmp-iso", "--case", "case-cmp", "--project", "svc", "--memory-mode", mode)
            rid = ev["run_id"]
            self.cli("scenario", "claim", rid, "measure", "--agent", "claude")
            self.cli("scenario", "report", rid, "measure", "--json", "-", stdin_text=json.dumps({"status": "done", "summary": "ok", "data": {"score": 12}}))
            _cst, cmp_res = self.step_result(rid, "cmp")
            self.assertFalse(cmp_res["data"]["has_baseline"], f"mode={mode} leaked baseline")

        # Non-prod fork run rejects --remember-pattern
        fork_prod = self.cli("--project-path", str(self.repo), "scenario", "fork", r_prod, "--from", "measure")["run_id"]
        err_rem = self.cli(
            "scenario", "feedback", fork_prod,
            "--finding", "f1", "--verdict", "false_positive",
            "--remember-pattern", "--scope-path", "src/auth.py",
            "--safety-condition", "Gateway verifies signature before handler",
            ok=False,
        )
        self.assertEqual(err_rem["code"], "invalid_state")

    # ---- R5-IND-10, R5-ARCH-06, R5-ARCH-14: golden_add / eval_start project isolation & eval --input

    def test_golden_project_isolation_eval_inputs_and_string_only_autofill(self):
        case_doc = {
            "id": "case-iso-1",
            "title": "Iso 1",
            "project_id": "svc",
            "base_rev": self.head_sha,
            "head_rev": self.head_sha,
            "expected_bugs": [{"id": "b1", "file": "src/auth.py", "line_range": [31, 34]}],
        }
        self.cli("--project-path", str(self.repo), "scenario", "golden", "add", "--json", "-", stdin_text=json.dumps(case_doc))

        # Cross-project overwrite of same case_id must be rejected
        case_other = {
            "id": "case-iso-1",
            "title": "Iso Other",
            "project_id": "other",
            "base_rev": self.head_sha,
            "head_rev": self.head_sha,
            "expected_bugs": [{"id": "b2", "file": "README.md", "line_range": [1, 1]}],
        }
        err_add = self.cli("--project-path", str(self.repo2), "scenario", "golden", "add", "--json", "-", stdin_text=json.dumps(case_other), ok=False)
        self.assertEqual(err_add["code"], "project_mismatch")

        # eval_start with mismatched --project must fail
        err_eval = self.cli("scenario", "eval", "pre-commit-review", "--case", "case-iso-1", "--project", "other", ok=False)
        self.assertEqual(err_eval["code"], "project_mismatch")

        # Typed non-string `target` input must NOT be auto-filled with commit sha, while --input overrides work
        defn = {
            "format": "richi-scenario",
            "format_version": 1,
            "id": "typed-eval-in",
            "title": "Typed Eval Inputs",
            "inputs": {
                "target": {"type": "number", "default": 42},
                "custom_req": {"type": "string", "required": True},
            },
            "steps": [{"id": "s1", "type": "condition", "title": "C", "needs": [], "params": {"expr": "true"}}],
        }
        _apply_def(self.cli, defn, scope="project", project="svc")
        ev = self.cli(
            "--project-path", str(self.repo),
            "scenario", "eval", "typed-eval-in",
            "--case", "case-iso-1", "--project", "svc",
            "--input", "custom_req=hello-eval",
        )
        self.assertEqual(ev["inputs"]["target"], 42)
        self.assertEqual(ev["inputs"]["custom_req"], "hello-eval")

    # ---- R5-IND-14: end_line interval overlap & exact max-cardinality min-cost bipartite matching

    def test_eval_score_end_line_overlap_and_exact_bipartite_matching(self):
        # Interval overlap: bug spans lines 10..25, finding spans 22..28 (exact overlap!)
        q = scenario_eval._match_quality(
            {"file": "src/auth.py", "line": 22, "end_line": 28, "title": "Missing check"},
            {"file": "src/auth.py", "line_range": [10, 25]},
        )
        self.assertIsNotNone(q)
        self.assertEqual(q[0], 0)
        # Finding starts at line 5, ends at line 12 -> overlaps [10, 25] even though abs(5-10)=5 and end extends inside!
        q2 = scenario_eval._match_quality(
            {"file": "src/auth.py", "line": 3, "end_line": 12, "title": "Multi-line span"},
            {"file": "src/auth.py", "line_range": [10, 25]},
        )
        self.assertIsNotNone(q2)
        self.assertEqual(q2[0], 0)

        # Classic bipartite failure case for greedy:
        # f0 matches spec0 (exact line+symbol -> cost 0) AND spec1 (line within slop -> cost 1)
        # f1 matches ONLY spec0 (line within slop -> cost 2; distance to spec1 is 10 > slop=5)
        # Greedy picks (f0, spec0) leaving (f1, spec1) unmatched (cardinality=1).
        # Exact SPFA max-cardinality min-cost matching MUST pair (f0, spec1) and (f1, spec0) -> cardinality=2!
        specs = [
            {"id": "b0", "file": "src/auth.py", "line_range": [10, 10], "symbols": ["verify_token"]},
            {"id": "b1", "file": "src/auth.py", "line_range": [18, 18]},
        ]
        candidates = [
            {"finding_id": "f0", "file": "src/auth.py", "line": 14, "title": "verify_token bug"},
            {"finding_id": "f1", "file": "src/auth.py", "line": 8, "title": "other issue"},
        ]
        matched = scenario_eval._min_cost_max_matching(specs, candidates)
        self.assertEqual(len(matched), 2)
        self.assertEqual(matched[0]["finding_id"], "f1")
        self.assertEqual(matched[1]["finding_id"], "f0")

    # ---- R5-CROSS-04 & R5-ARCH-08: Cyrillic line=None dedup, empty-file guard, custom findings_grounded.path

    def test_cyrillic_line_none_dedup_empty_file_guard_and_custom_grounded_path(self):
        f1 = {"file": "src/auth.py", "line": None, "severity": "P1", "title": "Утечка токена при проверке", "evidence": "", "step_id": "s1", "step_key": "s1"}
        f2 = {"file": "src/auth.py", "line": None, "severity": "P1", "title": "Утечка токена при проверке входа", "evidence": "", "step_id": "s2", "step_key": "s2"}
        self.assertTrue(scenario_findings.findings_similar(f1, f2))
        deduped = scenario_findings.dedupe_findings([f1, f2])
        self.assertEqual(len(deduped), 1)

        # Empty file must not merge unrelated findings
        e1 = {"file": "", "line": None, "severity": "P2", "title": "Утечка токена", "evidence": "", "step_id": "s1", "step_key": "s1"}
        e2 = {"file": "", "line": None, "severity": "P2", "title": "Утечка токена", "evidence": "", "step_id": "s2", "step_key": "s2"}
        self.assertEqual(len(scenario_findings.dedupe_findings([e1, e2])), 2)

        # Custom findings_grounded.path ("custom_bugs") must be extracted by _extract_run_findings
        defn = {
            "steps": [
                {
                    "id": "custom_rev",
                    "type": "instruction",
                    "needs": [],
                    "checks": [{"kind": "findings_grounded", "path": "custom_bugs"}],
                }
            ]
        }
        step_rows = [
            {
                "step_id": "custom_rev",
                "step_key": "custom_rev",
                "status": "done",
                "matrix_json": None,
                "result_json": json.dumps({
                    "data": {
                        "custom_bugs": [
                            {"file": "src/auth.py", "line": 31, "severity": "P1", "title": "Custom path bug", "evidence": "def verify_token"}
                        ]
                    }
                }),
            }
        ]
        extracted, ext_meta = scenario_review._extract_run_findings(defn, step_rows)
        self.assertEqual(len(extracted), 1)
        self.assertEqual(extracted[0]["title"], "Custom path bug")
        self.assertTrue(ext_meta["has_emitting_step"])

    # ---- R5-IND-09, R5-IND-12, R5-IND-13, R5-SELF-03, R5-CROSS-05, R5-IND-15, R5-IND-18

    def test_review_hub_disproved_ancestry_cancelled_likely_fixed_dotdir_and_matrix_order(self):
        defn = {
            "format": "richi-scenario",
            "format_version": 1,
            "id": "rev-3stage",
            "title": "Review 3-Stage",
            "tags": ["review"],
            "steps": [
                {
                    "id": "angle",
                    "type": "instruction",
                    "title": "Angle {{matrix.a}}",
                    "needs": [],
                    "matrix": {"a": ["sec", "perf"]},
                    "checks": [{"kind": "findings_grounded", "path": "findings"}],
                    "params": {"text": "Review"},
                },
                {
                    "id": "merge",
                    "type": "instruction",
                    "title": "Merge",
                    "needs": ["angle"],
                    "checks": [{"kind": "findings_grounded", "path": "findings"}],
                    "params": {"text": "Merge"},
                },
                {
                    "id": "critic",
                    "type": "instruction",
                    "title": "Critic",
                    "needs": ["merge"],
                    "checks": [{"kind": "findings_grounded", "path": "findings"}],
                    "params": {"text": "Disprove"},
                },
            ],
        }
        _apply_def(self.cli, defn, scope="project", project="svc")

        run_started = self.cli("--project-path", str(self.repo), "scenario", "run", "rev-3stage", "--project", "svc")
        run1 = run_started["run_id"]
        angle_keys = [s["key"] for s in run_started["steps"] if s["id"] == "angle"]
        self.assertEqual(len(angle_keys), 2)
        for key, sev in zip(angle_keys, ("P0", "P1")):
            self.cli("--project-path", str(self.repo), "scenario", "claim", run1, key, "--agent", "claude")
            self.cli(
                "--project-path", str(self.repo), "scenario", "report", run1, key, "--json", "-",
                stdin_text=json.dumps({
                    "status": "done",
                    "summary": "found",
                    "data": {
                        "findings": [
                            {"file": "src/auth.py", "line": 31, "severity": sev, "title": "Token bypass in verify_token", "evidence": "def verify_token(token):"},
                            {"file": "src/pay.py", "line": 2, "severity": "P2", "title": "Negative amount check", "evidence": "if amount < 0:"},
                        ]
                    },
                }),
            )
        self.cli("--project-path", str(self.repo), "scenario", "claim", run1, "merge", "--agent", "claude")
        self.cli(
            "--project-path", str(self.repo), "scenario", "report", run1, "merge", "--json", "-",
            stdin_text=json.dumps({
                "status": "done",
                "summary": "merged",
                "data": {
                    "findings": [
                        {"file": "src/auth.py", "line": 31, "severity": "P0", "title": "Token bypass in verify_token", "evidence": "def verify_token(token):"},
                        {"file": "src/pay.py", "line": 2, "severity": "P2", "title": "Negative amount check", "evidence": "if amount < 0:"},
                    ]
                },
            }),
        )
        self.cli("--project-path", str(self.repo), "scenario", "claim", run1, "critic", "--agent", "claude")
        self.cli(
            "--project-path", str(self.repo), "scenario", "report", run1, "critic", "--json", "-",
            stdin_text=json.dumps({
                "status": "done",
                "summary": "critiqued",
                "data": {
                    "findings": [
                        {"file": "src/auth.py", "line": 31, "severity": "P0", "title": "Token bypass in verify_token", "evidence": "def verify_token(token):"},
                        {"file": "src/pay.py", "line": 2, "severity": "P2", "title": "Negative amount check", "evidence": "if amount < 0:", "status": "disproved"},
                    ]
                },
            }),
        )

        # Cancelled run must be excluded from Review Hub
        run_cancel = self.cli("--project-path", str(self.repo), "scenario", "run", "rev-3stage", "--project", "svc")["run_id"]
        self.cli("scenario", "cancel", run_cancel)

        conn = memory.connect(self.db)
        try:
            snap = scenario_review.review_hub_snapshot(conn, memory, project_id="svc")
            self.assertEqual(snap["runs_analyzed"], 1)
            by_file = {f["file"]: f for f in snap["findings"]}
            auth_f = by_file["src/auth.py"]
            pay_f = by_file["src/pay.py"]

            # Disproved via status="disproved" inside data.findings
            self.assertEqual(pay_f["status"], "disproved")
            self.assertEqual(snap["kpis"]["disprove_yield"]["disproved"], 1)

            # Confirmed by critic, upstream provenance excludes dominating `merge` step, preserves severity_votes
            self.assertEqual(auth_f["status"], "confirmed")
            self.assertNotIn("merge", auth_f["corroborated_by"])
            self.assertIn("sec", auth_f["corroborated_by"])
            self.assertIn("perf", auth_f["corroborated_by"])
            self.assertEqual(auth_f["severity_votes"].get("P0"), 1)
            self.assertEqual(auth_f["severity_votes"].get("P1"), 1)
            self.assertFalse(auth_f["likely_fixed"])

            # Insert a newer done run for rev-3stage with NO done emitting steps -> must NOT flip likely_fixed!
            r_empty_id = "run-empty-done-check"
            conn.execute(
                "INSERT INTO scenario_runs(id, scenario_id, scenario_version, scope, project_id, scenario_sha256, definition_json, inputs_json, status, revision, created_at, updated_at) "
                "SELECT ?, scenario_id, scenario_version, scope, project_id, scenario_sha256, definition_json, inputs_json, 'done', 1, '2099-01-01T00:00:00Z', '2099-01-01T00:00:00Z' "
                "FROM scenario_runs WHERE id = ?",
                (r_empty_id, run1),
            )
            conn.commit()
            snap2 = scenario_review.review_hub_snapshot(conn, memory, project_id="svc")
            auth_f2 = next(f for f in snap2["findings"] if f["file"] == "src/auth.py")
            self.assertFalse(auth_f2["likely_fixed"])
        finally:
            conn.close()

        # R5-IND-15: dotdir normalization in _extract_path_hints
        hints = scenario_runs._extract_path_hints("Check ./src/auth.py and ./src/pay.py", {})
        self.assertIn("src/auth.py", hints)
        self.assertNotIn("./src/auth.py", hints)

    # ---- R5-CROSS-02 & R5-IND-07: workspace-wide FP isolation & stop hook --project-path

    def test_workspace_wide_fp_isolation_and_stop_hook_project_path(self):
        self.save_entry(
            "fact:proj-fp", title="Proj FP", summary="Scope paths: src/auth.py\nSafety condition: Project only guard",
            project_ids=["svc"], tags=["false-positive"], project_path=self.repo,
        )
        self.save_entry("fact:global-fp", title="Global FP", summary="Workspace wide", tags=["false-positive"])
        conn = memory.connect(self.db)
        try:
            ws_only, _ = scenario_review.load_scoped_fp_learnings(conn, project_id=None)
            self.assertEqual({x["title"] for x in ws_only}, {"Global FP"})
            all_hub, _ = scenario_review.load_scoped_fp_learnings(conn, project_id=None, include_all_projects=True)
            self.assertEqual({x["title"] for x in all_hub}, {"Global FP", "Proj FP"})
            # R5-IND-15: dotdir ./src/auth.py scores scope match against src/auth.py (ranks Proj FP first)
            dot_items, _ = scenario_review.load_scoped_fp_learnings(conn, project_id="svc", changed_files=["./src/auth.py"])
            self.assertEqual(dot_items[0]["title"], "Proj FP")
        finally:
            conn.close()

        # Workspace-wide run stop hook must NOT emit --project-path
        _apply_def(
            self.cli,
            {
                "format": "richi-scenario",
                "format_version": 1,
                "id": "ws-stop",
                "title": "WS Stop",
                "steps": [{"id": "work", "type": "instruction", "title": "W", "needs": [], "params": {"text": "Work"}}],
            },
            scope="global",
            project=None,
        )
        r_ws = self.cli("scenario", "run", "ws-stop")["run_id"]
        self.cli("scenario", "claim", r_ws, "work", "--agent", "claude")

        conn = memory.connect(self.db)
        try:
            decision = scenario_hooks.stop_check(
                conn, memory, chat="claude:chat-r5", agent="claude", project_path=str(self.repo)
            )
            self.assertEqual(decision["decision"], "block")
            self.assertNotIn("--project-path", decision["reason"])
        finally:
            conn.close()

    # ---- R5-IND-08/16, R5-ARCH-04/05/10/11/13, R5-CROSS-03/07/11/12, R5-SELF-05

    def test_hooks_starvation_malformed_meta_no_dirty_git_preflight_resume_and_cli_flags(self):
        _apply_def(
            self.cli,
            {
                "format": "richi-scenario",
                "format_version": 1,
                "id": "starve-flow",
                "title": "Starve",
                "steps": [
                    {"id": "work", "type": "instruction", "title": "W", "needs": [], "params": {"text": 'mentions "no_dirty_git" literally'}},
                    {"id": "ask", "type": "gate", "title": "Ask", "needs": ["work"], "params": {"question": "Ok?"}},
                ],
            },
            scope="project",
            project="svc",
        )
        r_prod = self.cli("--project-path", str(self.repo), "scenario", "run", "starve-flow", "--project", "svc")["run_id"]
        self.cli("scenario", "claim", r_prod, "work", "--agent", "claude")

        conn = sqlite3.connect(self.db)
        try:
            conn.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES(?, ?)", (f"scenario-run-meta:{r_prod}", "{corrupt-json"))
            for i in range(55):
                erid = f"run-eval-flood-{i:03d}"
                conn.execute(
                    "INSERT INTO scenario_runs(id, scenario_id, scenario_version, scope, project_id, scenario_sha256, definition_json, inputs_json, status, revision, created_at, updated_at) "
                    "SELECT ?, scenario_id, scenario_version, scope, project_id, scenario_sha256, definition_json, inputs_json, 'waiting', 1, ?, ? "
                    "FROM scenario_runs WHERE id = ?",
                    (erid, f"2099-01-01T00:00:{i:02d}Z", f"2099-01-01T00:00:{i:02d}Z", r_prod),
                )
                conn.execute(
                    "INSERT INTO scenario_steps(run_id, step_key, step_id, position, type, status, executor_role) "
                    "VALUES(?, 'ask', 'ask', 1, 'gate', 'waiting_human', 'human')",
                    (erid,),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES(?, ?)",
                    (f"scenario-run-meta:{erid}", json.dumps({"kind": "eval"})),
                )
            conn.commit()
        finally:
            conn.close()

        conn = memory.connect(self.db)
        try:
            dec = scenario_hooks.stop_check(conn, memory, chat="claude:chat-r5", agent="claude")
            self.assertEqual(dec["decision"], "block")
            self.assertIn(r_prod, dec["reason"])

            snap = scenarios.map_snapshot(conn, memory)
            self.assertEqual(snap["runs_waiting"], 0)
            self.assertEqual(len(snap["waiting_queue"]), 0)

            listed = scenario_runs.list_runs(conn, memory, limit=10)
            self.assertTrue(listed["runs"])
        finally:
            conn.close()

        # 2. R5-ARCH-05: preflight parses definition_json so literal "no_dirty_git" in instruction text does NOT require --project-path
        self.cli("scenario", "report", r_prod, "work", "--json", "-", stdin_text=json.dumps({"status": "done", "summary": "done without project-path"}))
        st_work, _ = self.step_result(r_prod, "work")
        self.assertEqual(st_work, "done")

        # 3. R5-CROSS-12: pause -> resume bumps revision by exactly 1
        p_res = self.cli("scenario", "pause", r_prod)
        rev_after_pause = p_res["revision"]
        res_res = self.cli("scenario", "resume", r_prod)
        self.assertEqual(res_res["revision"], rev_after_pause + 1)

        # 4. R5-IND-08 / R5-ARCH-13: no_dirty_git claim warning + release/re-claim recovery hint
        _apply_def(
            self.cli,
            {
                "format": "richi-scenario",
                "format_version": 1,
                "id": "ndg-warn",
                "title": "NDG Warn",
                "steps": [
                    {
                        "id": "chk",
                        "type": "instruction",
                        "title": "Check",
                        "needs": [],
                        "checks": [{"kind": "no_dirty_git", "mode": "unchanged"}],
                        "params": {"text": "Read only"},
                    }
                ],
            },
            scope="project",
            project="svc",
        )
        r_ndg = self.cli("--project-path", str(self.repo), "scenario", "run", "ndg-warn", "--project", "svc")["run_id"]
        cl_warn = self.cli("scenario", "claim", r_ndg, "chk", "--agent", "claude")
        self.assertIn("no_dirty_git_warning", cl_warn["step"])
        rep_rej = self.cli(
            "--project-path", str(self.repo), "scenario", "report", r_ndg, "chk", "--json", "-",
            stdin_text=json.dumps({"status": "done", "summary": "done"}),
        )
        self.assertEqual(rep_rej["status"], "rejected")
        self.assertIn("missing baseline worktree fingerprint", json.dumps(rep_rej))
        self.assertIn("richi scenario release", json.dumps(rep_rej))

        # 5. R5-ARCH-11: file_exists rejects .GIT and symlink
        link_p = self.repo / "src" / "sym_auth.py"
        link_p.symlink_to(self.repo / "src" / "auth.py")
        self.assertIsNotNone(scenario_runs._verify_file_exists_io(".GIT/config", str(self.repo)))
        self.assertIsNotNone(scenario_runs._verify_file_exists_io("src/sym_auth.py", str(self.repo)))
        self.assertIsNone(scenario_runs._verify_file_exists_io("src/auth.py", str(self.repo)))
        with self.assertRaises((ValueError, memory.MemoryError)):
            scenario_runs._check_cwd_containment(".GIT", str(self.repo))

        # 6. R5-ARCH-09 / R5-CROSS-03: scenario_events records compact step_diff (<2 KiB)
        meta_cur = self.cli("scenario", "show", "ndg-warn", "--project", "svc")
        upd_def = json.loads(json.dumps(meta_cur["definition"]))
        upd_def["steps"].append({"id": "chk2", "type": "instruction", "title": "Two", "needs": ["chk"], "params": {"text": "Second"}})
        self.cli(
            "scenario", "apply", "--json", "-",
            stdin_text=json.dumps({
                "action": "update", "id": "ndg-warn", "scope": "project", "project": "svc",
                "expected_updated_at": meta_cur["updated_at"], "definition": upd_def,
            }),
        )
        log_out = self.cli("scenario", "log", "--entries", "10")
        upd_ev = next(ev for ev in log_out["entries"] if ev["action"] == "scenario_updated" and ev["scenario_id"] == "ndg-warn")
        self.assertIn("step_diff", upd_ev["detail"])
        self.assertEqual(upd_ev["detail"]["step_diff"].get("added"), [{"id": "chk2", "title": "Two"}])
        self.assertLess(len(json.dumps(upd_ev["detail"])), 2048)

        # 7. R5-CROSS-07: CLI flag validation
        self.assertIn(self.cli("scenario", "eval", "ndg-warn", "--case", "c1", "--score", "", ok=False)["code"], ("usage_error", "invalid_input"))
        self.assertIn(self.cli("scenario", "eval", "ndg-warn", "--case", "c1", "--compare-run", "r1", ok=False)["code"], ("usage_error", "invalid_input"))
        self.assertIn(self.cli("scenario", "eval", "ndg-warn", "--case", "c1", "--score", "r1", "--project", "svc", ok=False)["code"], ("usage_error", "invalid_input"))
        self.assertIn(self.cli("scenario", "feedback", r_prod, "--finding", "f1", "--verdict", "false_positive", "--scope-path", "src/auth.py", ok=False)["code"], ("usage_error", "invalid_input"))
        self.assertIn(self.cli("scenario", "feedback", r_prod, "--batch", "-", "--finding", "f1", stdin_text='{"items":[]}', ok=False)["code"], ("usage_error", "invalid_input"))
        self.assertIn(self.cli("scenario", "feedback", r_prod, "--batch", "-", "--remember-pattern", stdin_text='{"items":[]}', ok=False)["code"], ("usage_error", "invalid_input"))


if __name__ == "__main__":
    unittest.main()
