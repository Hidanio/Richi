"""The frozen updater retains its discovery path for one rollback generation."""
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_agent_setup
import test_dev_skill_links
from richi_launcher import agent_setup, installation, setup_files
from richi_launcher.config import ConfigError


class SkillRenameTests(test_agent_setup.AgentSetupFixture, unittest.TestCase):
    def transition(self):
        bundle = self.site / "richi_launcher/agent_skill/SKILL.md"
        bundle.write_text("---\nname: project-memory\ndescription: Old skill\n---\n# Project Memory\n")
        instructions = self.home / ".codex/AGENTS.md"
        instructions.parent.mkdir()
        instructions.write_bytes(b"Keep this user text.\r\n")
        self.configure()
        first = self.next_state()
        with patch.object(installation, "_collect", return_value={}):
            installation._activate(self.ctx, first, "updated")
        # The previous manager would retain this path when rolling back.
        self.old_skill = self.home / ".agents/skills/project-memory"
        self.new_skill = self.old_skill.with_name("richi")
        self.assertIn("name: richi", (self.old_skill / "SKILL.md").read_text())
        self.assertIn(b"`project-memory` skill", instructions.read_bytes())
        self.assertEqual(agent_setup.command(["--status"])["status"], "configured")
        # Both retained generations will have branded bundles after the next update.
        generation = "v0.3.0-test"
        prefix = self.root / "versions" / generation
        site = prefix / "site"
        self.package(site, "0.3.0")
        first["generations"][generation] = dict(kind="release", version="0.3.0", prefix=str(prefix), site=str(site))
        first["previous"], first["current"] = first["current"], generation
        return instructions, first

    def test_interrupted_rename_recovers_profile_instructions_and_manifest_together(self):
        instructions, state = self.transition()
        old_bytes = instructions.read_bytes()
        manifest = self.root / agent_setup.MANIFEST
        old_manifest = manifest.read_bytes()
        operations = agent_setup.activation_operations(self.ctx, state)
        setup_files.begin(self.ctx, operations, generation=state["current"])
        self.assertFalse(self.old_skill.is_symlink())
        self.assertTrue(self.new_skill.is_symlink())
        self.assertTrue(instructions.read_bytes().startswith(b"Keep this user text.\r\n"))
        self.assertIn(b"`richi` skill", instructions.read_bytes())
        setup_files.recover(self.ctx)
        self.assertTrue(self.old_skill.is_symlink())
        self.assertFalse(self.new_skill.is_symlink())
        self.assertEqual(instructions.read_bytes(), old_bytes)
        self.assertEqual(manifest.read_bytes(), old_manifest)

    def test_competing_skill_during_rename_keeps_all_user_and_managed_files(self):
        instructions, state = self.transition()
        old_bytes = instructions.read_bytes()
        old_target = os.readlink(self.old_skill)
        shadow = self.home / ".codex/skills/richi"
        shadow.parent.mkdir()
        for competing in (self.new_skill, shadow):
            with self.subTest(competing=competing):
                competing.write_text("User skill")
                with self.assertRaisesRegex(ConfigError, "outside skill discovery"):
                    installation._activate(self.ctx, state, "updated")
                self.assertEqual(competing.read_text(), "User skill")
                self.assertEqual(os.readlink(self.old_skill), old_target)
                self.assertEqual(instructions.read_bytes(), old_bytes)
                self.assertFalse((self.root / setup_files.JOURNAL).exists())
                self.assertEqual(agent_setup.agents_command(["--status"])["agents"][0]["status"], "conflict")
                self.configure("--replace-skill")
                self.assertFalse(competing.exists())
                backup, = (competing.parent.parent / "richi-skill-backups").glob("richi-*")
                self.assertEqual(backup.read_text(), "User skill")

    def test_first_setup_of_copied_old_skills_requires_explicit_backup(self):
        for agent, root in (("codex", ".agents"), ("claude", ".claude"), ("cursor", ".cursor")):
            with self.subTest(agent=agent):
                old = self.home / root / "skills/project-memory"
                old.mkdir(parents=True)
                (old / "SKILL.md").write_text("Local copy")
                with self.assertRaisesRegex(ConfigError, "replace-skill"):
                    self.configure("--agent", agent)
                result = self.configure("--agent", agent, "--replace-skill")
                backup, = result["skill_backups"]
                self.assertEqual((Path(backup) / "SKILL.md").read_text(), "Local copy")
                self.assertFalse(old.exists())
                self.assertTrue(old.with_name("richi").is_symlink())

    def test_dev_switch_follows_recorded_layout_when_previous_package_is_damaged(self):
        self.transition()
        checkout = self.home / "checkout"
        branded = Path(test_dev_skill_links.DevSkillLinkTests.make_checkout(checkout, "dev"))
        legacy = branded.with_name("project-memory")
        legacy.mkdir()
        (legacy / "SKILL.md").symlink_to(branded / "SKILL.md")
        (self.site / "richi_launcher/agent_skill/SKILL.md").unlink()
        report = agent_setup.switch_skills(True, checkout)
        self.assertEqual(report["status"], "changed")
        self.assertEqual(os.readlink(self.old_skill), str(legacy))
        self.assertTrue(agent_setup.skill_links(True, checkout)["matches_config"])
        self.assertEqual(agent_setup.switch_skills(False, checkout)["status"], "changed")
