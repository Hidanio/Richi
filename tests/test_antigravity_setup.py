"""Exercise local Antigravity setup, detection and rule protection in disposable profiles."""
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

import test_agent_setup
from richi_launcher import agent_setup, antigravity_setup, installation
from richi_launcher.config import ConfigError


class AntigravitySetupTests(test_agent_setup.AgentSetupFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.profile = self.home / ".gemini/config"
        self.rule = self.profile / "rules/richi-project-memory.md"
        self.skill = self.profile / "skills/richi"

    def connect(self, *extra):
        return self.configure("--agent", "antigravity", *extra)

    def test_discovery_checks_antigravity_paths_and_mac_app_not_bare_gemini_cli(self):
        system = self.home / "system/Applications"
        # A bare ~/.gemini/skills profile belongs to Gemini CLI, not Antigravity.
        (self.home / ".gemini/skills").mkdir(parents=True)
        with patch.object(antigravity_setup, "SYSTEM_APPLICATIONS", system), \
             patch.object(shutil, "which", return_value=None), patch.object(sys, "platform", "darwin"):
            self.assertFalse(antigravity_setup.detected())
            for directory in (system, self.home / "Applications"):
                executable = directory / "Antigravity.app/Contents/MacOS/Antigravity"
                executable.parent.mkdir(parents=True)
                executable.touch(mode=0o644)
                self.assertFalse(antigravity_setup.detected())
                executable.chmod(0o755)
                self.assertTrue(antigravity_setup.detected())
                with patch.object(sys, "platform", "linux"):
                    self.assertFalse(antigravity_setup.detected())
                executable.unlink()
            wrapper = self.home / ".gemini/antigravity/bin/agy"
            wrapper.parent.mkdir(parents=True)
            # ~/.gemini/antigravity itself marks an Antigravity installation.
            self.assertTrue(antigravity_setup.detected())
            shutil.rmtree(self.home / ".gemini/antigravity")
            self.assertFalse(antigravity_setup.detected())
        self.assertFalse(self.profile.exists())

    def test_setup_preserves_user_rules_and_repeated_setup_is_idempotent(self):
        before = self.paths()
        agent_setup.command(["--agent", "antigravity", "--dry-run"])
        self.assertEqual(before, self.paths())
        self.profile.mkdir(parents=True)
        agents_md = self.profile / "AGENTS.md"
        gemini_md = self.profile / "GEMINI.md"
        agents_md.write_bytes(b"# Personal Antigravity rules\n")
        gemini_md.write_bytes(b"# Gemini rules\n")
        self.connect()
        self.assertEqual(agents_md.read_bytes(), b"# Personal Antigravity rules\n")
        self.assertEqual(gemini_md.read_bytes(), b"# Gemini rules\n")
        rule = self.rule.read_text(encoding="utf-8")
        self.assertTrue(rule.startswith("---\ntrigger: always_on\n---\n"))
        self.assertIn("ANTIGRAVITY_CONVERSATION_ID", rule)
        self.assertIn("antigravity:<conversation_id>", rule)
        self.assertIn("`richi` skill", rule)
        self.assertLessEqual(len(self.rule.read_bytes()), antigravity_setup.INSTRUCTION_LIMIT)
        self.assertTrue(self.skill.is_symlink())
        self.assertTrue((self.skill / "SKILL.md").is_file())
        stored = {p: p.read_bytes() for p in (self.rule, self.root / agent_setup.MANIFEST)}
        self.connect()
        for path, data in stored.items():
            self.assertEqual(path.read_bytes(), data, str(path))
        status = agent_setup.command(["--agent", "antigravity", "--status"])
        self.assertEqual((status["status"], status["changes"]), ("configured", []))
        discovered = agent_setup.agents_command(["--status"])["agents"]
        self.assertIn({"agent": "antigravity", "detected": True, "status": "configured"}, discovered)
        self.assertFalse((self.home / ".codex").exists())
        self.assertFalse((self.home / ".claude").exists())
        self.assertFalse((self.home / ".cursor").exists())
        self.assertFalse(list(self.home.rglob("*.sqlite3")))

    def test_foreign_or_edited_rule_and_existing_skill_are_protected(self):
        self.rule.parent.mkdir(parents=True)
        original = b"---\ntrigger: manual\n---\nUser rule\n"
        self.rule.write_bytes(original)
        before = self.paths()
        with self.assertRaises(ConfigError):
            self.connect()
        self.assertEqual(self.rule.read_bytes(), original)
        self.assertEqual(self.paths(), before)
        self.rule.unlink()

        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("custom skill", encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "--replace-skill"):
            self.connect()
        self.connect("--replace-skill")
        backup, = (self.profile / "richi-skill-backups").glob("richi-*")
        self.assertEqual((backup / "SKILL.md").read_text(encoding="utf-8"), "custom skill")

        edited = self.rule.read_bytes().replace(b"always_on", b"manual", 1)
        self.rule.write_bytes(edited)
        with self.assertRaises(ConfigError):
            self.connect()
        self.assertEqual(self.rule.read_bytes(), edited)

        with self.assertRaisesRegex(ConfigError, "24 KB"):
            antigravity_setup.instruction_file(b"x" * antigravity_setup.INSTRUCTION_LIMIT)

    def test_release_updates_and_rollbacks_keep_antigravity_skill_in_sync(self):
        self.connect()
        rule_bytes = self.rule.read_bytes()
        previous = installation._state(self.ctx)
        candidate = self.next_state()
        new_site = Path(candidate["generations"][candidate["current"]]["site"])
        # Older releases whose catalogs lack Antigravity cannot drop its setup.
        with self.assertRaisesRegex(ConfigError, "cannot maintain"):
            installation._activate(self.ctx, candidate, "updated")
        for site in (self.site, new_site):
            launcher = site / "richi_launcher"
            shutil.copyfile(Path(antigravity_setup.__file__), launcher / "antigravity_setup.py")
            (launcher / "agent_adapters.py").write_text("AGENTS = {'antigravity': ('antigravity_setup', 1)}\n")
        with patch.object(installation, "_collect", return_value={}):
            installation._activate(self.ctx, candidate, "updated")
            self.assertIn("Synthetic release 0.2.0", (self.skill / "SKILL.md").read_text(encoding="utf-8"))
            previous["generations"].update(candidate["generations"])
            previous["previous"] = candidate["current"]
            installation._activate(self.ctx, previous, "rolled_back")
        self.assertIn("Synthetic release 0.1.0", (self.skill / "SKILL.md").read_text(encoding="utf-8"))
        self.assertEqual(self.rule.read_bytes(), rule_bytes)


if __name__ == "__main__":
    unittest.main()
