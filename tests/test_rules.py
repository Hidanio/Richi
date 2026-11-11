"""Standing rules: scopes, guards, concurrency and isolation in synthetic stores."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from richi import memory, rules
from richi_launcher import agent_setup, chats, config
from richi_launcher.claude_setup import HOOK


class RulesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-rules-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        environment = mock.patch.dict(os.environ, {"HOME": str(self.root / "home")})
        environment.start()
        self.addCleanup(environment.stop)
        for key in [key for key in os.environ if key.startswith("RICHI_") or key == "CODEX_THREAD_ID"]:
            del os.environ[key]
        self.configs, self.repos = {}, {}
        for name in ("alpha", "beta"):
            store = self.root / name
            store.mkdir()
            self.configs[name] = store / "config.json"
            self.configs[name].write_text(json.dumps({"database": "memory.sqlite3"}))
            self.command(name, "init")
            for project in ("service", "tools"):
                repo = self.root / (name + "-" + project)
                (repo / "src").mkdir(parents=True)
                self.repos[name, project] = repo
                self.command(name, "project", "add", repo, "--id", project)

    def command(self, store, *argv, ok=True, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), mock.patch.object(sys, "stdin", io.StringIO(stdin or "")):
            result = memory.main(["--config", str(self.configs[store]), *map(str, argv)])
        self.assertEqual(result == 0, ok, out.getvalue() + err.getvalue())
        return json.loads(out.getvalue() if ok else err.getvalue())

    def add(self, store, identifier, *scope, text="Rule text.", **options):
        argv = ["rules", "add", identifier, *scope, "--text", text]
        for key, value in options.items():
            argv += ["--" + key, value]
        return self.command(store, *argv)["rule"]

    def test_show_returns_global_then_project_rules_in_full(self):
        long_text = "Keep this exact wording. " * 70
        self.add("alpha", "zeta-global", "--global", text="Reply in Russian.", source="claude:example")
        self.add("alpha", "branch-naming", "--project", "service", text=long_text.strip())
        self.add("alpha", "tools-only", "--project", "tools")
        self.add("alpha", "alpha-global", "--global")
        plain = self.command("alpha", "rules", "show")
        self.assertEqual([rule["id"] for rule in plain["rules"]], ["alpha-global", "zeta-global"])
        self.assertEqual(plain["project_rules_not_shown"]["projects"], ["service", "tools"])
        self.assertTrue(plain["note"].startswith("Standing rules"))
        scoped = self.command("alpha", "--project-path", self.repos["alpha", "service"] / "src", "rules", "show")
        self.assertEqual(scoped["project"], "service")
        self.assertEqual([rule["id"] for rule in scoped["rules"]], ["alpha-global", "zeta-global", "branch-naming"])
        self.assertEqual(scoped["rules"][2]["text"], long_text.strip())
        self.assertNotIn("project_rules_not_shown", scoped)
        self.assertEqual(scoped["rules"][1]["source"], "claude:example")
        explicit = self.command("alpha", "rules", "show", "--project", "tools")
        self.assertEqual([rule["id"] for rule in explicit["rules"]], ["alpha-global", "zeta-global", "tools-only"])
        listing = self.command("alpha", "rules", "list")
        self.assertEqual(len(listing["rules"]), 4)
        self.assertEqual(listing["characters"], sum(len(rule["text"]) for rule in listing["rules"]))
        self.assertEqual([rule["id"] for rule in self.command("alpha", "rules", "list", "--global")["rules"]],
                         ["alpha-global", "zeta-global"])
        self.assertEqual([rule["id"] for rule in self.command("alpha", "rules", "list", "--project", "tools")["rules"]],
                         ["tools-only"])

    def test_workspaces_keep_separate_rules(self):
        self.add("alpha", "shared-id", "--global", text="Alpha rule.")
        self.assertEqual(self.command("beta", "rules", "show")["rules"], [])
        self.add("beta", "shared-id", "--global", text="Beta rule.")
        self.assertEqual(self.command("alpha", "rules", "show")["rules"][0]["text"], "Alpha rule.")
        self.assertEqual(self.command("beta", "rules", "show")["rules"][0]["text"], "Beta rule.")
        self.command("beta", "--project-path", self.repos["alpha", "service"], "rules", "show", ok=False)

    def test_invalid_rules_are_rejected_without_writes(self):
        for argv in (["rules", "add", "a", "--project", "missing", "--text", "x"],
                     ["rules", "add", "a", "--global", "--text", "   "],
                     ["rules", "add", "a", "--global", "--text", "x" * (rules.MAX_TEXT + 1)],
                     ["rules", "add", "a", "--global", "--text", "x", "--source", "two\nlines"],
                     ["rules", "add", "a", "--global", "--text", "x", "--source", "s" * (rules.MAX_SOURCE + 1)]):
            with self.subTest(argv=argv):
                self.command("alpha", *argv, ok=False)
        for identifier in ("-bad", "has space", "a/b", "x" * 101, "ру"):
            with self.subTest(identifier=identifier):
                self.command("alpha", "rules", "add", identifier, "--global", "--text", "x", ok=False)
        self.command("alpha", "rules", "add", "a", "--global", "--project", "service", "--text", "x", ok=False)
        self.command("alpha", "rules", "add", "a", "--text", "x", ok=False)
        self.assertEqual(self.command("alpha", "rules", "list")["rules"], [])
        self.add("alpha", "a", "--global")
        error = self.command("alpha", "rules", "add", "a", "--global", "--text", "other", ok=False)
        self.assertIn("already exists", error["error"])
        self.assertEqual(self.command("alpha", "rules", "list")["rules"][0]["text"], "Rule text.")

    def test_total_size_is_bounded_for_add_and_update(self):
        per_rule = rules.MAX_TEXT
        count = rules.MAX_TOTAL // per_rule
        for index in range(count):
            self.add("alpha", "r%d" % index, "--global", text="x" * per_rule)
        error = self.command("alpha", "rules", "add", "extra", "--global", "--text", "y", ok=False)
        self.assertIn("at most %d characters in all" % rules.MAX_TOTAL, error["error"])
        rule = self.command("alpha", "rules", "list")["rules"][0]
        self.command("alpha", "rules", "update", rule["id"], "--expected-updated-at", rule["updated_at"],
                     "--text", "short")
        self.add("alpha", "extra", "--global", text="y")

    def test_updates_and_removals_use_optimistic_concurrency(self):
        rule = self.add("alpha", "naming", "--global", text="First wording.", source="claude:one")
        stale = self.command("alpha", "rules", "update", "naming", "--expected-updated-at", "2000-01-01T00:00:00Z",
                             "--text", "Lost wording.", ok=False)
        self.assertIn("conflict", stale["error"])
        self.command("alpha", "rules", "update", "naming", "--expected-updated-at", rule["updated_at"], ok=False)
        unchanged = self.command("alpha", "rules", "update", "naming", "--expected-updated-at", rule["updated_at"],
                                 "--text", "First wording.")
        self.assertEqual(unchanged["results"][0]["status"], "unchanged")
        updated = self.command("alpha", "rules", "update", "naming", "--expected-updated-at", rule["updated_at"],
                               "--text", "Second wording.", "--source", "")
        self.assertEqual(updated["results"][0]["status"], "updated")
        changed = updated["rule"]
        self.assertEqual((changed["text"], changed["source"], changed["created_at"]),
                         ("Second wording.", None, rule["created_at"]))
        self.assertNotEqual(changed["updated_at"], rule["updated_at"])
        self.command("alpha", "rules", "remove", "naming", "--expected-updated-at", rule["updated_at"], ok=False)
        self.assertEqual(self.command("alpha", "rules", "show")["rules"], [changed])
        removed = self.command("alpha", "rules", "remove", "naming", "--expected-updated-at", changed["updated_at"])
        self.assertEqual(removed["results"][0]["status"], "removed")
        self.assertEqual(self.command("alpha", "rules", "show")["rules"], [])
        missing = self.command("alpha", "rules", "remove", "naming", "--expected-updated-at", changed["updated_at"], ok=False)
        self.assertEqual(missing["code"], "not_found")

    def test_project_path_guards_rule_scope(self):
        service = self.repos["alpha", "service"]
        self.command("alpha", "--project-path", service, "rules", "add", "x", "--project", "tools", "--text", "x", ok=False)
        self.command("alpha", "--project-path", service, "rules", "add", "x", "--project", "service", "--text", "x")
        self.command("alpha", "--project-path", service, "rules", "add", "g", "--global", "--text", "g")
        tools_rule = self.add("alpha", "t", "--project", "tools")
        error = self.command("alpha", "--project-path", service, "rules", "remove", "t",
                             "--expected-updated-at", tools_rule["updated_at"], ok=False)
        self.assertIn("belongs to project tools", error["error"])
        self.command("alpha", "--project-path", service, "rules", "show", "--project", "tools", ok=False)
        self.command("alpha", "--project-path", self.root / "missing", "rules", "show", ok=False)

    def test_text_from_stdin_keeps_lines(self):
        text = "First line.\nSecond line.\n"
        rule = self.command("alpha", "rules", "add", "multi", "--global", "--text", "-", stdin=text)["rule"]
        self.assertEqual(rule["text"], text)

    def test_rules_stay_out_of_search_and_travel_with_backups(self):
        marker = "zebracornflux"
        self.add("alpha", "marker", "--global", text="Mention " + marker + " in replies.")
        payload = self.root / "entry.json"
        payload.write_text(json.dumps({"id": "note:marker", "kind": "note", "title": "Marker " + marker,
                                       "summary": "Synthetic " + marker + " note."}))
        self.command("alpha", "entry", "put", "--json", payload)
        found = self.command("alpha", "search", marker)
        self.assertEqual([item["id"] for item in found["entries"]], ["note:marker"])
        recalled = self.command("alpha", "recall", marker, "--include-hypotheses")
        self.assertEqual([item["id"] for item in recalled["results"]], ["note:marker"])
        self.assertEqual(self.command("alpha", "check")["status"], "ok")
        backup = self.root / "backup.sqlite3"
        self.command("alpha", "backup", "--output", backup)
        with sqlite3.connect(str(backup)) as conn:
            rows = conn.execute("SELECT key FROM metadata WHERE key LIKE 'rule:%'").fetchall()
        self.assertEqual(rows, [("rule:marker",)])

    def test_reading_does_not_write_and_damaged_rows_are_reported(self):
        database = self.configs["alpha"].parent / "memory.sqlite3"
        before = database.stat().st_mtime_ns
        self.command("alpha", "rules", "show")
        self.command("alpha", "rules", "list")
        self.assertEqual(database.stat().st_mtime_ns, before)
        with sqlite3.connect(str(database)) as conn:
            conn.execute("INSERT INTO metadata(key, value) VALUES ('rule:broken', '{\"id\": \"other\"}')")
        error = self.command("alpha", "rules", "show", ok=False)
        self.assertIn("Invalid stored rule rule:broken", error["error"])


class RulesIntegrationTests(unittest.TestCase):
    def test_bound_chat_is_told_to_load_rules(self):
        with tempfile.TemporaryDirectory(prefix="richi-rules-chat-") as directory, \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(Path, "home", return_value=Path(directory)), \
                mock.patch.object(config.sys, "platform", "linux"):
            self.assertEqual(chats.current_chat("chat")["next_commands"][0], "richi chat bind --current")
            bound = chats.bind_chat("chat", "default")
            self.assertEqual(bound["next_commands"], chats.RULES_COMMANDS)
            current = chats.current_chat("chat")
            self.assertEqual(current["next_commands"], chats.RULES_COMMANDS)
            current["next_commands"].append("mutated")
            self.assertNotIn("mutated", chats.RULES_COMMANDS)

    def test_claude_hook_reloads_rules_after_compaction(self):
        def context(**payload):
            result = subprocess.run([sys.executable, "-I", "-c", HOOK], text=True, capture_output=True, check=True,
                                    input=json.dumps(dict(session_id="abc", hook_event_name="SessionStart", **payload)),
                                    env={key: value for key, value in os.environ.items() if key != "CLAUDE_ENV_FILE"})
            return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        startup = context(source="startup")
        self.assertIn("richi rules show", startup)
        self.assertNotIn("load the rules again", startup)
        self.assertIn("compacted", context(source="compact"))
        self.assertIn("resumed", context(source="resume"))
        self.assertEqual(context(), startup)

    def test_managed_instructions_load_rules(self):
        block = agent_setup._block("/opt/richi/bin/richi").decode()
        self.assertIn("richi rules show", block)
        self.assertIn("standing instructions", block)


if __name__ == "__main__":
    unittest.main()
