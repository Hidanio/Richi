"""Exercise the local Cursor hook contract and setup in disposable profiles."""
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_agent_setup
from richi_launcher import agent_setup, chats, installation, workspaces
from richi_launcher.config import ConfigError


class CursorSetupTests(test_agent_setup.AgentSetupFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.profile = self.home / ".cursor"
        self.settings = self.profile / "hooks.json"
        self.rule = self.profile / "rules/richi-project-memory.mdc"
        self.script = self.profile / "richi/session_start.py"
        self.skill = self.profile / "skills/project-memory"

    def connect(self):
        return self.configure("--agent", "cursor")

    def emit_hook(self, payload):
        # Execute the script and options as registered in the actual hooks.json.
        entry, = json.loads(self.settings.read_text())["hooks"]["sessionStart"]
        python, *options = shlex.split(entry["command"])
        self.assertEqual(python, str(self.prefix / "bin/python"))
        return subprocess.run([sys.executable, *options], input=json.dumps(payload),
                              text=True, capture_output=True, cwd=self.home)

    def test_native_configuration_preserves_other_hooks_and_repeated_setup_is_idempotent(self):
        before = self.paths()
        agent_setup.command(["--agent", "cursor", "--dry-run"])
        self.assertEqual(before, self.paths())
        self.profile.mkdir()
        user_start = {"command": "./my-hook.sh", "timeout": 10}
        stop = [{"command": "./stop.sh"}]
        self.settings.write_text(json.dumps({"version": 1, "hooks": {"sessionStart": [user_start],
                                                                      "stop": stop}, "custom": "kept"}))
        self.settings.chmod(0o640)
        self.connect()
        configuration = json.loads(self.settings.read_text())
        self.assertEqual(configuration["version"], 1)
        self.assertEqual(configuration["custom"], "kept")
        self.assertEqual(configuration["hooks"]["stop"], stop)
        existing, installed = configuration["hooks"]["sessionStart"]
        self.assertEqual(existing, user_start)
        self.assertEqual(shlex.split(installed["command"]), [str(self.prefix / "bin/python"), "-I", str(self.script)])
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o640)
        rule = self.rule.read_text()
        self.assertTrue(rule.startswith("---\n"))
        self.assertIn("alwaysApply: true", rule.split("---", 2)[1])
        self.assertIn("--chat", rule)
        self.assertTrue(self.skill.is_symlink())
        self.assertTrue((self.skill / "SKILL.md").is_file())
        stored = {p: p.read_bytes() for p in (self.rule, self.script, self.settings, self.root / agent_setup.MANIFEST)}
        self.connect()
        for path, data in stored.items():
            self.assertEqual(path.read_bytes(), data, str(path))
        status = agent_setup.command(["--agent", "cursor", "--status"])
        self.assertEqual((status["status"], status["changes"]), ("configured", []))
        discovered = agent_setup.agents_command(["--status"])["agents"]
        self.assertIn({"agent": "cursor", "detected": True, "status": "configured"}, discovered)
        self.assertFalse((self.home / ".codex").exists())
        self.assertFalse((self.home / ".claude").exists())
        self.assertFalse(list(self.home.rglob("*.sqlite3")))

    def test_hook_chat_identity_survives_resume_and_isolates_two_chats(self):
        self.connect()
        for workspace in ("alpha", "beta"):
            workspaces.create_workspace(workspace)
        for session, workspace, resume in (("a-123", "alpha", False), ("b-456", "beta", False),
                                            ("a-123", "alpha", True)):
            output = self.emit_hook({"hook_event_name": "sessionStart", "session_id": session,
                                     "conversation_id": session})
            self.assertEqual(output.returncode, 0, output.stderr)
            event = json.loads(output.stdout)
            chat = "cursor:" + session
            self.assertEqual(event["env"]["RICHI_CHAT_ID"], chat)
            self.assertIn("--chat " + chat, event["additional_context"])
            self.assertIn("chat current", event["additional_context"])
            self.assertIn("workspace list", event["additional_context"])
            self.assertEqual(chats.current_chat(chat)["bound"], resume)
            if not resume:
                chats.bind_chat(chat, workspace)
            self.assertEqual(chats.pin_chat(chat, {})["workspace"], workspace)
        self.assertFalse(list(self.home.rglob("*.sqlite3")))

    def test_hook_rejects_missing_unsafe_and_conflicting_identity(self):
        self.connect()
        base = {"hook_event_name": "sessionStart", "session_id": "good"}
        malformed = ({}, dict(base, session_id="../invalid"), dict(base, session_id="x" * 194),
                     dict(base, session_id="hello world"), dict(base, session_id=123),
                     dict(base, hook_event_name="sessionEnd"), dict(base, conversation_id="other"))
        for payload in malformed:
            with self.subTest(payload=payload):
                output = self.emit_hook(payload)
                self.assertNotEqual(output.returncode, 0)
                self.assertFalse(output.stdout.strip())

    def test_malformed_settings_and_foreign_rule_block_setup_before_writes(self):
        self.profile.mkdir()
        for raw in (b'{"version":', b'{"hooks":{},"hooks":{}}', b'{"version":true}',
                    b'{"version":2}', b'[]', b'{"hooks":[]}', b'{"hooks":{"sessionStart":"oops"}}'):
            with self.subTest(raw=raw):
                self.settings.write_bytes(raw)
                before = self.paths()
                with self.assertRaises(ConfigError):
                    self.connect()
                self.assertEqual(self.paths(), before)
                self.assertEqual(self.settings.read_bytes(), raw)
        self.settings.unlink()
        self.rule.parent.mkdir()
        original = b"---\nalwaysApply: false\n---\nUser-authored rule\n"
        self.rule.write_bytes(original)
        before = self.paths()
        with self.assertRaises(ConfigError):
            self.connect()
        self.assertEqual(self.rule.read_bytes(), original)
        self.assertEqual(self.paths(), before)

    def test_edited_rule_header_script_or_hook_registration_is_preserved(self):
        self.connect()
        rule = self.rule.read_bytes()
        self.assertIn(b"alwaysApply: true", rule)
        settings = json.loads(self.settings.read_text())
        settings["hooks"]["sessionStart"].clear()
        changed = ((self.rule, rule.replace(b"alwaysApply: true", b"alwaysApply: false", 1)),
                   (self.script, b"local edits"), (self.settings, json.dumps(settings).encode()))
        for path, edit in changed:
            with self.subTest(path=path):
                original = path.read_bytes()
                path.write_bytes(edit)
                with self.assertRaises(ConfigError):
                    self.connect()
                self.assertEqual(path.read_bytes(), edit)
                path.write_bytes(original)

    def test_release_updates_and_rollbacks_select_matching_hook_and_skill(self):
        from richi_launcher import cursor_setup
        self.connect()
        initial = self.script.read_bytes()
        original_settings = self.settings.read_bytes()
        previous = installation._state(self.ctx)
        candidate = self.next_state()
        new_site = Path(candidate["generations"][candidate["current"]]["site"])
        # Older releases whose catalogs lack Cursor cannot drop its setup.
        with self.assertRaisesRegex(ConfigError, "cannot maintain"):
            installation._activate(self.ctx, candidate, "updated")
        self.assertEqual(self.script.read_bytes(), initial)
        for site in (self.site, new_site):
            launcher = site / "richi_launcher"
            shutil.copyfile(Path(cursor_setup.__file__), launcher / "cursor_setup.py")
            (launcher / "agent_adapters.py").write_text("AGENTS = {'cursor': ('cursor_setup', 1)}\n")
        candidate_source = new_site / "richi_launcher/cursor_setup.py"
        new_hook = initial.decode() + "\n# Synthetic release 0.2.0\n"
        candidate_source.write_text("HOOK = " + repr(new_hook) + "\n")
        with patch.object(installation, "_collect", return_value={}):
            installation._activate(self.ctx, candidate, "updated")
            self.assertEqual(self.script.read_text(), new_hook)
            self.assertIn("Synthetic release 0.2.0", (self.skill / "SKILL.md").read_text())
            previous["generations"].update(candidate["generations"])
            previous["previous"] = candidate["current"]
            installation._activate(self.ctx, previous, "rolled_back")
        self.assertEqual(self.script.read_bytes(), initial)
        self.assertIn("Synthetic release 0.1.0", (self.skill / "SKILL.md").read_text())
        self.assertEqual(self.settings.read_bytes(), original_settings)
