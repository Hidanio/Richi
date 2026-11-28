"""Tests for M3 process memory: memory.record, compare with baseline, past runs in memory.brief, and --task linking."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from richi import memory


def _clean_env(root):
    return {
        "HOME": str(root / "home"),
        "PATH": os.environ.get("PATH", ""),
        "XDG_CONFIG_HOME": str(root / "xdg"),
    }


class ScenarioProcessMemoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="richi-m3-")
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

    def cli(self, *args, chat="claude:chat-m3", stdin_text=None, ok=True):
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

    def test_memory_record_compare_baseline_past_runs_and_task_link(self):
        # Create a target task entry to link runs to.
        target_task = {
            "id": "task:perf-opt", "kind": "task", "title": "Optimize checkout latency",
            "summary": "Keep p95 under 150ms", "project_ids": ["svc"],
            "knowledge_state": "confirmed", "sources": [{"reference": "docs/perf.md"}],
            "expected_updated_at": None,
        }
        self.cli("entry", "put", "--json", "-", stdin_text=json.dumps(target_task))

        defn = {
            "format": "richi-scenario", "format_version": 1, "id": "perf-check", "title": "Perf Check",
            "steps": [
                {"id": "ctx", "type": "memory.brief", "title": "Prior context",
                 "params": {"question": "checkout latency"}},
                {"id": "bench", "type": "instruction", "title": "Run benchmark", "needs": ["ctx"],
                 "params": {"text": "Measure latency given {{steps.ctx.summary}}"},
                 "outputs": {"data": {"p95_ms": {"type": "number", "required": True},
                                      "findings": {"type": "list"}}}},
                {"id": "cmp", "type": "compare", "title": "Compare with baseline", "needs": ["bench"],
                 "params": {"step": "bench", "metrics": ["p95_ms", "findings"]}},
                {"id": "rec", "type": "memory.record", "title": "Record decision", "needs": ["cmp"],
                 "params": {
                     "kind": "decision",
                     "title": "Perf check result for {{run.id}}",
                     "summary": "Bench: {{steps.bench.summary}}. Delta: {{steps.cmp.summary}}",
                     "tags": ["perf"],
                     "knowledge_state": "confirmed",
                 }},
            ],
        }
        self.cli("scenario", "apply", "--json", "-",
                 stdin_text=json.dumps({"action": "add", "scope": "project", "project": "svc", "definition": defn}))

        # Run 1: no baseline yet; establishes reference and records a confirmed decision linked to task:perf-opt.
        r1 = self.cli("--project-path", str(self.repo), "scenario", "run", "perf-check", "--task", "task:perf-opt")
        r1_id = r1["run_id"]
        self.cli("--project-path", str(self.repo), "scenario", "next", r1_id, "--claim")
        rep1 = self.cli("--project-path", str(self.repo), "scenario", "report", r1_id, "bench", "--json", "-",
                        stdin_text=json.dumps({"status": "done", "summary": "p95=100ms",
                                               "data": {"p95_ms": 100, "findings": ["slow-query-1"]}}))
        self.assertEqual(rep1["run_status"], "done")
        st1 = self.cli("--project-path", str(self.repo), "scenario", "status", r1_id)
        cmp1 = next(s for s in st1["steps"] if s["id"] == "cmp")
        self.assertIn("No prior baseline", cmp1["summary"])
        rec1 = next(s for s in st1["steps"] if s["id"] == "rec")
        self.assertIn("Recorded decision:scenario-", rec1["summary"])

        # Pin Run 1 as explicit baseline
        pin = self.cli("--project-path", str(self.repo), "scenario", "baseline", r1_id)
        self.assertTrue(pin["is_baseline"])
        st1_pin = self.cli("--project-path", str(self.repo), "scenario", "status", r1_id)
        self.assertTrue(st1_pin["is_baseline"])

        # Run 2: memory.brief sees Run 1 in past runs, compare computes +45% regression against Run 1!
        r2 = self.cli("--project-path", str(self.repo), "scenario", "run", "perf-check")
        r2_id = r2["run_id"]
        st2_init = self.cli("--project-path", str(self.repo), "scenario", "status", r2_id)
        ctx2 = next(s for s in st2_init["steps"] if s["id"] == "ctx")
        self.assertIn(r1_id, ctx2["summary"])

        self.cli("--project-path", str(self.repo), "scenario", "next", r2_id, "--claim")
        rep2 = self.cli("--project-path", str(self.repo), "scenario", "report", r2_id, "bench", "--json", "-",
                        stdin_text=json.dumps({"status": "done", "summary": "p95=145ms",
                                               "data": {"p95_ms": 145, "findings": ["slow-query-1", "n+1-cart"]}}))
        self.assertEqual(rep2["run_status"], "done")
        st2 = self.cli("--project-path", str(self.repo), "scenario", "status", r2_id)
        cmp2 = next(s for s in st2["steps"] if s["id"] == "cmp")
        self.assertIn(r1_id, cmp2["summary"])
        self.assertIn("+45.0%", cmp2["summary"])
        self.assertIn("1 -> 2 items [+1]", cmp2["summary"])

        # Verify recorded decision is searchable in recall!
        recalled = self.cli("recall", "Perf check result", "--project", "svc")
        self.assertTrue(any(r["id"].startswith("decision:scenario-") for r in recalled["results"]))

        # Verify scenario stats CLI reports accurate completion and first-pass metrics
        stats = self.cli("--project-path", str(self.repo), "scenario", "stats", "--scenario", "perf-check")
        self.assertEqual(stats["total_runs"], 2)
        self.assertEqual(stats["completion_rate"], 1.0)
        self.assertEqual(stats["first_pass_rate"], 1.0)
        self.assertEqual(stats["per_scenario"][0]["done"], 2)

        # Clear baseline
        cleared = self.cli("--project-path", str(self.repo), "scenario", "baseline", r1_id, "--clear")
        self.assertFalse(cleared["is_baseline"])


if __name__ == "__main__":
    unittest.main()
