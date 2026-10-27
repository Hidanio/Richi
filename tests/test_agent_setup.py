"""Global integration preserves unrelated bytes; every home and package is synthetic."""
import copy
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import richi_launcher
from richi_launcher import agent_setup, installation, setup_files
from richi_launcher.config import ConfigError


class AgentSetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-agent-setup-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.prefix = self.home / "venv"
        self.site = self.prefix / "lib/python3.9/site-packages"
        self.site.mkdir(parents=True)
        self.package(self.site, "0.1.0")
        (self.site / "richi_bootstrap.py").write_text("AGENT_SETUP_PROTOCOL = 1\n")
        (self.prefix / "bin").mkdir()
        (self.prefix / "bin/python").write_text("fixture")
        (self.prefix / "bin/richi").write_text("fixture")
        (self.prefix / "pyvenv.cfg").write_text("include-system-site-packages = false\n")
        info = self.site / "richi-0.1.0.dist-info"
        info.mkdir()
        (info / "METADATA").write_text("Name: richi\nVersion: 0.1.0\n")
        self.ctx = {"prefix": str(self.prefix), "original_prefix": str(self.prefix),
                    "site": str(self.site), "bootstrap": str(self.site / "richi_bootstrap.py"),
                    "root": str(self.home / "data/installations/setup-test")}
        self.root = Path(self.ctx["root"])
        environment = {"HOME": str(self.home), "SHELL": "/bin/zsh",
                       "CODEX_HOME": str(self.home / ".codex")}
        for item in (patch.dict(os.environ, environment, clear=True),
                     patch.object(installation, "_context", return_value=self.ctx)):
            item.start()
            self.addCleanup(item.stop)

    def package(self, site, version):
        (site / "richi").mkdir(parents=True)
        (site / "richi/__init__.py").write_text('__version__ = "' + version + '"\n')
        (site / "richi/memory.py").write_text("VERSION = 2\n")
        launcher = site / "richi_launcher"
        launcher.mkdir()
        (launcher / "cli.py").write_text("# fixture\n")
        (launcher / "agent_setup.py").write_text("PROTOCOL = 1\n")
        source = Path(richi_launcher.__file__).parent / "agent_skill"
        shutil.copytree(source, launcher / "agent_skill")
        with (launcher / "agent_skill/SKILL.md").open("a") as stream:
            stream.write("\nSynthetic release " + version + "\n")

    def configure(self, *args):
        return agent_setup.command(["--yes", *args])

    def paths(self):
        return set(self.home.rglob("*"))

    def next_state(self):
        state = copy.deepcopy(installation._state(self.ctx))
        generation = "v0.2.0-test"
        prefix = self.root / "versions" / generation
        site = prefix / "site"
        self.package(site, "0.2.0")
        state["generations"][generation] = {"kind": "release", "version": "0.2.0", "prefix": str(prefix), "site": str(site)}
        state["previous"], state["current"] = state["current"], generation
        return state

    def test_preserves_exact_user_bytes_and_repeated_setup_is_idempotent(self):
        instructions = self.home / ".codex/AGENTS.md"
        instructions.parent.mkdir()
        original = b"# Personal\r\nUse my conventions.\r\nNo final newline"
        instructions.write_bytes(original)
        instructions.chmod(0o640)
        result = self.configure()
        self.assertEqual(result["status"], "configured")
        first = instructions.read_bytes()
        self.assertTrue(first.startswith(original))
        self.assertEqual(first.count(agent_setup.START), 1)
        before = self.paths()
        self.configure()
        self.assertEqual(instructions.read_bytes(), first)
        self.assertEqual(self.paths(), before)
        self.assertEqual(instructions.stat().st_mode & 0o777, 0o640)
        self.assertFalse(list(self.home.rglob("*.sqlite3")))
        self.assertEqual(agent_setup.command(["--status"])["status"], "configured")

    def test_nonempty_override_is_the_effective_instruction_file(self):
        directory = self.home / ".codex"
        directory.mkdir()
        (directory / "AGENTS.md").write_text("base")
        (directory / "AGENTS.override.md").write_text("override")
        result = self.configure()
        self.assertEqual(result["instructions"], str(directory / "AGENTS.override.md"))
        self.assertEqual((directory / "AGENTS.md").read_text(), "base")

    def test_bash_login_setup_does_not_shadow_existing_user_settings(self):
        profile = self.home / ".bash_login"
        profile.write_bytes(b"# User startup\nexport EXAMPLE=1\n")
        with patch.dict(os.environ, SHELL="/bin/bash"):
            self.configure()
        self.assertFalse((self.home / ".bash_profile").exists())
        self.assertTrue(profile.read_bytes().startswith(b"# User startup\nexport EXAMPLE=1\n"))
        self.assertIn(agent_setup.PATH_START, profile.read_bytes())

    def test_dry_run_has_no_filesystem_side_effects(self):
        before = self.paths()
        result = agent_setup.command(["--dry-run"])
        self.assertEqual(result["status"], "planned")
        self.assertEqual(self.paths(), before)

    def test_conflicting_command_does_not_modify_instructions(self):
        command = self.home / ".local/bin/richi"
        command.parent.mkdir(parents=True)
        command.write_text("not Richi")
        before = self.paths()
        with self.assertRaisesRegex(ConfigError, "Another command"):
            self.configure()
        self.assertEqual(self.paths(), before)
        self.assertEqual(command.read_text(), "not Richi")

    def test_existing_skill_requires_explicit_backup_and_preserves_edits(self):
        skill = self.home / ".agents/skills/project-memory"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("personal skill")
        with self.assertRaisesRegex(ConfigError, "--replace-skill"):
            self.configure()
        self.configure("--replace-skill")
        backup, = (self.home / ".agents/richi-skill-backups").glob("project-memory-*")
        self.assertEqual((backup / "SKILL.md").read_text(), "personal skill")
        self.assertTrue(skill.is_symlink())

    def test_external_skill_symlink_is_backed_up_without_touching_target(self):
        external = self.home / "source-skill"
        external.mkdir()
        (external / "SKILL.md").write_text("source")
        skill = self.home / ".agents/skills/project-memory"
        skill.parent.mkdir(parents=True)
        skill.symlink_to(external)
        self.configure("--replace-skill")
        self.assertEqual((external / "SKILL.md").read_text(), "source")
        backup, = (self.home / ".agents/richi-skill-backups").glob("project-memory-*")
        self.assertEqual(backup.resolve(), external)

    def test_symlinked_instructions_are_not_overwritten(self):
        destination = self.home / "personal.md"
        destination.write_text("preserve me")
        instruction = self.home / ".codex/AGENTS.md"
        instruction.parent.mkdir()
        instruction.symlink_to(destination)
        with self.assertRaisesRegex(ConfigError, "regular files"):
            self.configure()
        self.assertEqual(destination.read_text(), "preserve me")

    def test_duplicate_and_edited_managed_blocks_are_rejected(self):
        self.configure()
        instruction = self.home / ".codex/AGENTS.md"
        data = instruction.read_bytes()
        instruction.write_bytes(data.replace(b"project continuity", b"custom continuity"))
        with self.assertRaisesRegex(ConfigError, "edited"):
            self.configure()
        instruction.write_bytes(data + data)
        with self.assertRaisesRegex(ConfigError, "duplicate"):
            self.configure()

    def test_changed_profile_and_new_override_are_not_silently_ignored(self):
        self.configure()
        shell = self.home / ".zshrc"
        shell.write_text("my own PATH")
        with self.assertRaisesRegex(ConfigError, "removed"):
            self.configure()
        (self.home / ".codex/AGENTS.override.md").write_text("new policy")
        with self.assertRaisesRegex(ConfigError, "shadows"):
            self.configure()

    def test_legacy_duplicate_skill_is_reported(self):
        legacy = self.home / ".codex/skills/project-memory"
        legacy.mkdir(parents=True)
        with self.assertRaisesRegex(ConfigError, "legacy"):
            self.configure()
        self.assertFalse((self.home / ".agents/skills/project-memory").exists())
        (legacy / "SKILL.md").write_text("legacy customization")
        self.configure("--replace-skill")
        self.assertFalse(legacy.exists())
        backup, = (self.home / ".codex/richi-skill-backups").glob("project-memory-*")
        self.assertEqual((backup / "SKILL.md").read_text(), "legacy customization")

    def test_instruction_discovery_budget_is_not_silently_exceeded(self):
        instructions = self.home / ".codex/AGENTS.md"
        instructions.parent.mkdir()
        original = b"x" * 32768
        instructions.write_bytes(original)
        with self.assertRaisesRegex(ConfigError, "32 KiB"):
            self.configure()
        self.assertEqual(instructions.read_bytes(), original)

    def test_oversized_journal_is_rejected_before_any_changes(self):
        path = self.home / "large-profile"
        path.write_bytes(b"x" * 400000)
        op = setup_files.change(path, setup_files.file_value(b"y" * 400000))
        with self.assertRaisesRegex(ConfigError, "journal exceeds"):
            setup_files.begin(self.ctx, [op])
        self.assertEqual(path.read_bytes(), b"x" * 400000)
        self.assertFalse(self.root.exists())

    def test_candidate_without_setup_protocol_cannot_activate(self):
        self.configure()
        state = self.next_state()
        site = Path(state["generations"][state["current"]]["site"])
        (site / "richi_launcher/agent_setup.py").write_text("PROTOCOL = 2\n")
        with self.assertRaisesRegex(ConfigError, "cannot maintain"):
            installation._activate(self.ctx, state, "updated")
        self.assertFalse((self.root / "state.json").exists())

    def test_edited_skill_blocks_runtime_activation(self):
        self.configure()
        state = self.next_state()
        skill = self.home / ".agents/skills/project-memory/SKILL.md"
        skill.write_text("edited locally")
        with self.assertRaises(ConfigError):
            installation._activate(self.ctx, state, "updated")
        self.assertFalse((self.root / "state.json").exists())
        self.assertEqual(skill.read_text(), "edited locally")

    def test_activation_and_rollback_select_matching_skill_versions(self):
        self.configure()
        old = installation._state(self.ctx)
        new = self.next_state()
        instruction = (self.home / ".codex/AGENTS.md").read_bytes()
        with patch.object(installation, "_collect", return_value={}):
            installation._activate(self.ctx, new, "updated")
            skill = self.home / ".agents/skills/project-memory/SKILL.md"
            self.assertIn("Synthetic release 0.2.0", skill.read_text())
            old["generations"].update(new["generations"])
            old["previous"] = new["current"]
            installation._activate(self.ctx, old, "rolled_back")
            self.assertIn("Synthetic release 0.1.0", skill.read_text())
        self.assertEqual((self.home / ".codex/AGENTS.md").read_bytes(), instruction)

    def test_failed_state_commit_restores_skill_and_keeps_old_runtime(self):
        self.configure()
        state = self.next_state()
        original_write = installation._atomic_json
        def fail_state(path, value):
            if path.name == "state.json":
                raise OSError("disk full")
            return original_write(path, value)
        with patch.object(installation, "_atomic_json", side_effect=fail_state):
            with self.assertRaisesRegex(OSError, "disk full"):
                installation._activate(self.ctx, state, "updated")
        self.assertIn("Synthetic release 0.1.0", (self.home / ".agents/skills/project-memory/SKILL.md").read_text())
        self.assertFalse((self.root / setup_files.JOURNAL).exists())

    def test_crash_before_and_after_runtime_commit_recovers_from_authority(self):
        self.configure()
        new = self.next_state()
        ops = agent_setup.activation_operations(self.ctx, new)
        setup_files.begin(self.ctx, ops, generation=new["current"])
        setup_files.recover(self.ctx)
        self.assertEqual(agent_setup._load(self.ctx)["generation"], "seed")
        ops = agent_setup.activation_operations(self.ctx, new)
        setup_files.begin(self.ctx, ops, generation=new["current"])
        installation._atomic_json(self.root / "state.json", new)
        setup_files.recover(self.ctx)
        self.assertEqual(agent_setup._load(self.ctx)["generation"], new["current"])

    def test_recovery_does_not_overwrite_concurrent_user_edits(self):
        self.configure()
        instruction = self.home / ".codex/AGENTS.md"
        op = setup_files.change(instruction, setup_files.file_value(b"temporary"))
        setup_files.begin(self.ctx, [op])
        instruction.write_text("user edited after crash")
        with self.assertRaisesRegex(ConfigError, "changed"):
            setup_files.recover(self.ctx)
        self.assertEqual(instruction.read_text(), "user edited after crash")
        self.assertTrue((self.root / setup_files.JOURNAL).exists())

    def test_interrupted_initial_skill_adoption_restores_original_directory(self):
        skill = self.home / ".agents/skills/project-memory"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("original")
        _, ops = agent_setup.plan(self.ctx, replace_skill=True)
        self.root.mkdir(parents=True)
        setup_files.begin(self.ctx, ops)
        setup_files.recover(self.ctx)
        self.assertFalse(skill.is_symlink())
        self.assertEqual((skill / "SKILL.md").read_text(), "original")

    def test_old_seed_launcher_requires_explicit_upgrade(self):
        Path(self.ctx["bootstrap"]).write_text("# old bootstrap\n")
        with self.assertRaisesRegex(ConfigError, "predates"):
            self.configure()


if __name__ == "__main__":
    unittest.main()
