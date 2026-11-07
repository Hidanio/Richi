"""Install the distributable zipapp and exercise the real managed wheel lifecycle."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from cli_environment import cli_environment, synthetic_port
import test_update_e2e as lifecycle


class InstallerLifecycleTests(unittest.TestCase):
    make_wheel = lifecycle.UpdateWheelLifecycleTests.make_wheel
    feed_fixture = lifecycle.UpdateWheelLifecycleTests.feed_fixture
    run_process = lifecycle.UpdateWheelLifecycleTests.run_process

    def test_zipapp_install_repeat_update_and_rollback_preserve_user_state(self):
        with tempfile.TemporaryDirectory(prefix="richi-setup-lifecycle-") as temporary:
            self.root = Path(temporary).resolve()
            home = self.root / "cli-home"
            # After an update the first command looks for maps on its port.
            self.environment = cli_environment(self.root, SHELL="/bin/zsh", RICHI_PORT=synthetic_port())
            for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
                self.environment.pop(key, None)
            instructions = home / ".codex/AGENTS.md"
            instructions.parent.mkdir(parents=True)
            original = b"# Existing user instructions\r\nKeep my preferences.\r\n"
            instructions.write_bytes(original)
            skill = home / ".agents/skills/project-memory"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_bytes(b"My customized existing skill")
            wheels = {v: self.make_wheel(v) for v in ("0.1.0", "0.2.0", "0.3.0", "0.4.0")}
            feeds = {v: self.feed_fixture(w, v) for v, w in wheels.items()}
            archive = self.root / "richi-installer.pyz"
            builder = Path(__file__).resolve().parents[1] / "scripts/build_installer.py"
            self.run_process([sys.executable, builder, wheels["0.1.0"], "--output", archive])
            self.assertEqual(archive.with_suffix(".pyz.sha256").read_text().split()[0],
                             hashlib.sha256(archive.read_bytes()).hexdigest())
            second = self.root / "second.pyz"
            self.run_process([sys.executable, builder, wheels["0.1.0"], "--output", second])
            self.assertEqual(archive.read_bytes(), second.read_bytes())
            command = [sys.executable, "-I", archive, "--wheel", wheels["0.1.0"],
                       "--manifest", self.root / "0.1.0-manifest.json", "--no-path"]
            # Preview is read-only, even with a pre-existing skill.
            before = set(home.rglob("*"))
            self.run_process([*command, "--dry-run"])
            self.assertEqual(before, set(home.rglob("*")))
            result = self.run_process([*command, "--yes"], ok=False)
            self.assertIn("replace-skill", result.stderr)
            self.assertEqual(instructions.read_bytes(), original)
            self.assertEqual((skill / "SKILL.md").read_bytes(), b"My customized existing skill")
            # Retry the same owned installation; adopt only with explicit consent.
            configured = json.loads(self.run_process([*command, "--yes", "--replace-skill"]).stdout)
            self.assertEqual(configured["status"], "configured")
            backup = list((home / ".agents/richi-skill-backups").glob("project-memory-*"))
            self.assertEqual(len(backup), 1)
            self.assertEqual((backup[0] / "SKILL.md").read_bytes(), b"My customized existing skill")
            self.assertTrue(instructions.read_bytes().startswith(original))
            installed_instructions = instructions.read_bytes()
            console = home / ".local/bin/richi"
            prefix = Path(configured["prefix"])
            python = prefix / "bin/python"
            self.assertEqual(self.run_process([console, "--version"]).stdout.strip(), "Richi 0.1.0")
            self.assertEqual(json.loads(self.run_process([console, "setup", "--status"]).stdout)["status"], "configured")
            self.assertFalse(list(home.rglob("*.sqlite3")))
            self.assertFalse(list(home.rglob("chat-workspaces.json")))
            repeated = json.loads(self.run_process([*command, "--yes"]).stdout)
            self.assertEqual(repeated["changes"], [])
            self.assertEqual(instructions.read_bytes(), installed_instructions)
            self.run_process([console, "setup", "--agent", "claude", "--yes", "--no-path"])
            claude_skill = home / ".claude/skills/project-memory"
            self.assertEqual(claude_skill.resolve(), skill.resolve())
            # Setup works even inside an unbound chat and does not bind it.
            self.environment["CODEX_THREAD_ID"] = "synthetic-new-chat"
            self.run_process([console, "setup", "--yes"])
            self.assertFalse(list(home.rglob("chat-workspaces.json")))
            self.environment.pop("CODEX_THREAD_ID")
            self.run_process([console, "workspace", "create", "alpha"])
            self.run_process([console, "--chat", "synthetic-chat", "chat", "bind", "alpha"])
            self.run_process([console, "--chat", "synthetic-chat", "init"])
            protected = {p: p.read_bytes() for p in home.rglob("*")
                         if p.is_file() and (p.suffix == ".sqlite3" or p.name in {
                             "workspaces.json", "chat-workspaces.json", "config.json"})}
            offline = self.root / "offline.py"
            offline.write_text(lifecycle._OFFLINE_COMMAND)
            first_target = os.readlink(skill)
            for version in ("0.2.0", "0.3.0", "0.4.0"):
                self.run_process([python, "-I", offline, feeds[version], "update", "apply"])
                self.assertEqual(self.run_process([console, "--version"]).stdout.strip(), "Richi " + version)
                self.assertNotEqual(os.readlink(skill), first_target)
                status = json.loads(self.run_process([console, "setup", "--status"]).stdout)
                self.assertEqual(status["status"], "configured")
                self.assertIn("v" + version, status["generation"])
                claude_status = json.loads(self.run_process([console, "setup", "--agent", "claude", "--status"]).stdout)
                self.assertEqual(claude_status["status"], "configured")
                self.assertEqual(claude_skill.resolve(), skill.resolve())
                self.assertTrue((skill / "SKILL.md").is_file())
            self.run_process([console, "update", "rollback"])
            self.assertEqual(self.run_process([console, "--version"]).stdout.strip(), "Richi 0.3.0")
            self.assertIn("v0.3.0", json.loads(self.run_process([console, "setup", "--status"]).stdout)["generation"])
            self.assertEqual(claude_skill.resolve(), skill.resolve())
            self.assertEqual(json.loads(self.run_process([console, "setup", "--agent", "claude", "--status"]).stdout)["status"], "configured")
            self.assertEqual(instructions.read_bytes(), installed_instructions)
            for path, contents in protected.items():
                self.assertEqual(path.read_bytes(), contents, str(path))
            # Neither an update nor rollback may discard local skill edits.
            with (skill / "SKILL.md").open("a") as stream:
                stream.write("\nUser edit\n")
            failed = self.run_process([console, "update", "rollback"], ok=False)
            self.assertIn("edited", failed.stderr)
            self.assertEqual(self.run_process([console, "--version"]).stdout.strip(), "Richi 0.3.0")

    def test_dev_mode_moves_managed_skills_through_update_rollback_and_recovery(self):
        with tempfile.TemporaryDirectory(prefix="richi-dev-skills-") as temporary:
            self.root = Path(temporary).resolve()
            home = self.root / "cli-home"
            self.environment = cli_environment(self.root, SHELL="/bin/zsh")
            for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
                self.environment.pop(key, None)
            wheels = {v: self.make_wheel(v) for v in ("0.1.0", "0.2.0")}
            feeds = {v: self.feed_fixture(w, v) for v, w in wheels.items()}
            archive = self.root / "richi-installer.pyz"
            repository = Path(__file__).resolve().parents[1]
            self.run_process([sys.executable, repository / "scripts/build_installer.py", wheels["0.1.0"],
                              "--output", archive])
            configured = json.loads(self.run_process([
                sys.executable, "-I", archive, "--yes", "--no-path", "--wheel", wheels["0.1.0"],
                "--manifest", self.root / "0.1.0-manifest.json"]).stdout)
            console, python = home / ".local/bin/richi", Path(configured["prefix"]) / "bin/python"
            self.run_process([console, "setup", "--agent", "claude", "--yes", "--no-path"])
            codex, claude = home / ".agents/skills/project-memory", home / ".claude/skills/project-memory"
            protected = {path: path.read_bytes() for path in (
                home / ".codex/AGENTS.md", home / ".claude/CLAUDE.md", home / ".claude/settings.json")}
            # A synthetic Richi checkout: runtime sources and the canonical skill.
            checkout = self.root / "checkout"
            shutil.copytree(Path(lifecycle.richi.__file__).resolve().parent, checkout / "src/richi",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            (checkout / "src/richi/__init__.py").write_text('__version__ = "dev-checkout"\n')
            shutil.copyfile(repository / "pyproject.toml", checkout / "pyproject.toml")
            shutil.copytree(repository / "skills/project-memory", checkout / "skills/project-memory")
            dev_link = str(checkout / "skills/project-memory")

            def config(*arguments):
                return json.loads(self.run_process([console, "config", *arguments]).stdout)

            def status(agent):
                return json.loads(self.run_process([console, "setup", "--agent", agent, "--status"]).stdout)

            release = os.readlink(codex)
            self.assertEqual(config("set", "development.source", checkout)["agent_skills"]["mode"], "release")
            enabled = config("set", "dev", "true")
            self.assertEqual((enabled["runtime"]["mode"], enabled["agent_skills"]["status"]), ("dev", "changed"))
            self.assertEqual(sorted(enabled["agent_skills"]["changes"]), sorted([str(codex), str(claude)]))
            self.assertEqual((os.readlink(codex), os.readlink(claude)), (dev_link, dev_link))
            self.assertEqual(self.run_process([console, "--version"]).stdout.strip(), "Richi dev-checkout")
            for agent in ("codex", "claude"):
                self.assertEqual((status(agent)["status"], status(agent)["skill_mode"]), ("configured", "dev"))
            offline = self.root / "offline.py"
            offline.write_text(lifecycle._OFFLINE_COMMAND)
            updated = json.loads(self.run_process([python, "-I", offline, feeds["0.2.0"], "update", "apply"]).stdout)
            self.assertEqual(updated["agent_skills"]["claude"]["mode"], "dev")
            self.assertEqual((os.readlink(codex), os.readlink(claude)), (dev_link, dev_link))
            self.assertIn("v0.2.0", status("codex")["generation"])
            # The rollback target's manager (also dev-capable) keeps the checkout.
            self.run_process([console, "update", "rollback"])
            self.assertEqual((os.readlink(codex), os.readlink(claude)), (dev_link, dev_link))
            self.assertEqual(status("claude")["status"], "configured")
            # Disabling dev restores release skills even without the checkout.
            checkout.rename(self.root / "moved-checkout")
            self.run_process([console, "--version"], ok=False)
            disabled = config("set", "dev", "false")
            self.assertEqual((disabled["runtime"]["mode"], disabled["agent_skills"]["mode"]), ("release", "release"))
            self.assertEqual((os.readlink(codex), os.readlink(claude)), (release, release))
            self.assertEqual(self.run_process([console, "--version"]).stdout.strip(), "Richi 0.1.0")
            for agent in ("codex", "claude"):
                self.assertEqual((status(agent)["status"], status(agent)["skill_mode"]), ("configured", "release"))
            for path, data in protected.items():
                self.assertEqual(path.read_bytes(), data, str(path))


if __name__ == "__main__":
    unittest.main()
