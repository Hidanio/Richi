"""Tests for M2: agent lifecycle hooks (session-start, stop, pre-compact), server-side stop counter, and rules.scenario."""
from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from richi import memory, rules, scenario_hooks, scenario_runs, scenarios


def _clean_env(root):
    return {
        "HOME": str(root / "home"),
        "PATH": os.environ.get("PATH", ""),
        "XDG_CONFIG_HOME": str(root / "xdg"),
    }


class ScenarioHooksAndRulesLinkTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="richi-hooks-")
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

    def cli(self, *args, chat="antigravity:chat-1", stdin_text=None, ok=True):
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

    def test_stop_enforcement_counter_limit_pause_and_reset_on_report(self):
        defn = {
            "format": "richi-scenario", "format_version": 1, "id": "hook-flow", "title": "Hook Flow",
            "steps": [
                {"id": "s1", "type": "instruction", "title": "Analyze", "params": {"text": "Do s1"}},
                {"id": "g1", "type": "gate", "title": "Confirm", "needs": ["s1"],
                 "params": {"question": "Proceed?", "options": ["yes", "no"]}},
                {"id": "s2", "type": "instruction", "title": "Codex step", "needs": ["g1"],
                 "executor": {"agent": "codex"}, "params": {"text": "Do s2"}},
            ],
        }
        self.cli("scenario", "apply", "--json", "-",
                 stdin_text=json.dumps({"action": "add", "scope": "project", "project": "svc", "definition": defn}))
        started = self.cli("--project-path", str(self.repo), "scenario", "run", "hook-flow")
        rid = started["run_id"]

        # Before claiming: stop is allowed
        st0 = self.cli("--project-path", str(self.repo), "scenario", "hook", "stop", "--max-continuations", "2")
        self.assertEqual(st0["decision"], "allow")

        # Claim s1 -> Stop enforces up to max-continuations=2, then allows with limit_reached
        self.cli("--project-path", str(self.repo), "scenario", "next", rid, "--claim")
        b1 = self.cli("--project-path", str(self.repo), "scenario", "hook", "stop", "--max-continuations", "2")
        self.assertEqual(b1["decision"], "block")
        self.assertEqual(b1["continuations"], 1)
        self.assertIn("s1", b1["reason"])

        # Native Cursor format returns followup_message on block
        b2 = self.cli("--project-path", str(self.repo), "scenario", "hook", "stop",
                      "--max-continuations", "2", "--format", "cursor")
        self.assertIn("followup_message", b2)
        self.assertIn("2/2", b2["followup_message"])

        # 3rd Stop hits server-side cap (protects Antigravity when continuationActive stays false)
        cap = self.cli("--project-path", str(self.repo), "scenario", "hook", "stop", "--max-continuations", "2")
        self.assertEqual(cap["decision"], "allow")
        self.assertTrue(cap["limit_reached"])

        # Native Claude format returns {} on allow
        cap_claude = self.cli("--project-path", str(self.repo), "scenario", "hook", "stop",
                              "--max-continuations", "2", "--format", "claude")
        self.assertEqual(cap_claude, {})

        # PreCompact returns compact handoff summary with run_id and claimed step
        pc = self.cli("--project-path", str(self.repo), "scenario", "hook", "pre-compact")
        self.assertIn(rid, pc["handoff_summary"])
        self.assertIn("claimed=s1", pc["handoff_summary"])

        # SessionStart sees claimed step and rules show exposes active_scenarios
        ss = self.cli("--project-path", str(self.repo), "scenario", "hook", "session-start")
        self.assertIn(rid, ss["context_note"])
        shown = self.cli("--project-path", str(self.repo), "rules", "show")
        self.assertIn("active_scenarios", shown)
        self.assertEqual(shown["active_scenarios"]["runs"][0]["run_id"], rid)

        # Pausing releases stop enforcement immediately
        self.cli("--project-path", str(self.repo), "scenario", "pause", rid)
        paused_stop = self.cli("--project-path", str(self.repo), "scenario", "hook", "stop", "--max-continuations", "2")
        self.assertEqual(paused_stop["decision"], "allow")
        self.cli("--project-path", str(self.repo), "scenario", "resume", rid)

        # Report s1 -> moves to waiting_human gate g1 -> Stop allows with human gate reminder
        self.cli("--project-path", str(self.repo), "scenario", "report", rid, "s1", "--json", "-",
                 stdin_text=json.dumps({"status": "done", "summary": "s1 ok"}))
        gate_stop = self.cli("--project-path", str(self.repo), "scenario", "hook", "stop")
        self.assertEqual(gate_stop["decision"], "allow")
        self.assertEqual(gate_stop["waiting_human"][0]["step_key"], "g1")

        # Answer g1 -> s2 becomes ready for codex -> Codex session-start discovers cross-agent handoff!
        self.cli("--project-path", str(self.repo), "scenario", "answer", rid, "g1", "yes")
        codex_ss = self.cli("--project-path", str(self.repo), "scenario", "hook", "session-start",
                            "--agent", "codex", chat="codex:other-chat")
        self.assertEqual(len(codex_ss["agent_ready_steps"]), 1)
        self.assertEqual(codex_ss["agent_ready_steps"][0]["step_key"], "s2")
        self.assertIn(rid, codex_ss["context_note"])

    def test_rules_scenario_link_add_update_and_clear(self):
        added = self.cli("rules", "add", "pre-merge-review", "--project", "svc",
                         "--text", "Before merging into main, offer to run deep-review.",
                         "--scenario", "deep-review")
        self.assertEqual(added["rule"]["scenario"], "deep-review")
        shown = self.cli("--project-path", str(self.repo), "rules", "show")
        self.assertEqual(shown["rules"][0]["scenario"], "deep-review")

        updated = self.cli("rules", "update", "pre-merge-review",
                           "--expected-updated-at", added["rule"]["updated_at"],
                           "--scenario", "review")
        self.assertEqual(updated["rule"]["scenario"], "review")

        cleared = self.cli("rules", "update", "pre-merge-review",
                           "--expected-updated-at", updated["rule"]["updated_at"],
                           "--scenario", "")
        self.assertIsNone(cleared["rule"]["scenario"])


if __name__ == "__main__":
    unittest.main()
