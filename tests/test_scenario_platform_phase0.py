"""Phase 0 tests: platform registry, table-existence guards, artifact URI contract, and SQL-JOIN stats."""
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

from richi import memory, scenario_registry, scenario_runs, scenarios


def _clean_env(root):
    return {
        "HOME": str(root / "home"),
        "PATH": os.environ.get("PATH", ""),
        "XDG_CONFIG_HOME": str(root / "xdg"),
    }


class ScenarioPlatformPhase0Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="richi-phase0-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        (self.root / "home").mkdir()
        self.db = self.root / "store.sqlite3"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.test"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=self.repo, check=True)
        (self.repo / "app.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self.repo, check=True)
        self.cli("init")
        self.cli("project", "add", str(self.repo), "--id", "svc", "--name", "Svc")

    def cli(self, *args, chat="claude:chat-p0", stdin_text=None, ok=True):
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

    def test_scenario_catalog_and_artifact_capability_detection(self):
        cat = self.cli("scenario", "catalog")
        self.assertEqual(cat["view"], "scenario-catalog")
        self.assertTrue(cat["capabilities"]["scenarios"])
        self.assertTrue(cat["capabilities"]["review_hub"])
        self.assertFalse(cat["capabilities"]["artifacts"])
        self.assertIn("richi://artifact/", cat["artifact_uri_contract"])

        by_type = {b["type"]: b for b in cat["step_blocks"]}
        self.assertTrue(by_type["instruction"]["available"])
        self.assertFalse(by_type["artifact.fetch"]["available"])
        self.assertEqual(by_type["artifact.fetch"]["status"], "activates_with_artifacts")

        check_kinds = {c["kind"] for c in cat["checks"]}
        self.assertIn("no_dirty_git", check_kinds)
        self.assertIn("findings_grounded", check_kinds)
        self.assertTrue(any(v["id"] == "review" for v in cat["domain_views"]))

        # Simulate Artifact Storage tables appearing (v4/v5 merge) -> capability flips True automatically
        conn = sqlite3.connect(str(self.db))
        try:
            for tbl in scenario_registry.ARTIFACT_TABLES:
                conn.execute(f"CREATE TABLE {tbl} (id TEXT PRIMARY KEY)")
            conn.commit()
            self.assertTrue(scenario_registry.has_artifact_tables(conn))
            self.assertTrue(scenario_registry.detect_capabilities(conn)["artifacts"])
        finally:
            conn.close()

    def test_v4_database_without_scenario_tables_degrades_safely(self):
        # Simulate a parallel branch store where user_version=4 belongs to Artifact Storage, not Scenarios.
        conn = sqlite3.connect(str(self.db))
        try:
            for tbl in ("scenario_events", "scenario_steps", "scenario_runs"):
                conn.execute(f"DROP TABLE IF EXISTS {tbl}")
            conn.execute("PRAGMA user_version = 4")
            conn.commit()
            self.assertFalse(scenario_registry.has_scenario_tables(conn))
            snap = scenarios.map_snapshot(conn, None, run_limit=10)
            self.assertFalse(snap["platform"]["capabilities"]["scenarios"])
            self.assertEqual(snap["runs"], [])
            stats = scenario_runs.scenario_stats(conn, None)
            self.assertEqual(stats["total_runs"], 0)
            self.assertEqual(scenario_runs.waiting_gates_queue(conn), [])
        finally:
            conn.close()

    def test_report_artifacts_contract_and_stats_isolation(self):
        defn = {
            "format": "richi-scenario", "format_version": 1, "id": "p0-demo", "title": "P0 Demo",
            "steps": [
                {"id": "work", "type": "instruction", "title": "Do work", "params": {"text": "Work"}},
                {"id": "approve", "type": "gate", "title": "Approve", "needs": ["work"],
                 "params": {"question": "Ok?", "options": ["yes", "no"]}},
            ],
        }
        self.cli("scenario", "apply", "--json", "-",
                 stdin_text=json.dumps({"action": "add", "scope": "project", "project": "svc", "definition": defn}))

        # Run 1: done with valid artifact URI; invalid URI is rejected first
        r1 = self.cli("--project-path", str(self.repo), "scenario", "run", "p0-demo")["run_id"]
        self.cli("--project-path", str(self.repo), "scenario", "next", r1, "--claim")

        bad = self.cli("--project-path", str(self.repo), "scenario", "report", r1, "work", "--json", "-",
                       stdin_text=json.dumps({
                           "status": "done", "summary": "Bad URI",
                           "artifacts": [{"uri": "http://example.com/x", "kind": "report"}],
                       }))
        self.assertEqual(bad["status"], "rejected")
        self.assertIn("richi://artifact/", json.dumps(bad["reasons"]))

        good_uri = "richi://artifact/art-review-1@v20261128/report.md"
        self.cli("--project-path", str(self.repo), "scenario", "report", r1, "work", "--json", "-",
                 stdin_text=json.dumps({
                     "status": "done", "summary": "Done with artifact",
                     "artifacts": [{"uri": good_uri, "kind": "report", "title": "Review report"}],
                 }))
        self.cli("--project-path", str(self.repo), "scenario", "answer", r1, "approve", "yes")
        st1 = self.cli("--project-path", str(self.repo), "scenario", "status", r1)
        self.assertEqual(st1["run_kind"], "prod")
        work_step = next(s for s in st1["steps"] if s["id"] == "work")
        self.assertEqual(work_step["artifacts"][0]["uri"], good_uri)

        # Run 2: cancelled -> lowers completion_rate and raises abort_rate
        r2 = self.cli("--project-path", str(self.repo), "scenario", "run", "p0-demo")["run_id"]
        self.cli("--project-path", str(self.repo), "scenario", "cancel", r2)

        # Run 3: mark as eval via run_meta -> excluded from default prod stats and rejected by baseline
        r3 = self.cli("--project-path", str(self.repo), "scenario", "run", "p0-demo")["run_id"]
        self.cli("--project-path", str(self.repo), "scenario", "next", r3, "--claim")
        self.cli("--project-path", str(self.repo), "scenario", "report", r3, "work", "--json", "-",
                 stdin_text=json.dumps({"status": "done", "summary": "Eval work"}))
        self.cli("--project-path", str(self.repo), "scenario", "answer", r3, "approve", "yes")
        conn = sqlite3.connect(str(self.db))
        try:
            scenario_runs.set_run_meta(conn, r3, {"kind": "eval", "case_id": "bug-1"})
            conn.commit()
        finally:
            conn.close()

        # Baseline rejects non-prod run
        self.cli("--project-path", str(self.repo), "scenario", "baseline", r3, ok=False)

        # Default prod stats see only r1 (done) and r2 (cancelled) -> completion_rate=0.5, abort_rate=0.5
        st_prod = self.cli("--project-path", str(self.repo), "scenario", "stats", "--scenario", "p0-demo")
        self.assertEqual(st_prod["total_runs"], 2)
        self.assertEqual(st_prod["completion_rate"], 0.5)
        self.assertEqual(st_prod["abort_rate"], 0.5)
        self.assertIsNotNone(st_prod["agent_active_p50_sec"])
        self.assertIsNotNone(st_prod["human_wait_p50_sec"])

        # --kind eval sees only r3
        st_eval = self.cli("--project-path", str(self.repo), "scenario", "stats", "--scenario", "p0-demo", "--kind", "eval")
        self.assertEqual(st_eval["total_runs"], 1)
        self.assertEqual(st_eval["completion_rate"], 1.0)

        # --kind all --last 2 slices to 2 latest runs
        st_all = self.cli("--project-path", str(self.repo), "scenario", "stats", "--scenario", "p0-demo", "--kind", "all", "--last", "2")
        self.assertEqual(st_all["total_runs"], 2)


if __name__ == "__main__":
    unittest.main()
