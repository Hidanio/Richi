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


# Feed bytes are the only replacement: real release validation, download
# staging, pip installation, self-check, activation and collection all run.
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

with patch("richi_launcher.releases._transfer", side_effect=transfer):
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

    def make_wheel(self, version):
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
        files["richi_bootstrap.py"] = Path(richi_bootstrap.__file__).read_bytes()
        info = "richi-" + version + ".dist-info/"
        files[info + "METADATA"] = ("Metadata-Version: 2.1\nName: richi\nVersion: " + version
                                     + "\nRequires-Python: >=3.9\n\n").encode()
        files[info + "WHEEL"] = b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n"
        files[info + "entry_points.txt"] = b"[console_scripts]\nrichi = richi_bootstrap:main\n"
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

    def feed_fixture(self, wheel, version):
        data = wheel.read_bytes()
        filename = wheel.name
        manifest = {"format_version": 1, "version": version, "updater_protocol": 1,
                    "python_min": [3, 9], "database_schemas": [1, 2], "map_schema": 2,
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
                result = json.loads(self.run_process([self.python, "-I", "-B", runner, response_path,
                                                      "update", "apply", "--version", version]).stdout)
                self.assertEqual(result["status"], "updated", result)
                self.assertEqual(result["current"]["version"], version)
                self.assertEqual(result["cleanup"]["failures"], [])
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
            rollback = self.console_json("update", "rollback")
            self.assertEqual(rollback["status"], "rolled_back")
            self.assertEqual(rollback["current"]["version"], "0.3.0")
            self.assert_public_version("0.3.0")
            self.assertEqual(self.console_path.read_bytes(), console_bytes)
            self.assertEqual(bootstrap_path.read_bytes(), bootstrap_bytes)
            self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in protected_paths}, protected)
            self.assertFalse(database.with_name(database.name + "-wal").exists())
            self.assertFalse(database.with_name(database.name + "-shm").exists())


if __name__ == "__main__":
    unittest.main()
