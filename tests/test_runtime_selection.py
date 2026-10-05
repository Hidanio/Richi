"""Exercise the stable launcher against fixed and development package copies."""
import hashlib
import json
import os
from pathlib import Path
import py_compile
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ("import sys; sys.path.insert(0, sys.argv[1]); "
             "from richi_launcher.cli import main; sys.exit(main(sys.argv[2:]))")


class RuntimeSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-runtime-selection-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.installed = self.root / "fixed package"
        self.development = self.root / "development checkout"
        self.dev_package = self.development / "src" / "richi"
        ignored = shutil.ignore_patterns("__pycache__", "*.pyc")
        shutil.copytree(ROOT / "launcher" / "richi_launcher",
                        self.installed / "richi_launcher", ignore=ignored)
        shutil.copytree(ROOT / "src" / "richi", self.installed / "richi", ignore=ignored)
        shutil.copytree(ROOT / "src" / "richi", self.dev_package, ignore=ignored)
        shutil.copyfile(ROOT / "pyproject.toml", self.development / "pyproject.toml")
        self.set_version(self.installed / "richi", "release-test")
        self.set_version(self.dev_package, "dev-alpha")
        self.database = self.root / "store" / "knowledge.sqlite3"
        self.config = self.root / "settings.json"
        self.original_config = {"database": str(self.database),
                                "data_dir": str(self.database.parent), "port": 8943}
        self.config.write_text(json.dumps(self.original_config), encoding="utf-8")
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith("RICHI_") and key != "CODEX_THREAD_ID"}
        self.environment.update(HOME=str(self.root / "home"),
                                XDG_CONFIG_HOME=str(self.root / "config"),
                                XDG_DATA_HOME=str(self.root / "data"))
        self.bad_config = self.root / "broken environment config.json"
        self.bad_config.write_text("not JSON", encoding="utf-8")
        self.environment["RICHI_CONFIG"] = str(self.bad_config)

    @staticmethod
    def set_version(package, version):
        (package / "__init__.py").write_text('__version__ = "' + version + '"\n',
                                             encoding="utf-8")

    def command(self, *args, ok=True, cwd=None, input_json=None):
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", BOOTSTRAP, str(self.installed),
             "--config", str(self.config), *map(str, args)],
            cwd=cwd or self.root, env=self.environment,
            input=json.dumps(input_json) if input_json is not None else None,
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return result

    def json_command(self, *args, **kwargs):
        return json.loads(self.command(*args, **kwargs).stdout)

    def enable_development(self):
        self.json_command("config", "set", "development.source", self.development)
        result = self.json_command("config", "set", "dev", "true")
        self.assertEqual(result["runtime"]["mode"], "dev")
        self.assertEqual(result["runtime"]["package"], str(self.dev_package))
        return result

    def test_fixed_runtime_ignores_source_edits_cwd_and_pythonpath(self):
        self.json_command("config", "set", "development.source", self.development)
        self.dev_package.joinpath("__init__.py").write_text("invalid syntax !\n", encoding="utf-8")
        shadow = self.root / "shadow"
        for name in ("richi", "richi_launcher"):
            package = shadow / name
            package.mkdir(parents=True)
            package.joinpath("__init__.py").write_text(
                'raise RuntimeError("must not import cwd or PYTHONPATH package")\n', encoding="utf-8")
        self.environment["PYTHONPATH"] = str(shadow)
        self.assertEqual(self.command("--version", cwd=shadow).stdout.strip(), "Richi release-test")
        shown = self.json_command("config", "show", cwd=shadow)
        self.assertFalse(shown["dev"])
        self.assertEqual(shown["runtime"]["package"], str(self.installed / "richi"))
        self.assertEqual(shown["runtime"]["mode"], "release")
        self.assertFalse(self.database.exists())
        self.assertEqual(self.bad_config.read_text(encoding="utf-8"), "not JSON")

    def test_development_reads_same_size_same_timestamp_edits_then_returns_to_fixed_code(self):
        self.enable_development()
        self.assertEqual(self.command("--version").stdout.strip(), "Richi dev-alpha")
        source = self.dev_package / "__init__.py"
        before = source.stat()
        # Pre-create a valid timestamp-based cache for the old bytes. Ordinary
        # -B alone can still read it after an equal-length, equal-time edit.
        cache = source.parent / "__pycache__" / ("__init__." + sys.implementation.cache_tag + ".pyc")
        py_compile.compile(str(source), cfile=str(cache), doraise=True, optimize=0,
                           invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
        self.set_version(self.dev_package, "dev-bravo")
        self.assertEqual(source.stat().st_size, before.st_size)
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(self.command("--version").stdout.strip(), "Richi dev-bravo")
        disabled = self.json_command("config", "set", "dev", "false")
        self.assertEqual(disabled["runtime"]["mode"], "release")
        self.assertEqual(self.command("--version").stdout.strip(), "Richi release-test")
        self.assertFalse(self.database.exists())

    def test_configuration_recovers_from_broken_or_missing_development_sources(self):
        original = (self.dev_package / "memory.py").read_bytes()
        for broken in ("syntax", "import", "version", "missing"):
            with self.subTest(broken=broken):
                self.enable_development()
                if broken == "syntax":
                    (self.dev_package / "memory.py").write_text("def broken(:\n", encoding="utf-8")
                elif broken == "import":
                    (self.dev_package / "memory.py").write_text(
                        'raise RuntimeError("synthetic import failure")\n', encoding="utf-8")
                elif broken == "version":
                    (self.dev_package / "__init__.py").write_text("invalid syntax !\n", encoding="utf-8")
                else:
                    self.development.rename(self.root / "moved checkout")
                failed = self.command("--version", ok=False)
                self.assertIn("error", json.loads(failed.stderr))
                shown = self.json_command("config", "show")
                self.assertTrue(shown["dev"])
                self.assertEqual(shown["runtime"]["mode"], "dev")
                disabled = self.json_command("config", "set", "dev", "false")
                self.assertFalse(disabled["dev"])
                self.assertEqual(self.command("--version").stdout.strip(), "Richi release-test")
                if broken == "missing":
                    (self.root / "moved checkout").rename(self.development)
                (self.dev_package / "memory.py").write_bytes(original)
                self.set_version(self.dev_package, "dev-alpha")
        self.assertFalse(self.database.exists())

    def test_switching_runtime_preserves_storage_schema_history_and_artifacts(self):
        self.json_command("init")
        self.json_command("project", "upsert", "--json", "-",
                          input_json={"id": "demo", "name": "Synthetic project"})
        entry = {"id": "note:runtime-fixture", "kind": "note", "title": "Runtime fixture",
                 "summary": "First observation", "project_ids": ["demo"],
                 "work_state": "done", "knowledge_state": "confirmed",
                 "sources": [{"reference": "test://runtime-selection"}]}
        self.json_command("entry", "put", "--json", "-", input_json=entry)
        first = self.json_command("entry", "get", entry["id"])
        entry.update(summary="Updated observation", expected_updated_at=first["updated_at"])
        self.json_command("entry", "put", "--json", "-", input_json=entry)
        history = self.json_command("entry", "get", entry["id"], "--history")
        self.assertGreater(len(history["history"]), 0)
        artifact = self.database.parent / "artifacts" / "git" / "synthetic-evidence.txt"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"historical source bytes\n")

        def snapshot():
            with sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True) as connection:
                schema = connection.execute("PRAGMA user_version").fetchone()[0]
                contents = tuple(connection.iterdump())
            return (schema, contents, hashlib.sha256(self.database.read_bytes()).hexdigest(),
                    hashlib.sha256(artifact.read_bytes()).hexdigest())

        baseline = snapshot()
        self.assertEqual(baseline[0], 2)
        for enabled in (True, False):
            if enabled:
                result = self.enable_development()
            else:
                result = self.json_command("config", "set", "dev", "false")
            for key, value in self.original_config.items():
                self.assertEqual(result[key], value)
                self.assertEqual(json.loads(self.config.read_text(encoding="utf-8"))[key], value)
            self.assertEqual(self.json_command("check")["status"], "ok")
            self.assertEqual(self.json_command("entry", "get", entry["id"], "--history"), history)
            self.assertEqual(snapshot(), baseline)
        self.assertEqual(list(self.root.rglob("*.sqlite3")), [self.database])
        self.assertEqual(self.bad_config.read_text(encoding="utf-8"), "not JSON")

    def test_abbreviated_config_selectors_cannot_change_runtime_selection(self):
        alternate = self.root / "development settings.json"
        alternate.write_text(json.dumps(dict(self.original_config, dev=True,
                                             development={"source": str(self.development)})),
                             encoding="utf-8")
        for args in (("--conf", alternate, "--version"),
                     ("map", "--conf", alternate, "--no-open")):
            with self.subTest(args=args):
                failed = self.command(*args, ok=False)
                self.assertTrue(json.loads(failed.stderr)["error"])
                self.assertEqual(failed.stdout, "")
        self.assertEqual(self.command("--version").stdout.strip(), "Richi release-test")
        self.assertFalse(self.database.exists())

    def test_map_rejects_removed_mode_flag_in_both_runtimes(self):
        for enabled in (False, True):
            if enabled:
                self.enable_development()
            for args in (("map", "--dev", "--no-open"), ("map", "serve", "--dev")):
                with self.subTest(dev=enabled, args=args):
                    failed = self.command(*args, ok=False)
                    self.assertIn("--dev", json.loads(failed.stderr)["error"])
        self.assertFalse(self.database.exists())


if __name__ == "__main__":
    unittest.main()
