"""Install the distributable zipapp and exercise the real managed wheel lifecycle."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from cli_environment import cli_environment
import test_update_e2e as lifecycle


class InstallerLifecycleTests(unittest.TestCase):
    make_wheel = lifecycle.UpdateWheelLifecycleTests.make_wheel
    feed_fixture = lifecycle.UpdateWheelLifecycleTests.feed_fixture
    run_process = lifecycle.UpdateWheelLifecycleTests.run_process

    def test_zipapp_install_repeat_update_and_rollback_preserve_user_state(self):
        with tempfile.TemporaryDirectory(prefix="richi-setup-lifecycle-") as temporary:
            self.root = Path(temporary).resolve()
            home = self.root / "cli-home"
            self.environment = cli_environment(self.root, SHELL="/bin/zsh")
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
                self.assertTrue((skill / "SKILL.md").is_file())
            self.run_process([console, "update", "rollback"])
            self.assertEqual(self.run_process([console, "--version"]).stdout.strip(), "Richi 0.3.0")
            self.assertIn("v0.3.0", json.loads(self.run_process([console, "setup", "--status"]).stdout)["generation"])
            self.assertEqual(instructions.read_bytes(), installed_instructions)
            for path, contents in protected.items():
                self.assertEqual(path.read_bytes(), contents, str(path))
            # Neither an update nor rollback may discard local skill edits.
            with (skill / "SKILL.md").open("a") as stream:
                stream.write("\nUser edit\n")
            failed = self.run_process([console, "update", "rollback"], ok=False)
            self.assertIn("edited", failed.stderr)
            self.assertEqual(self.run_process([console, "--version"]).stdout.strip(), "Richi 0.3.0")


if __name__ == "__main__":
    unittest.main()
