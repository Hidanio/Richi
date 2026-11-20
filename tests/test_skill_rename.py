"""The frozen updater retains its discovery path for one rollback generation."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_agent_setup
import test_dev_skill_links
from richi_launcher import agent_setup, installation, setup_files
from richi_launcher.config import ConfigError


class SkillRenameTests(test_agent_setup.AgentSetupFixture, unittest.TestCase):
    def package(self, site, version):
        super().package(site, version)
        (site / "richi_launcher/agent_setup.py").write_text(test_dev_skill_links.DEV_CAPABLE)

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

    def test_chat_rename_notice_checks_both_codex_roots_and_verifies_managed_copy(self):
        self.transition()
        self.assertEqual(agent_setup.rename_conflicts(), [])
        shadow = self.home / ".codex/skills/richi"
        for path in (self.new_skill, shadow):
            path.mkdir(parents=True)
            (path / "SKILL.md").write_text("---\nname: richi\n---\nUser skill\n")
        notice, = agent_setup.rename_conflicts()
        self.assertEqual(notice["agent"], "codex")
        self.assertEqual(notice["managed_skill"], str(self.old_skill / "SKILL.md"))
        self.assertEqual(notice["conflicting_paths"], [str(self.new_skill), str(shadow)])
        self.old_skill.unlink()
        self.old_skill.symlink_to(shadow)
        notice, = agent_setup.rename_conflicts()
        self.assertNotIn("managed_skill", notice)
        self.assertIn("changed", notice["managed_skill_error"])
        self.assertEqual(self.old_skill.resolve(), shadow)

    def test_foreign_frontmatter_in_cursor_compatibility_root_stays_user_owned(self):
        self.transition()
        self.configure("--agent", "cursor")
        self.assertEqual(agent_setup.rename_conflicts(), [])  # Cursor also sees Codex's owned link.
        foreign = self.home / ".agents/skills/user-tools"
        foreign.mkdir()
        doc = foreign / "SKILL.md"
        doc.write_text("---\nname: richi\ndescription: Personal tool\n---\n")
        notices = {item["agent"]: item for item in agent_setup.rename_conflicts()}
        self.assertEqual(set(notices), {"codex", "cursor"})
        for agent, notice in notices.items():
            self.assertEqual(notice["conflicting_paths"], [str(foreign)])
            self.assertNotIn("preview_command", notice)
            self.assertTrue(Path(notice["managed_skill"]).is_file())
            with self.assertRaisesRegex(ConfigError, str(foreign)):
                self.configure("--agent", agent, "--replace-skill")
        self.assertTrue(doc.is_file())

    def test_fresh_cursor_setup_checks_skills_in_other_profiles(self):
        foreign = self.home / ".claude/skills/tools/user"
        foreign.mkdir(parents=True)
        doc = foreign / "SKILL.md"
        doc.write_text("---\nname: richi\n---\nPersonal skill\n")
        with self.assertRaisesRegex(ConfigError, str(foreign)):
            self.configure("--agent", "cursor", "--replace-skill")
        self.assertTrue(doc.is_file())
        self.assertFalse((self.home / ".cursor/skills/richi").exists())

    def test_background_update_checks_the_configured_codex_and_claude_profiles(self):
        profiles = {"CODEX_HOME": str(self.home / ".codex"),
                    "CLAUDE_CONFIG_DIR": str(self.home / "claude-custom")}
        with patch.dict(os.environ, profiles):
            _, state = self.transition()
            self.configure("--agent", "claude")
        paths = {}
        for agent, profile in zip(("codex", "claude"), profiles.values()):
            paths[agent] = Path(profile) / "skills/other"
            paths[agent].mkdir(parents=True)
            (paths[agent] / "SKILL.md").write_text("---\nname: richi\n---\nUser skill.")
        with patch.dict(os.environ, {key: str(self.home / "unused") for key in profiles}):
            notices = agent_setup.rename_conflicts()
            self.assertEqual({item["agent"]: item["conflicting_paths"] for item in notices},
                             {agent: [str(path)] for agent, path in paths.items()})
            with self.assertRaisesRegex(ConfigError, "Another skill named richi"):
                installation._activate(self.ctx, state, "updated")
        self.assertTrue(self.old_skill.is_symlink())
        self.assertFalse(self.new_skill.exists())

    def test_empty_symlinked_codex_skill_root_allows_managed_rename(self):
        _, state = self.transition()
        external = self.home / "dotfiles/skills"
        external.mkdir(parents=True)
        (self.home / ".codex/skills").symlink_to(external)
        with patch.object(installation, "_collect", return_value={}):
            installation._activate(self.ctx, state, "updated")
        self.assertTrue(self.new_skill.is_symlink())
        self.assertEqual(list(external.iterdir()), [])

    def test_rename_does_not_adopt_an_unrecorded_link_to_the_same_bundle(self):
        _, state = self.transition()
        target = Path(state["generations"][state["current"]]["site"]) / "richi_launcher/agent_skill"
        self.new_skill.symlink_to(target)
        with self.assertRaisesRegex(ConfigError, "already exists"):
            installation._activate(self.ctx, state, "updated")
        self.assertEqual(os.readlink(self.new_skill), str(target))
        self.assertTrue(self.old_skill.is_symlink())

    def test_second_codex_root_pointing_to_its_managed_directory_is_the_same_skill(self):
        _, state = self.transition()
        (self.home / ".codex/skills").symlink_to(self.old_skill.parent)
        self.assertEqual(agent_setup.rename_conflicts(), [])
        with patch.object(installation, "_collect", return_value={}):
            installation._activate(self.ctx, state, "updated")
        self.assertEqual(agent_setup.command(["--status"])["status"], "configured")

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

    def test_pinned_legacy_checkout_remains_discoverable_across_branded_updates(self):
        instructions, state = self.transition()
        checkout = self.home / "pinned"
        branded = Path(test_dev_skill_links.DevSkillLinkTests.make_checkout(checkout, "pinned"))
        legacy = branded.rename(branded.with_name("project-memory"))
        (legacy / "SKILL.md").write_text("---\nname: project-memory\n---\n# Pinned working copy\n")
        self.assertEqual(agent_setup.switch_skills(True, checkout)["mode"], "dev")
        self.configure("--agent", "claude")
        with patch.object(installation, "_collect", return_value={}):
            installation._activate(self.ctx, state, "updated")
        for agent, skill in (("codex", self.old_skill), ("claude", self.home / ".claude/skills/project-memory")):
            status = agent_setup.command(["--agent", agent, "--status"])
            self.assertEqual((status["status"], status["skill_mode"], status["dev_source"]),
                             ("configured", "dev", str(checkout)))
            self.assertEqual(os.readlink(skill), str(legacy))
            self.assertFalse(skill.with_name("richi").is_symlink())
        self.assertTrue(agent_setup.skill_links(True, checkout)["matches_config"])
        self.assertIn(b"`project-memory` skill", instructions.read_bytes())

        # Switching to release moves both rules and links, and switching back
        # to the pinned checkout restores the compatible name for every agent.
        manifest = self.root / agent_setup.MANIFEST
        previous = (manifest.read_bytes(), instructions.read_bytes())
        operations, _ = agent_setup._switch_operations(self.ctx, state, None)
        setup_files.begin(self.ctx, operations)
        self.assertTrue(self.new_skill.is_symlink())
        setup_files.recover(self.ctx)
        self.assertEqual((manifest.read_bytes(), instructions.read_bytes()), previous)
        self.assertEqual(os.readlink(self.old_skill), str(legacy))
        self.assertEqual(agent_setup.switch_skills(False, checkout)["mode"], "release")
        self.assertTrue(self.new_skill.is_symlink())
        self.assertFalse(self.old_skill.is_symlink())
        self.assertIn(b"`richi` skill", instructions.read_bytes())
        self.assertFalse(agent_setup.skill_links(True, checkout)["matches_config"])
        self.assertEqual(agent_setup.switch_skills(True, checkout)["mode"], "dev")
        self.assertEqual(os.readlink(self.old_skill), str(legacy))
        self.assertTrue(instructions.read_bytes().startswith(b"Keep this user text.\r\n"))
        # Pulling the checkout in place keeps its configured source, but the
        # desired skill and its discovery path are now branded.
        legacy.rename(branded)
        (branded / "SKILL.md").write_text("---\nname: richi\n---\n# Updated checkout\n")
        self.assertFalse(agent_setup.skill_links(True, checkout)["matches_config"])
        self.assertEqual(agent_setup.switch_skills(True, checkout)["status"], "changed")
        self.assertEqual(os.readlink(self.new_skill), str(branded))
        self.assertTrue(agent_setup.skill_links(True, checkout)["matches_config"])

    def test_setup_uses_committed_layout_when_previous_bundle_is_damaged(self):
        self.transition()
        previous = self.site / "richi_launcher/agent_skill/SKILL.md"
        for damage in ("missing", "invalid UTF-8"):
            with self.subTest(damage=damage):
                if damage == "missing":
                    previous.unlink()
                else:
                    previous.write_bytes(b"\xff")
                self.assertEqual(agent_setup.command(["--status"])["status"], "configured")
                self.assertEqual(self.configure()["changes"], [])
                claude = self.configure("--agent", "claude")
                self.assertEqual(Path(claude["skill"]).name, "project-memory")
                self.assertFalse(self.new_skill.is_symlink())
                self.assertEqual(agent_setup.switch_skills(False, None)["status"], "unchanged")

    def test_first_agent_after_update_with_unreadable_previous_preserves_rollback_layout(self):
        first = self.next_state()
        with patch.object(installation, "_collect", return_value={}):
            installation._activate(self.ctx, first, "updated")
        (self.site / "richi_launcher/agent_skill/SKILL.md").write_bytes(b"\xff")
        self.assertEqual(Path(self.configure()["skill"]).name, "project-memory")
        self.assertEqual(agent_setup.command(["--status"])["status"], "configured")

    def test_setup_rejects_an_unrecognized_recorded_name_before_adding_an_agent(self):
        self.configure()
        manifest = self.root / agent_setup.MANIFEST
        record = json.loads(manifest.read_text())
        record["skill"] = str(Path(record["skill"]).with_name("other"))
        manifest.write_text(json.dumps(record))
        with self.assertRaisesRegex(ConfigError, "unsupported managed skill directory"):
            self.configure("--agent", "claude")
        self.assertFalse((self.home / ".claude").exists())
