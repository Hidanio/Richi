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
import time
import unittest
import venv
import zipfile

import richi
import richi_bootstrap
import richi_launcher
from richi_launcher import maps

from cli_environment import cli_environment, synthetic_port


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

# An updater that cannot hand off maps, like the frozen managers up to 1.1.0.
_FROZEN_UPDATE = _OFFLINE_COMMAND.replace(
    'with patch("richi_launcher.releases._transfer", side_effect=transfer), \\',
    'with patch("richi_launcher.maps.after_activation", side_effect=lambda result: result), \\\n'
    '        patch("richi_launcher.releases._transfer", side_effect=transfer), \\')
assert _FROZEN_UPDATE != _OFFLINE_COMMAND

# A rollback performed by such a manager: the previous generation swaps the
# pointer and nothing else. Like 1.0.1, the unchecked variant does not compare
# database schema support.
_FROZEN_ROLLBACK = r'''
import json, os
from pathlib import Path
import sys
import richi_bootstrap as bootstrap
if os.environ.get("SYNTHETIC_UNCHECKED_SCHEMAS"):
    bootstrap.require_schema_compatible = lambda current, target: None

ctx = bootstrap.context()
state = bootstrap.read_state(ctx)
prefix = Path(state["generations"][state["previous"]]["prefix"])
if prefix != Path(sys.prefix).resolve():
    python = str(prefix / "bin" / "python")
    os.execve(python, [python, "-I", "-B", __file__], dict(os.environ))
from richi_launcher import installation
print(json.dumps(installation.rollback()))
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

    def make_wheel(self, version, *, storage_schema=None):
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
        if storage_schema is not None:
            # An older runtime whose newest storage schema is lower, as in 1.0.1.
            memory = files["richi/memory.py"].decode("utf-8")
            self.assertIn("SCHEMA = 3\n", memory)
            files["richi/memory.py"] = memory.replace("SCHEMA = 3\n", "SCHEMA = %d\n" % storage_schema, 1).encode()
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

    def feed_fixture(self, wheel, version, *, database_schemas=(1, 2)):
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

    def test_schema_three_release_installs_in_place_and_blocks_rollback(self):
        # The manifest keeps the [1, 2] declaration that 1.0.1 updaters require;
        # the manager learns real support from the release's self-check.
        with tempfile.TemporaryDirectory(prefix="richi-schema-in-place-") as temporary:
            self.root = Path(temporary).resolve()
            # After an update the first command looks for maps on its port.
            self.environment = cli_environment(self.root, RICHI_PORT=synthetic_port())
            for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
                self.environment.pop(key, None)
            older = self.make_wheel("0.1.0", storage_schema=2)
            latest = self.make_wheel("0.2.0")
            seed = self.root / "seed"
            venv.EnvBuilder(with_pip=True, symlinks=True).create(str(seed))
            self.run_process([seed / "bin/python", "-I", "-B", "-m", "pip", "--isolated", "--disable-pip-version-check",
                              "install", "--no-index", "--no-deps", "--no-cache-dir", "--no-compile", older])
            self.python, self.console_path = seed / "bin/python", seed / "bin/richi"
            runner = self.root / "offline_update.py"
            runner.write_text(_OFFLINE_COMMAND, encoding="utf-8")
            feed = self.feed_fixture(latest, "0.2.0")
            self.assertEqual(json.loads(self.run_process([self.python, "-I", "-B", runner, feed, "update", "check"]).stdout)["status"],
                             "update_available")
            updated = json.loads(self.run_process([self.python, "-I", "-B", runner, feed, "update", "apply"]).stdout)
            self.assertEqual((updated["current"]["version"], updated["current"]["database_schemas"]), ("0.2.0", [1, 2, 3]))
            self.assert_public_version("0.2.0")
            refused = self.run_process([self.python, "-I", "-B", runner, feed, "update", "rollback"], ok=False)
            self.assertIn("drops database schema support for 3", refused.stdout + refused.stderr)
            self.assert_public_version("0.2.0")

    def test_running_map_follows_every_activation_on_the_same_url(self):
        with tempfile.TemporaryDirectory(prefix="richi-map-handoff-") as temporary:
            self.root = Path(temporary).resolve()
            port = int(synthetic_port())
            # Every command addresses this ephemeral port, never a real map. The
            # map never follows by itself until the last step, so each step
            # proves who handed it off however slow the machine is.
            self.environment = cli_environment(self.root, RICHI_PORT=str(port),
                                               **{maps.GRACE_ENV: "3600"})
            for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
                self.environment.pop(key, None)
            wheels = {version: self.make_wheel(version) for version in ("0.1.0", "0.2.0", "0.3.0", "0.4.0", "0.5.0")}
            seed = self.root / "seed"
            venv.EnvBuilder(with_pip=True, symlinks=True).create(str(seed))
            self.python, self.console_path = seed / "bin/python", seed / "bin/richi"
            self.run_process([self.python, "-I", "-B", "-m", "pip", "--isolated", "--disable-pip-version-check",
                              "install", "--no-index", "--no-deps", "--no-cache-dir", "--no-compile", wheels["0.1.0"]])
            scripts = {}
            for name, text in (("offline", _OFFLINE_COMMAND), ("frozen", _FROZEN_UPDATE),
                               ("frozen-rollback", _FROZEN_ROLLBACK)):
                scripts[name] = self.root / (name + ".py")
                scripts[name].write_text(text, encoding="utf-8")

            def updater(name, *arguments):
                return json.loads(self.run_process([self.python, "-I", "-B", scripts[name], *arguments]).stdout)

            def health():
                return maps.read_health(port, timeout=2)

            def wait_for(version, timeout=30):
                deadline = time.monotonic() + timeout
                while True:
                    current = health()
                    if maps.is_map(current) and current["runtime"]["version"] == version:
                        return current
                    self.assertLess(time.monotonic(), deadline, "Map did not reach " + version + ": " + str(current))
                    time.sleep(0.1)

            def map_log():
                return "\n".join(path.read_text() for path in self.root.rglob("runtime/map-server.log"))

            def assert_handed_off(result, version, previous):
                self.assertFalse(result["restart_maps_required"], json.dumps(result) + "\n" + map_log())
                self.assertEqual([(entry["url"], entry["status"], entry["from_version"], entry["version"], entry["pid"])
                                  for entry in result["maps"]],
                                 [(maps.url(port), "restarted", previous, version, pid)])
                current = health()
                # The map re-executes itself: same process, same URL, new code and lease.
                self.assertEqual((current["pid"], current["runtime"]["version"]), (pid, version))
                return current

            self.console("init")
            started = self.console_json("map", "--no-open")
            self.assertEqual((started["status"], started["url"]), ("started", maps.url(port)))
            pid = started["pid"]
            try:
                self.assertEqual(health()["installation_generation"], "seed")
                # An unmanaged installation writes nothing to its data directory.
                self.assertEqual(list(self.root.rglob("installations")), [])
                applied = updater("offline", self.feed_fixture(wheels["0.2.0"], "0.2.0"), "update", "apply")
                self.assertEqual(applied["status"], "updated")
                current = assert_handed_off(applied, "0.2.0", "0.1.0")
                self.assertEqual(current["installation_generation"], applied["current"]["generation"])
                # Once managed, the map registers, so updaters find it on any port.
                registry = Path(applied["root"]) / "maps" / ("%d.json" % port)
                self.assertEqual(json.loads(registry.read_text())["pid"], pid)
                self.assertNotIn("restart_maps_required", updater("offline", self.feed_fixture(
                    wheels["0.2.0"], "0.2.0"), "update", "check")["installation"])

                feed = self.feed_fixture(wheels["0.3.0"], "0.3.0")
                self.assertTrue(updater("offline", feed, "update", "auto", "enable", "--install")["auto_install"])
                installed = updater("offline", feed, "update", "auto", "run")
                self.assertEqual(installed["status"], "updated", installed)
                assert_handed_off(installed, "0.3.0", "0.2.0")
                # The seed's code was retired while no map leased it.
                self.assertFalse((Path(applied["previous"]["site"]) / "richi/memory.py").exists())

                rolled_back = self.console_json("update", "rollback")
                self.assertEqual(rolled_back["status"], "rolled_back")
                assert_handed_off(rolled_back, "0.2.0", "0.3.0")

                # A frozen updater activates 0.4.0 and leaves the map behind. The
                # first command of the new generation asks it to switch, once,
                # without waiting for it.
                frozen = updater("frozen", self.feed_fixture(wheels["0.4.0"], "0.4.0"), "update", "apply")
                self.assertEqual(frozen["status"], "updated")
                self.assertNotIn("maps", frozen)
                self.assertEqual(health()["runtime"]["version"], "0.2.0")
                self.console("--version")
                marker = json.loads((Path(frozen["root"]) / "maps" / "handoff.json").read_text())
                self.assertEqual((marker["generation"], marker["scanned"], marker["pending"]),
                                 (frozen["current"]["generation"], [port], []))
                self.assertEqual([(entry["status"], entry["from_version"]) for entry in marker["maps"]],
                                 [("requested", "0.2.0")])
                self.assertEqual(wait_for("0.4.0")["pid"], pid)

                # The desktop shortcut runs richi map right after a frozen
                # update: it asks the map to switch in place on the same URL.
                self.assertEqual(updater("frozen", self.feed_fixture(wheels["0.5.0"], "0.5.0"),
                                         "update", "apply")["status"], "updated")
                shortcut = self.console_json("map", "--no-open")
                self.assertEqual((shortcut["status"], shortcut["url"], shortcut["pid"]),
                                 ("restarted", maps.url(port), pid))
                self.assertEqual((shortcut["previous"]["version"], health()["runtime"]["version"]), ("0.4.0", "0.5.0"))

                # A frozen rollback runs no Richi command at all: the map notices
                # the installation change and follows it by itself after a grace.
                self.environment[maps.GRACE_ENV] = "1"
                restarted = self.console_json("map", "restart", "--no-open")
                self.assertEqual((restarted["status"], restarted["previous"]["pid"]), ("restarted", pid))
                pid = restarted["pid"]
                self.assertEqual(updater("frozen-rollback")["status"], "rolled_back")
                self.assertEqual(wait_for("0.4.0")["pid"], pid)
                self.assertEqual(self.console_json("map", "--no-open")["status"], "existing")
            finally:
                current = health()
                if maps.is_map(current) and current["pid"] == pid:
                    stopped = self.console_json("map", "stop")
                    self.assertEqual((stopped["status"], stopped["pid"], stopped["exited"]), ("stopped", pid, True))
            self.assertTrue(maps.port_free(port))
            cleaned = self.console_json("update", "cleanup")
            self.assertEqual(cleaned["cleanup"]["deferred"], [])
            self.assertEqual(len(list((Path(cleaned["root"]) / "versions").iterdir())), 2)

    def test_running_map_keeps_its_version_when_the_current_release_cannot_read_the_store(self):
        with tempfile.TemporaryDirectory(prefix="richi-map-schema-") as temporary:
            self.root = Path(temporary).resolve()
            port = int(synthetic_port())
            self.environment = cli_environment(self.root, RICHI_PORT=str(port), **{maps.GRACE_ENV: "1"})
            for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
                self.environment.pop(key, None)
            older, newer = self.make_wheel("0.1.0", storage_schema=2), self.make_wheel("0.2.0")
            seed = self.root / "seed"
            venv.EnvBuilder(with_pip=True, symlinks=True).create(str(seed))
            self.python, self.console_path = seed / "bin/python", seed / "bin/richi"
            self.run_process([self.python, "-I", "-B", "-m", "pip", "--isolated", "--disable-pip-version-check",
                              "install", "--no-index", "--no-deps", "--no-cache-dir", "--no-compile", older])
            offline, rollback = self.root / "offline.py", self.root / "unchecked-rollback.py"
            offline.write_text(_OFFLINE_COMMAND, encoding="utf-8")
            rollback.write_text(_FROZEN_ROLLBACK, encoding="utf-8")
            self.console("init")
            self.assertEqual(json.loads(self.run_process([self.python, "-I", "-B", offline, self.feed_fixture(
                newer, "0.2.0"), "update", "apply"]).stdout)["status"], "updated")
            self.console("backup")
            self.assertEqual(self.console_json("migrate")["schema_version"], 3)
            started = self.console_json("map", "--no-open")
            pid = started["pid"]
            log = next(self.root.rglob("runtime/map-server.log"))
            try:
                # Like 1.0.1, this rollback does not compare schema support.
                swapped = subprocess.run([str(self.python), "-I", "-B", str(rollback)],
                                         env=dict(self.environment, SYNTHETIC_UNCHECKED_SCHEMAS="1"),
                                         cwd=self.root, stdin=subprocess.DEVNULL, capture_output=True,
                                         text=True, timeout=90)
                self.assertEqual(swapped.returncode, 0, swapped.stdout + swapped.stderr)
                self.assertEqual(json.loads(swapped.stdout)["current"]["version"], "0.1.0")
                deadline = time.monotonic() + 20
                while "cannot read database schema 3" not in log.read_text():
                    self.assertLess(time.monotonic(), deadline, log.read_text())
                    time.sleep(0.1)
                # The map keeps serving the store on its version instead of exiting.
                time.sleep(2)
                current = maps.read_health(port, timeout=2)
                self.assertEqual((current["pid"], current["runtime"]["version"]), (pid, "0.2.0"))
                self.assertEqual(log.read_text().count("cannot read database schema 3"), 1)
                # Rolling forward again needs no restart.
                forward = self.console_json("update", "rollback")
                self.assertEqual((forward["current"]["version"], forward["restart_maps_required"]), ("0.2.0", False))
                self.assertEqual([(entry["status"], entry["pid"], entry["version"]) for entry in forward["maps"]],
                                 [("current", pid, "0.2.0")])
                self.assertEqual(maps.read_health(port, timeout=2)["pid"], pid)
            finally:
                current = maps.read_health(port, timeout=2)
                if maps.is_map(current) and current["pid"] == pid:
                    self.assertEqual(self.console_json("map", "stop")["status"], "stopped")

    def test_real_wheels_update_prune_and_recover_without_changing_workspaces(self):
        with tempfile.TemporaryDirectory(prefix="richi-wheel-lifecycle-") as temporary:
            self.root = Path(temporary).resolve()
            # After an update the first command looks for maps on its port.
            self.environment = cli_environment(self.root, RICHI_PORT=synthetic_port())
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
