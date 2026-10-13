"""Synthetic scheduler registrations never touch the real OS job registry."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
from unittest.mock import patch

from richi_launcher import update_scheduler as scheduler
from richi_launcher.config import ConfigError

REAL_PLATFORM = sys.platform


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="richi-timer-test-")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve() / 'home with spaces $d %n "quotes"'
        self.home.mkdir()
        self.original = self.home / 'fixed venv'
        self.original.mkdir()
        (self.original / "bin").mkdir()
        (self.original / "bin" / "python").symlink_to(sys.executable)
        self.platform = patch.object(scheduler.sys, "platform", "darwin")
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.environment = patch.dict(os.environ, {"HOME": str(self.home)}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.identity = hashlib.sha256(str(self.original).encode()).hexdigest()[:24]
        self.ctx = {"original_prefix": str(self.original), "prefix": "/obsolete/version",
                    "root": str(self.home / "Library" / "Application Support" / "Richi" / "installations" / self.identity)}
        self.loaded = False
        self.active = False
        self.enabled = False
        self.calls = []
        self.fail = None
        self.launchd_path = None
        self.service_fragment = None
        self.drop_ins = {"timer": "", "service": ""}
        self.runner = patch.object(scheduler.subprocess, "run", side_effect=self._manager)
        self.runner.start()
        self.addCleanup(self.runner.stop)

    def linux(self):
        scheduler.sys.platform = "linux"
        self.ctx["root"] = str(self.home / "custom data" / "richi" / "installations" / self.identity)
        os.environ["XDG_CONFIG_HOME"] = str(self.home / "custom config")

    def _manager(self, command, **kwargs):
        self.calls.append(command)
        self.assertEqual(kwargs["timeout"], scheduler.COMMAND_TIMEOUT)
        self.assertFalse(kwargs["check"])
        args = command[1:]
        if args[0] == "print":
            path = str(next(iter(scheduler._plan(self.ctx)["files"])))
            output = "\tpath = " + (self.launchd_path if self.launchd_path is not None else path) + "\n"
            return subprocess.CompletedProcess(command, 0 if self.loaded or args[1].count("/") == 1 else 113, output, "")
        if args[0] == "bootstrap":
            self.loaded = self.active = self.enabled = True
        elif args[0] == "bootout":
            self.loaded = self.active = self.enabled = False
        elif args[:2] == ["--user", "show"]:
            plan = scheduler._plan(self.ctx)
            unit = args[2]
            suffix = unit.rsplit(".", 1)[1]
            exists = (plan["directory"] / unit).exists()
            if not exists:
                output = "LoadState=not-found\nActiveState=inactive\nUnitFileState=\nFragmentPath=\nDropInPaths=\n"
            else:
                fragment = self.service_fragment if suffix == "service" and self.service_fragment else str(plan["directory"] / unit)
                output = ("LoadState=loaded\nActiveState=" + ("active" if self.active else "inactive") +
                          "\nUnitFileState=" + ("enabled" if self.enabled else "disabled") +
                          "\nFragmentPath=" + fragment + "\nDropInPaths=" + self.drop_ins[suffix] + "\n")
            return subprocess.CompletedProcess(command, 0, output, "")
        elif args[:2] == ["--user", "enable"]:
            self.enabled = True
            if "--now" in args:
                self.active = True
        elif args[:2] == ["--user", "disable"]:
            self.enabled = self.active = False
        elif args[:2] == ["--user", "start"]:
            self.active = True
        if self.fail and self.fail(command):
            return subprocess.CompletedProcess(command, 1, "", "fixture activation failed")
        return subprocess.CompletedProcess(command, 0, "", "")

    def test_launchd_roundtrip_and_idempotent_install(self):
        result = scheduler.install(self.ctx)
        self.assertEqual(result["status"], "installed")
        plan = scheduler._plan(self.ctx)
        path = next(iter(plan["files"]))
        data = plistlib.loads(path.read_bytes())
        self.assertEqual(data["StartInterval"], 900)
        self.assertTrue(data["RunAtLoad"])
        self.assertNotIn("KeepAlive", data)
        self.assertEqual(data["ProgramArguments"][4], str(self.original / "bin" / "python"))
        self.assertEqual(data["ProgramArguments"][-7:], ["-I", "-B", "-c",
                         "from richi_bootstrap import main; raise SystemExit(main())", "update", "auto", "run"])
        self.assertEqual(data["StandardOutPath"], "/dev/null")
        self.assertNotIn("/obsolete/version", path.read_text())
        self.assertTrue(scheduler.status(self.ctx)["active"])
        self.calls.clear()
        self.assertEqual(scheduler.install(self.ctx)["status"], "already_installed")
        self.assertFalse(any(command[1] == "bootstrap" for command in self.calls))
        self.assertEqual(scheduler.remove(self.ctx)["status"], "removed")
        self.assertFalse(path.exists())
        self.assertFalse(self.loaded)
        self.assertEqual(scheduler.remove(self.ctx)["status"], "already_removed")

    def test_systemd_arguments_preserve_path_characters_and_data_root(self):
        self.linux()
        scheduler.install(self.ctx)
        plan = scheduler._plan(self.ctx)
        service = plan["directory"] / (plan["label"] + ".service")
        text = service.read_text()
        line = next(line for line in text.splitlines() if line.startswith("ExecStart="))
        args = [part.replace("%%", "%").replace("$$", "$") for part in shlex.split(line.split("=", 1)[1])]
        self.assertEqual(args[:4], ["/usr/bin/env", "-i", "HOME=" + str(self.home), "PATH=/usr/bin:/bin"])
        self.assertEqual(args[4], "XDG_DATA_HOME=" + str(self.home / "custom data"))
        self.assertEqual(args[5], str(self.original / "bin" / "python"))
        self.assertEqual(args[6:], ["-I", "-B", "-c",
                         "from richi_bootstrap import main; raise SystemExit(main())", "update", "auto", "run"])
        self.assertIn('$$d %%n', line)
        timer = (plan["directory"] / (plan["label"] + ".timer")).read_text()
        self.assertIn("OnActiveSec=1min\n", timer)
        self.assertIn("OnUnitInactiveSec=15min\n", timer)
        self.assertTrue(scheduler.status(self.ctx)["active"])
        scheduler.remove(self.ctx)
        self.assertFalse(service.exists())
        self.assertTrue(any(command[1:3] == ["--user", "stop"] for command in self.calls))

    def test_tick_environment_excludes_chat_workspace_and_python_overrides(self):
        plan = scheduler._plan(self.ctx)
        args = plistlib.loads(next(iter(plan["files"].values())))["ProgramArguments"]
        self.runner.stop()
        with patch.dict(os.environ, {"RICHI_WORKSPACE": "wrong", "RICHI_CONFIG": "/private", "CODEX_THREAD_ID": "chat",
                                     "PYTHONPATH": "/foreign", "RICHI_INSTALLATION_ROOT": "/foreign"}):
            result = subprocess.run([*args[:4], sys.executable, "-I", "-c", "import os,json; print(json.dumps(dict(os.environ)))"],
                                    capture_output=True, text=True, check=True)
        environment = json.loads(result.stdout)
        self.assertEqual(environment["HOME"], str(self.home))
        self.assertFalse(any(key.startswith("RICHI_") for key in environment))
        self.assertNotIn("CODEX_THREAD_ID", environment)
        self.assertNotIn("PYTHONPATH", environment)

    def test_tick_enters_bootstrap_even_when_seed_richi_namespace_is_broken(self):
        # GC can retire the seed runtime while a new scheduled process starts.
        # Its first import must be the permanent bootstrap, which owns leases.
        self.runner.stop()
        (self.original / "bin" / "python").unlink()
        with patch.object(scheduler.sys, "platform", REAL_PLATFORM):
            venv.EnvBuilder(with_pip=False).create(str(self.original))
        python = str(self.original / "bin" / "python")
        query = subprocess.run([python, "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                               capture_output=True, text=True, check=True)
        site = Path(query.stdout.strip())
        (site / "richi").mkdir()
        (site / "richi" / "__init__.py").write_text("raise RuntimeError('seed namespace is being collected')\n")
        (site / "richi_bootstrap.py").write_text("def main():\n    print('permanent bootstrap entered')\n    return 0\n")
        plan = scheduler._plan(self.ctx)
        argv = plistlib.loads(next(iter(plan["files"].values())))["ProgramArguments"]
        result = subprocess.run(argv, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), "permanent bootstrap entered")

    def test_status_never_creates_folders_and_does_not_infer_active_from_file(self):
        before = set(self.home.rglob("*"))
        result = scheduler.status(self.ctx)
        self.assertFalse(result["installed"])
        self.assertFalse(result["registered"])
        self.assertEqual(before, set(self.home.rglob("*")))
        plan = scheduler._plan(self.ctx)
        for path, content in plan["files"].items():
            scheduler._write(path, content)
        result = scheduler.status(self.ctx)
        self.assertTrue(result["installed"])
        self.assertFalse(result["active"])

    def test_foreign_or_modified_file_is_never_overwritten_or_removed(self):
        plan = scheduler._plan(self.ctx)
        path = next(iter(plan["files"]))
        path.parent.mkdir(parents=True)
        path.write_bytes(b"another application")
        for operation in (scheduler.install, scheduler.remove):
            with self.assertRaisesRegex(ConfigError, "unowned or modified"):
                operation(self.ctx)
        self.assertEqual(path.read_bytes(), b"another application")
        self.assertFalse(self.calls)
        self.assertIn("unowned", scheduler.status(self.ctx)["reason"])

    def test_symlinked_file_and_parent_are_rejected(self):
        plan = scheduler._plan(self.ctx)
        path = next(iter(plan["files"]))
        target = self.home / "foreign"
        target.write_bytes(b"foreign")
        path.parent.mkdir(parents=True)
        path.symlink_to(target)
        with self.assertRaisesRegex(ConfigError, "canonical"):
            scheduler.install(self.ctx)
        path.unlink()
        path.parent.rmdir()
        other = self.home / "redirected"
        other.mkdir()
        path.parent.symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(ConfigError, "canonical"):
            scheduler.remove(self.ctx)
        self.assertEqual(target.read_bytes(), b"foreign")
        self.assertEqual(list(other.iterdir()), [])

    def test_mismatched_installation_identity_is_rejected(self):
        self.ctx["root"] = str(Path(self.ctx["root"]).with_name("another-installation"))
        with self.assertRaisesRegex(ConfigError, "identity"):
            scheduler.install(self.ctx)
        self.assertFalse(self.calls)

    def test_missing_original_python_refuses_broken_scheduled_job(self):
        (self.original / "bin" / "python").unlink()
        with self.assertRaisesRegex(ConfigError, "executable Python"):
            scheduler.install(self.ctx)
        self.assertFalse(self.calls)
        self.assertFalse(scheduler._plan(self.ctx)["directory"].exists())

    def test_launchd_failure_removes_new_job_and_owned_file(self):
        self.fail = lambda command: command[1] == "bootstrap"
        with self.assertRaisesRegex(ConfigError, "fixture activation failed"):
            scheduler.install(self.ctx)
        self.assertFalse(self.loaded)
        self.assertFalse(any(path.exists() for path in scheduler._plan(self.ctx)["files"]))

    def test_systemd_failure_removes_new_files_and_stops_timer(self):
        self.linux()
        self.fail = lambda command: command[1:3] == ["--user", "enable"]
        with self.assertRaisesRegex(ConfigError, "fixture activation failed"):
            scheduler.install(self.ctx)
        self.assertFalse(self.active)
        self.assertFalse(any(path.exists() for path in scheduler._plan(self.ctx)["files"]))
        self.assertEqual(sum(command[1:3] == ["--user", "daemon-reload"] for command in self.calls), 2)

    def test_failed_registration_keeps_preexisting_owned_file(self):
        plan = scheduler._plan(self.ctx)
        path, content = next(iter(plan["files"].items()))
        scheduler._write(path, content)
        self.fail = lambda command: command[1] == "bootstrap"
        with self.assertRaises(ConfigError):
            scheduler.install(self.ctx)
        self.assertEqual(path.read_bytes(), content)

    def test_missing_user_manager_is_reported_without_writes(self):
        before = set(self.home.rglob("*"))
        with patch.object(scheduler.subprocess, "run", side_effect=FileNotFoundError("fixture unavailable")):
            result = scheduler.status(self.ctx)
            self.assertIsNone(result["active"])
            self.assertIn("Cannot contact", result["reason"])
            with self.assertRaisesRegex(ConfigError, "Cannot contact"):
                scheduler.install(self.ctx)
        self.assertEqual(set(self.home.rglob("*")), before)

    def test_registered_job_without_owned_file_is_not_stopped(self):
        self.loaded = True
        with self.assertRaisesRegex(ConfigError, "without complete owned files"):
            scheduler.remove(self.ctx)
        self.assertTrue(self.loaded)
        self.assertFalse(any(command[1] == "bootout" for command in self.calls))

    def test_linux_loaded_foreign_fragment_is_not_touched(self):
        self.linux()
        output = "LoadState=loaded\nActiveState=active\nUnitFileState=enabled\nFragmentPath=/foreign.service\n"
        with patch.object(scheduler.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output, "")):
            with self.assertRaisesRegex(ConfigError, "unowned path"):
                scheduler.install(self.ctx)
        self.assertFalse(scheduler._plan(self.ctx)["directory"].exists())

    def test_launchd_foreign_origin_is_not_stopped_despite_owned_local_file(self):
        scheduler.install(self.ctx)
        self.launchd_path = "/somewhere/foreign.plist"
        self.calls.clear()
        with self.assertRaisesRegex(ConfigError, "ownership"):
            scheduler.remove(self.ctx)
        with self.assertRaisesRegex(ConfigError, "ownership"):
            scheduler.install(self.ctx)
        self.assertTrue(self.loaded)
        self.assertTrue(all(command[1] == "print" for command in self.calls))
        self.assertTrue(all(path.exists() for path in scheduler._plan(self.ctx)["files"]))

    def test_launchd_unknown_diagnostic_format_fails_closed(self):
        scheduler.install(self.ctx)
        with patch.object(scheduler.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "new diagnostic format", "")):
            with self.assertRaisesRegex(ConfigError, "ownership"):
                scheduler.remove(self.ctx)
            result = scheduler.status(self.ctx)
        self.assertIsNone(result["active"])
        self.assertIn("ownership", result["reason"])

    def test_linux_foreign_service_and_dropins_prevent_stopping_any_job(self):
        self.linux()
        scheduler.install(self.ctx)
        self.service_fragment = "/another/service.service"
        self.calls.clear()
        with self.assertRaisesRegex(ConfigError, "service.*unowned"):
            scheduler.remove(self.ctx)
        self.service_fragment = None
        for suffix in ("timer", "service"):
            self.drop_ins[suffix] = "/foreign/drop-in.conf"
            with self.assertRaisesRegex(ConfigError, "drop-in"):
                scheduler.remove(self.ctx)
            self.drop_ins[suffix] = ""
        self.assertTrue(self.active)
        self.assertTrue(all(command[1:3] == ["--user", "show"] for command in self.calls))
        self.assertTrue(all(path.exists() for path in scheduler._plan(self.ctx)["files"]))

    def test_already_active_linux_timer_does_not_reload_or_restart(self):
        self.linux()
        scheduler.install(self.ctx)
        self.calls.clear()
        self.assertEqual(scheduler.install(self.ctx)["status"], "already_installed")
        self.assertTrue(all(command[1:3] == ["--user", "show"] for command in self.calls))

    @unittest.skipUnless(REAL_PLATFORM == "linux", "systemd's unit parser is Linux-only")
    def test_generated_units_pass_real_systemd_parser(self):
        self.linux()
        binary = shutil.which("systemd-analyze")
        if binary is None:
            self.skipTest("systemd-analyze is not installed")
        plan = scheduler._plan(self.ctx)
        for path, content in plan["files"].items():
            scheduler._write(path, content)
        self.runner.stop()
        result = subprocess.run([binary, "verify", "--man=no", *map(str, plan["files"])],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
