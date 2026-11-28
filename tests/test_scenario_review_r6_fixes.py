"""Comprehensive regression tests for all 16 Review R6 unanimous consensus clusters."""
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
    scenario_review,
    scenario_runs,
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


class ReviewR6RegressionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="richi-r6-")
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
        self.head_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, text=True
        ).strip()

        self.unreg = self.root / "unregistered_dir"
        self.unreg.mkdir()

        self.cli("init")
        self.cli("project", "add", str(self.repo), "--id", "svc", "--name", "Svc")

    def cli(self, *args, chat="claude:chat-r6", stdin_text=None, ok=True):
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

    def put_entry(self, entry_id, *, kind="fact", title="T", summary="S", project_ids=None, tags=None, decision_card=None):
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
        if decision_card is not None:
            doc["decision_card"] = decision_card
        return self.cli("entry", "put", "--json", "-", stdin_text=json.dumps(doc))

    def step_result(self, run_id, step_key):
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT status, result_json, reason FROM scenario_steps WHERE run_id = ? AND step_key = ?",
                (run_id, step_key),
            ).fetchone()
            self.assertIsNotNone(row, f"Missing step {step_key} in {run_id}")
            return row[0], (json.loads(row[1]) if row[1] else {}), row[2]
        finally:
            conn.close()

    # ---- C1 (R6-IND-01 + R6-CROSS-03): as_of_commit temporal isolation in recall & task_brief

    def test_as_of_commit_blocks_post_commit_annotations_and_graph_edges(self):
        # 1. Pre-commit entries: pre-seed and pre-neighbor (unrelated lexically to query)
        self.put_entry("fact:pre-seed", title="Quantum auth anchor", summary="Early auth anchor", project_ids=["svc"])
        self.put_entry("fact:pre-neighbor", title="Unrelated legacy helper", summary="Helper details", project_ids=["svc"])
        # 2. Post-commit decision_card entry (task) matching query "quantumflux" linked to pre-neighbor
        self.put_entry(
            "task:post-annotated",
            kind="task",
            title="Later cache decision",
            summary="Later decision",
            project_ids=["svc"],
            decision_card={
                "question": "Use quantumflux pool?",
                "observations": [{"text": "quantumflux pool reduces tail latency", "reference": "src/auth.py"}],
                "decision": {"status": "accepted", "text": "Adopt quantumflux pool", "reference": "src/auth.py", "by": "alice"},
            },
        )
        # Edge from post-annotated -> pre-neighbor (pre-commit date doesn't matter because seed is post-commit)
        self.cli("edge", "put", "--json", "-", stdin_text=json.dumps({
            "id": "e-post-seed",
            "from_ref": "entry:task:post-annotated",
            "to_ref": "entry:fact:pre-neighbor",
            "kind": "depends_on",
            "description": "post link",
            "knowledge_state": "confirmed",
            "sources": [{"reference": "src/auth.py"}],
        }))
        # Post-commit edge linking two pre-commit entries (pre-seed -> pre-neighbor)
        self.cli("edge", "put", "--json", "-", stdin_text=json.dumps({
            "id": "e-post-link",
            "from_ref": "entry:fact:pre-seed",
            "to_ref": "entry:fact:pre-neighbor",
            "kind": "related_to",
            "description": "discovered later",
            "knowledge_state": "confirmed",
            "sources": [{"reference": "src/auth.py"}],
        }))
        # Pre-commit AND post-commit measurement-annotated edges targeting project:svc (Topic 4 guardrail!)
        self.cli("edge", "put", "--json", "-", stdin_text=json.dumps({
            "id": "e-pre-meas",
            "from_ref": "entry:fact:pre-seed",
            "to_ref": "project:svc",
            "kind": "related_to",
            "description": "pre measurement edge",
            "knowledge_state": "confirmed",
            "sources": [{
                "reference": "src/auth.py",
                "measurements": [{"metric": "Bandwidth", "value": "12", "unit": "Mbps", "sample": "baseline"}],
            }],
        }))
        self.cli("edge", "put", "--json", "-", stdin_text=json.dumps({
            "id": "e-post-meas",
            "from_ref": "entry:fact:pre-seed",
            "to_ref": "project:svc",
            "kind": "related_to",
            "description": "post measurement edge",
            "knowledge_state": "confirmed",
            "sources": [{
                "reference": "src/auth.py",
                "measurements": [{"metric": "Bandwidth", "value": "99", "unit": "Mbps", "sample": "later"}],
            }],
        }))

        conn = sqlite3.connect(self.db)
        try:
            for eid in ("fact:pre-seed", "fact:pre-neighbor"):
                conn.execute("UPDATE entries SET created_at='2025-01-01T00:00:00Z', updated_at='2025-01-01T00:00:00Z' WHERE id=?", (eid,))
            conn.execute("UPDATE entries SET created_at='2026-06-01T00:00:00Z', updated_at='2026-06-01T00:00:00Z' WHERE id='task:post-annotated'")
            conn.execute("UPDATE graph_edges SET created_at='2025-01-01T00:00:00Z', updated_at='2025-01-01T00:00:00Z' WHERE id IN ('e-post-seed', 'e-pre-meas')")
            conn.execute("UPDATE graph_edges SET created_at='2026-06-01T00:00:00Z', updated_at='2026-06-01T00:00:00Z' WHERE id IN ('e-post-link', 'e-post-meas')")
            # Keep measurement & decision-card metadata updated_at in sync with their owner records
            for key_prefix, ts in (
                ("source-measurements:v1:edge:e-pre-meas", "2025-01-01T00:00:00Z"),
                ("source-measurements-info:v1:edge:e-pre-meas", "2025-01-01T00:00:00Z"),
                ("source-measurements:v1:edge:e-post-meas", "2026-06-01T00:00:00Z"),
                ("source-measurements-info:v1:edge:e-post-meas", "2026-06-01T00:00:00Z"),
                ("decision-card:v1:task:post-annotated", "2026-06-01T00:00:00Z"),
            ):
                mrow = conn.execute("SELECT value FROM metadata WHERE key = ?", (key_prefix,)).fetchone()
                if mrow and mrow[0]:
                    mobj = json.loads(mrow[0])
                    if "updated_at" in mobj:
                        mobj["updated_at"] = ts
                        conn.execute("UPDATE metadata SET value = ? WHERE key = ?", (json.dumps(mobj, separators=(",", ":")), key_prefix))
            conn.commit()
        finally:
            conn.close()

        defn = {
            "format": "richi-scenario",
            "format_version": 1,
            "id": "as-of-graph-iso",
            "title": "AsOf Graph Iso",
            "steps": [
                {"id": "b_annot", "type": "memory.brief", "title": "Brief Annot", "needs": [], "params": {"question": "quantumflux pool", "limit": 10}},
                {"id": "b_meas", "type": "memory.brief", "title": "Brief Meas", "needs": [], "params": {"question": "Bandwidth", "limit": 10}},
                {"id": "r_edge", "type": "memory.recall", "title": "Recall Edge", "needs": [], "params": {"query": "Quantum auth anchor", "limit": 10}},
            ],
        }
        _apply_def(self.cli, defn, scope="project", project="svc")
        case_doc = {
            "id": "case-graph-asof",
            "title": "Graph AsOf",
            "project_id": "svc",
            "base_rev": self.head_sha,
            "head_rev": self.head_sha,
            "commit_time": "2025-06-01T00:00:00Z",
            "expected_bugs": [{"id": "b1", "file": "src/auth.py", "line_range": [1, 4]}],
        }
        self.cli("--project-path", str(self.repo), "scenario", "golden", "add", "--json", "-", stdin_text=json.dumps(case_doc))
        ev = self.cli(
            "--project-path", str(self.repo),
            "scenario", "eval", "as-of-graph-iso", "--case", "case-graph-asof",
            "--project", "svc", "--memory-mode", "as_of_commit",
        )
        rid = ev["run_id"]

        # Post-commit decision_card must NOT enter direct_by_ref or pull pre-neighbor via graph!
        _st, b_annot, _ = self.step_result(rid, "b_annot")
        self.assertEqual(b_annot["data"]["refs"], [])

        # Measurement-annotated edge with project:svc endpoint: pre-commit kept, post-commit excluded, no crash!
        _st, b_meas, _ = self.step_result(rid, "b_meas")
        self.assertIn("edge:e-pre-meas", b_meas["data"]["refs"])
        self.assertNotIn("edge:e-post-meas", b_meas["data"]["refs"])

        # Post-commit edge e-post-link must NOT pull pre-neighbor into memory.recall!
        _st, r_edge, _ = self.step_result(rid, "r_edge")
        self.assertIn("entry:fact:pre-seed", r_edge["data"]["refs"])
        self.assertNotIn("entry:fact:pre-neighbor", r_edge["data"]["refs"])

    # ---- C2 (R6-IND-02 + R6-ARCH-02 + R6-CROSS-02): trust surfaces & unconditional --trust-sha256 pin

    def test_import_security_summary_trust_surfaces_and_strict_digest_pin(self):
        # 1. Scenario with only when_to_use + memory.brief + gate + checklist MUST require trust
        prompt_only = {
            "format": "richi-scenario",
            "format_version": 1,
            "id": "prompt-gate-imp",
            "title": "Prompt Gate",
            "when_to_use": "W" * 240,
            "steps": [
                {"id": "br", "type": "memory.brief", "title": "Brief", "needs": [], "params": {"question": "Secrets?"}},
                {"id": "gt", "type": "gate", "title": "Gate", "needs": ["br"], "params": {"question": "Approve {{steps.br.summary}}?", "options": ["ok", "no"]}},
                {"id": "cl", "type": "checklist", "title": "Check", "needs": ["gt"], "params": {"items": [{"text": "Verify item", "required": True}]}},
                {"id": "cm", "type": "compare", "title": "Cmp", "needs": ["cl"], "params": {"step": "br"}},
            ],
        }
        p_path = self.root / "prompt_gate.json"
        p_path.write_text(json.dumps(prompt_only), encoding="utf-8")

        res1 = self.cli("scenario", "import", str(p_path), "--project", "svc")
        self.assertEqual(res1["status"], "trust_required")
        sec = res1["security_summary"]
        self.assertTrue(sec["requires_trust"])
        self.assertIsInstance(sec["when_to_use"], str)
        self.assertEqual(sec["when_to_use_chars"], 240)
        self.assertTrue(sec["when_to_use_truncated"])
        self.assertEqual(len(sec["memory_reads"]), 1)
        self.assertEqual(len(sec["human_prompts"]), 2)
        self.assertEqual(len(sec["other_steps"]), 1)
        sha = res1["sha256"]

        # Empty or mismatched --trust-sha256 raises digest_mismatch
        self.assertEqual(self.cli("scenario", "import", str(p_path), "--project", "svc", "--trust-sha256", "", ok=False)["code"], "digest_mismatch")
        self.assertEqual(self.cli("scenario", "import", str(p_path), "--project", "svc", "--trust-sha256", "0" * 64, ok=False)["code"], "digest_mismatch")

        # Valid trust imports cleanly
        res2 = self.cli("scenario", "import", str(p_path), "--project", "svc", "--trust-sha256", sha)
        self.assertEqual(res2["status"], "created")

        # Already trusted: passing WRONG --trust-sha256 MUST still raise digest_mismatch (R6-CROSS-02)
        self.assertEqual(
            self.cli("scenario", "import", str(p_path), "--project", "svc", "--replace", "--trust-sha256", "f" * 64, ok=False)["code"],
            "digest_mismatch",
        )

        # Pure condition-only scenario (requires_trust == False): passing wrong --trust-sha256 MUST still raise digest_mismatch
        cond_only = {
            "format": "richi-scenario",
            "format_version": 1,
            "id": "cond-only-imp",
            "title": "Cond Only",
            "steps": [{"id": "c1", "type": "condition", "title": "C", "needs": [], "params": {"expr": "true"}}],
        }
        c_path = self.root / "cond_only.json"
        c_path.write_text(json.dumps(cond_only), encoding="utf-8")
        self.assertEqual(
            self.cli("scenario", "import", str(c_path), "--project", "svc", "--trust-sha256", "0" * 64, ok=False)["code"],
            "digest_mismatch",
        )

    # ---- C3 (R6-ARCH-03): compare baseline prod isolation, legacy baseline dedup, as_of_commit julianday streaming

    def test_compare_baseline_prod_isolation_legacy_dedup_and_as_of_streaming(self):
        defn = {
            "format": "richi-scenario",
            "format_version": 1,
            "id": "cmp-r6",
            "title": "Cmp R6",
            "inputs": {"base_override": {"type": "string", "default": "auto-none"}},
            "steps": [
                {"id": "m", "type": "instruction", "title": "M", "needs": [], "params": {"text": "Measure"}},
                {"id": "c", "type": "compare", "title": "C", "needs": ["m"], "params": {"step": "m", "metrics": ["score"], "baseline_run": "{{inputs.base_override}}"}},
            ],
        }
        _apply_def(self.cli, defn, scope="project", project="svc")

        # Prod run 1 & prod run 2
        r_old = self.cli("--project-path", str(self.repo), "scenario", "run", "cmp-r6", "--project", "svc")["run_id"]
        self.cli("scenario", "claim", r_old, "m", "--agent", "claude")
        self.cli("scenario", "report", r_old, "m", "--json", "-", stdin_text=json.dumps({"status": "done", "summary": "ok", "data": {"score": 10}}))

        r_new = self.cli("--project-path", str(self.repo), "scenario", "run", "cmp-r6", "--project", "svc")["run_id"]
        self.cli("scenario", "claim", r_new, "m", "--agent", "claude")
        self.cli("scenario", "report", r_new, "m", "--json", "-", stdin_text=json.dumps({"status": "done", "summary": "ok", "data": {"score": 20}}))

        # Fork run (done, run_kind='fork')
        r_fork = self.cli("--project-path", str(self.repo), "scenario", "fork", r_new, "--from", "m")["run_id"]
        self.cli("scenario", "claim", r_fork, "m", "--agent", "claude")
        self.cli("scenario", "report", r_fork, "m", "--json", "-", stdin_text=json.dumps({"status": "done", "summary": "ok", "data": {"score": 999}}))

        # 1. Explicit baseline_run pointing to a fork run must be ignored (falls back to r_new prod run, NOT r_fork!)
        r_chk = self.cli("--project-path", str(self.repo), "scenario", "run", "cmp-r6", "--project", "svc", "--input", f"base_override={r_fork}")["run_id"]
        self.cli("scenario", "claim", r_chk, "m", "--agent", "claude")
        self.cli("scenario", "report", r_chk, "m", "--json", "-", stdin_text=json.dumps({"status": "done", "summary": "ok", "data": {"score": 25}}))
        _, cmp_res, _ = self.step_result(r_chk, "c")
        self.assertEqual(cmp_res["data"]["baseline_run_id"], r_new)

        # 2. Coexisting legacy & v2 baseline keys: ONLY active v2 baseline gets is_baseline=True in list_runs & _all_baseline_ids
        self.cli("scenario", "baseline", r_new)
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES(?, ?)",
                (scenario_runs._legacy_baseline_key("cmp-r6", "svc"), r_old),
            )
            conn.commit()
        finally:
            conn.close()
        conn = memory.connect(self.db)
        try:
            self.assertEqual(scenario_runs._all_baseline_ids(conn), {r_new})
            listed = scenario_runs.list_runs(conn, memory, scenario_id="cmp-r6", project_id="svc")
            flagged = [r["id"] for r in listed["runs"] if r["is_baseline"]]
            self.assertEqual(flagged, [r_new])
        finally:
            conn.close()

        # 3. as_of_commit streaming: exact Z timestamp (2025-01-01T00:00:00Z) found even past 55 newer-updated runs!
        self.cli("scenario", "baseline", r_new, "--clear")
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE scenario_runs SET created_at='2025-01-01T00:00:00Z', updated_at='2025-01-01T00:00:00Z' WHERE id=?", (r_old,))
            conn.execute("UPDATE scenario_runs SET created_at='2026-06-01T00:00:00Z', updated_at='2026-06-01T00:00:00Z' WHERE id IN (?, ?)", (r_new, r_chk))
            for i in range(55):
                mid = f"run-cmp-mid-{i:03d}"
                conn.execute(
                    "INSERT INTO scenario_runs(id, scenario_id, scenario_version, scope, project_id, scenario_sha256, definition_json, inputs_json, status, revision, created_at, updated_at) "
                    "SELECT ?, scenario_id, scenario_version, scope, project_id, scenario_sha256, definition_json, inputs_json, 'done', 1, '2025-01-01T00:00:00Z', '2026-06-01T00:00:00Z' "
                    "FROM scenario_runs WHERE id = ?",
                    (mid, r_old),
                )
            conn.commit()
        finally:
            conn.close()

        case_doc = {
            "id": "case-cmp-stream",
            "title": "Cmp Stream",
            "project_id": "svc",
            "base_rev": self.head_sha,
            "head_rev": self.head_sha,
            "commit_time": "2025-01-01T00:00:00Z",
            "expected_bugs": [{"id": "b1", "file": "src/auth.py", "line_range": [1, 4]}],
        }
        self.cli("--project-path", str(self.repo), "scenario", "golden", "add", "--json", "-", stdin_text=json.dumps(case_doc))
        ev = self.cli("--project-path", str(self.repo), "scenario", "eval", "cmp-r6", "--case", "case-cmp-stream", "--project", "svc", "--memory-mode", "as_of_commit")
        self.cli("scenario", "claim", ev["run_id"], "m", "--agent", "claude")
        self.cli("scenario", "report", ev["run_id"], "m", "--json", "-", stdin_text=json.dumps({"status": "done", "summary": "ok", "data": {"score": 15}}))
        _, ev_cmp, _ = self.step_result(ev["run_id"], "c")
        self.assertTrue(ev_cmp["data"]["has_baseline"])
        self.assertEqual(ev_cmp["data"]["baseline_run_id"], r_old)

    # ---- C4 (R6-IND-03 + R6-CROSS-04): skip_step clears result_json, guards downstream, compare ignores failed scalar

    def test_skip_step_clears_result_guards_downstream_and_compare_ignores_failed_scalar(self):
        defn = {
            "format": "richi-scenario",
            "format_version": 1,
            "id": "skip-cmp-r6",
            "title": "Skip Cmp",
            "steps": [
                {"id": "s1", "type": "instruction", "title": "S1", "needs": [], "continue_on_failure": True, "params": {"text": "Work"}},
                {"id": "s2", "type": "compare", "title": "S2", "needs": ["s1"], "params": {"step": "s1", "metrics": ["score"]}},
                {"id": "s3", "type": "instruction", "title": "S3", "needs": ["s1"], "params": {"text": "Saw {{steps.s1.summary}}"}},
            ],
        }
        _apply_def(self.cli, defn, scope="project", project="svc")

        # Prod baseline with score=10
        r_base = self.cli("--project-path", str(self.repo), "scenario", "run", "skip-cmp-r6", "--project", "svc")["run_id"]
        self.cli("scenario", "claim", r_base, "s1", "--agent", "claude")
        self.cli("scenario", "report", r_base, "s1", "--json", "-", stdin_text=json.dumps({"status": "done", "summary": "ok", "data": {"score": 10}}))
        self.cli("scenario", "claim", r_base, "s3", "--agent", "claude")
        self.cli("scenario", "report", r_base, "s3", "--json", "-", stdin_text=json.dumps({"status": "done", "summary": "ok"}))

        # Run 2: s1 fails with score=999; compare s2 must NOT ingest 999, while s3 still gets {{steps.s1.summary}}
        r2 = self.cli("--project-path", str(self.repo), "scenario", "run", "skip-cmp-r6", "--project", "svc")["run_id"]
        self.cli("scenario", "claim", r2, "s1", "--agent", "claude")
        self.cli("scenario", "report", r2, "s1", "--json", "-", stdin_text=json.dumps({"status": "failed", "summary": "OLD FAILURE", "data": {"score": 999}}))
        _, s2_res, _ = self.step_result(r2, "s2")
        self.assertIsNone(s2_res["data"]["diffs"]["score"]["current"])

        # Attempting to skip s1 AFTER s2 is already done must fail with invalid_state!
        err_skip = self.cli("scenario", "skip", r2, "s1", "--reason", "too late", ok=False)
        self.assertEqual(err_skip["code"], "invalid_state")

        # Separate run without continue_on_failure: fail s1, then skip s1 -> result_json must be cleared!
        defn2 = {
            "format": "richi-scenario",
            "format_version": 1,
            "id": "skip-clear-r6",
            "title": "Skip Clear",
            "steps": [{"id": "s1", "type": "instruction", "title": "S1", "needs": [], "params": {"text": "Work"}}],
        }
        _apply_def(self.cli, defn2, scope="project", project="svc")
        r3 = self.cli("--project-path", str(self.repo), "scenario", "run", "skip-clear-r6", "--project", "svc")["run_id"]
        self.cli("scenario", "claim", r3, "s1", "--agent", "claude")
        self.cli("scenario", "report", r3, "s1", "--json", "-", stdin_text=json.dumps({"status": "failed", "summary": "OLD FAILURE", "data": {"score": 777}}))
        self.cli("scenario", "skip", r3, "s1", "--reason", "waived by human")
        st_s1, res_s1, reason_s1 = self.step_result(r3, "s1")
        self.assertEqual(st_s1, "skipped")
        self.assertEqual(res_s1, {})
        self.assertEqual(reason_s1, "waived by human")
        conn = memory.connect(self.db)
        try:
            self.assertEqual(scenario_runs._step_data_for_compare(conn, r3, "s1"), {})
        finally:
            conn.close()

    # ---- R6-CROSS-01: scenario hook unregistered directory fallback

    def test_scenario_hook_unregistered_dir_fallback(self):
        _apply_def(
            self.cli,
            {
                "format": "richi-scenario",
                "format_version": 1,
                "id": "hook-ws-r6",
                "title": "Hook WS",
                "steps": [{"id": "work", "type": "instruction", "title": "W", "needs": [], "params": {"text": "Work"}}],
            },
            scope="project",
            project="svc",
        )
        rid = self.cli("--project-path", str(self.repo), "scenario", "run", "hook-ws-r6", "--project", "svc")["run_id"]
        self.cli("scenario", "claim", rid, "work", "--agent", "claude")

        # All 3 hook events must succeed in an unregistered directory (never crash with project_unregistered)
        ss = self.cli("--project-path", str(self.unreg), "scenario", "hook", "session-start", "--agent", "claude")
        self.assertIn(rid, json.dumps(ss))

        pc = self.cli("--project-path", str(self.unreg), "scenario", "hook", "pre-compact", "--agent", "claude")
        self.assertIn(rid, json.dumps(pc))

        st = self.cli("--project-path", str(self.unreg), "scenario", "hook", "stop", "--agent", "claude")
        self.assertEqual(st["decision"], "block")
        self.assertNotIn(str(self.unreg), st.get("reason", ""))

        # Explicit --project mismatch STILL raises strictly!
        err_strict = self.cli("--project-path", str(self.unreg), "scenario", "hook", "session-start", "--project", "svc", ok=False)
        self.assertIn(err_strict["code"], ("project_unregistered", "project_mismatch", "invalid_input", "error"))

    # ---- C6 & C9: eval_score workspace-wide run vs project case & exact lexicographic bipartite weights

    def test_eval_score_workspace_run_mismatch_and_large_bipartite_weights(self):
        _apply_def(
            self.cli,
            {
                "format": "richi-scenario",
                "format_version": 1,
                "id": "ws-eval-score",
                "title": "WS Eval",
                "steps": [{"id": "s1", "type": "condition", "title": "C", "needs": [], "params": {"expr": "true"}}],
            },
            scope="global",
            project=None,
        )
        ws_run = self.cli("scenario", "run", "ws-eval-score")["run_id"]
        case_doc = {
            "id": "case-proj-only",
            "title": "Proj Only",
            "project_id": "svc",
            "base_rev": self.head_sha,
            "head_rev": self.head_sha,
            "expected_bugs": [{"id": "b1", "file": "src/auth.py", "line_range": [1, 4]}],
        }
        self.cli("--project-path", str(self.repo), "scenario", "golden", "add", "--json", "-", stdin_text=json.dumps(case_doc))
        err = self.cli("scenario", "eval", "--case", "case-proj-only", "--score", ws_run, ok=False)
        self.assertEqual(err["code"], "project_mismatch")
        self.assertNotIn("'None'", err["error"])

        # C9: 50 specs where specs 0..24 match at quality 1 (within slop) and specs 25..49 match at quality 0 (exact).
        # With only 25 findings shared across both groups, exact matching MUST pick quality 0 (specs 25..49), not specs 0..24!
        specs = [
            {"id": f"b-lo-{i}", "file": "src/auth.py", "line_range": [100 + i * 20, 100 + i * 20]}
            for i in range(25)
        ] + [
            {"id": f"b-hi-{i}", "file": "src/auth.py", "line_range": [103 + i * 20, 103 + i * 20]}
            for i in range(25)
        ]
        candidates = [
            {"finding_id": f"f-{i:02d}", "file": "src/auth.py", "line": 103 + i * 20, "title": "bug"}
            for i in range(25)
        ]
        matched = scenario_eval._min_cost_max_matching(specs, candidates)
        self.assertEqual(len(matched), 25)
        self.assertEqual(set(matched.keys()), set(range(25, 50)))

    # ---- C7 (R6-IND-05 + R6-ARCH-05): start_run links initial memory.record produces edge

    def test_start_run_links_initial_memory_record_produces_edge(self):
        defn = {
            "format": "richi-scenario",
            "format_version": 1,
            "id": "init-rec-edge",
            "title": "Init Rec Edge",
            "steps": [
                {
                    "id": "rec",
                    "type": "memory.record",
                    "title": "Record",
                    "needs": [],
                    "params": {"kind": "decision", "title": "Initial output", "summary": "Recorded during start_run"},
                }
            ],
        }
        _apply_def(self.cli, defn, scope="project", project="svc")
        res = self.cli("--project-path", str(self.repo), "scenario", "run", "init-rec-edge", "--project", "svc")
        rid = res["run_id"]
        task_id = res["task_entry_id"]
        _, rec_res, _ = self.step_result(rid, "rec")
        out_entry = rec_res["data"]["entry_id"]

        conn = sqlite3.connect(self.db)
        try:
            edge = conn.execute(
                "SELECT id, from_ref, to_ref, kind FROM graph_edges WHERE from_ref = ? AND to_ref = ? AND kind = 'produces'",
                ("entry:" + task_id, "entry:" + out_entry),
            ).fetchone()
            self.assertIsNotNone(edge)
        finally:
            conn.close()

    # ---- C10 (R6-SELF-05 + R6-CROSS-06): read_request validates feedback/eval flags BEFORE reading files/stdin

    def test_read_request_rejects_invalid_batch_and_eval_flags_before_io(self):
        missing = str(self.root / "definitely_missing.json")
        # Each of these must fail with usage_error WITHOUT touching stdin or raising file-not-found!
        self.assertEqual(
            self.cli("scenario", "feedback", "run-any", "--batch", missing, "--finding", "f1", ok=False)["code"],
            "usage_error",
        )
        self.assertEqual(
            self.cli("scenario", "feedback", "run-any", "--batch", "", ok=False)["code"],
            "usage_error",
        )
        self.assertEqual(
            self.cli("scenario", "eval", "--case", "c1", "--score", "run-1", "--inputs-json", missing, ok=False)["code"],
            "usage_error",
        )
        self.assertEqual(
            self.cli("scenario", "eval", "--case", "c1", "--compare-run", "run-1", "--inputs-json", missing, ok=False)["code"],
            "usage_error",
        )
        self.assertEqual(
            self.cli("scenario", "eval", "--case", "c1", "--inputs-json", missing, ok=False)["code"],
            "usage_error",
        )

    # ---- C13 (R6-IND-08) & R6-CROSS-05: bidirectional FP scope matching & retry without step_key

    def test_fp_bidirectional_prefix_match_and_retry_prefers_blocking_failure(self):
        self.put_entry(
            "fact:fp-nested",
            title="Nested Auth FP",
            summary="Scope paths: ./src/auth/jwt.py\nSafety condition: Gateway validates JWT before handler",
            project_ids=["svc"],
            tags=["false-positive"],
        )
        self.put_entry(
            "fact:fp-other",
            title="Billing FP",
            summary="Scope paths: src/billing/tax.py\nSafety condition: Tax engine rounds to cents",
            project_ids=["svc"],
            tags=["false-positive"],
        )
        conn = memory.connect(self.db)
        try:
            items, _ = scenario_review.load_scoped_fp_learnings(conn, project_id="svc", changed_files=["src/auth"])
            self.assertEqual(items[0]["entry_id"], "fact:fp-nested")
        finally:
            conn.close()

        # R6-CROSS-05: s1 (continue_on_failure) fails -> s2 fails -> retry without step_key retries s2!
        defn = {
            "format": "richi-scenario",
            "format_version": 1,
            "id": "retry-pick-r6",
            "title": "Retry Pick",
            "steps": [
                {"id": "lint", "type": "instruction", "title": "Lint", "needs": [], "continue_on_failure": True, "params": {"text": "Lint"}},
                {"id": "tests", "type": "instruction", "title": "Tests", "needs": ["lint"], "params": {"text": "Tests"}},
            ],
        }
        _apply_def(self.cli, defn, scope="project", project="svc")
        rid = self.cli("--project-path", str(self.repo), "scenario", "run", "retry-pick-r6", "--project", "svc")["run_id"]
        self.cli("scenario", "claim", rid, "lint", "--agent", "claude")
        self.cli("scenario", "report", rid, "lint", "--json", "-", stdin_text=json.dumps({"status": "failed", "summary": "lint warn"}))
        self.cli("scenario", "claim", rid, "tests", "--agent", "claude")
        self.cli("scenario", "report", rid, "tests", "--json", "-", stdin_text=json.dumps({"status": "failed", "summary": "tests failed"}))

        retried = self.cli("scenario", "retry", rid)
        self.assertEqual(retried["status"], "retried")
        self.assertEqual(retried["step_key"], "tests")


if __name__ == "__main__":
    unittest.main()
