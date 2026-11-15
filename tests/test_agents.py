from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

from richi import agent_commands, agents, memory, skill_catalog
from richi_launcher import config


def write_skill(directory, header):
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(header, encoding="utf-8")


def synthetic_agent_sources(environ, home):
    return [agents.SkillSource(key="skills", kind="skills", layout="skills", path=home / ".synthetic" / "skills")]


# A third agent, described only by a registry entry, as a new integration would be.
SYNTHETIC = agents.Agent(id="synthetic", name="Synthetic Agent",
                         session_variables=("SYNTHETIC_SESSION_ID",), shell_variables={"SYNTHETIC_SHELL": "1"},
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
                    or key in {"CODEX_THREAD_ID", "CLAUDECODE", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_CONFIG_HOME"}]:
            del os.environ[key]
        agents._cache.clear()
        self.addCleanup(agents._cache.clear)

    def cli(self, *argv, ok=True, config_file=None):
        out, err = io.StringIO(), io.StringIO()
        selectors = ["--config", str(config_file)] if config_file else []
        with redirect_stdout(out), redirect_stderr(err), mock.patch.object(sys, "stdin", io.StringIO("")):
            result = memory.main([*selectors, *map(str, argv)])
        self.assertEqual(result == 0, ok, out.getvalue() + err.getvalue())
        return json.loads(out.getvalue() if ok else err.getvalue())

    def listed(self):
        return {agent["id"]: agent for agent in self.cli("skill-sources", "list")["agents"]}


class RegistryTests(IsolatedHome):
    def test_entries_are_unique_and_well_formed(self):
        self.assertEqual(agents.ids(), ("claude", "codex"))
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
                    self.assertIn(source.layout, {"skills", "cache", "claude-app"})
        self.assertEqual(agents.describe(), [{"id": "claude", "name": "Claude Code"}, {"id": "codex", "name": "Codex"}])
        for identifier, name, folder in agents.PRESETS:
            with self.subTest(preset=identifier):
                self.assertTrue(agents.valid_id(identifier) and agents.valid_name(name) and agents.valid_folder(folder))
                self.assertNotIn(identifier, agents.builtin_ids())
        self.assertEqual(len({preset[0] for preset in agents.PRESETS}), len(agents.PRESETS))

    def test_detection_prefers_chat_identity_then_session_then_shell(self):
        self.assertIsNone(agents.detect({}))
        self.assertEqual(agents.detect({"CLAUDECODE": "1"}), "claude")
        # Codex started from a Claude Code terminal inherits CLAUDECODE but has its own session.
        self.assertEqual(agents.detect({"CLAUDECODE": "1", "CODEX_THREAD_ID": "thread"}), "codex")
        self.assertEqual(agents.detect({"RICHI_CHAT_ID": "claude:chat", "CODEX_THREAD_ID": "thread"}), "claude")
        self.assertIsNone(agents.detect({"RICHI_CHAT_ID": "claude", "CLAUDECODE": "0"}))
        self.assertIsNone(agents.detect({"RICHI_CHAT_ID": "unknown:chat"}))
        self.assertEqual(agents.variables(agents.AGENTS[0]), ["CLAUDECODE=1"])
        self.assertEqual(agents.variables(agents.AGENTS[1]), ["CODEX_THREAD_ID"])
        for value in ("Claude", "-x", "a" * 33, "", None, "with space"):
            self.assertFalse(agents.valid_id(value))

    def test_settings_live_in_the_launchers_configuration_directory(self):
        self.assertEqual(agents.settings_path(), config._platform_dirs()[0] / agents.SETTINGS_FILE)
        self.assertEqual(agents.settings_path({}, self.home).name, "agents.json")
        with mock.patch.object(agents.sys, "platform", "linux"), mock.patch.object(config.sys, "platform", "linux"):
            for configured in (None, str(self.root / "xdg"), "relative"):
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
        every = self.add("every", "claude=x", "codex=x", "synthetic=x")["rule"]
        self.assertEqual((every["skill"], every["skills"]), ("x", None))
        with mock.patch.dict(os.environ, {"SYNTHETIC_SESSION_ID": "session", "CLAUDECODE": "1"}):
            shown = self.command("rules", "show")
        self.assertEqual(shown["agent"], "synthetic")
        self.assertEqual({item["id"]: item["use_skill"] for item in shown["rules"]},
                         {"every": "x", "partial": None, "review": "reviewer"})
        self.assertIn("synthetic: Synthetic Agent", self.command("rules", "show")["note"])
        self.assertEqual(self.command("rules", "list")["agents"][-1], {"id": "synthetic", "name": "Synthetic Agent"})

    def test_unknown_agents_are_refused_but_kept_when_a_rule_already_names_them(self):
        self.assertIn("known agents: claude, codex, synthetic", self.add("x", "cursor=review", ok=False)["error"])
        # A newer Richi may have stored a skill for an agent this one does not know.
        self.add("newer", "review")
        database = self.config.parent / "memory.sqlite3"
        with sqlite3.connect(str(database)) as conn:
            row = json.loads(conn.execute("SELECT value FROM metadata WHERE key = 'rule:newer'").fetchone()[0])
            row["skills"] = {"cursor": "cursor-review"}
            conn.execute("UPDATE metadata SET value = ? WHERE key = 'rule:newer'", (json.dumps(row),))
        listed = self.command("rules", "list")["rules"][0]
        self.assertEqual(listed["skills"], {"cursor": "cursor-review"})
        shown = self.command("rules", "show", "--agent", "cursor")["rules"][0]
        self.assertEqual(shown["use_skill"], "cursor-review")
        changed = self.command("rules", "update", "newer", "--expected-updated-at", listed["updated_at"],
                               "--skill", "review", "--skill", "cursor=cursor-review-2")["rule"]
        self.assertEqual((changed["skill"], changed["skills"]), ("review", {"cursor": "cursor-review-2"}))
        self.command("rules", "show", "--agent", "Not An Agent", ok=False)

    def test_catalog_reads_skill_sources_from_the_registry(self):
        write_skill(self.home / ".synthetic" / "skills" / "reviewer", "---\nname: reviewer\ndescription: Reviews\n---\n")
        write_skill(self.home / ".agents" / "skills" / "reviewer", "---\nname: reviewer\n---\n")
        result = skill_catalog.catalog({}, self.home)
        skills = {skill["name"]: skill for skill in result["skills"]}
        self.assertEqual(skills["reviewer"]["agents"], ["codex", "synthetic"])
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

    def test_an_own_agent_is_described_detected_and_gets_its_skill(self):
        before = self.cli("skill-sources", "list")
        self.assertEqual([agent["id"] for agent in before["agents"]], ["claude", "codex"])
        self.assertIsNone(before["problem"])
        presets = {preset["id"]: preset for preset in before["presets"]}
        self.assertEqual((presets["gemini-cli"]["folder"], presets["gemini-cli"]["exists"], presets["cursor"]["exists"]),
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
        self.assertIsNone(agents.detect({"GEMINI_CLI": "1", "CLAUDECODE": "1"}))
        self.assertEqual(agents.detect({"GEMINI_CLI": "1", "CODEX_THREAD_ID": "thread"}), "codex")
        self.assertEqual(agents.detect({"RICHI_CHAT_ID": "gemini-cli:chat", "CLAUDECODE": "1"}), "gemini-cli")
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
        self.assertEqual(self.cli("skill-sources", "update", "cursor", "--name", "Cursor", ok=False)["code"], "not_found")
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
        self.assertEqual(names, {"release": ["claude"]})
        self.assertEqual(self.cli("skill-sources", "update", "claude", "--enable", "skills", ok=False)["code"], "conflict")
        self.assertEqual(self.cli("skill-sources", "update", "claude", "--expected-updated-at", adjusted["updated_at"],
                                  "--add-skills", "~/.claude/skills", ok=False)["code"], "exists")
        # Undoing every adjustment leaves no stored settings, as if nothing was changed.
        restored = self.cli("skill-sources", "update", "claude", "--expected-updated-at", adjusted["updated_at"],
                            "--enable", "skills", "--remove-skills", "~/team")["agent"]
        self.assertEqual((restored["updated_at"], restored["skills"]), (None, 1))
        self.assertEqual(json.loads(agents.settings_path().read_text(encoding="utf-8")), {"version": 1, "agents": {}})
        again = self.cli("skill-sources", "update", "claude", "--disable", "claude-app")["agent"]
        reset = self.cli("skill-sources", "remove", "claude", "--expected-updated-at", again["updated_at"])
        self.assertEqual((reset["results"][0]["status"], reset["agent"]["updated_at"]), ("reset", None))
        self.assertTrue(all(source["enabled"] for source in reset["agent"]["sources"]))

    def test_a_damaged_settings_file_is_reported_and_never_breaks_commands(self):
        path = agents.settings_path()
        path.parent.mkdir(parents=True)
        for text in ("{not json", json.dumps({"version": 2, "agents": {}}),
                     json.dumps({"version": 1, "agents": {"Bad ID": {"updated_at": "t"}}}),
                     json.dumps({"version": 1, "agents": {"x": {"updated_at": "t", "name": "X",
                                                                "folders": [{"path": "relative", "enabled": True}]}}})):
            with self.subTest(text=text[:30]):
                path.write_text(text, encoding="utf-8")
                agents._cache.clear()
                listed = self.cli("skill-sources", "list")
                self.assertEqual([agent["id"] for agent in listed["agents"]], ["claude", "codex"])
                self.assertIn(str(path), listed["problem"])
                self.assertEqual(agents.ids(), ("claude", "codex"))
                self.assertEqual(self.cli("skill-sources", "update", "claude", "--disable", "skills", ok=False)["code"],
                                 "settings")
                self.assertEqual(path.read_text(encoding="utf-8"), text)
        # Settings a newer Richi wrote for a built-in agent this one lacks are kept and ignored.
        path.write_text(json.dumps({"version": 1, "agents": {"future": {"updated_at": "t", "disabled": ["skills"]}}}),
                        encoding="utf-8")
        agents._cache.clear()
        self.assertEqual((agents.ids(), self.cli("skill-sources", "list")["problem"]), (("claude", "codex"), None))
        self.cli("skill-sources", "update", "claude", "--disable", "skills")
        self.assertIn("future", json.loads(path.read_text(encoding="utf-8"))["agents"])

    def test_changes_are_validated_for_every_caller(self):
        for action, identifier, options in (
                ("add", "x", {"name": "X", "folders": "~/x"}), ("add", "x", {"name": "X", "folders": [5]}),
                ("add", "x", {"name": 5, "folders": ["~/x"]}), ("add", "x", {"name": "X", "folders": ["~/x", "~/x/"]}),
                ("add", "x", {"name": "X", "folders": ["~/x"], "manual_skills": "other"}),
                ("add", "x", {"name": "X", "folders": ["~/x"], "variable": "A B"}),
                ("update", "claude", {"expected_updated_at": 5, "disable": ["skills"]}),
                ("update", "claude", {"disable": "skills"}), ("drop", "claude", {}), ("add", 5, {}),
                ("add", "x", ["name"])):
            with self.subTest(action=action, options=options), self.assertRaises(agent_commands.AgentError):
                agent_commands.change(action, identifier, options)
        self.assertFalse(agents.settings_path().exists())
        with self.assertRaises(agent_commands.AgentError) as raised:
            agent_commands.change("add", "many", {"name": "Many", "folders": ["~/f%d" % n for n in range(21)]})
        self.assertEqual(raised.exception.code, "limit")


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

    def test_a_change_returns_the_whole_listing_once_when_asked(self):
        write_skill(self.home / "team" / "release", "---\nname: release\n---\n")
        with mock.patch.object(skill_catalog, "catalog", wraps=skill_catalog.catalog) as scans:
            result = agent_commands.change("update", "claude", {"add_folders": ["~/team"]}, full=True)
        self.assertEqual(scans.call_count, 1)
        self.assertEqual([agent["id"] for agent in result["listing"]["agents"]], ["claude", "codex"])
        self.assertEqual([skill["name"] for skill in result["listing"]["catalog"]["skills"]], ["release"])
        self.assertNotIn("listing", agent_commands.change("update", "claude", {
            "disable": ["plugins"], "expected_updated_at": result["agent"]["updated_at"]}))
        self.assertNotIn("catalog", agent_commands.listing())

    def test_an_unknown_launcher_answer_is_named_not_hidden(self):
        from richi_launcher import agent_setup
        with mock.patch.object(agent_setup, "agents_command", side_effect=ValueError("setup record is damaged")):
            listed = agent_commands.listing()
        self.assertIn("setup record is damaged", listed["connections_problem"])
        self.assertEqual([agent["connection"] for agent in listed["agents"]], [None, None])
        with mock.patch.object(agent_setup, "agents_command", return_value={"agents": [
                {"agent": "claude", "detected": False, "status": "configured"}]}):
            listed = agent_commands.listing()
        self.assertEqual((listed["connections_problem"], listed["agents"][0]["connection"],
                          listed["agents"][0]["detected"]), (None, "configured", False))


def stat_mode(path):
    return path.stat().st_mode & 0o777


if __name__ == "__main__":
    unittest.main()
