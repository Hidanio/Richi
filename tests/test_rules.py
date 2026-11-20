"""Standing rules: scopes, guards, concurrency and isolation in synthetic stores."""
from contextlib import closing, redirect_stderr, redirect_stdout
from concurrent.futures import ThreadPoolExecutor
import functools
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
import threading
import unittest
from unittest import mock

from richi import agents, memory, rules, skill_catalog
from richi.output import cut_name
from richi_launcher import agent_setup, chats, config
from richi_launcher.claude_setup import HOOK


def all_agent_skill_options(skill):
    return [arg for agent in agents.builtin_ids() for arg in ("--skill", agent + "=" + skill)]


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
                    or key in {"CODEX_THREAD_ID", "CLAUDECODE", "CURSOR_AGENT", "XDG_CONFIG_HOME"}]:
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
        # Which limit it is travels beside the code, so that nobody has to read the message.
        self.assertEqual((error["code"], error["subject"]), ("limit", "rules"))
        long = self.command("alpha", "rules", "add", "long", "--global", "--text", "y" * (rules.MAX_TEXT + 1), ok=False)
        self.assertEqual((long["code"], long["subject"]), ("limit", "text"))
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

    def test_a_previous_runtime_can_append_a_newer_log_entry(self):
        frozen = "2026-01-02T00:00:00.000000Z"
        later = "2026-01-03T00:00:00.000000Z"
        with mock.patch.object(memory, "now", return_value=frozen):
            self.add("alpha", "first", "--global")
            # The previous writer records its change in the log but knows nothing about rule_clock.
            with closing(sqlite3.connect(self.configs["alpha"].parent / "memory.sqlite3")) as conn, conn:
                conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?)",
                             ("rule-log:" + later + ":other", json.dumps({"at": later, "id": "other"})))
            newest = self.add("alpha", "last", "--global")
        self.assertGreater(newest["updated_at"], later)
        self.assertEqual([item["id"] for item in self.command("alpha", "rules", "log")["entries"]],
                         ["last", "other", "first"])

    def test_repeated_clock_does_not_reuse_versions_or_drop_audit_entries(self):
        frozen = "2026-01-02T00:00:00.000000Z"
        with mock.patch.object(memory, "now", return_value=frozen):
            first = self.add("alpha", "names", "--global")
            second = self.command("alpha", "rules", "update", "names", "--expected-updated-at", first["updated_at"],
                                  "--text", "New wording.")["rule"]
            self.assertNotEqual(first["updated_at"], second["updated_at"])
            self.assertEqual(self.command("alpha", "rules", "remove", "names", "--expected-updated-at",
                                          first["updated_at"], ok=False)["code"], "conflict")
            self.command("alpha", "rules", "remove", "names", "--expected-updated-at", second["updated_at"])
            entries = self.command("alpha", "rules", "log")["entries"]
            self.assertEqual([entry["action"] for entry in entries], ["removed", "updated", "created"])
            self.command("alpha", "rules", "clear-log")
        with mock.patch.object(memory, "now", return_value="2025-01-01T00:00:00.000000Z"):
            replacement = self.add("alpha", "names", "--global")
            self.assertGreater(replacement["updated_at"], entries[0]["at"])

    def test_two_writers_of_the_same_version_commit_only_one_change(self):
        rule = self.add("alpha", "naming", "--global")
        database = self.configs["alpha"].parent / "memory.sqlite3"
        ready = threading.Barrier(2, timeout=10)

        def update(wording):
            with closing(memory.connect(database)) as conn:
                ready.wait()
                try:
                    with memory.transaction(conn):
                        return rules.change(conn, {"action": "update", "id": "naming", "text": wording,
                                                   "expected_updated_at": rule["updated_at"]}, memory)["rule"]["text"]
                except memory.MemoryError as exc:
                    return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(update, ["One.", "Two."]))
        current = self.command("alpha", "rules", "show")["rules"][0]["text"]
        self.assertCountEqual(results, ["conflict", current])
        entries = self.command("alpha", "rules", "log")["entries"]
        self.assertEqual([entry["action"] for entry in entries], ["updated", "created"])
        self.assertEqual(entries[0]["after"]["text"], current)

    def test_clearing_legacy_history_does_not_reuse_a_removed_version(self):
        original = "2026-01-02T00:00:00.000000Z"
        removed_at = "2026-01-02T00:00:00.100000Z"
        for store, options in (("alpha", []), ("beta", ["--id", "names"])):
            # The previous runtime has removed this rule. Its audit history is the only surviving
            # version evidence in that store; it did not maintain a separate rule clock.
            self.stored(store, "rule-log:" + removed_at + ":names",
                        json.dumps({"at": removed_at, "action": "removed", "id": "names",
                                    "before": {"id": "names", "updated_at": original}, "after": None}))
            # This unreadable key sorts after every timestamp. The valid legacy version still counts.
            self.stored(store, "rule-log:zzz", "{not json")
            with self.subTest(options=options), mock.patch.object(memory, "now", return_value=original):
                self.command(store, "rules", "clear-log", *options)
                self.assertEqual(self.command(store, "rules", "log")["entries"], [])
                replacement = self.add(store, "names", "--global")
                self.assertGreater(replacement["updated_at"], removed_at)
                self.assertEqual(self.command(store, "rules", "remove", "names", "--expected-updated-at",
                                              original, ok=False)["code"], "conflict")

    def test_an_old_recovery_command_cannot_remove_a_changed_unversioned_row(self):
        self.stored("alpha", "rule:broken", b"\xff")
        old = self.command("alpha", "rules", "list")["invalid_rules"][0]
        self.stored("alpha", "rule:broken", b"\xfe")
        new = self.command("alpha", "rules", "list")["invalid_rules"][0]
        stale = self.command("alpha", *shlex.split(old["next"])[1:], ok=False)
        self.assertEqual(stale["code"], "conflict")
        self.assertNotEqual(old["updated_at"], new["updated_at"])
        self.command("alpha", *shlex.split(new["next"])[1:])
        self.assertNotIn("invalid_rules", self.command("alpha", "rules", "list"))

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

    def test_legacy_skill_in_shared_and_agent_rules_follows_the_installed_skill(self):
        old = "project-memory"
        skill = Path(__file__).resolve().parents[1] / "skills/richi/SKILL.md"
        # The first upgraded release may still be discovered in the old folder.
        directory = self.root / "home/.claude/skills/project-memory"
        directory.mkdir(parents=True)
        installed = directory / "SKILL.md"
        legacy = "---\nname: project-memory\ndescription: Legacy skill\n---\n"
        foreign = "---\nname: richi\ndescription: Another skill\n---\n"
        installed.write_text(legacy)
        self.add("alpha", "shared", "--global", skill=old)
        self.add("alpha", "override", "--global", skill="claude=" + old)
        stored = self.command("alpha", "rules", "list")["rules"]
        log = self.command("alpha", "rules", "log")["entries"]

        def used():
            shown = self.command("alpha", "rules", "show", "--agent", "claude")["rules"]
            return [rule["use_skill"] for rule in shown]

        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(directory.parent.parent)}):
            self.assertEqual(used(), [old, old])
            installed.write_bytes(skill.read_bytes())
            self.assertEqual(used(), ["richi", "richi"])
            # A separately installed skill with the exact requested name wins.
            other = directory.parent / "unrelated-folder"
            other.mkdir()
            (other / "SKILL.md").write_text(legacy)
            self.assertEqual(used(), [old, old])
            # Two different skills named richi make the target ambiguous.
            (other / "SKILL.md").write_text(foreign)
            self.assertEqual(used(), [old, old])
            installed.write_text(foreign)
            self.assertEqual(used(), [old, old])
            installed.write_text(legacy)  # rollback
            self.assertEqual(used(), [old, old])
        self.assertEqual(self.command("alpha", "rules", "list")["rules"], stored)
        self.assertEqual(self.command("alpha", "rules", "log")["entries"], log)
        # No catalog scan at startup for a workspace without a legacy skill rule.
        self.add("beta", "custom", "--global", skill="my-review")
        with mock.patch.object(skill_catalog, "catalog") as scan:
            shown = self.command("beta", "rules", "show", "--agent", "claude")["rules"]
        scan.assert_not_called()
        self.assertEqual(shown[0]["use_skill"], "my-review")

    def test_disabled_codex_skill_does_not_resolve_a_legacy_rule(self):
        home = Path.home()
        profile = home / "custom-codex"
        profile.mkdir(parents=True)
        skill = home / ".agents/skills/richi"
        skill.parent.mkdir(parents=True)
        skill.symlink_to(Path(__file__).resolve().parents[1] / "skills/richi")
        self.add("alpha", "legacy", "--global", skill="project-memory")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(profile)}):
            def used(agent):
                return self.command("alpha", "rules", "show", "--agent", agent)["rules"][0]["use_skill"]

            self.assertEqual(used("codex"), "richi")
            config = profile / "config.toml"
            for path in ("~/.agents/skills/richi/SKILL.md", str((skill / "SKILL.md").resolve())):
                with self.subTest(path=path):
                    config.write_text("[[skills.config]]\npath = " + json.dumps(path) + "\nenabled = false\n")
                    self.assertEqual(used("codex"), "project-memory")
                    self.assertEqual(used("cursor"), "richi")
            with config.open("a") as stream:
                stream.write('[[skills.config]]\npath = "../.agents/skills/richi/SKILL.md"\nenabled = true\n')
            self.assertEqual(used("codex"), "project-memory")  # ambiguous opt-out

    def test_agents_share_a_skill_or_each_get_their_own(self):
        def add(identifier, *skills):
            argv = ["rules", "add", identifier, "--global", "--text", "Review code."]
            for skill in skills:
                argv += ["--skill", skill]
            return self.command("alpha", *argv)["rule"]

        review = add("review", "claude=engineering:code-review", "codex=code-review", "cursor=cursor-review")
        self.assertEqual((review["skill"], review["skills"]),
                         (None, {"claude": "engineering:code-review", "codex": "code-review", "cursor": "cursor-review"}))
        # The same skill for every agent is stored once, as the shared skill.
        same = add("same", *(agent + "=pr-description" for agent in agents.builtin_ids()))
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
        self.assertEqual(used(RICHI_CHAT_ID="cursor:chat-1")[:2],
                         ("cursor", {"mixed": "pr-description", "only": "pr-description",
                                     "review": "cursor-review", "same": "pr-description"}))
        # Codex started from a Claude Code terminal carries both agents' signs: neither is named,
        # so no agent is told to use the other's skill.
        for environment in ({}, {"RICHI_CHAT_ID": "claude:chat-1", "CODEX_THREAD_ID": "thread-1"},
                            {"CLAUDECODE": "1", "CODEX_THREAD_ID": "thread-1"}):
            with self.subTest(environment=environment):
                agent, skills, note = used(**environment)
                self.assertEqual((agent, set(skills.values())), (None, {"-"}))
        self.assertIn("skills maps an agent ID (" + ", ".join(a.id + ": " + a.name for a in agents.AGENTS) + ")", note)
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-1"}):
            explicit = self.command("alpha", "rules", "show", "--agent", "claude")
        self.assertEqual(explicit["rules"][2]["use_skill"], "engineering:code-review")

        for skills in (["gemini=x"], ["a", "b"], ["codex=a", "codex=b"], ["codex=two words"]):
            with self.subTest(skills=skills):
                argv = ["rules", "add", "bad", "--global", "--text", "x"]
                for skill in skills:
                    argv += ["--skill", skill]
                self.command("alpha", *argv, ok=False)
        changed = self.command("alpha", "rules", "update", "review", "--expected-updated-at", review["updated_at"],
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

    def test_project_path_cannot_remove_a_rule_of_unverifiable_scope(self):
        path = self.repos["alpha", "service"]
        for identifier, fields in (("damaged", {"text": False}), ("newer", {"requires": rules.FORMAT + 1})):
            rule = self.add("alpha", identifier, "--project", "tools")
            self.stored("alpha", "rule:" + identifier, **fields)
            remove = ["rules", "remove", identifier, "--expected-updated-at", rule["updated_at"]]
            refused = self.command("alpha", "--project-path", path, *remove, ok=False)
            self.assertIn("--project-path", refused["error"])
            self.assertEqual(self.command("alpha", *remove)["results"][0]["status"], "removed")

    def test_project_path_cannot_clear_workspace_rule_history(self):
        self.add("alpha", "naming", "--project", "tools")
        before = self.command("alpha", "rules", "log")["entries"]
        self.assertEqual(len(before), 1)
        for options in ([], ["--id", "naming"]):
            error = self.command("alpha", "--project-path", self.repos["alpha", "service"],
                                 "rules", "clear-log", *options, ok=False)
            self.assertEqual(error["code"], "usage_error")
            self.assertEqual(self.command("alpha", "rules", "log")["entries"], before)

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
                       {"action": "add", "id": "x", "scope": "global", "text": "x", "skills": {"unknown": "x"}},
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
                versions = {item["id"]: item["updated_at"] for item in shown["invalid_rules"]}
                self.assertEqual(list(versions), ["broken", "garbled", "partial"])
                self.assertRegex(versions["broken"], "^invalid-[0-9a-f]{32}$")
                self.assertNotEqual(versions["broken"], versions["garbled"])
                self.assertEqual(versions["partial"], "v1")
                for item in shown["invalid_rules"]:
                    self.assertEqual(item["next"], "richi rules remove %s --expected-updated-at=%s" %
                                     (item["id"], item["updated_at"]))
                self.assertTrue(all(item["error"] for item in shown["invalid_rules"]))
        updated = self.command("alpha", "rules", "update", "kept", "--expected-updated-at", kept["updated_at"],
                               "--text", "Reply in English.")["rule"]
        self.assertEqual((updated["text"], updated["when"]), ("Reply in English.", "pull-request"))
        # A damaged row cannot be changed, only removed, and with the version the listing names.
        self.assertIn("rules remove broken --expected-updated-at=invalid",
                      self.command("alpha", "rules", "update", "broken", "--expected-updated-at", "invalid",
                                   "--text", "x", ok=False)["error"])
        self.assertEqual(self.command("alpha", "rules", "remove", "partial", "--expected-updated-at", "invalid",
                                      ok=False)["code"], "conflict")
        for identifier, version in versions.items():
            removed = self.command("alpha", "rules", "remove", identifier, "--expected-updated-at", version)
            self.assertEqual((removed["results"][0]["status"], removed["rule"]), ("removed", None))
        self.assertNotIn("invalid_rules", self.command("alpha", "rules", "list"))
        self.assertIn("{not json",
                      self.command("alpha", "rules", "log", "--id", "garbled")["entries"][0]["before"]["unreadable"])

    def test_changes_are_logged_with_who_made_them(self):
        with mock.patch.dict(os.environ, {"RICHI_CHAT_ID": "claude:chat-1"}):
            created = self.add("alpha", "names", "--global", text="First.")
        self.assertEqual((created["changed_by"], created["changed_by_agent"]), ("claude:chat-1", "claude"))
        version = ["--expected-updated-at", created["updated_at"]]
        # The map names itself, whatever chat its server was started from.
        with mock.patch.dict(os.environ, {"RICHI_CHAT_ID": "claude:chat-1", "RICHI_RULES_ACTOR": "richi map"}):
            updated = self.command("alpha", "rules", "update", "names", *version, "--text", "Second.")["rule"]
        self.assertEqual((updated["changed_by"], updated["changed_by_agent"]), ("richi map", None))
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

    def test_a_skill_for_every_agent_stays_beside_one_for_each_known_agent(self):
        def add(identifier, *skills):
            argv = ["rules", "add", identifier, "--global", "--text", "Review code."]
            for skill in skills:
                argv += ["--skill", skill]
            return self.command("alpha", *argv)["rule"]

        # The plain skill is for the agents this computer does not list; it once vanished without a word.
        beside = add("beside", "general", *(agent + "=special" for agent in agents.builtin_ids()))
        self.assertEqual((beside["skill"], beside["skills"]), ("general", dict.fromkeys(agents.builtin_ids(), "special")))
        without = add("without", "general", *(agent + "=" for agent in agents.builtin_ids()))
        self.assertEqual((without["skill"], without["skills"]), ("general", dict.fromkeys(agents.builtin_ids())))
        # The map sends the same request as one object.
        sent = {"action": "add", "id": "page", "scope": "global", "text": "Review code.", "skill": "general",
                "skills": dict.fromkeys(agents.builtin_ids(), "special")}
        page = self.command("alpha", "rules", "apply", "--json", "-", stdin=json.dumps(sent))["rule"]
        self.assertEqual((page["skill"], page["skills"]), (beside["skill"], beside["skills"]))
        changed = self.command("alpha", "rules", "update", "without", "--expected-updated-at", without["updated_at"],
                               "--skill", "other", *all_agent_skill_options("own"))["rule"]
        self.assertEqual((changed["skill"], changed["skills"]), ("other", dict.fromkeys(agents.builtin_ids(), "own")))

        def used(agent):
            shown = self.command("alpha", "rules", "show", "--agent", agent)
            return shown, {rule["id"]: rule["use_skill"] for rule in shown["rules"]}

        self.assertEqual(used("codex")[1], {"beside": "special", "page": "special", "without": "own"})
        # An agent nobody lists here gets the skill for every agent, and is told that nothing knows
        # its ID: most likely it is a misspelt one, and a known agent would then miss its own skill.
        shown, skills = used("cluade")
        self.assertEqual(skills, {"beside": "general", "page": "general", "without": "other"})
        self.assertEqual((shown["agent"], shown["agent_problem"]["known"]),
                         ("cluade", agents.describe()))
        self.assertIn("cluade", shown["agent_problem"]["error"])
        self.assertIn("--agent", shown["agent_problem"]["next"])
        self.assertNotIn("agent_problem", used("codex")[0])
        self.assertNotIn("agent_problem", self.command("alpha", "rules", "show"))
        # An ID that a rule names is not unknown: another computer or a newer Richi has that agent.
        self.stored("alpha", "rule:beside", skills=dict(beside["skills"], future="own"))
        shown, skills = used("future")
        self.assertEqual((skills["beside"], skills["page"]), ("own", "general"))
        self.assertNotIn("agent_problem", shown)
        # Without one, the same skill for every known agent is the action's skill: agents added later
        # get it too. An empty one beside per-agent skills could only mean "none for the others",
        # which a rule cannot keep apart from no skill, so it is refused rather than ignored.
        same = add("same-for-all", *(agent + "=pr" for agent in agents.builtin_ids()))
        self.assertEqual((same["skill"], same["skills"]), ("pr", None))
        refused = self.command("alpha", "rules", "add", "empty-beside", "--global", "--text", "x", "--skill", "",
                               *all_agent_skill_options("pr"), ok=False)
        self.assertIn("empty --skill", refused["error"])
        self.assertEqual(used("future")[1]["same-for-all"], "pr")

    def test_half_a_surrogate_pair_in_a_stored_row_does_not_hide_the_rules(self):
        self.add("alpha", "kept", "--global", text="Reply in Russian.")
        # A whole pair spelt as two escapes is an ordinary character.
        self.stored("alpha", "rule:kept", note="\ud83d\ude00")
        broken = self.add("alpha", "broken", "--global")
        # JSON may spell half of a pair as an escape; such text can be neither printed nor stored
        # again. Any field may hold it, also one this version does not know.
        for field in ("text", "source", "updated_at", "future"):
            with self.subTest(field=field):
                self.stored("alpha", "rule:broken", json.dumps(dict(broken, **{field: "half \ud800 pair"})))
                for view in ("show", "list"):
                    listed = self.command("alpha", "rules", view)
                    self.assertEqual([rule["id"] for rule in listed["rules"]], ["kept"])
                    self.assertEqual([item["id"] for item in listed["invalid_rules"]], ["broken"])
                    self.assertRegex(listed["invalid_rules"][0]["updated_at"], "^invalid-[0-9a-f]{32}$")
                    self.assertIn("surrogate", listed["invalid_rules"][0]["error"])
                self.assertIn("surrogate", self.command("alpha", "rules", "update", "broken", "--expected-updated-at",
                                                        "invalid", "--text", "x", ok=False)["error"])
        # The row is removed by the command it names, and the log of that removal can be read.
        words = shlex.split(listed["invalid_rules"][0]["next"])
        self.assertEqual(self.command("alpha", *words[1:])["results"], [{"id": "broken", "status": "removed"}])
        self.assertNotIn("invalid_rules", self.command("alpha", "rules", "list"))
        self.assertEqual(self.command("alpha", "rules", "log", "--id", "broken")["entries"][0]["action"], "removed")
        # A log entry written elsewhere with such text is passed over, and clearing the log removes it.
        self.stored("alpha", "rule-log:9999:broken", json.dumps({"at": "9999", "id": "broken", "by": "\ud800"}))
        self.assertEqual([entry["at"] for entry in self.command("alpha", "rules", "log")["entries"]
                          if entry["at"] == "9999"], [])
        self.command("alpha", "rules", "clear-log")
        database = self.configs["alpha"].parent / "memory.sqlite3"
        with closing(sqlite3.connect(str(database))) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM metadata WHERE key LIKE 'rule-log:%'").fetchone()[0], 0)

    def test_a_change_that_is_not_one_is_refused_in_words(self):
        self.add("alpha", "kept", "--global")
        # Each of these once ended in an error of Python instead of a refusal: a value that cannot
        # be a dictionary key, and text that is not UTF-8 on its way to SQLite or into a message.
        for change in ({"action": ["add"], "id": "x"}, {"action": {"add": 1}, "id": "x"}, {"action": 5, "id": "x"},
                       {"action": "remove", "id": "half \ud800", "expected_updated_at": "t"},
                       {"action": "remove", "id": "kept", "expected_updated_at": "half \ud800"},
                       {"action": "remove", "id": "kept", "expected_updated_at": ["t"]},
                       {"action": "update", "id": "kept", "expected_updated_at": {"t": 1}, "text": "x"},
                       {"action": "add", "id": "x", "scope": "project", "project": "half \ud800", "text": "x"},
                       {"action": "add", "id": "x", "scope": "global", "text": "half \ud800"},
                       {"action": "add", "id": "x", "scope": "global", "text": "x", "source": "half \ud800"}):
            with self.subTest(change=change):
                failed = self.command("alpha", "rules", "apply", "--json", "-", ok=False, stdin=json.dumps(change))
                for word in ("codec", "unhashable", "surrogates not allowed"):
                    self.assertNotIn(word, failed["error"])
        self.assertEqual([rule["id"] for rule in self.command("alpha", "rules", "list")["rules"]], ["kept"])

    def test_rows_other_programs_wrote_are_reported_not_fatal(self):
        kept = self.add("alpha", "kept", "--global", text="Reply in Russian.")
        database = self.configs["alpha"].parent / "memory.sqlite3"

        def row(identifier, **changes):
            return json.dumps(dict(kept, id=identifier, **changes))
        with closing(sqlite3.connect(str(database))) as conn, conn:
            # Numbers JSON cannot write back (Infinity, NaN): JSON reads them, nobody can print them.
            conn.execute("INSERT INTO metadata(key, value) VALUES ('rule:huge', ?)", (row("huge").replace(
                '"source": null', '"source": 1e999'),))
            conn.execute("INSERT INTO metadata(key, value) VALUES ('rule:nan', ?)", (row("nan").replace(
                '"source": null', '"source": NaN'),))
            # Bytes that are not UTF-8, as text and as a BLOB, in the value and in the key.
            conn.execute("INSERT INTO metadata(key, value) VALUES ('rule:latin', CAST(? AS TEXT))",
                         (json.dumps(dict(kept, id="latin", text="caf\u00e9"), ensure_ascii=False).encode("latin-1"),))
            conn.execute("INSERT INTO metadata(key, value) VALUES ('rule:blob', ?)", (b"\xff\x00 not json",))
            conn.execute("INSERT INTO metadata(key, value) VALUES (CAST(? AS TEXT), ?)", (b"rule:bad\xff", row("x")))
            # A key and a version far longer than any Richi writes.
            conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?)", ("rule:" + "k" * 600, "{}"))
            conn.execute("INSERT INTO metadata(key, value) VALUES ('rule:old', ?)",
                         (json.dumps({"id": "old", "updated_at": "v" * 600000}),))
            # Log entries alike: they are passed over, and clearing the log removes them.
            conn.execute("INSERT INTO metadata(key, value) VALUES ('rule-log:9999:latin', CAST(? AS TEXT))",
                         (b"\xff",))
            # A key of another spelling is no rule.
            conn.execute("INSERT INTO metadata(key, value) VALUES ('RULE:upper', '{}')")
        # Cut, and told apart from any other key cut the same way by its length and a hash.
        LONG_KEY = cut_name("k" * 600, rules.MAX_NAMED)
        for view in ("show", "list"):
            with self.subTest(view=view):
                listed = self.command("alpha", "rules", view)
                self.assertEqual([rule["id"] for rule in listed["rules"]], ["kept"])
                invalid = {item["id"]: item for item in listed["invalid_rules"]}
                self.assertEqual(sorted(invalid), sorted(["huge", "nan", "latin", "blob", "bad\\xff", "old",
                                                          LONG_KEY]))
                self.assertLess(len(json.dumps(listed)), 20000)
                self.assertIn("not finite", invalid["nan"]["error"])
                self.assertIn("not UTF-8", invalid["latin"]["error"])
                self.assertRegex(invalid["old"]["updated_at"], "^invalid-[0-9a-f]{32}$")
                self.assertNotIn("next", invalid[LONG_KEY])
        # The log is read past an entry that is not text.
        self.assertEqual([entry["id"] for entry in self.command("alpha", "rules", "log")["entries"]], ["kept"])
        # Each row Richi can name is removed by the command it names, as a shell would split it.
        for item in listed["invalid_rules"]:
            if "next" in item:
                words = shlex.split(item["next"])
                self.assertEqual(self.command("alpha", *words[1:])["results"], [{"id": item["id"], "status": "removed"}])
        self.assertEqual([item["id"] for item in self.command("alpha", "rules", "list")["invalid_rules"]],
                         [LONG_KEY])
        self.command("alpha", "rules", "clear-log")
        with closing(sqlite3.connect(str(database))) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM metadata WHERE key LIKE 'rule-log:%'").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM metadata WHERE key = 'RULE:upper'").fetchone()[0], 1)

    def test_text_a_shell_could_not_decode_is_kept_out_of_the_store(self):
        # A byte the shell could not decode arrives as half of a surrogate pair.
        with mock.patch.dict(os.environ, {"RICHI_RULES_ACTOR": "map\udcff"}):
            added = self.add("alpha", "actor", "--global")
        self.assertEqual(added["changed_by"], "map")
        for view in ("show", "list"):
            failed = self.command("alpha", "rules", view, "--project", "x\udcff", ok=False)
            self.assertEqual(failed["code"], "not_found")
            self.assertNotIn("codec", failed["error"])

    def test_a_change_that_is_json_null_is_refused_without_reading_again(self):
        # Once read for null, a second read inside the write transaction found nothing on a pipe and
        # would have waited for a terminal while the store was locked.
        failed = self.command("alpha", "rules", "apply", "--json", "-", stdin="null", ok=False)
        self.assertIn("A rule change is an object", failed["error"])
        expected = self.command("alpha", "rules", "apply", "--json", "-", ok=False, stdin=json.dumps(
            {"action": "remove", "id": self.add("alpha", "typed", "--global")["id"], "expected_updated_at": 5}))
        self.assertNotIn("code", expected)
        self.assertIn("expected_updated_at", expected["error"])

    def test_list_and_log_say_what_they_leave_out(self):
        self.add("alpha", "global", "--global", text="a" * 10)
        self.add("alpha", "service", "--project", "service", text="b" * 20)
        # The size is the workspace's, beside the workspace's limit, whatever the list shows.
        for scope in ([], ["--global"], ["--project", "service"]):
            listed = self.command("alpha", "rules", "list", *scope)
            self.assertEqual((listed["characters"], listed["limit_characters"]), (30, rules.MAX_TOTAL))
        self.assertEqual(len(self.command("alpha", "rules", "list", "--global")["rules"]), 1)
        log = self.command("alpha", "rules", "log", "--entries", "1")
        self.assertEqual((len(log["entries"]), log["more"]), (1, True))
        self.assertFalse(self.command("alpha", "rules", "log", "--entries", "2")["more"])
        self.assertFalse(self.command("alpha", "rules", "log", "--id", "global")["more"])
        # A project nobody registered is an error, not an empty list.
        self.assertEqual(self.command("alpha", "rules", "show", "--project", "missing", ok=False)["code"], "not_found")
        self.assertEqual(self.command("alpha", "rules", "show", "--project", "", ok=False)["code"], "not_found")
        self.assertEqual(self.command("alpha", "rules", "list", "--project", "missing", ok=False)["code"], "not_found")

    def test_an_update_or_removal_needs_the_version(self):
        rule = self.add("alpha", "versioned", "--global")
        for change in ({"action": "update", "id": "versioned", "text": "y"}, {"action": "remove", "id": "versioned"}):
            with self.subTest(action=change["action"]):
                failed = self.command("alpha", "rules", "apply", "--json", "-", stdin=json.dumps(change), ok=False)
                self.assertEqual(failed["code"], "conflict")
        self.assertEqual(self.command("alpha", "rules", "list")["rules"][0]["text"], rule["text"])

    def test_a_complete_row_with_one_wrong_field_is_not_served(self):
        good = self.add("alpha", "good", "--global")
        # Each row is complete but for one field; no damaged row in the other tests reached these checks.
        broken = {"other-id": {"id": "someone-else"}, "text": {"text": 5}, "version": {"updated_at": 5},
                  "long-text": {"text": "x" * (rules.MAX_TEXT + 1)}, "empty-text": {"text": "  "},
                  "nul-text": {"text": "a\x00b"}, "long-version": {"updated_at": "v" * (rules.MAX_VERSION + 1)},
                  "empty-version": {"updated_at": ""}, "bad-created": {"created_at": []},
                  "skill": {"skill": "two words"}, "skills": {"skills": {"codex": "two words"}},
                  "agent": {"skills": {"Codex": "x"}}, "empty-skills": {"skills": {}},
                  "requires-0": {"requires": 0}, "requires-true": {"requires": True},
                  "global-project": {"project_id": "service"}}
        for name, changes in broken.items():
            self.stored("alpha", "rule:" + name, json.dumps({**good, "id": name, **changes}))
        self.stored("alpha", "rule:two words", json.dumps({**good, "id": "two words"}))
        self.stored("alpha", "rule:no-created", json.dumps({key: value for key, value in good.items()
                                                            if key != "created_at"} | {"id": "no-created"}))
        listed = self.command("alpha", "rules", "list")
        self.assertEqual([rule["id"] for rule in listed["rules"]], ["good"])
        self.assertEqual(sorted(item["id"] for item in listed["invalid_rules"]),
                         sorted([*broken, "two words", "no-created"]))

    def test_clearing_one_rules_log_keeps_entries_nobody_can_read(self):
        self.add("alpha", "cleared", "--global")
        self.stored("alpha", "rule-log:0000:unreadable", "{not json")
        self.assertEqual(self.command("alpha", "rules", "clear-log", "--id", "cleared")["results"][0]["entries"], 1)
        database = self.configs["alpha"].parent / "memory.sqlite3"
        with closing(sqlite3.connect(str(database))) as conn:
            self.assertEqual(conn.execute("SELECT key FROM metadata WHERE key LIKE 'rule-log:%'").fetchall(),
                             [("rule-log:0000:unreadable",)])

    def test_unusable_agent_settings_are_named_where_rules_need_agents(self):
        settings = agents.settings_path()
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text("{not json", encoding="utf-8")
        agents.forget_settings()
        self.addCleanup(agents.forget_settings)
        for view in ("show", "list"):
            problem = self.command("alpha", "rules", view)["agent_settings_problem"]
            self.assertIn(str(settings), problem["error"])
            self.assertEqual(problem["code"], "damaged")
            # What really stops: the agent still gets its skills when it names itself.
            self.assertIn("fix or remove the file", problem["next"])
            self.assertIn("rules show --agent ID", problem["next"])
        failed = self.command("alpha", "rules", "add", "own", "--global", "--text", "x", "--skill", "gemini-cli=r",
                              ok=False)
        self.assertIn("Unknown agent in skills: gemini-cli", failed["error"])
        self.assertIn(str(settings), failed["error"])
        # A change says it too: what it stored may differ from what it would store with the settings.
        added = self.command("alpha", "rules", "add", "while-damaged", "--global", "--text", "x")
        self.assertEqual(added["agent_settings_problem"]["code"], "damaged")
        settings.unlink()
        self.assertNotIn("agent_settings_problem", self.command("alpha", "rules", "show"))
        # An own agent and a rule that names it; then a newer Richi rewrites the agent's settings.
        own = self.command("alpha", "skill-sources", "add", "gemini-cli", "--name", "Gemini CLI", "--skills",
                           "~/.gemini/skills")["agent"]
        self.add("alpha", "named", "--global", skill="gemini-cli=gem")
        stored = json.loads(settings.read_text(encoding="utf-8"))
        stored["agents"]["gemini-cli"]["manual_skills"] = "openai-yaml"
        settings.write_text(json.dumps(stored), encoding="utf-8")
        agents.forget_settings()
        shown = self.command("alpha", "rules", "show", "--agent", "gemini-cli")
        self.assertEqual((shown["agent_settings_problem"]["code"], "gemini-cli" in shown["agent_settings_problem"]["error"]),
                         ("agents", True))
        # Rules that name it still serve it, and the ID is no misspelling.
        self.assertEqual(([rule["use_skill"] for rule in shown["rules"] if rule["id"] == "named"],
                          "agent_problem" in shown), (["gem"], False))
        self.assertEqual(own["id"], "gemini-cli")
        # Settings a newer Richi wrote as a whole: nobody is told to remove them.
        settings.write_text(json.dumps({"version": agents.SETTINGS_VERSION + 1, "agents": {}}), encoding="utf-8")
        agents.forget_settings()
        problem = self.command("alpha", "rules", "show")["agent_settings_problem"]
        self.assertEqual((problem["code"], "richi update" in problem["next"], "remove" in problem["error"] + problem["next"]),
                         ("newer", True, False))

    def test_a_store_that_another_command_holds_is_reported_as_busy(self):
        database = self.configs["alpha"].parent / "memory.sqlite3"
        # The command waits for the lock as long as its timeout says; here that is a moment.
        impatient = functools.partial(memory.connect, timeout=0.2)
        with closing(sqlite3.connect(str(database), isolation_level=None)) as holder:
            holder.execute("BEGIN IMMEDIATE")
            with mock.patch.object(memory, "connect", impatient):
                failed = self.command("alpha", "rules", "add", "late", "--global", "--text", "x", ok=False)
            holder.execute("ROLLBACK")
        # A code of its own: the command was right, and trying again helps.
        self.assertEqual(failed, {"error": "database is locked", "code": "busy"})
        self.add("alpha", "late", "--global")
        # Only SQLite's busy is; other errors that mention a lock are not.
        for message in ("database table is locked", "no such table: is locked", "database is locked (5)"):
            self.assertFalse(memory._store_busy(sqlite3.OperationalError(message)), message)
        self.assertFalse(memory._store_busy(ValueError("database is locked")))

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
        # zsh would run a word that begins with "=" as the path of that command.
        self.stored("alpha", "rule:=ls", "[2]")
        for view in ("show", "list"):
            with self.subTest(view=view):
                listed = self.command("alpha", "rules", view)
                self.assertEqual([rule["id"] for rule in listed["rules"]], ["first"])
                versions = {item["id"]: item["updated_at"] for item in listed["invalid_rules"]}
                self.assertEqual({item["id"]: item["next"] for item in listed["invalid_rules"]}, {
                    "": "richi rules remove '' --expected-updated-at=" + versions[""],
                    "-dash": "richi rules remove --expected-updated-at=" + versions["-dash"] + " -- -dash",
                    "=ls": "richi rules remove '=ls' --expected-updated-at=" + versions["=ls"],
                    "second": "richi rules remove second --expected-updated-at=" + versions["second"],
                    "third": "richi rules remove third --expected-updated-at=" + versions["third"],
                    "two words": "richi rules remove 'two words' --expected-updated-at=" + versions["two words"]})
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

    def test_corrupt_keys_that_share_a_display_name_cannot_remove_each_other(self):
        database = self.configs["alpha"].parent / "memory.sqlite3"
        spelling = r"letter\xff"
        with closing(sqlite3.connect(str(database))) as conn, conn:
            for key in (b"rule:letter\xff", ("rule:" + spelling).encode(), b"rule:a\x00b"):
                conn.execute("INSERT INTO metadata(key, value) VALUES (CAST(? AS TEXT), '{}')", (key,))
        invalid = self.command("alpha", "rules", "list")["invalid_rules"]
        self.assertCountEqual([item["id"] for item in invalid], [spelling, spelling, "a\x00b"])
        self.assertTrue(all("next" not in item for item in invalid))
        self.command("alpha", "rules", "remove", spelling, "--expected-updated-at", "invalid", ok=False)
        self.assertEqual(self.command("alpha", "rules", "list")["invalid_rules"], invalid)

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
                                   "updated_at": paused["updated_at"], "characters": len("Use the old flow."),
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

    def test_a_newer_format_counts_toward_the_workspace_limit(self):
        paused = self.add("alpha", "paused", "--global", text="p" * 1990)
        self.stored("alpha", "rule:paused", requires=rules.FORMAT + 1)
        listed = self.command("alpha", "rules", "list")
        self.assertEqual((listed["unsupported_rules"][0]["characters"], listed["characters"]), (1990, 1990))
        # This version does not serve it, but the version that does would find the workspace over its limit.
        with mock.patch.object(rules, "MAX_TOTAL", 2000):
            failed = self.command("alpha", "rules", "add", "more", "--global", "--text", "x" * 11, ok=False)
            self.assertEqual((failed["code"], failed["subject"]), ("limit", "rules"))
            fits = self.add("alpha", "fits", "--global", text="x" * 10)
        # Over the limit already (a newer Richi may allow more): what does not make the rules longer is saved,
        # also the shortening the refusal asks for; what makes them longer is not.
        with mock.patch.object(rules, "MAX_TOTAL", 1995):
            for options in (["--skill", "review"], ["--text", "x" * 8], ["--text", "y" * 8]):
                with self.subTest(options=options):
                    fits = self.command("alpha", "rules", "update", "fits", "--expected-updated-at", fits["updated_at"],
                                        *options)["rule"]
            longer = self.command("alpha", "rules", "update", "fits", "--expected-updated-at", fits["updated_at"],
                                  "--text", "y" * 9, ok=False)
            self.assertEqual((longer["code"], longer["subject"]), ("limit", "rules"))
            self.command("alpha", "rules", "add", "another", "--global", "--text", "z", ok=False)
        self.assertEqual(paused["text"], "p" * 1990)

    def test_removing_a_newer_format_keeps_the_original_record_in_the_audit(self):
        rule = self.add("alpha", "future", "--global")
        original = {**rule, "requires": rules.FORMAT + 1, "future": {"notes": "x" * 6000}}
        self.stored("alpha", "rule:future", json.dumps(original))
        self.command("alpha", "rules", "remove", "future", "--expected-updated-at", rule["updated_at"])
        entry = self.command("alpha", "rules", "log", "--id", "future", "--entries", "1")["entries"][0]
        self.assertEqual(entry["action"], "removed")
        self.assertEqual(entry["before"], original)
        self.assertIsNone(entry["after"])

    def test_rows_another_program_wrote_are_reported_within_bounds(self):
        database = self.configs["alpha"].parent / "memory.sqlite3"
        good = self.add("alpha", "good", "--global")
        with closing(sqlite3.connect(str(database))) as conn, conn:
            # SQLite sorts a key stored as bytes after every text key: such a row was once neither
            # served nor reported, and a rule of the same name could be added beside it.
            conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?)",
                         (b"rule:dup", json.dumps({**good, "id": "dup"})))
            # A key of a newer format's row is not repeated in full either.
            conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?)",
                         ("rule:" + "k" * 300, json.dumps({**good, "id": "k" * 300, "requires": rules.FORMAT + 1})))
            # Larger than Richi writes: not parsed for its version, which older Pythons do in quadratic time.
            conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?)",
                         ("rule:big", json.dumps({**good, "id": "big", "text": "x" * rules.MAX_STORED})))
            conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?)",
                         ("rule-log:9999:big", json.dumps({"id": "big", "at": "9999", "action": "updated",
                                                            "before": "x" * (3 * rules.MAX_STORED)})))
        listed = self.command("alpha", "rules", "list")
        self.assertEqual([rule["id"] for rule in listed["rules"]], ["good"])
        invalid = {item["id"]: item for item in listed["invalid_rules"]}
        self.assertEqual(sorted(invalid), ["big", "dup"])
        # No command can name the bytes key apart from a text one.
        self.assertEqual(("next" in invalid["dup"], "stored as bytes" in invalid["dup"]["error"],
                          "SQLite tool" in invalid["dup"]["error"]), (False, True, True))
        self.assertRegex(invalid["big"]["updated_at"], "^invalid-[0-9a-f]{32}$")
        newer = listed["unsupported_rules"]
        self.assertEqual([(item["id"], item["characters"]) for item in newer],
                         [(cut_name("k" * 300, rules.MAX_NAMED), len(good["text"]))])
        self.assertTrue(newer[0]["id"].startswith("k" * rules.MAX_NAMED + "… (300 characters, sha256 "))
        twin = self.add("alpha", "dup", "--global")
        self.command("alpha", "rules", "remove", "dup", "--expected-updated-at", twin["updated_at"])
        self.assertEqual([item["id"] for item in self.command("alpha", "rules", "list")["invalid_rules"]],
                         ["big", "dup"])
        self.command("alpha", *shlex.split(invalid["big"]["next"])[1:])
        # The log row larger than any entry Richi writes is passed over, and only clearing everything removes it.
        self.assertEqual([entry["action"] for entry in self.command("alpha", "rules", "log", "--id", "big")["entries"]],
                         ["removed"])
        self.command("alpha", "rules", "clear-log", "--id", "big")
        with closing(sqlite3.connect(str(database))) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM metadata WHERE key = 'rule-log:9999:big'").fetchone(), (1,))
        self.command("alpha", "rules", "clear-log")
        with closing(sqlite3.connect(str(database))) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM metadata WHERE key LIKE 'rule-log:%'").fetchone(), (0,))

    def test_line_ends_are_stored_one_way(self):
        piped = self.command("alpha", "rules", "add", "piped", "--global", "--text", "-",
                             stdin="Line one.\r\nLine two.\r\n")["rule"]
        self.assertEqual(piped["text"], "Line one.\nLine two.")
        self.assertEqual(self.add("alpha", "old-mac", "--global", text="One.\rTwo.")["text"], "One.\nTwo.")
        # A page's text field gives the lines with \n: the same rule, nothing to save.
        unchanged = self.command("alpha", "rules", "update", "piped", "--expected-updated-at", piped["updated_at"],
                                 "--text", "Line one.\r\nLine two.")
        self.assertEqual(unchanged["results"][0]["status"], "unchanged")

    def test_the_same_skill_of_every_agent_is_merged_only_while_every_agent_is_known(self):
        same = all_agent_skill_options("rev")

        def skills(rule):
            return rule["skill"], rule["skills"]
        # Every agent this computer has gets one skill: the skill of the action, for every agent.
        merged = self.command("alpha", "rules", "add", "merged", "--global", "--text", "x", *same)
        self.assertEqual((skills(merged["rule"]), "agent_settings_problem" in merged), (("rev", None), False))
        # Not while the agent settings cannot be used: the user may have agents Richi does not know now,
        # and they would get the skill once the settings are fixed.
        settings = agents.settings_path()
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text("{not json", encoding="utf-8")
        agents.forget_settings()
        self.addCleanup(agents.forget_settings)
        kept = self.command("alpha", "rules", "add", "kept-apart", "--global", "--text", "x", *same)
        self.assertEqual((skills(kept["rule"]), kept["agent_settings_problem"]["code"]),
                         ((None, dict.fromkeys(agents.builtin_ids(), "rev")), "damaged"))
        settings.unlink()
        agents.forget_settings()
        # The rule names an own agent, which the user then removes from this computer.
        own = self.command("alpha", "skill-sources", "add", "gemini-cli", "--name", "Gemini CLI", "--skills",
                           "~/.gemini/skills")["agent"]
        three = self.add("alpha", "three", "--global", skill="gemini-cli=gem")
        four = self.add("alpha", "four", "--global", skill="gemini-cli=gem")
        self.command("alpha", "skill-sources", "remove", "gemini-cli", "--expected-updated-at", own["updated_at"])
        # The same skill for the agents left is the skill of the action; the one the rule names keeps its none.
        three = self.command("alpha", "rules", "update", "three", "--expected-updated-at", three["updated_at"], *same,
                             "--skill", "gemini-cli=")["rule"]
        self.assertEqual(skills(three), ("rev", {"gemini-cli": None}))
        # Its own skill alike.
        four = self.command("alpha", "rules", "update", "four", "--expected-updated-at", four["updated_at"], *same,
                            "--skill", "gemini-cli=gem")["rule"]
        self.assertEqual(skills(four), ("rev", {"gemini-cli": "gem"}))
        # An agent added later gets the skill of the action; the agent the rule names, once back, its own choice.
        self.command("alpha", "skill-sources", "add", "later", "--name", "Later", "--skills", "~/later/skills")
        self.command("alpha", "skill-sources", "add", "gemini-cli", "--name", "Gemini CLI", "--skills", "~/.gemini/skills")
        for agent, wanted in (("later", {"three": "rev", "four": "rev"}), ("gemini-cli", {"three": None, "four": "gem"})):
            shown = self.command("alpha", "rules", "show", "--agent", agent)
            self.assertEqual({rule["id"]: rule["use_skill"] for rule in shown["rules"] if rule["id"] in wanted}, wanted)

    def test_a_builtin_agents_unusable_settings_do_not_stop_the_skill_of_the_action(self):
        # A newer Richi stored something about Claude Code this version cannot read: Claude Code stays known.
        settings = agents.settings_path()
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({"version": agents.SETTINGS_VERSION, "agents": {"claude": {
            "updated_at": "t", "folders": [{"path": "~/x", "enabled": True, "label": "team"}]}}}), encoding="utf-8")
        agents.forget_settings()
        self.addCleanup(agents.forget_settings)
        added = self.command("alpha", "rules", "add", "pr", "--global", "--text", "x", *all_agent_skill_options("review"))
        self.assertEqual(((added["rule"]["skill"], added["rule"]["skills"]), "agent_settings_problem" in added),
                         (("review", None), False))
        self.assertEqual([item["id"] for item in self.command("alpha", "skill-sources", "list")["unknown_agents"]],
                         ["claude"])
        # Settings of an own agent this version cannot use still stop it, and are named, a few at most.
        stored = json.loads(settings.read_text(encoding="utf-8"))
        stored["agents"].update({"own-%02d" % number: {"name": "Own", "updated_at": "t", "manual_skills": "openai-yaml"}
                                 for number in range(8)})
        # Entries without a name adjust an agent built into a newer Richi: not an own agent, never named as one.
        stored["agents"]["x" * 5000] = {"updated_at": "t"}
        stored["agents"]["gemini-cli"] = {"updated_at": "t", "folders": [{"path": "~/g", "enabled": True, "label": "x"}]}
        settings.write_text(json.dumps(stored), encoding="utf-8")
        agents.forget_settings()
        added = self.command("alpha", "rules", "add", "pr2", "--global", "--text", "x", *all_agent_skill_options("review"))
        problem = added["agent_settings_problem"]
        self.assertEqual(((added["rule"]["skill"], added["rule"]["skills"]), problem["code"]),
                         ((None, dict.fromkeys(agents.builtin_ids(), "review")), "agents"))
        self.assertIn("own-00, own-01, own-02, own-03, own-04 and 3 more", problem["error"])
        self.assertNotIn("claude", problem["error"])
        self.assertNotIn("gemini-cli", problem["error"])
        # Without the own agents, a newer Richi's built-in agent alone does not stop the skill of the action.
        for number in range(8):
            del stored["agents"]["own-%02d" % number]
        settings.write_text(json.dumps(stored), encoding="utf-8")
        agents.forget_settings()
        added = self.command("alpha", "rules", "add", "pr3", "--global", "--text", "x", *all_agent_skill_options("review"))
        self.assertEqual(((added["rule"]["skill"], added["rule"]["skills"]), "agent_settings_problem" in added),
                         (("review", None), False))

    def test_a_change_goes_by_one_reading_of_the_agent_settings(self):
        # Another window may change the agent settings while a rule is saved: what the change stores and what
        # its answer says of them come from the same reading.
        database = self.configs["alpha"].parent / "memory.sqlite3"
        readings = [agents.Settings({}, {"gem": ({"name": "Gem", "updated_at": "t"}, "written by a newer Richi")}, {})]

        def read_settings(*_args, **_kwargs):
            return readings.pop(0) if readings else agents.Settings({}, {}, {})
        with mock.patch.object(agents, "read_settings", side_effect=read_settings), \
                closing(memory.connect(database)) as conn, memory.transaction(conn):
            answer = rules.change(conn, {"action": "add", "id": "pr", "scope": "global", "text": "x",
                                         "skills": dict.fromkeys(agents.builtin_ids(), "review")}, memory)
        self.assertEqual(((answer["rule"]["skill"], answer["rule"]["skills"]), answer["agent_settings_problem"]["code"]),
                         ((None, dict.fromkeys(agents.builtin_ids(), "review")), "agents"))
        self.assertLess(len(json.dumps(self.command("alpha", "rules", "show"))), 4000)

    def test_each_rule_command_uses_one_agent_configuration(self):
        self.command("alpha", "skill-sources", "add", "gem", "--name", "Gem", "--skills", "~/.gem/skills",
                     "--variable", "GEM_TEST=1")
        settings = agents.read_settings()
        with mock.patch.dict(os.environ, {"GEM_TEST": "1"}):
            for args in (["rules", "add", "pr", "--global", "--text", "x", "--skill", "gem=review"],
                         ["rules", "show"], ["rules", "list"]):
                with self.subTest(args=args), mock.patch.object(agents, "read_settings", side_effect=[settings]):
                    answer = self.command("alpha", *args)
                    if args[1] == "show":
                        self.assertEqual((answer["agent"], answer["rules"][0]["use_skill"]), ("gem", "review"))
                    elif args[1] == "list":
                        self.assertIn({"id": "gem", "name": "Gem"}, answer["agents"])
                    else:
                        self.assertEqual(answer["rule"]["changed_by_agent"], "gem")

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
        self.assertEqual((refused["code"], refused["subject"]), ("limit", "record"))
        self.assertEqual([rule["id"] for rule in self.command("alpha", "rules", "list")["rules"]], ["kept"])

    def test_a_change_names_the_author_it_is_given(self):
        database = self.configs["alpha"].parent / "memory.sqlite3"
        # change() reads nothing from the environment: a command resolves the author once and passes it.
        with mock.patch.dict(os.environ, {"RICHI_CHAT_ID": "claude:ambient", "CLAUDECODE": "1"}), \
                closing(memory.connect(database)) as conn, memory.transaction(conn):
            unnamed = rules.change(conn, {"action": "add", "id": "unnamed", "scope": "global", "text": "x"}, memory)
            # As the launcher starts a Codex chat: the identity is the thread ID itself.
            named = rules.change(conn, {"action": "add", "id": "named", "scope": "global", "text": "x"}, memory,
                                 author=rules.Author("019a1b2c-thread", "codex"))
            self.assertEqual(rules.current_author(), rules.Author("claude:ambient", "claude"))
        self.assertEqual([(change["rule"]["changed_by"], change["rule"]["changed_by_agent"]) for change in (unnamed, named)],
                         [(None, None), ("019a1b2c-thread", "codex")])
        self.assertEqual(rules.current_author({"RICHI_CHAT_ID": "019a1b2c-thread", "CODEX_THREAD_ID": "019a1b2c-thread"}),
                         rules.Author("019a1b2c-thread", "codex"))
        self.assertEqual(rules.current_author({"RICHI_RULES_ACTOR": "richi map", "CLAUDECODE": "1"}),
                         rules.Author("richi map", None))
        entries = self.command("alpha", "rules", "log")["entries"]
        self.assertEqual([(entry["id"], entry["by"], entry["agent"]) for entry in entries],
                         [("named", "019a1b2c-thread", "codex"), ("unnamed", None, None)])

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
            expected = ["richi rules show", "richi --project-path PATH rules show"]
            self.assertEqual(bound["next_commands"], expected)
            current = chats.current_chat("chat")
            self.assertEqual(current["next_commands"], expected)

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

    def test_legacy_managed_instructions_stay_as_released(self):
        # The previous launcher keeps these bytes during update and expects them
        # when rolling back. The modern block is installed after that window.
        for agent, digest in (("codex", "a908ceb314de9bdbd7c5c22366957d76862553701baccec9a5c6ae86cca8153c"),
                              ("claude", "6ef11912428f7f4356b598d7a9cd74dad3e30fdd02ac6d2888030f0903a7bc79")):
            with self.subTest(agent=agent):
                block = agent_setup._block("/opt/richi/bin/richi", agent, agent_setup.LEGACY_NAME)
                self.assertEqual(hashlib.sha256(block).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
