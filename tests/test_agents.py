from contextlib import closing, redirect_stderr, redirect_stdout
import errno
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

from richi import agent_commands, agents, memory, private_files, rules, skill_catalog
from richi_launcher import config


def write_skill(directory, header):
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(header, encoding="utf-8")


def synthetic_agent_sources(environ, home):
    return [agents.SkillSource(key="skills", kind="skills", layout="skills", path=home / ".synthetic" / "skills")]


# An additional agent, described only by a registry entry, as a new integration would be.
SYNTHETIC = agents.Agent(id="synthetic", name="Synthetic Agent", variables={"SYNTHETIC_SHELL": "1"},
                         invocation="none", plugin_commands=False, sources=synthetic_agent_sources)


class IsolatedHome(unittest.TestCase):
    """A disposable home: agent settings are the user's, so tests must never read the real ones."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-agents-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        environment = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        for key in [key for key in os.environ if key.startswith("RICHI_")
                    or key in {"CODEX_THREAD_ID", "CLAUDECODE", "CURSOR_AGENT",
                               "ANTIGRAVITY_AGENT", "ANTIGRAVITY_CONVERSATION_ID",
                               "ANTIGRAVITY_APP_DATA_DIR",
                               "CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_CONFIG_HOME"}]:
            del os.environ[key]
        agents.forget_settings()
        self.addCleanup(agents.forget_settings)

    def cli(self, *argv, ok=True, config_file=None):
        out, err = io.StringIO(), io.StringIO()
        selectors = ["--config", str(config_file)] if config_file else []
        with redirect_stdout(out), redirect_stderr(err), mock.patch.object(sys, "stdin", io.StringIO("")):
            result = memory.main([*selectors, *map(str, argv)])
        self.assertEqual(result == 0, ok, out.getvalue() + err.getvalue())
        return self.answer(out.getvalue() if ok else err.getvalue())

    def answer(self, text):
        """The command's JSON; anything else in the stream, such as a warning, is shown as it is."""
        try:
            return json.loads(text)
        except ValueError:
            self.fail("The command printed something other than one JSON value: " + repr(text))

    def listed(self):
        return {agent["id"]: agent for agent in self.cli("skill-sources", "list")["agents"]}


class RegistryTests(IsolatedHome):
    def test_entries_are_unique_and_well_formed(self):
        self.assertEqual(agents.ids(), agents.builtin_ids())
        self.assertEqual(len(set(agents.ids())), len(agents.AGENTS))
        for agent in agents.AGENTS:
            with self.subTest(agent=agent.id):
                self.assertTrue(agents.valid_id(agent.id))
                self.assertIn(agent.invocation, {"front-matter", "openai-yaml", "none"})
                sources = agent.sources({}, Path("/home/example"))
                self.assertEqual(len({source.key for source in sources}), len(sources))
                for source in sources:
                    self.assertRegex(source.key, r"\A[a-z][a-z0-9-]*\Z")
                    self.assertIn(source.kind, skill_catalog.KINDS)
                    self.assertIn(source.layout, {"skills", "nested-skills", "cache", "claude-app", "antigravity-plugins"})
        self.assertEqual(agents.describe(), [{"id": "claude", "name": "Claude Code"}, {"id": "codex", "name": "Codex"},
                                             {"id": "cursor", "name": "Cursor"},
                                             {"id": "antigravity", "name": "Antigravity"}])
        for identifier, name, folder in agents.PRESETS:
            with self.subTest(preset=identifier):
                self.assertTrue(agents.valid_id(identifier) and agents.valid_name(name) and agents.valid_folder(folder))
                self.assertNotIn(identifier, agents.builtin_ids())
        self.assertEqual(len({preset[0] for preset in agents.PRESETS}), len(agents.PRESETS))

    def test_detection_names_an_agent_only_when_every_sign_agrees(self):
        self.assertIsNone(agents.detect({}))
        for environment, agent in (({"CLAUDECODE": "1"}, "claude"), ({"CODEX_THREAD_ID": "thread"}, "codex"),
                                   ({"RICHI_CHAT_ID": "claude:chat"}, "claude"),
                                   ({"RICHI_CHAT_ID": "cursor:chat"}, "cursor"), ({"CURSOR_AGENT": "1"}, "cursor"),
                                   ({"RICHI_CHAT_ID": "antigravity:chat"}, "antigravity"),
                                   ({"ANTIGRAVITY_AGENT": "1"}, "antigravity"),
                                   ({"RICHI_CHAT_ID": "antigravity:chat", "ANTIGRAVITY_AGENT": "1"}, "antigravity"),
                                   # Codex's chat identity is its thread ID: it names no agent, the variable does.
                                   ({"RICHI_CHAT_ID": "019a1b2c-thread", "CODEX_THREAD_ID": "019a1b2c-thread"}, "codex"),
                                   # A chat identity that names no agent says nothing either way.
                                   ({"RICHI_CHAT_ID": "terminal:chat", "CLAUDECODE": "1"}, "claude")):
            with self.subTest(environment=environment):
                self.assertEqual(agents.detect(environment), agent)
        # One agent started from another's terminal inherits its variables and chat identity. In
        # either direction the signs disagree, and Richi names neither agent.
        for environment in ({"CLAUDECODE": "1", "CODEX_THREAD_ID": "thread"},
                            {"RICHI_CHAT_ID": "claude:chat", "CODEX_THREAD_ID": "thread"},
                            {"RICHI_CHAT_ID": "cursor:chat", "CODEX_THREAD_ID": "thread"},
                            {"RICHI_CHAT_ID": "antigravity:chat", "CODEX_THREAD_ID": "thread"},
                            {"RICHI_CHAT_ID": "antigravity:chat", "CLAUDECODE": "1"},
                            {"RICHI_CHAT_ID": "antigravity:chat", "CURSOR_AGENT": "1"},
                            {"RICHI_CHAT_ID": "claude:chat", "ANTIGRAVITY_AGENT": "1"},
                            {"ANTIGRAVITY_AGENT": "1", "CLAUDECODE": "1"},
                            {"ANTIGRAVITY_AGENT": "1", "CURSOR_AGENT": "1"},
                            {"ANTIGRAVITY_AGENT": ""},
                            {"CURSOR_AGENT": "1", "CLAUDECODE": "1"}, {"CURSOR_AGENT": ""},
                            {"RICHI_CHAT_ID": "codex:chat", "CLAUDECODE": "1"},
                            {"RICHI_CHAT_ID": "claude:chat", "CLAUDECODE": "1", "CODEX_THREAD_ID": "thread"},
                            {"RICHI_CHAT_ID": "claude", "CLAUDECODE": "0"}, {"RICHI_CHAT_ID": "unknown:chat"},
                            {"CODEX_THREAD_ID": ""}):
            with self.subTest(environment=environment):
                self.assertIsNone(agents.detect(environment))
        self.assertEqual(agents.variables(agents.AGENTS[0]), ["CLAUDECODE=1"])
        self.assertEqual(agents.variables(agents.AGENTS[1]), ["CODEX_THREAD_ID"])
        self.assertEqual(agents.variables(agents.AGENTS[3]), ["ANTIGRAVITY_AGENT"])
        for value in ("Claude", "-x", "a" * 33, "", None, "with space"):
            self.assertFalse(agents.valid_id(value))

    def test_every_agent_the_launcher_connects_is_built_in(self):
        # An agent `richi setup` connects but the registry lacks would have a connection nobody sees.
        # A built-in agent without an integration is fine: it is listed without a connection state.
        from richi_launcher import agent_adapters, chats
        self.assertLessEqual(set(agent_adapters.AGENTS), set(agents.builtin_ids()))
        known_shell_vars = set(chats.CHAT_ID_VARS) | set(chats.AGENT_SHELL_VARS)
        for agent in agents.AGENTS:
            self.assertLessEqual(set(agent.variables), known_shell_vars, agent.id)

    def test_settings_live_in_the_launchers_configuration_directory(self):
        self.assertEqual(agents.settings_path(), config._platform_dirs()[0] / agents.SETTINGS_FILE)
        self.assertEqual(agents.settings_path({}, self.home).name, "agents.json")
        (self.root / "real").mkdir()
        (self.root / "link").symlink_to(self.root / "real")
        with mock.patch.object(agents.sys, "platform", "linux"), mock.patch.object(config.sys, "platform", "linux"):
            # A link or ".." in the configured directory names the same place for both.
            for configured in (None, str(self.root / "xdg"), "relative", str(self.root / "link"),
                               str(self.root / "real" / ".." / "other")):
                environment = {} if configured is None else {"XDG_CONFIG_HOME": configured}
                with self.subTest(configured=configured), mock.patch.dict(os.environ, environment):
                    self.assertEqual(agents.settings_path(), config._platform_dirs()[0] / agents.SETTINGS_FILE)


class AnotherAgentTests(IsolatedHome):
    """Supporting another built-in agent takes one registry entry and nothing else."""

    def setUp(self):
        super().setUp()
        registry = mock.patch.object(agents, "AGENTS", agents.AGENTS + (SYNTHETIC,))
        registry.start()
        self.addCleanup(registry.stop)
        self.config = self.root / "store" / "config.json"
        self.config.parent.mkdir()
        self.config.write_text(json.dumps({"database": "memory.sqlite3"}))
        self.command("init")

    def command(self, *argv, ok=True):
        return self.cli(*argv, ok=ok, config_file=self.config)

    def add(self, identifier, *skills, ok=True):
        argv = ["rules", "add", identifier, "--global", "--text", "Review code."]
        for skill in skills:
            argv += ["--skill", skill]
        return self.command(*argv, ok=ok)

    def test_rules_name_skills_for_a_registered_agent(self):
        rule = self.add("review", "claude=engineering:code-review", "codex=code-review", "synthetic=reviewer")["rule"]
        self.assertEqual(rule["skills"], {"claude": "engineering:code-review", "codex": "code-review",
                                          "synthetic": "reviewer"})
        # Two agents with the same skill do not make it shared while a third agent has none.
        partial = self.add("partial", "claude=pr-description", "codex=pr-description")["rule"]
        self.assertEqual((partial["skill"], partial["skills"]),
                         (None, {"claude": "pr-description", "codex": "pr-description"}))
        every = self.add("every", *(identifier + "=x" for identifier in agents.builtin_ids()))["rule"]
        self.assertEqual((every["skill"], every["skills"]), ("x", None))
        with mock.patch.dict(os.environ, {"SYNTHETIC_SHELL": "1", "CLAUDECODE": "1"}):
            unnamed = self.command("rules", "show")
        self.assertEqual((unnamed["agent"], ["use_skill" in item for item in unnamed["rules"]]), (None, [False] * 3))
        with mock.patch.dict(os.environ, {"SYNTHETIC_SHELL": "1"}):
            shown = self.command("rules", "show")
        self.assertEqual(shown["agent"], "synthetic")
        self.assertEqual({item["id"]: item["use_skill"] for item in shown["rules"]},
                         {"every": "x", "partial": None, "review": "reviewer"})
        self.assertIn("synthetic: Synthetic Agent", self.command("rules", "show")["note"])
        self.assertEqual(self.command("rules", "list")["agents"][-1], {"id": "synthetic", "name": "Synthetic Agent"})

    def test_unknown_agents_are_refused_but_kept_when_a_rule_already_names_them(self):
        self.assertIn("known agents: " + ", ".join(agents.builtin_ids()), self.add("x", "future=review", ok=False)["error"])
        # A newer Richi may have stored a skill for an agent this one does not know.
        self.add("newer", "review")
        database = self.config.parent / "memory.sqlite3"
        with closing(sqlite3.connect(str(database))) as conn, conn:
            row = json.loads(conn.execute("SELECT value FROM metadata WHERE key = 'rule:newer'").fetchone()[0])
            row["skills"] = {"future": "future-review"}
            conn.execute("UPDATE metadata SET value = ? WHERE key = 'rule:newer'", (json.dumps(row),))
        listed = self.command("rules", "list")["rules"][0]
        self.assertEqual(listed["skills"], {"future": "future-review"})
        shown = self.command("rules", "show", "--agent", "future")["rules"][0]
        self.assertEqual(shown["use_skill"], "future-review")
        changed = self.command("rules", "update", "newer", "--expected-updated-at", listed["updated_at"],
                               "--skill", "review", "--skill", "future=future-review-2")["rule"]
        self.assertEqual((changed["skill"], changed["skills"]), ("review", {"future": "future-review-2"}))
        self.command("rules", "show", "--agent", "Not An Agent", ok=False)

    def test_catalog_reads_skill_sources_from_the_registry(self):
        write_skill(self.home / ".synthetic" / "skills" / "reviewer", "---\nname: reviewer\ndescription: Reviews\n---\n")
        write_skill(self.home / ".agents" / "skills" / "reviewer", "---\nname: reviewer\n---\n")
        result = skill_catalog.catalog({}, self.home)
        skills = {skill["name"]: skill for skill in result["skills"]}
        self.assertEqual(skills["reviewer"]["agents"], ["codex", "cursor", "synthetic"])
        self.assertEqual(result["agents"][-1], {"id": "synthetic", "name": "Synthetic Agent"})
        self.assertIn(("synthetic", "skills", True),
                      [(entry["agent"], entry["kind"], entry["exists"]) for entry in result["roots"]])


class AgentSettingsTests(IsolatedHome):
    """The user's own agents and their adjustments of built-in ones (richi skill-sources)."""

    def setUp(self):
        super().setUp()
        write_skill(self.home / ".claude" / "skills" / "pr-description", "---\nname: pr-description\n---\n")
        write_skill(self.home / ".gemini" / "skills" / "review", "---\nname: review\ndescription: Reviews\n---\n")
        write_skill(self.home / ".gemini" / "skills" / "manual",
                    "---\nname: manual\ndisable-model-invocation: true\n---\n")
        write_skill(self.home / "team" / "release", "---\nname: release\n---\n")

    def test_a_preexisting_cursor_profile_keeps_its_folder_choices_under_the_builtin(self):
        previous = tuple(agent for agent in agents.AGENTS if agent.id != "cursor")
        with mock.patch.object(agents, "AGENTS", previous):
            cursor = self.cli("skill-sources", "add", "cursor", "--name", "Team Cursor", "--variable", "TEAM_EDITOR=1",
                              "--skills", "~/.cursor/skills", "--skills", "~/.gemini/skills")["agent"]
            saved = self.cli("skill-sources", "update", "cursor", "--expected-updated-at", cursor["updated_at"],
                             "--disable", "~/.cursor/skills")["agent"]
        path = agents.settings_path()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["agents"]["cursor"]["future"] = ["keep"]
        path.write_text(json.dumps(data), encoding="utf-8")
        before = path.read_bytes()
        listed = self.listed()["cursor"]
        self.assertEqual((listed["builtin"], listed["name"], listed["variables"], listed["updated_at"]),
                         (True, "Cursor", ["CURSOR_AGENT"], saved["updated_at"]))
        self.assertEqual([(s["key"], s["added"], s["enabled"]) for s in listed["sources"]],
                         [("cursor-skills", False, False), ("agents-skills", False, True), ("claude-skills", False, True),
                          ("codex-skills", False, True), ("system", False, True), ("~/.gemini/skills", True, True)])
        # The extra folder is still scanned, using Cursor's built-in rule for manual invocation.
        self.assertEqual(listed["sources"][-1]["skills"], 1)
        self.assertEqual(agents.detect({"CURSOR_AGENT": "1"}), "cursor")
        self.assertIsNone(agents.detect({"TEAM_EDITOR": "1"}))
        version = ["--expected-updated-at", saved["updated_at"]]
        refused = self.cli("skill-sources", "update", "cursor", *version, "--name", "Other", ok=False)
        self.assertEqual(refused["code"], "builtin")
        self.assertEqual(path.read_bytes(), before)
        current = listed
        for wanted in (False, True, True, False, False):
            before = path.read_bytes()
            was = current["sources"][0]["enabled"]
            changed = self.cli("skill-sources", "update", "cursor", "--expected-updated-at", current["updated_at"],
                               "--enable" if wanted else "--disable", "cursor-skills")
            self.assertEqual(changed["results"][0]["status"], "unchanged" if was == wanted else "updated")
            if was == wanted:
                self.assertEqual((path.read_bytes(), changed["agent"]["updated_at"]), (before, current["updated_at"]))
            current = changed["agent"]
            self.assertEqual(current["sources"][0]["enabled"], wanted)
            with mock.patch.object(agents, "AGENTS", previous):
                self.assertEqual(self.listed()["cursor"]["sources"][0]["enabled"], wanted)
        self.assertEqual(self.cli("skill-sources", "remove", "cursor", *version, ok=False)["code"], "conflict")
        stored = json.loads(path.read_text(encoding="utf-8"))["agents"]["cursor"]
        for field in ("name", "variable", "manual_skills", "created_at", "future"):
            self.assertEqual(stored[field], data["agents"]["cursor"][field])
        reset = self.cli("skill-sources", "remove", "cursor", "--expected-updated-at", current["updated_at"])
        self.assertEqual(reset["results"], [{"id": "cursor", "status": "reset"}])
        self.assertIsNone(reset["agent"]["updated_at"])
        self.assertEqual([s["path"] for s in reset["agent"]["sources"]],
                         ["~/.cursor/skills", "~/.agents/skills", "~/.claude/skills", "~/.codex/skills",
                          "~/.cursor/skills-cursor"])

    def test_an_own_agent_is_described_detected_and_gets_its_skill(self):
        before = self.cli("skill-sources", "list")
        self.assertEqual([agent["id"] for agent in before["agents"]], list(agents.builtin_ids()))
        self.assertIsNone(before["problem"])
        presets = {preset["id"]: preset for preset in before["presets"]}
        self.assertEqual((presets["gemini-cli"]["folder"], presets["gemini-cli"]["exists"], presets["opencode"]["exists"]),
                         ("~/.gemini/skills", True, False))
        created = self.cli("skill-sources", "add", "gemini-cli", "--name", "Gemini CLI", "--skills",
                           str(self.home / ".gemini" / "skills") + "/", "--variable", "GEMINI_CLI=1",
                           "--manual-skills", "front-matter")
        self.assertEqual(created["results"], [{"id": "gemini-cli", "status": "created"}])
        agent = created["agent"]
        self.assertEqual((agent["builtin"], agent["detected"], agent["connection"], agent["variables"], agent["skills"],
                          agent["manual_skills"]), (False, True, None, ["GEMINI_CLI=1"], 1, "front-matter"))
        # A folder under the home directory is stored with ~, and a skill only the user invokes is left out.
        self.assertEqual(agent["sources"], [{"key": "~/.gemini/skills", "kind": "skills", "layout": "skills",
                                             "path": "~/.gemini/skills", "exists": True, "denied": False,
                                             "enabled": True, "added": True, "skills": 1}])
        self.assertNotIn("gemini-cli", [preset["id"] for preset in self.cli("skill-sources", "list")["presets"]])
        self.assertEqual(agents.detect({"GEMINI_CLI": "1"}), "gemini-cli")
        self.assertIsNone(agents.detect({"GEMINI_CLI": "0"}))
        # Started from another agent's terminal, a process carries both agents' variables: Richi
        # then names neither, rather than give one agent the other's skills.
        for environment in ({"GEMINI_CLI": "1", "CLAUDECODE": "1"}, {"GEMINI_CLI": "1", "CODEX_THREAD_ID": "thread"},
                            {"RICHI_CHAT_ID": "gemini-cli:chat", "CLAUDECODE": "1"}):
            self.assertIsNone(agents.detect(environment))
        self.assertEqual(agents.detect({"RICHI_CHAT_ID": "gemini-cli:chat", "GEMINI_CLI": "1"}), "gemini-cli")
        self.assertEqual(skill_catalog.catalog()["agents"][-1], {"id": "gemini-cli", "name": "Gemini CLI"})
        # Rules accept the new agent like a built-in one.
        store = self.root / "store" / "config.json"
        store.parent.mkdir()
        store.write_text(json.dumps({"database": "memory.sqlite3"}))
        self.cli("init", config_file=store)
        rule = self.cli("rules", "add", "review", "--global", "--text", "Review.", "--skill", "claude=code-review",
                        "--skill", "gemini-cli=review", config_file=store)["rule"]
        self.assertEqual(rule["skills"], {"claude": "code-review", "gemini-cli": "review"})
        with mock.patch.dict(os.environ, {"GEMINI_CLI": "1"}):
            shown = self.cli("rules", "show", config_file=store)
        self.assertEqual((shown["agent"], shown["rules"][0]["use_skill"]), ("gemini-cli", "review"))
        self.assertEqual(stat_mode(agents.settings_path()), 0o600)
        # The directory Richi made for it is private too, as the launcher makes it.
        self.assertEqual(stat_mode(agents.settings_path().parent), 0o700)
        # Numbers longer than Richi needs are refused: older Pythons convert one in quadratic time.
        with self.assertRaises(ValueError):
            agents.parse_json("1" * (agents.MAX_DIGITS + 1))
        self.assertEqual(agents.parse_json("[1.5, %s]" % ("9" * agents.MAX_DIGITS)), [1.5, int("9" * agents.MAX_DIGITS)])

    def test_an_own_agent_changes_with_its_version_and_is_removed(self):
        created = self.cli("skill-sources", "add", "gemini-cli", "--name", "Gemini CLI", "--skills", "~/.gemini/skills")["agent"]
        self.assertEqual(self.cli("skill-sources", "add", "gemini-cli", "--name", "Again", "--skills", "~/x", ok=False)["code"],
                         "exists")
        self.assertEqual(self.cli("skill-sources", "update", "gemini-cli", "--name", "Gemini", ok=False)["code"], "conflict")
        version = ["--expected-updated-at", created["updated_at"]]
        self.assertEqual(self.cli("skill-sources", "update", "gemini-cli", *version, "--name", "Gemini CLI")["results"][0]["status"],
                         "unchanged")
        changed = self.cli("skill-sources", "update", "gemini-cli", *version, "--name", "Gemini", "--variable", "GEMINI_SESSION",
                           "--add-skills", self.home / "team", "--disable", "~/.gemini/skills")["agent"]
        self.assertEqual((changed["name"], changed["variables"], changed["skills"]), ("Gemini", ["GEMINI_SESSION"], 1))
        self.assertEqual([(source["key"], source["enabled"], source["skills"]) for source in changed["sources"]],
                         [("~/.gemini/skills", False, 0), ("~/team", True, 1)])
        self.assertEqual(agents.detect({"GEMINI_SESSION": "anything"}), "gemini-cli")
        self.assertEqual(self.cli("skill-sources", "update", "gemini-cli", *version, "--name", "Late", ok=False)["code"], "conflict")
        version = ["--expected-updated-at", changed["updated_at"]]
        for arguments, code in ((["--add-skills", "~/team"], "exists"), (["--remove-skills", "~/missing"], "not_found"),
                                (["--enable", "nothing"], "not_found"), (["--add-skills", "relative"], None),
                                (["--name", "x" * 61], None), (["--variable", "1BAD"], None), ([], None),
                                (["--remove-skills", "~/team", "--remove-skills", "~/.gemini/skills"], None)):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.cli("skill-sources", "update", "gemini-cli", *version, *arguments, ok=False).get("code"), code)
        # What an exists, not_found or limit refusal is about travels beside its code.
        for arguments, subject in ((["update", "gemini-cli", *version, "--add-skills", "~/team"], "source"),
                                   (["update", "gemini-cli", *version, "--enable", "nothing"], "source"),
                                   (["add", "gemini-cli", "--name", "Again", "--skills", "~/x"], "agent"),
                                   (["add", "claude", "--name", "Mine", "--skills", "~/x"], "agent"),
                                   (["update", "unknown", "--name", "Unknown"], "agent"),
                                   (["update", "gemini-cli", *version]
                                    + [word for number in range(agents.MAX_FOLDERS) for word in ("--add-skills", "~/f%d" % number)],
                                    "folders")):
            with self.subTest(arguments=arguments[:4]):
                self.assertEqual(self.cli("skill-sources", *arguments, ok=False)["subject"], subject)
        cleared = self.cli("skill-sources", "update", "gemini-cli", *version, "--variable", "", "--remove-skills", "~/team",
                           "--enable", "~/.gemini/skills")["agent"]
        self.assertEqual((cleared["variables"], [source["key"] for source in cleared["sources"]]),
                         ([], ["~/.gemini/skills"]))
        self.assertEqual(self.cli("skill-sources", "remove", "gemini-cli", "--expected-updated-at", changed["updated_at"],
                                  ok=False)["code"], "conflict")
        removed = self.cli("skill-sources", "remove", "gemini-cli", "--expected-updated-at", cleared["updated_at"])
        self.assertEqual((removed["results"], removed["agent"]), ([{"id": "gemini-cli", "status": "removed"}], None))
        self.assertEqual(self.cli("skill-sources", "remove", "gemini-cli", "--expected-updated-at", "x", ok=False)["code"],
                         "not_found")
        self.assertEqual(self.cli("skill-sources", "update", "unknown", "--name", "Unknown", ok=False)["code"], "not_found")
        for arguments in (["Bad ID", "--name", "x", "--skills", "~/x"], ["x", "--name", "x"], ["x", "--skills", "~/x"]):
            self.cli("skill-sources", "add", *arguments, ok=False)
        self.cli("--project-path", self.home, "skill-sources", "list", ok=False)

    def test_a_builtin_agent_keeps_its_definition_under_adjustments(self):
        self.assertEqual(self.cli("skill-sources", "add", "claude", "--name", "Mine", "--skills", "~/x", ok=False)["code"],
                         "exists")
        self.assertEqual(self.cli("skill-sources", "update", "claude", "--name", "Mine", ok=False)["code"], "builtin")
        self.assertEqual(self.cli("skill-sources", "remove", "claude", "--expected-updated-at", "x", ok=False)["code"],
                         "not_found")
        claude = self.listed()["claude"]
        self.assertEqual((claude["builtin"], claude["updated_at"], claude["skills"], claude["detected"],
                          claude["manual_skills"]), (True, None, 1, True, None))
        self.assertEqual([source["key"] for source in claude["sources"]], ["skills", "plugins", "claude-app"])
        # The first adjustment needs no version; a built-in source is switched by its key.
        adjusted = self.cli("skill-sources", "update", "claude", "--disable", "skills", "--add-skills", "~/team")["agent"]
        self.assertEqual([(source["key"], source["enabled"], source["added"], source["skills"])
                          for source in adjusted["sources"]],
                         [("skills", False, False, 0), ("plugins", True, False, 0), ("claude-app", True, False, 0),
                          ("~/team", True, True, 1)])
        self.assertEqual(adjusted["skills"], 1)
        names = {skill["name"]: skill["agents"] for skill in skill_catalog.catalog()["skills"]}
        self.assertEqual(names, {"pr-description": ["cursor"], "release": ["claude"]})
        self.assertEqual(self.cli("skill-sources", "update", "claude", "--enable", "skills", ok=False)["code"], "conflict")
        self.assertEqual(self.cli("skill-sources", "update", "claude", "--expected-updated-at", adjusted["updated_at"],
                                  "--add-skills", "~/.claude/skills", ok=False)["code"], "exists")
        # Undoing every adjustment leaves no stored settings, as if nothing was changed.
        restored = self.cli("skill-sources", "update", "claude", "--expected-updated-at", adjusted["updated_at"],
                            "--enable", "skills", "--remove-skills", "~/team")["agent"]
        self.assertEqual((restored["updated_at"], restored["skills"]), (None, 1))
        self.assertEqual(json.loads(agents.settings_path().read_text(encoding="utf-8"))["agents"], {})
        # A field that a newer Richi stored for the agent is not this version's to drop with the last adjustment.
        newer = self.cli("skill-sources", "update", "claude", "--disable", "plugins")["agent"]
        settings = json.loads(agents.settings_path().read_text(encoding="utf-8"))
        settings["agents"]["claude"]["future"] = {"keep": True}
        agents.settings_path().write_text(json.dumps(settings), encoding="utf-8")
        agents.forget_settings()
        kept = self.cli("skill-sources", "update", "claude", "--expected-updated-at", newer["updated_at"],
                        "--enable", "plugins")["agent"]
        stored = json.loads(agents.settings_path().read_text(encoding="utf-8"))["agents"]["claude"]
        self.assertEqual((stored["future"], sorted(stored), all(source["enabled"] for source in kept["sources"])),
                         ({"keep": True}, ["future", "updated_at"], True))
        self.cli("skill-sources", "remove", "claude", "--expected-updated-at", kept["updated_at"])
        self.assertEqual(json.loads(agents.settings_path().read_text(encoding="utf-8"))["agents"], {})
        again = self.cli("skill-sources", "update", "claude", "--disable", "claude-app")["agent"]
        reset = self.cli("skill-sources", "remove", "claude", "--expected-updated-at", again["updated_at"])
        self.assertEqual((reset["results"][0]["status"], reset["agent"]["updated_at"]), ("reset", None))
        self.assertTrue(all(source["enabled"] for source in reset["agent"]["sources"]))

    def test_a_damaged_settings_file_is_reported_and_never_breaks_commands(self):
        path = agents.settings_path()
        path.parent.mkdir(parents=True)
        for text, code in (("{not json", "damaged"), (json.dumps({"version": "1", "agents": {}}), "damaged"),
                           (json.dumps({"version": 1, "agents": []}), "damaged"),
                           (json.dumps({"version": 2, "agents": {}}), "newer"),
                           # A newer format may change the shape of everything else; it is still not damage.
                           (json.dumps({"version": 2, "agents": []}), "newer"),
                           (json.dumps({"version": 2, "profiles": {}}), "newer")):
            with self.subTest(text=text[:30]):
                path.write_text(text, encoding="utf-8")
                agents.forget_settings()
                listed = self.cli("skill-sources", "list")
                self.assertEqual([agent["id"] for agent in listed["agents"]], list(agents.builtin_ids()))
                self.assertEqual((str(path) in listed["problem"], listed["problem_code"]), (True, code))
                self.assertEqual(agents.ids(), agents.builtin_ids())
                refused = self.cli("skill-sources", "update", "claude", "--disable", "skills", ok=False)
                self.assertEqual((refused["code"], refused["subject"]), ("settings", code))
                # Settings a newer Richi wrote are not damage: nobody is told to remove them.
                self.assertEqual("remove" in listed["problem"] + refused["error"], code == "damaged")
                self.assertEqual(path.read_text(encoding="utf-8"), text)
        # Settings a newer Richi wrote for a built-in agent this one lacks are kept and ignored.
        path.write_text(json.dumps({"version": 1, "agents": {"future": {"updated_at": "t", "disabled": ["skills"]}}}),
                        encoding="utf-8")
        agents.forget_settings()
        listed = self.cli("skill-sources", "list")
        self.assertEqual((agents.ids(), listed["problem"]), (agents.builtin_ids(), None))
        # They are named with what removing them takes, and adding or changing that agent says the same.
        remove = "richi skill-sources remove future --expected-updated-at=t"
        self.assertEqual(listed["unknown_agents"], [{"id": "future", "updated_at": "t", "next": remove}])
        for arguments, code in ((["add", "future", "--name", "Future", "--skills", "~/x"], "exists"),
                                (["update", "future", "--expected-updated-at", "t", "--name", "Future"], "not_found")):
            with self.subTest(arguments=arguments):
                refused = self.cli("skill-sources", *arguments, ok=False)
                self.assertEqual((refused.get("code"), remove in refused["error"], "newer Richi" in refused["error"]),
                                 (code, True, True))
        self.cli("skill-sources", "update", "claude", "--disable", "skills")
        self.assertIn("future", json.loads(path.read_text(encoding="utf-8"))["agents"])
        self.cli(*remove.split()[1:])
        self.assertNotIn("unknown_agents", self.cli("skill-sources", "list"))
        # Brackets nested deeper than the parser goes are damage like any other. Python 3.14 parses this
        # depth, and then the value is too deep to copy and write back: the same verdict.
        path.write_text('{"version":1,"agents":{"x":{"updated_at":"t","name":"X","folders":[{"path":"~/x",'
                        '"enabled":true}],"deep":' + "[" * 100000 + "]" * 100000 + "}}}", encoding="utf-8")
        agents.forget_settings()
        self.assertIn("nested too deeply", self.cli("skill-sources", "list")["problem"])
        refused = self.cli("skill-sources", "update", "claude", "--disable", "skills", ok=False)
        self.assertEqual((refused["code"], "nested too deeply" in refused["error"]), ("settings", True))

    def test_settings_this_version_cannot_use_are_kept_and_the_rest_works(self):
        # Two versions of Richi may share the file (dev is chosen per workspace): what a newer one
        # wrote, or one damaged entry, must not take the other agents' settings down with it.
        path = agents.settings_path()
        path.parent.mkdir(parents=True)
        own = {"name": "Own", "updated_at": "t1", "folders": [{"path": "~/team", "enabled": True}]}
        unusable = {
            # A newer Richi's value of a known field, and a field of a folder this version does not know.
            "gemini-cli": {"name": "Gemini CLI", "updated_at": "t2", "folders": [{"path": "~/g", "enabled": True}],
                           "manual_skills": "openai-yaml"},
            "claude": {"updated_at": "t3", "folders": [{"path": "~/x", "enabled": True, "shared": True}]},
            # Damage: a folder relative to some working directory, and a key that is no agent ID.
            "x": {"name": "X", "updated_at": "t4", "folders": [{"path": "relative", "enabled": True}]},
            "Bad ID": {"updated_at": "t5"}}
        path.write_text(json.dumps({"version": agents.SETTINGS_VERSION, "agents": {"own": own, **unusable},
                                    "future": {"keep": [1]}}), encoding="utf-8")
        agents.forget_settings()
        listed = self.cli("skill-sources", "list")
        self.assertEqual([agent["id"] for agent in listed["agents"]], [*agents.builtin_ids(), "own"])
        self.assertEqual((listed["problem"], listed["problem_code"], agents.settings_complete()), (None, None, False))
        # Codex's chats are named by its thread ID alone, so the identity tells nobody it is Codex.
        self.assertEqual([agent["id"] for agent in listed["agents"] if not agent["chat_prefix"]], ["codex"])
        # Every own agent stored counts toward the limit, also one this version cannot use.
        with mock.patch.object(agents, "MAX_CUSTOM", 2):
            refused = self.cli("skill-sources", "add", "third", "--name", "Third", "--skills", "~/team", ok=False)
        self.assertEqual((refused["code"], refused["subject"]), ("limit", "agents"))
        kept = {item["id"]: item for item in listed["unknown_agents"]}
        self.assertEqual(sorted(kept), ["Bad ID", "claude", "gemini-cli", "x"])
        self.assertEqual(("next" in kept["Bad ID"], "manual_skills" in kept["gemini-cli"]["error"]), (False, True))
        self.assertEqual(kept["gemini-cli"]["next"], "richi skill-sources remove gemini-cli --expected-updated-at=t2")
        # A built-in agent stays, without what the user set for it.
        self.assertEqual([source["key"] for source in self.listed()["claude"]["sources"]],
                         [source.key for source in agents.AGENTS[0].sources(os.environ, self.home)])
        # A change of another agent writes them back as they were, and the file's other fields too.
        self.cli("skill-sources", "update", "own", "--expected-updated-at", "t1", "--name", "Renamed")
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual({key: stored["agents"][key] for key in unusable}, unusable)
        self.assertEqual((stored["future"], stored["agents"]["own"]["name"]), ({"keep": [1]}, "Renamed"))
        # This version changes none of them, a built-in agent's included; it removes them with their stored version.
        refused = self.cli("skill-sources", "update", "gemini-cli", "--expected-updated-at", "t2", "--name", "G", ok=False)
        self.assertEqual((refused["code"], refused["subject"], kept["gemini-cli"]["next"] in refused["error"]),
                         ("settings", "agent", True))
        refused = self.cli("skill-sources", "update", "claude", "--expected-updated-at", "t3", "--disable", "plugins",
                           ok=False)
        self.assertEqual((refused["code"], refused["subject"]), ("settings", "agent"))
        self.assertEqual(self.cli("skill-sources", "add", "gemini-cli", "--name", "G", "--skills", "~/g", ok=False)["code"],
                         "exists")
        self.assertEqual(self.cli("skill-sources", "remove", "x", "--expected-updated-at", "t0", ok=False)["code"],
                         "conflict")
        self.assertEqual([self.cli("skill-sources", "remove", identifier, "--expected-updated-at", version)
                          ["results"][0]["status"] for identifier, version in (("x", "t4"), ("claude", "t3"))],
                         ["removed", "reset"])
        self.assertEqual(sorted(json.loads(path.read_text(encoding="utf-8"))["agents"]), ["Bad ID", "gemini-cli", "own"])

    def test_beyond_the_own_agents_this_version_uses_the_ones_described_last_are_left_out(self):
        # A newer Richi may keep more own agents. One it adds must not take the place of an agent this
        # version described and uses, whatever their IDs.
        self.cli("skill-sources", "add", "zeta", "--name", "Zeta", "--skills", "~/team")
        path = agents.settings_path()
        settings = json.loads(path.read_text(encoding="utf-8"))
        settings["agents"]["alpha"] = {"name": "Alpha", "updated_at": "9999", "created_at": "9999",
                                       "folders": [{"path": "~/team", "enabled": True}]}
        path.write_text(json.dumps(settings), encoding="utf-8")
        # Settings of an agent a newer Richi has built in (no name) are no own agent and take no one's place.
        settings["agents"]["future"] = {"updated_at": "t"}
        path.write_text(json.dumps(settings), encoding="utf-8")
        with mock.patch.object(agents, "MAX_CUSTOM", 1):
            agents.forget_settings()
            listed = self.cli("skill-sources", "list")
        self.assertEqual([agent["id"] for agent in listed["agents"]], [*agents.builtin_ids(), "zeta"])
        self.assertEqual([(item["id"], item.get("error")) for item in listed["unknown_agents"]],
                         [("future", None), ("alpha", "this version uses at most 1 own agents")])
        agents.forget_settings()

    def test_a_null_entry_can_be_removed_only_with_its_advertised_version(self):
        path = agents.settings_path()
        path.parent.mkdir(parents=True)
        path.write_text(agent_commands._serialized({"own": None}), encoding="utf-8")
        next_command = self.cli("skill-sources", "list")["unknown_agents"][0]["next"]
        with self.assertRaises(agent_commands.AgentError) as refused:
            agent_commands.change("remove", "own", {})
        self.assertEqual(refused.exception.code, "conflict")
        self.assertEqual(self.cli(*shlex.split(next_command)[1:])["results"], [{"id": "own", "status": "removed"}])

    def test_replacing_an_unversioned_entry_invalidates_its_recovery_command(self):
        path = agents.settings_path()
        path.parent.mkdir(parents=True)
        path.write_text(agent_commands._serialized({"own": None}), encoding="utf-8")
        stale = self.cli("skill-sources", "list")["unknown_agents"][0]
        path.write_text(agent_commands._serialized({"own": {"name": "Other"}}), encoding="utf-8")
        refused = self.cli(*shlex.split(stale["next"])[1:], ok=False)
        self.assertEqual(refused["code"], "conflict")
        fresh = self.cli("skill-sources", "list")["unknown_agents"][0]
        self.assertNotEqual(fresh["updated_at"], stale["updated_at"])
        self.assertEqual(self.cli(*shlex.split(fresh["next"])[1:])["results"][0]["status"], "removed")

    def test_versions_survive_a_stopped_clock_and_recreation(self):
        with mock.patch.object(memory, "now", return_value="2026-01-02T03:04:05.000000Z"):
            add = ("skill-sources", "add", "own", "--name", "Own", "--skills", "~/team")
            first = self.cli(*add)["agent"]["updated_at"]
            renamed = self.cli("skill-sources", "update", "own", "--expected-updated-at", first,
                               "--name", "Renamed")["agent"]
            second = renamed["updated_at"]
            self.assertGreater(second, first)
            self.assertEqual(renamed["created_at"], first)
            self.cli("skill-sources", "remove", "own", "--expected-updated-at", second)
            reopened = self.cli(*add)["agent"]
            self.assertGreater(reopened["updated_at"], second)
            self.assertEqual(reopened["created_at"], reopened["updated_at"])
            for stale in (first, second):
                self.assertEqual(self.cli("skill-sources", "remove", "own", "--expected-updated-at", stale,
                                          ok=False)["code"], "conflict")

    def test_a_change_describes_its_own_committed_version(self):
        listing = agent_commands.listing

        def edited_before_answer(*args, **kwargs):
            # This writer gets the lock after creation but before the creator scans the skill folders.
            with mock.patch.object(agent_commands, "listing", listing):
                version = agents.read_settings().entries["own"]["updated_at"]
                agent_commands.change("update", "own", {"expected_updated_at": version, "name": "Second"})
            return listing(*args, **kwargs)

        with mock.patch.object(agent_commands, "listing", edited_before_answer):
            first = agent_commands.change("add", "own", {"name": "First", "folders": ["~/team"]}, full=True)
        self.assertEqual(first["agent"]["name"], "First")
        self.assertEqual(first["agent"], first["listing"]["agents"][-1])
        self.assertEqual(self.listed()["own"]["name"], "Second")
        self.assertEqual(self.cli("skill-sources", "remove", "own", "--expected-updated-at",
                                  first["agent"]["updated_at"], ok=False)["code"], "conflict")

    def test_a_stored_absolute_folder_can_be_switched_and_removed_by_either_spelling(self):
        path = agents.settings_path()
        path.parent.mkdir(parents=True)
        absolute = str(self.home / "team")
        path.write_text(agent_commands._serialized({"claude": {
            "updated_at": "t", "folders": [{"path": absolute, "enabled": True}]}}), encoding="utf-8")
        self.assertEqual(self.cli("skill-sources", "update", "claude", "--expected-updated-at", "t",
                                  "--add-skills", "~/team", ok=False)["code"], "exists")
        changed = self.cli("skill-sources", "update", "claude", "--expected-updated-at", "t", "--disable", "~/team")["agent"]
        source = changed["sources"][-1]
        self.assertEqual((source["key"], source["enabled"]), (absolute, False))
        changed = self.cli("skill-sources", "update", "claude", "--expected-updated-at", changed["updated_at"],
                           "--remove-skills", source["key"])["agent"]
        self.assertNotIn(absolute, [source["key"] for source in changed["sources"]])

    def test_settings_this_version_does_not_use_are_named_within_bounds(self):
        # Only a hand edit makes a key longer than an ID, and only the file's size limits how many there are.
        path = agents.settings_path()
        path.parent.mkdir(parents=True)
        entries = {"Bad %03d" % number: {"updated_at": "t"} for number in range(30)}
        entries["x" * 5000] = {"updated_at": "t"}
        path.write_text(json.dumps({"version": agents.SETTINGS_VERSION, "agents": entries}), encoding="utf-8")
        agents.forget_settings()
        listed = self.cli("skill-sources", "list")
        self.assertEqual((len(listed["unknown_agents"]), listed["unknown_agents_more"]), (20, 11))
        self.assertLess(len(json.dumps(listed)), 20000)
        path.write_text(json.dumps({"version": agents.SETTINGS_VERSION, "agents": {"x" * 5000: {"updated_at": "t"},
                                                                                 "y" + "x" * 4999: {}}}),
                        encoding="utf-8")
        agents.forget_settings()
        # Two keys cut the same way still read apart.
        shown = sorted(item["id"] for item in self.cli("skill-sources", "list")["unknown_agents"])
        self.assertEqual([name[:200] for name in shown], ["x" * 200, "y" + "x" * 199])
        self.assertTrue(all("(5000 characters, sha256 " in name for name in shown))
        self.assertNotIn("unknown_agents_more", self.cli("skill-sources", "list"))

    def test_a_switched_off_source_is_not_looked_at(self):
        # It may be on a share that no longer answers: a stat would wait for it, and with it the map.
        self.cli("skill-sources", "update", "claude", "--disable", "plugins")
        paths = {source.key: str(source.path) for source in agents.AGENTS[0].sources(os.environ, self.home)}
        looked, real = [], skill_catalog.folder_state
        with mock.patch.object(skill_catalog, "folder_state", side_effect=lambda path: looked.append(str(path)) or real(path)):
            claude = self.listed()["claude"]
        plugins = next(source for source in claude["sources"] if source["key"] == "plugins")
        self.assertEqual((plugins["enabled"], plugins["exists"], plugins["denied"]), (False, False, False))
        self.assertNotIn(paths["plugins"], looked)
        self.assertIn(paths["skills"], looked)
        # An own agent whose every folder is switched off: nothing tells whether it is installed.
        (self.home / "team").mkdir(exist_ok=True)
        own = self.cli("skill-sources", "add", "own", "--name", "Own", "--skills", "~/team")["agent"]
        self.assertTrue(own["detected"])
        off = self.cli("skill-sources", "update", "own", "--expected-updated-at", own["updated_at"],
                       "--disable", "~/team")["agent"]
        self.assertEqual(([source["enabled"] for source in off["sources"]], off["detected"]), ([False], None))

    def test_unknown_fields_that_could_not_be_written_back_are_damage(self):
        path = agents.settings_path()
        path.parent.mkdir(parents=True)
        entry = {"updated_at": "t", "name": "Own", "folders": [{"path": "~/team", "enabled": True}]}
        deep = "[" * (agents.MAX_DEPTH + 5) + "]" * (agents.MAX_DEPTH + 5)
        # Each once loaded cleanly, and then no change could be saved: copying or writing it failed.
        for name, field in (("deep", deep), ("half-pair", '"\\ud800"'), ("half-pair key", '{"\\udcff": 1}')):
            with self.subTest(name=name):
                path.write_text('{"version":1,"agents":{"own":' + json.dumps(entry)[:-1] + ',"extra":' + field
                                + '}}}', encoding="utf-8")
                agents.forget_settings()
                self.assertIn(str(path), self.cli("skill-sources", "list")["problem"])
                refused = self.cli("skill-sources", "update", "own", "--expected-updated-at", "t", "--name", "Renamed",
                                   ok=False)
                self.assertEqual(refused["code"], "settings")
        # Within the bound a value of a newer Richi is kept and written back as it was.
        path.write_text(json.dumps({"version": 1, "agents": {"own": dict(entry, extra=[[["x"]]])}}), encoding="utf-8")
        agents.forget_settings()
        self.cli("skill-sources", "update", "own", "--expected-updated-at", "t", "--name", "Renamed")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["agents"]["own"]["extra"], [[["x"]]])

    def test_a_change_that_would_make_the_file_unreadable_is_refused(self):
        self.cli("skill-sources", "add", "first", "--name", "First", "--skills", "~/team")
        path = agents.settings_path()
        before = path.read_text(encoding="utf-8")
        # Richi once wrote such a file, said "created", and then ignored every setting in it.
        with mock.patch.object(agents, "MAX_SETTINGS", len(before.encode("utf-8")) + 10):
            refused = self.cli("skill-sources", "add", "second", "--name", "Second", "--skills", "~/team", ok=False)
            self.assertEqual((refused["code"], refused["subject"]), ("limit", "size"))
            self.assertEqual(path.read_text(encoding="utf-8"), before)
            agents.forget_settings()
            self.assertEqual(agents.ids(), (*agents.builtin_ids(), "first"))

    def test_a_compact_settings_file_with_extensions_remains_editable(self):
        path = agents.settings_path()
        path.parent.mkdir(parents=True)
        # A newer writer stored a short JSON array on one line. Reindenting it alone exceeds the limit.
        extension = [0] * 1000
        old = {"name": "First", "updated_at": "t", "folders": [{"path": "~/team", "enabled": True}]}
        original = {"version": 1, "agents": {"own": old}, "extension": extension}
        path.write_text(json.dumps(original, separators=(",", ":")), encoding="utf-8")
        limit = path.stat().st_size + 128
        with mock.patch.object(agents, "MAX_SETTINGS", limit):
            result = self.cli("skill-sources", "update", "own", "--expected-updated-at", "t", "--name", "Other")
            self.assertEqual(result["agent"]["name"], "Other")
            self.assertLessEqual(path.stat().st_size, limit)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["extension"], extension)

    def test_a_file_that_could_not_be_read_now_is_read_again(self):
        self.cli("skill-sources", "add", "first", "--name", "First", "--skills", "~/team")
        agents.forget_settings()
        target, real_open = str(agents.settings_path()), open

        def exhausted(file, *arguments, **options):
            if str(file) == target:
                raise OSError(errno.EMFILE, "Too many open files")
            return real_open(file, *arguments, **options)
        with mock.patch("builtins.open", exhausted):
            entries, problem = agents.load_settings()
        self.assertEqual(entries, {})
        self.assertIn("could not be read", problem)
        self.assertNotIn("fix or remove", problem)
        # Not taken for damage: the next reading finds the agent.
        self.assertEqual(agents.ids(), (*agents.builtin_ids(), "first"))

    def test_settings_reads_reject_special_files_and_nonfinite_fields(self):
        path = agents.settings_path()
        path.parent.mkdir(parents=True)
        path.mkdir()
        self.assertEqual(agents.read_settings().code, "unreadable")
        path.rmdir()
        raw = '{"version":1,"agents":{},"future":1e1000}'
        path.write_text(raw, encoding="utf-8")
        self.assertEqual(self.cli("skill-sources", "list")["problem_code"], "damaged")
        self.cli("skill-sources", "update", "claude", "--disable", "skills", ok=False)
        self.assertEqual(path.read_text(encoding="utf-8"), raw)

    def test_a_new_version_with_the_same_time_and_size_is_read(self):
        self.cli("skill-sources", "add", "abc", "--name", "Abc", "--skills", "~/team")
        path = agents.settings_path()
        self.assertEqual(agents.describe()[-1]["name"], "Abc")
        info = path.stat()
        settings = json.loads(path.read_text(encoding="utf-8"))["agents"]
        settings["abc"]["name"] = "Abd"
        private_files.replace(path, agent_commands._serialized(settings))
        # The same length and, as on a file system with coarse timestamps, the same time.
        self.assertEqual(path.stat().st_size, info.st_size)
        os.utime(str(path), ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertEqual(agents.describe()[-1]["name"], "Abd")

    def test_a_restored_settings_file_is_read_when_its_original_mtime_is_preserved(self):
        path = agents.settings_path()
        path.parent.mkdir(parents=True)
        old = {"name": "First", "updated_at": "v1", "folders": [{"path": "~/team", "enabled": True}]}
        path.write_text(agent_commands._serialized({"own": old}), encoding="utf-8")
        info = path.stat()
        self.assertEqual(agents.read_settings().entries["own"]["name"], "First")
        path.write_text(agent_commands._serialized({"own": dict(old, name="Other", updated_at="v2")}), encoding="utf-8")
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        listed = self.listed()["own"]
        self.assertEqual((listed["name"], listed["updated_at"]), ("Other", "v2"))

    def test_one_listing_describes_one_version_of_the_settings(self):
        self.cli("skill-sources", "add", "xagent", "--name", "X", "--skills", "~/team")
        path, scan = agents.settings_path(), skill_catalog.catalog

        def removed_meanwhile(*arguments, **options):
            # Another process removes the agent while this listing scans the skill folders.
            settings = json.loads(path.read_text(encoding="utf-8"))["agents"]
            settings.pop("xagent")
            private_files.replace(path, agent_commands._serialized(settings))
            return scan(*arguments, **options)
        with mock.patch.object(skill_catalog, "catalog", removed_meanwhile):
            listed = agent_commands.listing({}, self.home)
        # Once it showed the agent as written by a newer Richi, with a command that could not work.
        self.assertEqual([agent["id"] for agent in listed["agents"]], [*agents.builtin_ids(), "xagent"])
        self.assertNotIn("unknown_agents", listed)
        self.assertEqual([source["key"] for source in listed["agents"][-1]["sources"]], ["~/team"])

    def test_a_change_waits_for_another_writer_only_so_long(self):
        self.cli("skill-sources", "add", "first", "--name", "First", "--skills", "~/team")
        with private_files.locked(agents.settings_path()), mock.patch.object(agent_commands, "LOCK_SECONDS", 0.2):
            refused = self.cli("skill-sources", "update", "first", "--expected-updated-at", "x", "--name", "Late",
                               ok=False)
        self.assertEqual(refused["code"], "busy")

    def test_refusals_say_what_they_are_about(self):
        created = self.cli("skill-sources", "add", "own", "--name", "Own", "--skills", "~/team")["agent"]
        version = ["--expected-updated-at", created["updated_at"]]
        many = [word for number in range(agents.MAX_FOLDERS + 1) for word in ("--skills", "~/f%d" % number)]
        for arguments, code, subject in (
                (["update", "own", *version, "--remove-skills", "~/missing"], "not_found", "source"),
                (["remove", "nobody", "--expected-updated-at", "x"], "not_found", "agent"),
                (["remove", "claude", "--expected-updated-at", "x"], "not_found", "agent"),
                (["add", "many", "--name", "Many", *many], "limit", "folders")):
            with self.subTest(arguments=arguments[:3]):
                refused = self.cli("skill-sources", *arguments, ok=False)
                self.assertEqual((refused.get("code"), refused.get("subject")), (code, subject))
        path = agents.settings_path()
        settings = json.loads(path.read_text(encoding="utf-8"))
        settings["agents"]["future"] = {"updated_at": "-t", "disabled": ["skills"]}
        path.write_text(json.dumps(settings), encoding="utf-8")
        refused = self.cli("skill-sources", "add", "future", "--name", "Future", "--skills", "~/x", ok=False)
        self.assertEqual((refused["code"], refused["subject"]), ("exists", "agent"))
        # A version that begins with a dash is still read as the version in the command offered.
        offered = self.cli("skill-sources", "list")["unknown_agents"][0]["next"]
        self.assertEqual(offered, "richi skill-sources remove future --expected-updated-at=-t")
        self.assertEqual(self.cli(*shlex.split(offered)[1:])["results"], [{"id": "future", "status": "removed"}])

    def test_an_own_agent_without_a_variable_is_named_only_by_its_chat(self):
        self.cli("skill-sources", "add", "plain", "--name", "Plain", "--skills", "~/team")
        # It has no sign of its own in a shell: it must not count as present in every one.
        self.assertEqual(agents.detect({"CLAUDECODE": "1"}), "claude")
        self.assertIsNone(agents.detect({}))
        self.assertEqual(agents.detect({"RICHI_CHAT_ID": "plain:chat"}), "plain")

    def test_the_lock_keeps_processes_apart_and_its_file_private(self):
        target = self.root / "settings.json"
        holder = subprocess.Popen(
            [sys.executable, "-c", "import sys\nfrom pathlib import Path\nfrom richi import private_files\n"
             "with private_files.locked(Path(sys.argv[1])):\n    print('held', flush=True)\n    sys.stdin.read()\n",
             str(target)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            with self.assertRaises(TimeoutError):
                with private_files.locked(target, timeout=0.3):
                    pass
        finally:
            holder.stdin.close()
            holder.wait(10)
            holder.stdout.close()
        with private_files.locked(target, timeout=5):
            pass
        self.assertEqual(stat_mode(Path(str(target) + ".lock")), 0o600)

    def test_settings_changed_by_another_process_are_read_again(self):
        self.cli("skill-sources", "add", "first", "--name", "First", "--skills", "~/team")
        self.assertEqual(agents.ids(), (*agents.builtin_ids(), "first"))
        # A map that runs for days learns what a command wrote from the file itself, not from being told.
        path = agents.settings_path()
        settings = json.loads(path.read_text(encoding="utf-8"))
        settings["agents"]["second"] = dict(settings["agents"]["first"], name="Second")
        path.write_text(json.dumps(settings), encoding="utf-8")
        self.assertEqual(agents.ids(), (*agents.builtin_ids(), "first", "second"))
        path.unlink()
        self.assertEqual(agents.ids(), agents.builtin_ids())

    def test_one_writer_changes_the_settings_at_a_time(self):
        self.cli("skill-sources", "add", "first", "--name", "First", "--skills", "~/team")
        path, reading = agents.settings_path(), threading.Event()
        read = agent_commands._read

        def seen(*arguments):
            reading.set()
            return read(*arguments)
        writer = threading.Thread(target=lambda: agent_commands.change("add", "second", {"name": "Second",
                                                                                         "folders": ["~/team"]}),
                                  daemon=True)
        # Another process is between reading the file and replacing it: this change must wait, or
        # the whole file it writes from an older reading would drop the other one's agent.
        with mock.patch.object(agent_commands, "_read", seen):
            with private_files.locked(path):
                writer.start()
                self.assertFalse(reading.wait(0.5), "the settings were read while another writer held them")
            writer.join(10)
        self.assertTrue(reading.is_set())
        self.assertEqual(agents.ids(), (*agents.builtin_ids(), "first", "second"))

    def test_the_settings_are_bounded(self):
        self.cli("skill-sources", "add", "first", "--name", "First", "--skills", "~/team")
        path = agents.settings_path()
        with mock.patch.object(agents, "MAX_CUSTOM", 1):
            refused = self.cli("skill-sources", "add", "second", "--name", "Second", "--skills", "~/team", ok=False)
            self.assertEqual((refused["code"], refused["subject"]), ("limit", "agents"))
        self.cli("skill-sources", "add", "second", "--name", "Second", "--skills", "~/team")
        # A file larger than this version reads is not used and not changed. It is no proof of damage: a newer
        # Richi may read a larger one, so nobody is told to remove it.
        with mock.patch.object(agents, "MAX_SETTINGS", path.stat().st_size - 1):
            agents.forget_settings()
            listed = self.cli("skill-sources", "list")
            self.assertEqual(([agent["id"] for agent in listed["agents"]], str(path) in listed["problem"],
                              listed["problem_code"]), (list(agents.builtin_ids()), True, "large"))
            refused = self.cli("skill-sources", "update", "claude", "--disable", "skills", ok=False)
            self.assertEqual((refused["code"], refused["subject"]), ("settings", "large"))
            self.assertNotIn("remove", listed["problem"] + refused["error"])
        # More own agents than this version keeps (a newer one may keep more): the first ones work, the
        # others are kept unused, and none can be added.
        with mock.patch.object(agents, "MAX_CUSTOM", 1):
            agents.forget_settings()
            listed = self.cli("skill-sources", "list")
            self.assertEqual(([agent["id"] for agent in listed["agents"]], listed["problem"]),
                             ([*agents.builtin_ids(), "first"], None))
            self.assertEqual([item["id"] for item in listed["unknown_agents"]], ["second"])
            self.cli("skill-sources", "update", "claude", "--disable", "skills")
            refused = self.cli("skill-sources", "add", "third", "--name", "Third", "--skills", "~/team", ok=False)
            self.assertEqual((refused["code"], refused["subject"]), ("limit", "agents"))
        self.assertIn("second", json.loads(path.read_text(encoding="utf-8"))["agents"])
        agents.forget_settings()
        self.assertEqual(agents.ids(), (*agents.builtin_ids(), "first", "second"))

    def test_a_folder_stored_for_a_builtin_agent_is_listed_once(self):
        adjusted = self.cli("skill-sources", "update", "claude", "--add-skills", "~/team")["agent"]
        # A folder that is already a built-in source cannot be added; one that got into the file
        # anyway, written by hand or by a Richi with other sources, is not listed a second time.
        path = agents.settings_path()
        settings = json.loads(path.read_text(encoding="utf-8"))
        settings["agents"]["claude"]["folders"].append({"path": "~/.claude/skills", "enabled": True})
        path.write_text(json.dumps(settings), encoding="utf-8")
        claude = self.listed()["claude"]
        self.assertEqual([source["key"] for source in claude["sources"]], ["skills", "plugins", "claude-app", "~/team"])
        self.assertEqual((claude["skills"], adjusted["skills"]), (2, 2))

    def test_enabling_one_builtin_source_keeps_others_at_a_former_folder_off(self):
        path = agents.settings_path()
        path.parent.mkdir(parents=True)
        stored = {"updated_at": "t", "folders": [{"path": "~/.agents/skills", "enabled": False}]}
        path.write_text(agent_commands._serialized({"codex": stored}), encoding="utf-8")
        # With this profile both agents-skills and codex-skills read the saved folder.
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home / ".agents")}):
            before = path.read_bytes()
            unchanged = self.cli("skill-sources", "update", "codex", "--expected-updated-at", "t",
                                 "--disable", "agents-skills")
            self.assertEqual((unchanged["results"][0]["status"], path.read_bytes()), ("unchanged", before))
            changed = self.cli("skill-sources", "update", "codex", "--expected-updated-at", "t",
                               "--enable", "agents-skills")["agent"]
            enabled = {source["key"]: source["enabled"] for source in changed["sources"]}
            self.assertTrue(enabled["agents-skills"])
            self.assertFalse(enabled["codex-skills"])
            self.cli("skill-sources", "update", "codex", "--expected-updated-at", changed["updated_at"],
                     "--disable", "agents-skills")
            restored = json.loads(path.read_text(encoding="utf-8"))["agents"]["codex"]
            self.assertEqual(restored["folders"], stored["folders"])
            self.assertNotIn("disabled", restored)

    def test_changes_are_validated_for_every_caller(self):
        for action, identifier, options in (
                ("add", "x", {"name": "X", "folders": "~/x"}), ("add", "x", {"name": "X", "folders": [5]}),
                ("add", "x", {"name": 5, "folders": ["~/x"]}), ("add", "x", {"name": "X", "folders": ["~/x", "~/x/"]}),
                ("add", "x", {"name": "X", "folders": ["~/x"], "manual_skills": "other"}),
                ("add", "x", {"name": "X", "folders": ["~/x"], "variable": "A B"}),
                ("update", "claude", {"expected_updated_at": 5, "disable": ["skills"]}),
                ("update", "claude", {"disable": "skills"}), ("drop", "claude", {}), ("add", 5, {}),
                ("update", "claude", {"disable": {}, "add_folders": ["~/x"]}),
                ("add", "x", {"name": "X", "folders": ["~/x"], "manual_skills": False}),
                ("add", "x", ["name"]), (["add"], "x", {}), ({"add": 1}, "x", {}), (None, "x", {}),
                # Half of a surrogate pair cannot be written to the settings file.
                ("add", "x", {"name": "X", "folders": ["~/x"], "variable": "A=half \ud800"}),
                ("add", "x", {"name": "half \ud800", "folders": ["~/x"]})):
            with self.subTest(action=action, options=options), self.assertRaises(agent_commands.AgentError):
                agent_commands.change(action, identifier, options)
        # Bytes of a command line that are not text arrive as such halves.
        self.assertIsNone(self.cli("skill-sources", "add", "x", "--name", "X", "--skills", "~/x", "--variable",
                                   "A=\udcff", ok=False).get("code"))
        # ~//x is a folder under the home directory, as its ~ says, not /x.
        self.assertEqual(agent_commands._folder("~//x/", self.home), "~/x")
        self.assertEqual(agents.folder_path("~//x", self.home), self.home / "x")
        with self.assertRaises(agent_commands.AgentError) as raised:
            agent_commands.change("add", "many", {"name": "Many", "folders": ["~/f%d" % n for n in range(21)]})
        self.assertEqual(raised.exception.code, "limit")
        # A refused change and a reading leave nothing behind, not even the settings directory.
        agent_commands.listing()
        self.assertFalse(agents.settings_path().parent.exists())


class UnreadableFolderTests(IsolatedHome):
    def test_a_folder_this_user_may_not_read_does_not_take_the_lists_down(self):
        locked = self.home / "locked"
        (locked / "inner" / "skills").mkdir(parents=True)
        write_skill(self.home / ".claude" / "skills" / "pr-description", "---\nname: pr-description\n---\n")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        if os.access(str(locked), os.R_OK):
            self.skipTest("this user reads every folder")
        added = self.cli("skill-sources", "update", "claude", "--add-skills", "~/locked/inner/skills",
                         "--add-skills", "~/locked")["agent"]
        states = {source["key"]: (source["exists"], source["denied"], source["skills"]) for source in added["sources"]}
        self.assertEqual((states["~/locked/inner/skills"], states["~/locked"], states["skills"]),
                         ((False, True, 0), (False, True, 0), (True, False, 1)))
        self.assertEqual(self.listed()["claude"]["skills"], 1)
        self.assertEqual([skill["name"] for skill in skill_catalog.catalog()["skills"]], ["pr-description"])
        self.assertEqual(skill_catalog.folder_state(self.home / "nothing"), "missing")
        self.assertEqual(skill_catalog.folder_state(self.home / ".claude" / "skills" / "pr-description" / "SKILL.md"),
                         "missing")


class ListingTests(IsolatedHome):
    """What the map reads: the agents, their connection and the skill catalog behind the counts."""

    def test_discovery_reads_a_short_header_and_finds_names_beyond_the_first_chunk(self):
        path = self.home / "SKILL.md"
        path.write_text("---\nname: richi\n---\n" + "body" * 10000)
        text, cut = skill_catalog._read_header(path, frontmatter_only=True)
        self.assertLessEqual(len(text), 1024)
        self.assertEqual(skill_catalog.front_matter(text, cut)["name"], "richi")
        path.write_text("---\n#" + "x" * 1200 + "\nname: project-memory\n---\n")
        self.assertEqual(skill_catalog.front_matter(*skill_catalog._read_header(
            path, frontmatter_only=True))["name"], "project-memory")

    def test_a_nested_yaml_separator_does_not_end_the_skill_header(self):
        root = self.home / ".claude" / "skills"
        metadata = "---\nmetadata:\n  readme: |\n    Intro\n    ---\n    More\n"
        write_skill(root / "restricted", metadata + "disable-model-invocation: true\n---\nBody\n")
        write_skill(root / "folder", metadata + "name: actual-name\n---\nBody\n")
        self.assertEqual([skill["name"] for skill in skill_catalog.catalog({}, self.home)["skills"]], ["actual-name"])

    def test_a_skill_file_that_is_not_a_file_is_skipped(self):
        skills = self.home / ".claude" / "skills"
        write_skill(skills / "real", "---\nname: real\n---\n")
        (skills / "pipe").mkdir()
        # Nobody will ever write to it: reading it as a file would wait for ever.
        os.mkfifo(str(skills / "pipe" / "SKILL.md"))
        (skills / "folder" / "SKILL.md").mkdir(parents=True)
        found = []
        scan = threading.Thread(target=lambda: found.append(skill_catalog.catalog({}, self.home)), daemon=True)
        scan.start()
        scan.join(10)
        self.assertFalse(scan.is_alive(), "the scan is still waiting on the named pipe")
        self.assertEqual([skill["name"] for skill in found[0]["skills"]], ["real"])

    def test_the_listing_gives_the_limits_a_form_needs(self):
        self.assertEqual(agent_commands.listing()["limits"], {"name": agents.MAX_NAME, "folder": agents.MAX_FOLDER})
        # The skill name pattern belongs to the agents, and rules use that one.
        self.assertIs(rules.valid_skill, agents.valid_skill)

    def test_one_unreadable_child_does_not_hide_other_skills(self):
        skills = self.home / ".claude" / "skills"
        (skills / "unreadable").mkdir(parents=True)
        write_skill(skills / "review", "---\nname: review\n---\n")
        is_dir = Path.is_dir

        def readable(path):
            if path.name == "unreadable":
                raise PermissionError("Cannot inspect this skill")
            return is_dir(path)

        with mock.patch.object(Path, "is_dir", readable):
            self.assertEqual([skill["name"] for skill in skill_catalog.catalog()["skills"]], ["review"])

    def test_a_change_returns_the_whole_listing_once_when_asked(self):
        write_skill(self.home / "team" / "release", "---\nname: release\n---\n")
        with mock.patch.object(skill_catalog, "catalog", wraps=skill_catalog.catalog) as scans:
            result = agent_commands.change("update", "claude", {"add_folders": ["~/team"]}, full=True)
        self.assertEqual(scans.call_count, 1)
        self.assertEqual([agent["id"] for agent in result["listing"]["agents"]], list(agents.builtin_ids()))
        self.assertEqual([skill["name"] for skill in result["listing"]["catalog"]["skills"]], ["release"])
        self.assertNotIn("listing", agent_commands.change("update", "claude", {
            "disable": ["plugins"], "expected_updated_at": result["agent"]["updated_at"]}))
        self.assertNotIn("catalog", agent_commands.listing())

    def test_an_unknown_launcher_answer_is_named_not_hidden(self):
        from richi_launcher import agent_setup
        with mock.patch.object(agent_setup, "agents_command", side_effect=ValueError("setup record is damaged")):
            listed = agent_commands.listing()
        self.assertIn("setup record is damaged", listed["connections_problem"])
        self.assertEqual([agent["connection"] for agent in listed["agents"]], [None] * len(agents.AGENTS))
        (self.home / ".agents/skills").mkdir(parents=True)
        (self.home / ".gemini/skills").mkdir(parents=True)
        with mock.patch.object(agent_setup, "agents_command", return_value={"agents": [
                {"agent": "claude", "detected": False, "status": "configured"}]}):
            listed = agent_commands.listing()
            self.assertFalse(next(agent for agent in listed["agents"] if agent["id"] == "cursor")["detected"])
            self.assertFalse(next(agent for agent in listed["agents"] if agent["id"] == "antigravity")["detected"])
            (self.home / ".cursor").mkdir()
            (self.home / ".gemini/config/skills").mkdir(parents=True)
            self.assertTrue(self.listed()["cursor"]["detected"])
            self.assertTrue(self.listed()["antigravity"]["detected"])
        self.assertEqual((listed["connections_problem"], listed["agents"][0]["connection"],
                          listed["agents"][0]["detected"]), (None, "configured", False))


def stat_mode(path):
    return path.stat().st_mode & 0o777


if __name__ == "__main__":
    unittest.main()
