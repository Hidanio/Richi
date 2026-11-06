"""One real offline wheel lifecycle, isolated from the user's installation/data."""

import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
import venv
import zipfile

import richi
import richi_bootstrap
import richi_launcher

from cli_environment import cli_environment


_REGISTRY_PATHS = """
import json
from richi_launcher import chats, workspaces
print(json.dumps([str(workspaces._registry_path()), str(chats._registry_path())]))
"""


class RegistryPathTests(unittest.TestCase):
    def test_linux_registries_use_config_root_instead_of_workspace_data_root(self):
        with tempfile.TemporaryDirectory(prefix="richi-linux-registry-") as temporary:
            environment = cli_environment(temporary)
            result = subprocess.run([sys.executable, "-I", "-B", "-c",
                                     "import sys; sys.platform = 'linux'\n" + _REGISTRY_PATHS],
                                    env=environment, capture_output=True, text=True, check=True)
            config_root = Path(environment["XDG_CONFIG_HOME"]) / "richi"
            self.assertEqual(json.loads(result.stdout),
                             [str(config_root / name) for name in ("workspaces.json", "chat-workspaces.json")])
            self.assertNotEqual(config_root, Path(environment["XDG_DATA_HOME"]) / "richi")


# Feed bytes and OS scheduler registration are the only replacements: real
# release validation, staging, pip, self-check, activation and collection run.
# Starting in the selected interpreter preserves the mock across bootstrap's
# normal process replacement; direct public entry points are checked separately.
_OFFLINE_COMMAND = r'''
import json, os
from pathlib import Path
import sys
from unittest.mock import patch
import richi_bootstrap as bootstrap

ctx = bootstrap.context()
if (Path(ctx["root"]) / "state.json").exists():
    state = bootstrap.read_state(ctx)
    prefix = Path(state["generations"][state["current"]]["prefix"])
    if prefix != Path(sys.prefix).resolve():
        python = str(prefix / "bin" / "python")
        os.execve(python, [python, "-I", "-B", __file__, *sys.argv[1:]], dict(os.environ))

responses = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))

def transfer(url, limit, write, asset=False):
    if url not in responses:
        raise AssertionError("Unexpected network request: " + url)
    data = Path(responses[url]).read_bytes()
    if len(data) > limit:
        raise AssertionError("Fixture exceeded the transport limit")
    write(data)
    return len(data)

with patch("richi_launcher.releases._transfer", side_effect=transfer), \
        patch("richi_launcher.update_scheduler.install", return_value={"status": "installed"}), \
        patch("richi_launcher.update_scheduler.remove", return_value={"status": "removed"}), \
        patch("richi_launcher.update_scheduler.status", return_value={
            "supported": True, "installed": True, "registered": True, "active": True,
            "enabled": True, "backend": "synthetic"}):
    raise SystemExit(bootstrap.main(sys.argv[2:]))
'''


@unittest.skipUnless(sys.platform in {"darwin", "linux"}, "Managed updater supports macOS/Linux")
class UpdateWheelLifecycleTests(unittest.TestCase):
    def run_process(self, arguments, *, ok=True, timeout=90):
        result = subprocess.run([str(item) for item in arguments], env=self.environment,
                                cwd=self.root, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=timeout)
        self.assertEqual(result.returncode == 0, ok,
                         "Command: " + repr(arguments) + "\n" + result.stdout + result.stderr)
        return result

    def console(self, *arguments, **kwargs):
        return self.run_process([self.console_path, *arguments], **kwargs)

    def console_json(self, *arguments):
        return json.loads(self.console(*arguments).stdout)

    def make_wheel(self, version, *, legacy_schemas=False):
        files = {}
        for package in (richi, richi_launcher):
            directory = Path(package.__file__).resolve().parent
            for path in directory.rglob("*"):
                if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                    files[package.__name__ + "/" + path.relative_to(directory).as_posix()] = path.read_bytes()
        original = files["richi/__init__.py"].decode("utf-8")
        changed, count = re.subn(r'(?m)^__version__\s*=.*$', '__version__ = "' + version + '"', original)
        self.assertEqual(count, 1)
        files["richi/__init__.py"] = changed.encode("utf-8")
        if legacy_schemas:
            # Keep the real package/launcher lifecycle while reproducing the
            # schema-2 manager's fixed protocol-1 compatibility restriction.
            memory = files["richi/memory.py"].decode("utf-8")
            self.assertIn("VERSION = 3\n", memory)
            files["richi/memory.py"] = memory.replace("VERSION = 3\n", "VERSION = 2\n", 1).encode()
            manager = files["richi_launcher/installation.py"].decode("utf-8")
            old = "not _contiguous_schemas(schemas)"
            self.assertIn(old, manager)
            files["richi_launcher/installation.py"] = manager.replace(old, "schemas != [1, 2]", 1).encode()
        files["richi_bootstrap.py"] = Path(richi_bootstrap.__file__).read_bytes()
        info = "richi-" + version + ".dist-info/"
        files[info + "METADATA"] = ("Metadata-Version: 2.1\nName: richi\nVersion: " + version
                                     + "\nRequires-Python: >=3.9\n\n").encode()
        files[info + "WHEEL"] = b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n"
        files[info + "entry_points.txt"] = b"[console_scripts]\nrichi = richi_bootstrap:main\n"
        files[info + "licenses/LICENSE.md"] = (Path(__file__).resolve().parents[1] / "LICENSE.md").read_bytes()
        record = io.StringIO(newline="")
        writer = csv.writer(record, lineterminator="\n")
        for name, data in sorted(files.items()):
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            writer.writerow((name, "sha256=" + digest, str(len(data))))
        writer.writerow((info + "RECORD", "", ""))
        files[info + "RECORD"] = record.getvalue().encode()
        wheel = self.root / ("richi-" + version + "-py3-none-any.whl")
        with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in sorted(files.items()):
                archive.writestr(name, data)
        return wheel

    def feed_fixture(self, wheel, version, *, database_schemas=(1, 2, 3)):
        data = wheel.read_bytes()
        filename = wheel.name
        manifest = {"format_version": 1, "version": version, "updater_protocol": 1,
                    "python_min": [3, 9], "database_schemas": list(database_schemas), "map_schema": 2,
                    "wheel": {"filename": filename, "sha256": hashlib.sha256(data).hexdigest(),
                              "size": len(data)}}
        base = "https://github.com/Hidanio/Richi/releases/"
        manifest_path = self.root / (version + "-manifest.json")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        manifest_url = base + "download/v" + version + "/richi-release.json"
        wheel_url = base + "download/v" + version + "/" + filename
        release = {"draft": False, "prerelease": False, "tag_name": "v" + version,
                   "html_url": base + "tag/v" + version, "assets": [
                       {"name": "richi-release.json", "state": "uploaded",
                        "size": manifest_path.stat().st_size, "browser_download_url": manifest_url},
                       {"name": filename, "state": "uploaded", "size": len(data),
                        "browser_download_url": wheel_url}]}
        release_path = self.root / (version + "-release.json")
        release_path.write_text(json.dumps(release), encoding="utf-8")
        responses = {"https://api.github.com/repos/Hidanio/Richi/releases/tags/v" + version: str(release_path),
                     "https://api.github.com/repos/Hidanio/Richi/releases/latest": str(release_path),
                     manifest_url: str(manifest_path), wheel_url: str(wheel)}
        response_path = self.root / (version + "-responses.json")
        response_path.write_text(json.dumps(responses), encoding="utf-8")
        return response_path

    def assert_public_version(self, version):
        # Alpha intentionally has broken dev sources. Explicit default checks
        # the installed release while preserving the selected workspace/dev flag.
        arguments = ["--workspace", "default", "--version"]
        self.assertEqual(self.console(*arguments).stdout.strip(), "Richi " + version)
        self.assertEqual(self.run_process([self.python, "-I", "-B", "-m", "richi", *arguments]).stdout.strip(),
                         "Richi " + version)

    def test_schema_three_transition_uses_fresh_prefix_and_preserves_workspace(self):
        with tempfile.TemporaryDirectory(prefix="richi-schema-transition-") as temporary:
            self.root = Path(temporary).resolve()
            self.environment = cli_environment(self.root)
            self.environment["CLAUDE_CONFIG_DIR"] = str(Path(self.environment["HOME"]) / ".claude")
            for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
                self.environment.pop(key, None)
            wheels = {version: self.make_wheel(version, legacy_schemas=True)
                      for version in ("0.1.0", "0.2.0", "0.3.0")}
            wheels["0.4.0"] = self.make_wheel("0.4.0")

            def install(prefix, wheel):
                venv.EnvBuilder(with_pip=True, symlinks=True).create(str(prefix))
                self.run_process([prefix / "bin/python", "-I", "-B", "-m", "pip", "--isolated",
                                  "--disable-pip-version-check", "install", "--no-index", "--no-deps",
                                  "--no-cache-dir", "--no-compile", wheel])

            original = self.root / "original"
            install(original, wheels["0.1.0"])
            self.python, self.console_path = original / "bin/python", original / "bin/richi"
            self.console("setup", "--yes", "--no-path")
            claude_profile = Path(self.environment["HOME"]) / ".claude"
            claude_profile.mkdir()
            claude_settings = claude_profile / "settings.json"
            unrelated_hook = {"hooks": [{"type": "command", "command": "printf synthetic-hook"}]}
            claude_settings.write_text(json.dumps({"theme": "synthetic", "hooks": {"SessionStart": [unrelated_hook]}}))
            self.console("setup", "--agent", "claude", "--yes", "--no-path")
            alpha = self.console_json("workspace", "create", "alpha")
            self.console("workspace", "use", "alpha")
            self.console("--chat", "schema-transition", "chat", "bind", "alpha")
            database = Path(alpha["database"])
            database.write_bytes(b"Synthetic store: the package transition must not open it\n")
            config = self.console_json("config", "show")
            registries = json.loads(self.run_process([self.python, "-I", "-B", "-c", _REGISTRY_PATHS]).stdout)
            protected_paths = [database, Path(alpha["config_file"]), *map(Path, registries)]
            protected = {path: path.read_bytes() for path in protected_paths}
            runner = self.root / "offline_update.py"
            runner.write_text(_OFFLINE_COMMAND, encoding="utf-8")
            for version in ("0.2.0", "0.3.0"):
                feed = self.feed_fixture(wheels[version], version, database_schemas=(1, 2))
                updated = json.loads(self.run_process([self.python, "-I", "-B", runner, feed,
                                                      "update", "apply"]).stdout)
            old_root = Path(updated["root"])
            self.assertNotIn("seed", json.loads((old_root / "state.json").read_text())["generations"])
            new_feed = self.feed_fixture(wheels["0.4.0"], "0.4.0")
            blocked = json.loads(self.run_process([self.python, "-I", "-B", runner, new_feed,
                                                  "update", "check"]).stdout)
            self.assertEqual(blocked["status"], "incompatible_release")
            self.run_process([self.python, "-I", "-B", runner, feed, "update", "auto", "enable"])
            self.run_process([self.python, "-I", "-B", runner, feed, "update", "auto", "disable"])
            old_claude = self.console_json("setup", "--agent", "claude", "--status")
            old_paths = [old_root / name for name in ("state.json", "auto.json", "agent-setup.json")]
            old_bytes = {path: path.read_bytes() for path in old_paths}

            replacement = self.root / "schema-three"
            install(replacement, wheels["0.4.0"])
            self.python, self.console_path = replacement / "bin/python", replacement / "bin/richi"
            self.assert_public_version("0.4.0")
            self.assertEqual(self.console_json("config", "show")["database"], config["database"])
            self.assertEqual(self.console_json("--chat", "schema-transition", "chat", "current")["workspace"], "alpha")
            auto = self.console_json("update", "auto", "status")
            self.assertFalse(auto["enabled"])
            self.assertFalse(auto["auto_install"])
            self.assertNotEqual(Path(auto["installation"]["root"]), old_root)

            command = Path(self.environment["HOME"]) / ".local/bin/richi"
            instructions = Path(self.environment["HOME"]) / ".codex/AGENTS.md"
            instruction_bytes = instructions.read_bytes()
            self.assertEqual(command.readlink(), original / "bin/richi")
            conflict = self.console("setup", "--dry-run", "--replace-skill", "--no-path", ok=False)
            self.assertIn("Another command owns", conflict.stderr)
            command.rename(command.with_name("richi-before-schema3"))
            self.console("setup", "--dry-run", "--replace-skill", "--no-path")
            adopted = self.console_json("setup", "--yes", "--replace-skill", "--no-path")
            self.assertTrue(adopted["skill_backups"])
            self.assertEqual(command.readlink(), replacement / "bin/richi")
            skill = Path(self.environment["HOME"]) / ".agents/skills/project-memory"
            self.assertTrue(skill.resolve().is_relative_to(replacement))
            self.assertEqual(self.console_json("setup", "--status")["status"], "configured")
            self.assertEqual(instructions.read_bytes(), instruction_bytes)
            hook_conflict = self.console("setup", "--agent", "claude", "--dry-run", "--replace-skill", ok=False)
            self.assertIn("Another file owns the Claude hook path", hook_conflict.stderr)
            (self.root / "claude-settings-before-schema3.json").write_bytes(claude_settings.read_bytes())
            settings = json.loads(claude_settings.read_text())
            self.assertEqual(settings["hooks"]["SessionStart"].count(old_claude["hook_entry"]), 1)
            settings["hooks"]["SessionStart"].remove(old_claude["hook_entry"])
            claude_settings.write_text(json.dumps(settings))
            (claude_profile / "richi/session_start.py").rename(self.root / "claude-session-before-schema3.py")
            self.console("setup", "--agent", "claude", "--dry-run", "--replace-skill", "--no-path")
            self.console("setup", "--agent", "claude", "--yes", "--replace-skill", "--no-path")
            new_claude = self.console_json("setup", "--agent", "claude", "--status")
            self.assertEqual(new_claude["status"], "configured")
            self.assertIn(str(replacement / "bin/python"), new_claude["hook_entry"]["hooks"][0]["command"])
            self.assertEqual(json.loads(claude_settings.read_text()),
                             {"theme": "synthetic", "hooks": {"SessionStart": [unrelated_hook, new_claude["hook_entry"]]}})
            self.assertEqual({path: path.read_bytes() for path in protected_paths}, protected)
            self.assertEqual({path: path.read_bytes() for path in old_paths}, old_bytes)
            self.assertFalse(database.with_name(database.name + "-wal").exists())
            self.assertFalse(database.with_name(database.name + "-shm").exists())

    def test_real_wheels_update_prune_and_recover_without_changing_workspaces(self):
        with tempfile.TemporaryDirectory(prefix="richi-wheel-lifecycle-") as temporary:
            self.root = Path(temporary).resolve()
            self.environment = cli_environment(self.root)
            for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
                self.environment.pop(key, None)
            wheels = {version: self.make_wheel(version) for version in ("0.1.0", "0.2.0", "0.3.0", "0.4.0")}
            seed = self.root / "seed"
            venv.EnvBuilder(with_pip=True, symlinks=True).create(str(seed))
            self.python = seed / "bin/python"
            self.console_path = seed / "bin/richi"
            self.run_process([self.python, "-I", "-B", "-m", "pip", "--isolated",
                              "--disable-pip-version-check", "install", "--no-index", "--no-deps",
                              "--no-cache-dir", "--no-compile", wheels["0.1.0"]])
            paths = json.loads(self.run_process([self.python, "-I", "-B", "-c",
                "import json, richi_bootstrap; from richi_launcher.config import _platform_dirs; "
                "print(json.dumps({'context': richi_bootstrap.context(), 'dirs': list(map(str, _platform_dirs()))}))"
            ]).stdout)
            context = paths["context"]
            config_dir = Path(paths["dirs"][0])
            alpha = self.console_json("workspace", "create", "alpha")
            self.console("--workspace", "alpha", "config", "set", "development.source", self.root / "missing-dev")
            self.console("--workspace", "alpha", "config", "set", "dev", "true")
            self.console("workspace", "use", "alpha")
            self.console("--chat", "wheel-lifecycle", "chat", "bind", "alpha")
            database = Path(alpha["database"])
            # A sentinel catches even an attempted SQLite migration/open/write.
            database.write_bytes(b"Synthetic workspace bytes: updater must never open this file\n")
            default_config = config_dir / "config.json"
            default_config.write_text('{"dev": false}\n', encoding="utf-8")
            protected_paths = [default_config, Path(alpha["config_file"]), database,
                               config_dir / "workspaces.json", config_dir / "chat-workspaces.json"]
            protected = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in protected_paths}
            console_bytes = self.console_path.read_bytes()
            bootstrap_path = Path(context["bootstrap"])
            bootstrap_bytes = bootstrap_path.read_bytes()
            self.assert_public_version("0.1.0")
            runner = self.root / "offline_update.py"
            runner.write_text(_OFFLINE_COMMAND, encoding="utf-8")
            results = []
            for version in ("0.2.0", "0.3.0", "0.4.0"):
                response_path = self.feed_fixture(wheels[version], version)
                if version == "0.3.0":
                    # Plain enable checks releases without changing the current
                    # version or collecting its previous seed. Omit wheel bytes
                    # from the feed so even an attempted download fails.
                    managed_root = Path(context["root"])
                    state_path = managed_root / "state.json"
                    before_state = (state_path.read_bytes(), state_path.stat().st_mtime_ns)
                    before_versions = sorted(path.name for path in (managed_root / "versions").iterdir())
                    runtime_paths = [Path(context["site"]) / "richi/memory.py",
                                     Path(context["site"]) / "richi_launcher/cli.py",
                                     Path(results[-1]["current"]["site"]) / "richi/memory.py"]
                    before_runtime = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in runtime_paths}
                    check_responses = json.loads(response_path.read_text())
                    check_responses = {url: path for url, path in check_responses.items() if not url.endswith(".whl")}
                    check_feed = self.root / "check-only-responses.json"
                    check_feed.write_text(json.dumps(check_responses))
                    enabled = json.loads(self.run_process([self.python, "-I", "-B", runner, check_feed,
                                                          "update", "auto", "enable"]).stdout)
                    self.assertEqual(enabled["status"], "enabled")
                    self.assertFalse(enabled["auto_install"])
                    available = json.loads(self.run_process([self.python, "-I", "-B", runner, check_feed,
                                                            "update", "auto", "run"]).stdout)
                    self.assertEqual(available["status"], "update_available", available)
                    self.assertEqual(available["version"], version)
                    self.assertEqual(available["action"], "richi update apply --version 0.3.0")
                    self.assert_public_version("0.2.0")
                    self.assertEqual(self.console_json("update", "auto", "run")["status"], "not_due")
                    self.assertEqual((state_path.read_bytes(), state_path.stat().st_mtime_ns), before_state)
                    self.assertEqual(sorted(path.name for path in (managed_root / "versions").iterdir()), before_versions)
                    self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in runtime_paths}, before_runtime)
                    self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in protected_paths}, protected)
                    # The explicit manual apply below remains available while
                    # the background policy only checks for releases.
                if version == "0.4.0":
                    enabled = json.loads(self.run_process([self.python, "-I", "-B", runner, response_path,
                                                          "update", "auto", "enable", "--install", "--interval", "6h"]).stdout)
                    self.assertEqual(enabled["status"], "enabled")
                    self.assertTrue(enabled["auto_install"])
                    self.assertEqual(enabled["interval_seconds"], 21600)
                    self.assertEqual(enabled["installation"]["current"]["version"], "0.3.0")
                    applied = json.loads(self.run_process([self.python, "-I", "-B", runner, response_path,
                                                          "update", "auto", "run"]).stdout)
                    self.assertEqual(applied["status"], "updated", applied)
                    self.assertEqual(applied["version"], version)
                    # A new public invocation selects the activated generation
                    # and returns locally without querying the offline feed.
                    self.assertEqual(self.console_json("update", "auto", "run")["status"], "not_due")
                    checked = json.loads(self.run_process([self.python, "-I", "-B", runner, response_path,
                                                          "update", "check"]).stdout)
                    self.assertEqual(checked["status"], "up_to_date")
                    result = checked["installation"]
                else:
                    result = json.loads(self.run_process([self.python, "-I", "-B", runner, response_path,
                                                          "update", "apply", "--version", version]).stdout)
                    self.assertEqual(result["status"], "updated", result)
                    self.assertEqual(result["cleanup"]["failures"], [])
                self.assertEqual(result["current"]["version"], version)
                results.append(result)
                self.assert_public_version(version)
                if version != "0.2.0":
                    self.assertFalse((Path(context["site"]) / "richi_launcher").exists())
                    self.assertFalse((Path(context["site"]) / "richi/memory.py").exists())
                    self.assertTrue((Path(context["site"]) / "richi/__main__.py").is_file())
            last = results[-1]
            self.assertEqual(last["previous"]["version"], "0.3.0")
            self.assertEqual(last["retired"], [])
            self.assertFalse(Path(results[0]["current"]["prefix"]).exists())
            self.assertEqual(len(list((Path(context["root"]) / "versions").iterdir())), 2)
            # Destroy current launcher imports. The unchanged original bootstrap
            # must route rollback through the previous healthy generation.
            broken_cli = Path(last["current"]["site"]) / "richi_launcher/cli.py"
            broken_cli.write_text("raise RuntimeError('synthetic broken current launcher')\n", encoding="utf-8")
            broken = self.console("--workspace", "default", "--version", ok=False)
            self.assertIn("synthetic broken current launcher", broken.stderr)
            recovered = self.console("--compact-json", "--chat=wheel-lifecycle", "update", "rollback")
            rollback = json.loads(recovered.stdout)
            self.assertEqual(recovered.stdout, json.dumps(rollback, ensure_ascii=False,
                                                         separators=(",", ":")) + "\n")
            self.assertEqual(rollback["status"], "rolled_back")
            self.assertEqual(rollback["current"]["version"], "0.3.0")
            self.assert_public_version("0.3.0")
            paused = self.console_json("update", "auto", "run")
            self.assertEqual(paused["status"], "paused")
            self.assertEqual(paused["pause"]["reason"], "manual_rollback")
            auto_state = json.loads((Path(context["root"]) / "auto-state.json").read_text())
            self.assertEqual([entry["status"] for entry in auto_state["history"]], ["update_available", "updated"])
            self.assertEqual(auto_state["history"][0]["version"], "0.3.0")
            self.assertEqual(auto_state["history"][0]["action"], "richi update apply --version 0.3.0")
            self.assertEqual(auto_state["history"][1]["version"], "0.4.0")
            self.assertEqual(self.console_path.read_bytes(), console_bytes)
            self.assertEqual(bootstrap_path.read_bytes(), bootstrap_bytes)
            self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in protected_paths}, protected)
            self.assertFalse(database.with_name(database.name + "-wal").exists())
            self.assertFalse(database.with_name(database.name + "-shm").exists())


if __name__ == "__main__":
    unittest.main()
