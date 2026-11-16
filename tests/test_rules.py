"""Standing rules: scopes, guards, concurrency and isolation in synthetic stores."""
from contextlib import closing, redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from richi import agents, memory, rules
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
        # Agent shells identify the agent; rules show must not depend on the shell running the tests.
        for key in [key for key in os.environ if key.startswith("RICHI_")
                    or key in {"CODEX_THREAD_ID", "CLAUDECODE", "XDG_CONFIG_HOME"}]:
            del os.environ[key]
        agents.forget_settings()
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
        return self.answer(out.getvalue() if ok else err.getvalue())

    def answer(self, text):
        """The command's JSON; anything else in the stream, such as a warning, is shown as it is."""
        try:
            return json.loads(text)
        except ValueError:
            self.fail("The command printed something other than one JSON value: " + repr(text))

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
        # An agent gets what it needs to follow a rule and to change it; where the rule came from
        # and who changed it stay in rules list.
        self.assertEqual(sorted(scoped["rules"][1]), sorted(rules.SHOWN))
        listed = {rule["id"]: rule for rule in self.command("alpha", "rules", "list")["rules"]}
        self.assertEqual(listed["zeta-global"]["source"], "claude:example")
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
        # Another workspace's repository is unknown here: beta answers with its own global rules
        # and says so, and nothing of alpha's leaks.
        self.add("alpha", "alpha-service", "--project", "service", text="Alpha service rule.")
        foreign = self.command("beta", "--project-path", self.repos["alpha", "service"], "rules", "show")
        self.assertEqual(([rule["text"] for rule in foreign["rules"]], foreign["project"]), (["Beta rule."], None))
        self.assertIn("project_path_problem", foreign)

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
        self.assertEqual(self.command("alpha", "rules", "show")["rules"], [{field: changed[field] for field in rules.SHOWN}])
        self.assertEqual(self.command("alpha", "rules", "list")["rules"], [changed])
        removed = self.command("alpha", "rules", "remove", "naming", "--expected-updated-at", changed["updated_at"])
        self.assertEqual(removed["results"][0]["status"], "removed")
        self.assertEqual(self.command("alpha", "rules", "show")["rules"], [])
        missing = self.command("alpha", "rules", "remove", "naming", "--expected-updated-at", changed["updated_at"], ok=False)
        self.assertEqual(missing["code"], "not_found")

    def test_rules_may_name_a_skill_and_errors_carry_codes(self):
        rule = self.add("alpha", "pr-skill", "--global", text="Use it for pull requests.", skill="plugin:pr-description")
        self.assertEqual(rule["skill"], "plugin:pr-description")
        shown = self.command("alpha", "rules", "show")
        self.assertEqual(shown["rules"][0]["skill"], "plugin:pr-description")
        self.assertIn("names a skill", shown["note"])
        self.assertIsNone(self.add("alpha", "plain", "--global")["skill"])
        for skill in ("-x", "two words", "s" * 101):
            with self.subTest(skill=skill):
                self.command("alpha", "rules", "add", "bad", "--global", "--text", "x", "--skill", skill, ok=False)
        renamed = self.command("alpha", "rules", "update", "pr-skill", "--expected-updated-at", rule["updated_at"],
                               "--skill", "pr-review")["rule"]
        self.assertEqual((renamed["skill"], renamed["text"]), ("pr-review", rule["text"]))
        cleared = self.command("alpha", "rules", "update", "pr-skill", "--expected-updated-at", renamed["updated_at"],
                               "--skill", "")["rule"]
        self.assertIsNone(cleared["skill"])
        self.assertEqual(self.command("alpha", "rules", "update", "pr-skill", "--expected-updated-at",
                                      rule["updated_at"], "--skill", "x", ok=False)["code"], "conflict")
        self.assertEqual(self.command("alpha", "rules", "add", "plain", "--global", "--text", "x", ok=False)["code"],
                         "exists")
        self.assertEqual(self.command("alpha", "rules", "add", "long", "--global", "--text", "x" * (rules.MAX_TEXT + 1),
                                      ok=False)["code"], "limit")
        # A row stored before rules could name a skill reads as having none.
        database = self.configs["alpha"].parent / "memory.sqlite3"
        earlier = {key: value for key, value in cleared.items() if key not in {"skill", "skills"}}
        earlier["id"] = "earlier"
        with closing(sqlite3.connect(str(database))) as conn, conn:
            conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?)", ("rule:earlier", json.dumps(earlier)))
        listed = {item["id"]: item for item in self.command("alpha", "rules", "list")["rules"]}
        self.assertIsNone(listed["earlier"]["skill"])
        self.assertIsNone(listed["earlier"]["skills"])

    def test_agents_share_a_skill_or_each_get_their_own(self):
        def add(identifier, *skills):
            argv = ["rules", "add", identifier, "--global", "--text", "Review code."]
            for skill in skills:
                argv += ["--skill", skill]
            return self.command("alpha", *argv)["rule"]

        both = add("review", "claude=engineering:code-review", "codex=code-review")
        self.assertEqual((both["skill"], both["skills"]),
                         (None, {"claude": "engineering:code-review", "codex": "code-review"}))
        # The same skill for every agent is stored once, as the shared skill.
        same = add("same", "claude=pr-description", "codex=pr-description")
        self.assertEqual((same["skill"], same["skills"]), ("pr-description", None))
        mixed = add("mixed", "pr-description", "codex=pr-codex", "claude=pr-description")
        self.assertEqual((mixed["skill"], mixed["skills"]), ("pr-description", {"codex": "pr-codex"}))
        only = add("only", "pr-description", "codex=")
        self.assertEqual(only["skills"], {"codex": None})

        def used(**environment):
            with mock.patch.dict(os.environ, environment):
                shown = self.command("alpha", "rules", "show")
            return shown["agent"], {rule["id"]: rule.get("use_skill", "-") for rule in shown["rules"]}, shown["note"]

        agent, skills, note = used(CODEX_THREAD_ID="thread-1")
        self.assertEqual((agent, skills), ("codex", {"mixed": "pr-codex", "only": None, "review": "code-review",
                                                     "same": "pr-description"}))
        self.assertIn("use_skill", note)
        self.assertEqual(used(RICHI_CHAT_ID="claude:chat-1")[:2],
                         ("claude", {"mixed": "pr-description", "only": "pr-description",
                                     "review": "engineering:code-review", "same": "pr-description"}))
        self.assertEqual(used(CLAUDECODE="1")[0], "claude")
        # Codex started from a Claude Code terminal carries both agents' signs: neither is named,
        # so no agent is told to use the other's skill.
        for environment in ({}, {"RICHI_CHAT_ID": "claude:chat-1", "CODEX_THREAD_ID": "thread-1"},
                            {"CLAUDECODE": "1", "CODEX_THREAD_ID": "thread-1"}):
            with self.subTest(environment=environment):
                agent, skills, note = used(**environment)
                self.assertEqual((agent, set(skills.values())), (None, {"-"}))
        self.assertIn("skills maps an agent ID (claude: Claude Code, codex: Codex)", note)
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-1"}):
            explicit = self.command("alpha", "rules", "show", "--agent", "claude")
        self.assertEqual(explicit["rules"][2]["use_skill"], "engineering:code-review")

        for skills in (["gemini=x"], ["a", "b"], ["codex=a", "codex=b"], ["codex=two words"]):
            with self.subTest(skills=skills):
                argv = ["rules", "add", "bad", "--global", "--text", "x"]
                for skill in skills:
                    argv += ["--skill", skill]
                self.command("alpha", *argv, ok=False)
        changed = self.command("alpha", "rules", "update", "review", "--expected-updated-at", both["updated_at"],
                               "--skill", "codex=code-review")["rule"]
        self.assertEqual((changed["skill"], changed["skills"]), (None, {"codex": "code-review"}))
        cleared = self.command("alpha", "rules", "update", "review", "--expected-updated-at", changed["updated_at"],
                               "--skill", "")["rule"]
        self.assertEqual((cleared["skill"], cleared["skills"]), (None, None))

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
        # Global rules bind the agent in any repository: an unregistered path loads them and says why
        # the project's rules are missing, instead of leaving the agent without rules.
        unregistered = self.root / "unregistered"
        unregistered.mkdir()
        for path in (unregistered, self.root / "missing"):
            with self.subTest(path=path.name):
                shown = self.command("alpha", "--project-path", path, "rules", "show")
                self.assertEqual(([rule["id"] for rule in shown["rules"]], shown["project"]), (["g"], None))
                self.assertIn("roject path", shown["project_path_problem"]["error"])
                self.assertIn("project add", shown["project_path_problem"]["next"])
        self.assertNotIn("project_path_problem", self.command("alpha", "--project-path", service, "rules", "show"))
        # Changes stay strict: a rule is never written from a path the workspace does not know.
        self.command("alpha", "--project-path", unregistered, "rules", "add", "y", "--global", "--text", "y", ok=False)
        self.command("alpha", "--project-path", unregistered, "rules", "list", ok=False)

    def test_apply_takes_the_same_change_as_the_command_options(self):
        def apply(change, ok=True, **options):
            return self.command("alpha", *options.get("selectors", ()), "rules", "apply", "--json", "-", ok=ok,
                                stdin=json.dumps(change))

        tricky = "--global\n'single' \"double\" $HOME `date` -"
        added = apply({"action": "add", "id": "review", "scope": "global", "text": tricky, "source": "map",
                       "skills": {"claude": "engineering:code-review", "codex": "code-review"}})
        self.assertEqual(added["results"], [{"id": "review", "status": "created"}])
        rule = added["rule"]
        self.assertEqual((rule["text"], rule["source"], rule["skill"], rule["skills"]),
                         (tricky, "map", None, {"claude": "engineering:code-review", "codex": "code-review"}))
        by_options = self.command("alpha", "rules", "add", "review-2", "--global", "--text", "-", "--source", "map",
                                  "--skill", "claude=engineering:code-review", "--skill", "codex=code-review",
                                  stdin=tricky)["rule"]
        same = ("scope", "project_id", "text", "skill", "skills", "source")
        self.assertEqual([rule[key] for key in same], [by_options[key] for key in same])
        self.assertEqual(apply({"action": "add", "id": "review", "scope": "global", "text": "x"}, ok=False)["code"],
                         "exists")
        scoped = apply({"action": "add", "id": "naming", "scope": "project", "project": "service", "text": "Names."})
        self.assertEqual(scoped["rule"]["project_id"], "service")
        apply({"action": "add", "id": "other", "scope": "project", "project": "tools", "text": "x"}, ok=False,
              selectors=("--project-path", self.repos["alpha", "service"]))
        self.assertEqual(apply({"action": "update", "id": "review", "expected_updated_at": "stale", "text": "x"},
                               ok=False)["code"], "conflict")
        version = {"id": "review", "expected_updated_at": rule["updated_at"]}
        # The skills of a rule are replaced together; the text stays unless it is given.
        updated = apply({"action": "update", **version, "skill": "pr-description", "skills": {"codex": None}})["rule"]
        self.assertEqual((updated["text"], updated["skill"], updated["skills"]),
                         (tricky, "pr-description", {"codex": None}))
        self.assertEqual(apply({"action": "update", "id": "review", "expected_updated_at": updated["updated_at"],
                                "skill": "pr-description", "skills": {"codex": None}})["results"][0]["status"],
                         "unchanged")
        for change in ({"action": "update", "id": "review", "expected_updated_at": updated["updated_at"]},
                       {"action": "add", "id": "x", "scope": "global", "text": "x", "unexpected": 1},
                       {"action": "add", "id": "x", "scope": "everywhere", "text": "x"},
                       {"action": "add", "id": "x", "scope": "global", "project": "service", "text": "x"},
                       {"action": "add", "id": "x", "scope": "global", "text": 5},
                       {"action": "add", "id": "x", "scope": "global", "text": "x", "skills": ["codex"]},
                       {"action": "add", "id": "x", "scope": "global", "text": "x", "skills": {"cursor": "x"}},
                       {"action": "add", "id": "-x", "scope": "global", "text": "x"},
                       {"action": "drop", "id": "review"}, ["add"], "add"):
            with self.subTest(change=change):
                self.assertNotIn(apply(change, ok=False).get("code"), {"exists", "conflict", "not_found"})
        self.command("alpha", "rules", "apply", "--json", "-", ok=False, stdin="{not json")
        self.command("alpha", "rules", "apply", "--json", self.root / "no-such-file.json", ok=False)
        change_file = self.root / "change.json"
        change_file.write_text(json.dumps({"action": "remove", "id": "review",
                                           "expected_updated_at": updated["updated_at"]}), encoding="utf-8")
        self.assertEqual(self.command("alpha", "rules", "apply", "--json", change_file)["results"][0]["status"],
                         "removed")

    def test_text_from_stdin_keeps_lines_but_not_the_newline_that_ends_the_pipe(self):
        def piped(identifier, text):
            return self.command("alpha", "rules", "add", identifier, "--global", "--text", "-", stdin=text)["rule"]

        multi = piped("multi", "First line.\nSecond line.\n")
        self.assertEqual(multi["text"], "First line.\nSecond line.")
        self.assertEqual(piped("bare", "No newline")["text"], "No newline")
        # Only the last newline goes: a blank line the user ended the text with is theirs.
        self.assertEqual(piped("spaced", "Line.\n\n")["text"], "Line.\n")
        # The same text arrives whichever way it is given, so a pipe does not "change" a rule.
        self.assertEqual(self.command("alpha", "rules", "update", "multi", "--expected-updated-at", multi["updated_at"],
                                      "--text", "-", stdin="First line.\nSecond line.\n")["results"][0]["status"],
                         "unchanged")
        # A text that begins with a dash is given as --text=TEXT.
        dashed = self.command("alpha", "rules", "add", "dashed", "--global", "--text=- one instruction")["rule"]
        self.assertEqual(dashed["text"], "- one instruction")

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
        with closing(sqlite3.connect(str(backup))) as conn:
            rows = conn.execute("SELECT key FROM metadata WHERE key LIKE 'rule:%'").fetchall()
        self.assertEqual(rows, [("rule:marker",)])

    def test_reading_does_not_write_and_a_damaged_row_does_not_hide_the_rules(self):
        database = self.configs["alpha"].parent / "memory.sqlite3"
        before = database.stat().st_mtime_ns
        for view in ("show", "list", "log"):
            self.command("alpha", "rules", view)
        self.assertEqual(database.stat().st_mtime_ns, before)
        kept = self.add("alpha", "kept", "--global", text="Reply in Russian.")
        with closing(sqlite3.connect(str(database))) as conn, conn:
            for key, value in (("rule:broken", '{"id": "other"}'), ("rule:garbled", "{not json"),
                               ("rule:partial", '{"id": "partial", "updated_at": "v1", "text": 5}')):
                conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?)", (key, value))
            # A newer Richi may store fields this one does not know: they are kept, not refused.
            row = json.loads(conn.execute("SELECT value FROM metadata WHERE key = 'rule:kept'").fetchone()[0])
            row["when"] = "pull-request"
            conn.execute("UPDATE metadata SET value = ? WHERE key = 'rule:kept'", (json.dumps(row),))
        for view in ("show", "list"):
            with self.subTest(view=view):
                shown = self.command("alpha", "rules", view)
                # The stored record keeps the field; an agent is not handed what this version cannot explain.
                self.assertEqual([(rule["id"], rule.get("when")) for rule in shown["rules"]],
                                 [("kept", "pull-request" if view == "list" else None)])
                self.assertEqual([(item["id"], item["updated_at"], item["next"]) for item in shown["invalid_rules"]], [
                    ("broken", "invalid", "richi rules remove broken --expected-updated-at invalid"),
                    ("garbled", "invalid", "richi rules remove garbled --expected-updated-at invalid"),
                    ("partial", "v1", "richi rules remove partial --expected-updated-at v1")])
                self.assertTrue(all(item["error"] for item in shown["invalid_rules"]))
        updated = self.command("alpha", "rules", "update", "kept", "--expected-updated-at", kept["updated_at"],
                               "--text", "Reply in English.")["rule"]
        self.assertEqual((updated["text"], updated["when"]), ("Reply in English.", "pull-request"))
        # A damaged row cannot be changed, only removed, and with the version the listing names.
        self.assertIn("rules remove broken --expected-updated-at invalid",
                      self.command("alpha", "rules", "update", "broken", "--expected-updated-at", "invalid",
                                   "--text", "x", ok=False)["error"])
        self.assertEqual(self.command("alpha", "rules", "remove", "partial", "--expected-updated-at", "invalid",
                                      ok=False)["code"], "conflict")
        for identifier, version in (("broken", "invalid"), ("garbled", "invalid"), ("partial", "v1")):
            removed = self.command("alpha", "rules", "remove", identifier, "--expected-updated-at", version)
            self.assertEqual((removed["results"][0]["status"], removed["rule"]), ("removed", None))
        self.assertNotIn("invalid_rules", self.command("alpha", "rules", "list"))
        self.assertIn("{not json",
                      self.command("alpha", "rules", "log", "--id", "garbled")["entries"][0]["before"]["unreadable"])

    def test_changes_are_logged_with_who_made_them(self):
        with mock.patch.dict(os.environ, {"RICHI_CHAT_ID": "claude:chat-1"}):
            created = self.add("alpha", "names", "--global", text="First.")
        self.assertEqual(created["changed_by"], "claude:chat-1")
        version = ["--expected-updated-at", created["updated_at"]]
        # The map names itself, whatever chat its server was started from.
        with mock.patch.dict(os.environ, {"RICHI_CHAT_ID": "claude:chat-1", "RICHI_RULES_ACTOR": "richi map"}):
            updated = self.command("alpha", "rules", "update", "names", *version, "--text", "Second.")["rule"]
        self.assertEqual(updated["changed_by"], "richi map")
        version = ["--expected-updated-at", updated["updated_at"]]
        self.assertEqual(self.command("alpha", "rules", "update", "names", *version, "--text", "Second.")
                         ["results"][0]["status"], "unchanged")
        self.command("alpha", "rules", "update", "names", "--expected-updated-at", "stale", "--text", "x", ok=False)
        self.command("alpha", "rules", "remove", "names", *version)
        self.assertIsNone(self.add("alpha", "other", "--global")["changed_by"])
        log = self.command("alpha", "rules", "log")
        self.assertEqual((log["view"], log["kept"]), ("rules-log", rules.MAX_LOG))
        # Newest first; a change that changed nothing and a refused one leave no entry.
        self.assertEqual([(entry["id"], entry["action"], entry["by"], entry["agent"]) for entry in log["entries"]],
                         [("other", "created", None, None), ("names", "removed", None, None),
                          ("names", "updated", "richi map", None), ("names", "created", "claude:chat-1", "claude")])
        self.assertEqual(sorted(entry["at"] for entry in log["entries"]), [entry["at"] for entry in log["entries"]][::-1])
        names = self.command("alpha", "rules", "log", "--id", "names", "--entries", "2")["entries"]
        self.assertEqual([(entry["action"], (entry["before"] or {}).get("text"), (entry["after"] or {}).get("text"))
                          for entry in names], [("removed", "Second.", None), ("updated", "First.", "Second.")])
        self.assertEqual(self.command("beta", "rules", "log")["entries"], [])
        for arguments in (["--entries", "0"], ["--entries", str(rules.MAX_LOG + 1)], ["--id", "-bad"]):
            self.command("alpha", "rules", "log", *arguments, ok=False)
        # Only the latest changes are kept, and the log never shows up as a rule.
        with mock.patch.object(rules, "MAX_LOG", 3):
            for number in range(4):
                self.add("alpha", "extra-%d" % number, "--global")
        self.assertEqual([entry["id"] for entry in self.command("alpha", "rules", "log")["entries"]],
                         ["extra-3", "extra-2", "extra-1"])
        self.assertEqual([rule["id"] for rule in self.command("alpha", "rules", "list")["rules"]],
                         ["extra-0", "extra-1", "extra-2", "extra-3", "other"])
        self.assertEqual(self.command("alpha", "check")["status"], "ok")

    def stored(self, store, key, value=None, **changes):
        """Write a metadata row directly, as a hand edit or another Richi version would; value None edits the row."""
        database = self.configs[store].parent / "memory.sqlite3"
        with closing(sqlite3.connect(str(database))) as conn, conn:
            if value is None:
                row = json.loads(conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()[0])
                row.update(changes)
                value = json.dumps(row)
            conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                         (key, value))

    def test_a_row_of_any_shape_or_key_is_listed_and_can_be_removed(self):
        self.add("alpha", "first", "--global")
        self.add("alpha", "second", "--global")
        self.add("alpha", "third", "--project", "service")
        # Each of these once took every rules command down, or could not be removed.
        self.stored("alpha", "rule:second", project_id=5)
        self.stored("alpha", "rule:third", project_id=["service"])
        self.stored("alpha", "rule:two words", "{}")
        self.stored("alpha", "rule:-dash", "[1]")
        self.stored("alpha", "rule:", '"text"')
        for view in ("show", "list"):
            with self.subTest(view=view):
                listed = self.command("alpha", "rules", view)
                self.assertEqual([rule["id"] for rule in listed["rules"]], ["first"])
                self.assertEqual({item["id"]: item["next"] for item in listed["invalid_rules"]}, {
                    "": "richi rules remove '' --expected-updated-at invalid",
                    "-dash": "richi rules remove --expected-updated-at invalid -- -dash",
                    "second": "richi rules remove second --expected-updated-at " + listed["invalid_rules"][2]["updated_at"],
                    "third": "richi rules remove third --expected-updated-at " + listed["invalid_rules"][3]["updated_at"],
                    "two words": "richi rules remove 'two words' --expected-updated-at invalid"})
        # A key that is not an ID is accepted only to remove the row that has it.
        for argv in (["rules", "remove", "no such row", "--expected-updated-at", "invalid"],
                     ["rules", "update", "two words", "--expected-updated-at", "invalid", "--text", "x"],
                     ["rules", "add", "two words", "--global", "--text", "x"]):
            self.command("alpha", *argv, ok=False)
        self.assertEqual(self.command("alpha", "rules", "remove", "two words", "--expected-updated-at", "other",
                                      ok=False)["code"], "conflict")
        # The command each row names removes it, as a shell would split it.
        for item in listed["invalid_rules"]:
            words = shlex.split(item["next"])
            self.assertEqual(words[0], "richi")
            self.assertEqual(self.command("alpha", *words[1:])["results"], [{"id": item["id"], "status": "removed"}])
        self.assertNotIn("invalid_rules", self.command("alpha", "rules", "list"))

    def test_a_rule_that_needs_a_newer_format_is_left_out_not_served(self):
        paused = self.add("alpha", "paused", "--global", text="Use the old flow.")
        self.add("alpha", "plain", "--global", text="Reply in Russian.")
        # A newer Richi marks a rule whose new field changes whether it applies; here, a pause.
        self.stored("alpha", "rule:paused", requires=rules.FORMAT + 1, enabled=False)
        # A marker this version satisfies changes nothing, and the fields beside it are kept.
        self.stored("alpha", "rule:plain", requires=rules.FORMAT, note="kept")
        for view in ("show", "list"):
            with self.subTest(view=view):
                listed = self.command("alpha", "rules", view)
                self.assertEqual([rule["id"] for rule in listed["rules"]], ["plain"])
                if view == "list":
                    self.assertEqual((listed["rules"][0]["requires"], listed["rules"][0]["note"]), (rules.FORMAT, "kept"))
                # The version is there for a user who removes the rule instead of updating Richi.
                self.assertEqual(listed["unsupported_rules"],
                                 [{"id": "paused", "requires": rules.FORMAT + 1, "next": "richi update",
                                   "updated_at": paused["updated_at"],
                                   "error": listed["unsupported_rules"][0]["error"]}])
                self.assertIn("format %d" % (rules.FORMAT + 1), listed["unsupported_rules"][0]["error"])
                self.assertNotIn("invalid_rules", listed)
        # It can be removed, with its own version, but not changed by a version that would drop its meaning.
        refused = self.command("alpha", "rules", "update", "paused", "--expected-updated-at", paused["updated_at"],
                               "--text", "x", ok=False)["error"]
        self.assertIn("update Richi", refused)
        self.assertEqual(self.command("alpha", "rules", "add", "paused", "--global", "--text", "x", ok=False)["code"],
                         "exists")
        # A marker of the wrong type is damage, not a newer format.
        self.stored("alpha", "rule:plain", requires="2")
        damaged = self.command("alpha", "rules", "list")
        self.assertEqual(([rule["id"] for rule in damaged["rules"]], [item["id"] for item in damaged["invalid_rules"]]),
                         ([], ["plain"]))
        removed = self.command("alpha", "rules", "remove", "paused", "--expected-updated-at",
                               damaged["unsupported_rules"][0]["updated_at"])
        self.assertEqual(removed["results"], [{"id": "paused", "status": "removed"}])
        self.assertNotIn("unsupported_rules", self.command("alpha", "rules", "list"))

    def test_input_is_read_before_the_store_is_locked(self):
        database = self.configs["alpha"].parent / "memory.sqlite3"
        change = self.root / "change.json"
        change.write_text(json.dumps({"action": "add", "id": "filed", "scope": "global", "text": "From a file."}))
        waited = []

        class Waiting(io.StringIO):
            """Standard input of a command whose text is still being typed."""

            def read(inner, *args):
                # Another writer gets in now: the command has not locked the store while it waits.
                with closing(sqlite3.connect(str(database), timeout=0, isolation_level=None)) as other:
                    other.execute("BEGIN IMMEDIATE")
                    other.execute("ROLLBACK")
                waited.append(True)
                return super().read(*args)

        for argv, text in ((["rules", "add", "typed", "--global", "--text", "-"], "Typed slowly.\n"),
                           (["rules", "apply", "--json", "-"],
                            json.dumps({"action": "add", "id": "applied", "scope": "global", "text": "Applied."}))):
            with self.subTest(argv=argv):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err), mock.patch.object(sys, "stdin", Waiting(text)):
                    self.assertEqual(memory.main(["--config", str(self.configs["alpha"]), *argv]), 0, err.getvalue())
        self.assertEqual(len(waited), 2)
        self.command("alpha", "rules", "apply", "--json", change)
        self.assertEqual([rule["id"] for rule in self.command("alpha", "rules", "list")["rules"]],
                         ["applied", "filed", "typed"])

    def test_one_stored_rule_is_bounded_whatever_it_carries(self):
        kept = self.add("alpha", "kept", "--global", text="Reply in Russian.")
        self.add("alpha", "heavy", "--global", text="Short.")
        # A field this version does not know may be large; a record beyond the bound is damage.
        self.stored("alpha", "rule:kept", notes="n" * (rules.MAX_STORED - 1000))
        self.stored("alpha", "rule:heavy", notes="n" * rules.MAX_STORED)
        shown = self.command("alpha", "rules", "show")
        self.assertEqual([sorted(rule) for rule in shown["rules"]], [sorted(rules.SHOWN)])
        self.assertLess(len(json.dumps(shown)), 3000)
        listed = self.command("alpha", "rules", "list")
        self.assertEqual(([rule["id"] for rule in listed["rules"]], [item["id"] for item in listed["invalid_rules"]]),
                         (["kept"], ["heavy"]))
        self.assertIn(str(rules.MAX_STORED), listed["invalid_rules"][0]["error"])
        # A change that would push a record over the bound is refused instead of making it unreadable.
        refused = self.command("alpha", "rules", "update", "kept", "--expected-updated-at", kept["updated_at"],
                               "--text", "x" * rules.MAX_TEXT, ok=False)
        self.assertEqual(refused["code"], "limit")
        self.assertEqual([rule["id"] for rule in self.command("alpha", "rules", "list")["rules"]], ["kept"])

    def test_a_change_names_the_author_it_is_given(self):
        database = self.configs["alpha"].parent / "memory.sqlite3"
        # change() reads nothing from the environment: a command resolves the author once and passes it.
        with mock.patch.dict(os.environ, {"RICHI_CHAT_ID": "claude:ambient", "CLAUDECODE": "1"}), \
                closing(memory.connect(database)) as conn, memory.transaction(conn):
            unnamed = rules.change(conn, {"action": "add", "id": "unnamed", "scope": "global", "text": "x"}, memory)
            named = rules.change(conn, {"action": "add", "id": "named", "scope": "global", "text": "x"}, memory,
                                 author=rules.Author("codex:chat", "codex"))
            self.assertEqual(rules.current_author(), rules.Author("claude:ambient", "claude"))
        self.assertEqual((unnamed["rule"]["changed_by"], named["rule"]["changed_by"]), (None, "codex:chat"))
        self.assertEqual(rules.current_author({"RICHI_RULES_ACTOR": "richi map", "CLAUDECODE": "1"}),
                         rules.Author("richi map", None))
        entries = self.command("alpha", "rules", "log")["entries"]
        self.assertEqual([(entry["id"], entry["by"], entry["agent"]) for entry in entries],
                         [("named", "codex:chat", "codex"), ("unnamed", None, None)])

    def test_the_change_log_can_be_cleared(self):
        secret = self.add("alpha", "secret", "--global", text="The staging token is EXAMPLE-0000.")
        self.add("alpha", "names", "--global", text="Name branches by task.")
        self.command("alpha", "rules", "remove", "secret", "--expected-updated-at", secret["updated_at"])
        self.add("beta", "other", "--global")
        # A removed rule's text stays in the log until the user clears it.
        self.assertIn("EXAMPLE-0000", json.dumps(self.command("alpha", "rules", "log")))
        cleared = self.command("alpha", "rules", "clear-log", "--id", "secret")
        self.assertEqual(cleared, {"view": "rules-log", "results": [{"status": "cleared", "entries": 2}]})
        left = self.command("alpha", "rules", "log")["entries"]
        self.assertEqual([entry["id"] for entry in left], ["names"])
        self.assertNotIn("EXAMPLE-0000", json.dumps(left))
        self.assertEqual(self.command("alpha", "rules", "clear-log", "--id", "secret")["results"][0]["entries"], 0)
        self.assertEqual(self.command("alpha", "rules", "clear-log")["results"][0]["entries"], 1)
        self.assertEqual(self.command("alpha", "rules", "log")["entries"], [])
        # The rules themselves and another workspace's log are untouched.
        self.assertEqual([rule["id"] for rule in self.command("alpha", "rules", "list")["rules"]], ["names"])
        self.assertEqual(len(self.command("beta", "rules", "log")["entries"]), 1)


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

    def test_managed_instructions_stay_as_released(self):
        # An update is activated by the launcher it replaces, which rewrites the skill link and the
        # Claude hook but not this block. A changed text would leave every updated installation
        # needing repair, so the rules step lives in the chat binding and the hook instead. Change
        # these only together with a way for an updated installation to get the new text.
        for agent, digest in (("codex", "a908ceb314de9bdbd7c5c22366957d76862553701baccec9a5c6ae86cca8153c"),
                              ("claude", "6ef11912428f7f4356b598d7a9cd74dad3e30fdd02ac6d2888030f0903a7bc79")):
            with self.subTest(agent=agent):
                self.assertEqual(hashlib.sha256(agent_setup._block("/opt/richi/bin/richi", agent)).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
