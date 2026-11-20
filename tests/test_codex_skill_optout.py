"""Renaming or retargeting a Codex skill preserves path-based opt-outs."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_agent_setup
import test_dev_skill_links
import test_skill_rename
from richi import codex_skill_settings, skill_catalog
from richi_launcher import agent_setup, installation, setup_files
from richi_launcher.config import ConfigError


class CodexSkillOptoutTests(test_agent_setup.AgentSetupFixture, unittest.TestCase):
    transition = test_skill_rename.SkillRenameTests.transition

    def package(self, site, version):
        super().package(site, version)
        (site / "richi_launcher/agent_setup.py").write_text(test_dev_skill_links.DEV_CAPABLE)

    def settings(self, *entries):
        config = self.home / ".codex/config.toml"
        config.parent.mkdir(exist_ok=True)
        config.write_text("".join("[[skills.config]]\npath = " + json.dumps(str(path))
                                  + "\nenabled = " + json.dumps(enabled) + "\n" for path, enabled in entries))
        return config

    def activate(self, state, action="updated"):
        with patch.object(installation, "_collect", return_value={}):
            return installation._activate(self.ctx, state, action)

    def test_fresh_setup_skips_a_disabled_foreign_name_only_for_codex(self):
        doc = self.home / ".agents/skills/my-tool/SKILL.md"
        doc.parent.parent.mkdir(parents=True)
        target = self.home / "personal/SKILL.md"
        target.parent.mkdir()
        target.write_text("---\nname: richi\n---\nPersonal skill.\n")
        doc.parent.symlink_to(target.parent, target_is_directory=True)
        config = self.settings(("../.agents/skills/my-tool/SKILL.md", False), (target, True))
        with self.assertRaisesRegex(ConfigError, "Another skill named richi"):
            self.configure()
        # The resolved file is disabled even though discovery uses a symlink.
        self.settings((target, False))
        before = config.read_bytes()
        catalog = skill_catalog.catalog()
        skill, = catalog["skills"]
        self.assertEqual((skill["name"], skill["agents"]), ("richi", ["cursor"]))
        self.assertEqual(self.configure()["status"], "configured")
        self.assertTrue(doc.parent.is_symlink())
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual(skill_catalog.catalog()["rule_aliases"], {"codex": {"project-memory": "richi"}})

    def test_rename_waits_for_disabled_destination_and_keeps_user_config(self):
        instructions, state = self.transition()
        old = self.old_skill / "SKILL.md"
        new = self.new_skill / "SKILL.md"
        previous = instructions.read_bytes()
        for entries in ([(old, False)], [(self.old_skill, False)], [(old.resolve(), False)],
                        [(old, False), (new.parent, False)], [(old, False), (new, False), (new, True)]):
            with self.subTest(entries=entries):
                config = self.settings(*entries)
                data = config.read_bytes()
                with self.assertRaisesRegex(ConfigError, "disabled.*setting"):
                    self.activate(state)
                self.assertTrue(self.old_skill.is_symlink())
                self.assertFalse(self.new_skill.is_symlink())
                self.assertEqual(instructions.read_bytes(), previous)
                self.assertEqual(config.read_bytes(), data)
                self.assertFalse((self.root / setup_files.JOURNAL).exists())
        config = self.settings((old, False), (new, False))
        data = config.read_bytes()
        self.activate(state)
        self.assertFalse(self.old_skill.is_symlink())
        self.assertTrue(self.new_skill.is_symlink())
        self.assertEqual(config.read_bytes(), data)

    def test_dev_switch_preserves_a_disabled_resolved_skill_target(self):
        self.configure()
        skill = self.home / ".agents/skills/richi"
        checkout = self.home / "checkout"
        target = test_dev_skill_links.DevSkillLinkTests.make_checkout(checkout, "branded")
        old_file = (skill / "SKILL.md").resolve()
        self.settings((old_file, False))
        self.assertEqual(agent_setup.switch_skills(True, checkout)["status"], "conflict")
        self.assertEqual((skill / "SKILL.md").resolve(), old_file)
        self.settings((old_file, False), (skill / "SKILL.md", False))
        self.assertEqual(agent_setup.switch_skills(True, checkout)["status"], "changed")
        self.assertEqual(os.readlink(skill), target)

    def test_claude_only_rename_in_codex_profile_preserves_codex_optout(self):
        profile = self.home / ".codex"
        old_file = profile / "skills/project-memory/SKILL.md"
        new_file = profile / "skills/richi/SKILL.md"
        checkout = self.home / "checkout"
        branded = Path(test_dev_skill_links.DevSkillLinkTests.make_checkout(checkout, "pinned"))
        legacy = branded.rename(branded.with_name("project-memory"))
        (legacy / "SKILL.md").write_text("---\nname: project-memory\n---\nPinned checkout\n")
        with patch.dict(os.environ, CLAUDE_CONFIG_DIR=str(profile)):
            self.configure("--agent", "claude")
            self.assertEqual(set(agent_setup._records(self.ctx)), {"claude"})
            self.assertEqual(agent_setup.switch_skills(True, checkout)["status"], "changed")
            config = self.settings((old_file, False))
            original = config.read_bytes()
            self.assertEqual(agent_setup.switch_skills(False, None)["status"], "conflict")
            branded.mkdir()
            (branded / "SKILL.md").write_text("---\nname: richi\n---\nUpdated checkout\n")
            next_release = self.next_state()
            with self.assertRaisesRegex(ConfigError, "disabled skill setting"):
                self.activate(next_release)
            self.assertEqual(config.read_bytes(), original)
            self.assertEqual(os.readlink(old_file.parent), str(legacy))
            self.assertFalse(new_file.parent.is_symlink())
            self.settings((old_file, False), (new_file, False))
            self.activate(next_release)
            self.assertEqual(os.readlink(new_file.parent), str(branded))

    def test_chat_current_with_cyclic_codex_skills_reports_other_discovery_conflicts(self):
        self.transition()
        cyclic = self.home / ".codex/skills"
        cyclic.symlink_to(cyclic.name)
        self.new_skill.write_text("Existing user skill")
        result = test_dev_skill_links.DevSkillLinkTests.cli(self, "--chat", "test:cycle", "chat", "current")
        notice, = result["skill_conflicts"]
        self.assertEqual(notice["conflicting_paths"], [str(self.new_skill)])
        self.assertEqual(os.readlink(cyclic), cyclic.name)

    def test_replace_skill_preserves_disabled_copy_in_alternate_discovery_root(self):
        old = self.home / ".codex/skills/project-memory/SKILL.md"
        old.parent.mkdir(parents=True)
        old.write_text("Local copy")
        self.settings((old, False))
        with self.assertRaisesRegex(ConfigError, "disabled.*setting"):
            self.configure("--replace-skill")
        self.assertEqual(old.read_text(), "Local copy")
        self.assertFalse((self.root / agent_setup.MANIFEST).exists())

    def test_replace_skill_backs_up_a_cyclic_link_with_an_unrelated_disabled_skill(self):
        old = self.home / ".agents/skills/project-memory"
        old.parent.mkdir(parents=True)
        old.symlink_to(old.name)
        config = self.settings((self.home / ".agents/skills/other/SKILL.md", False))
        data = config.read_bytes()
        result = self.configure("--replace-skill")
        backup, = result["skill_backups"]
        self.assertEqual(os.readlink(backup), old.name)
        self.assertFalse(old.is_symlink())
        self.assertTrue(old.with_name("richi").is_symlink())
        self.assertEqual(config.read_bytes(), data)

    def test_without_toml_reader_unrelated_config_allows_rename_and_possible_optout_defers_it(self):
        _, state = self.transition()
        config = self.settings((self.old_skill / "SKILL.md", False))
        with patch.object(codex_skill_settings, "_toml_reader", return_value=None):
            self.assertNotIn("codex", skill_catalog.catalog()["rule_aliases"])
            with self.assertRaisesRegex(ConfigError, "Cannot verify possible disabled"):
                self.activate(state)
            config.write_text('[features]\nexample = false\n[projects."/example"]\ntrust_level = "trusted"\n')
            self.assertEqual(skill_catalog.catalog()["rule_aliases"]["codex"], {"project-memory": "richi"})
            self.activate(state)
        self.assertTrue(self.new_skill.is_symlink())

    def test_rollback_with_damaged_outgoing_bundle_keeps_the_pinned_branded_checkout(self):
        self.configure()
        checkout = self.home / "checkout"
        target = test_dev_skill_links.DevSkillLinkTests.make_checkout(checkout, "branded")
        agent_setup.switch_skills(True, checkout)
        state = self.next_state()
        self.activate(state)
        site = Path(state["generations"][state["current"]]["site"])
        (site / "richi_launcher/agent_skill/SKILL.md").write_bytes(b"\xff")
        state["current"], state["previous"] = state["previous"], state["current"]
        result = self.activate(state, "rolled_back")
        self.assertEqual(result["agent_skills"]["codex"], {"mode": "dev", "target": target})
        self.assertEqual(agent_setup.command(["--status"])["dev_source"], str(checkout))
