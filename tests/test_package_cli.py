"""Public package invocation, isolation, and map routing contracts."""

from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from richi import memory


class PackageCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-package-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.env = dict(os.environ)
        for key in list(self.env):
            if key.startswith("RICHI_") or key == "CODEX_THREAD_ID":
                del self.env[key]
        self.env.update(HOME=str(self.root / "home"), USERPROFILE=str(self.root / "home"),
                        APPDATA=str(self.root / "appdata"), LOCALAPPDATA=str(self.root / "localappdata"),
                        XDG_CONFIG_HOME=str(self.root / "config"), XDG_DATA_HOME=str(self.root / "data"),
                        PYTHONPATH=str(Path(memory.__file__).resolve().parents[1]))

    def command(self, *argv, ok=True, cwd=None):
        result = subprocess.run([sys.executable, "-B", "-m", "richi", *map(str, argv)],
                                cwd=cwd or self.root, env=self.env, text=True,
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return result

    def test_config_show_is_readonly_and_cwd_independent(self):
        first = json.loads(self.command("config", "show").stdout)
        other = self.root / "other"
        other.mkdir()
        second = json.loads(self.command("config", "show", cwd=other).stdout)
        self.assertEqual(first, second)
        self.assertFalse(Path(first["database"]).exists())
        self.assertFalse(Path(first["data_dir"]).exists())
        self.assertFalse(Path(first["config_file"]).parent.exists())

    def test_explicit_database_is_the_only_created_store(self):
        default = json.loads(self.command("config", "show").stdout)
        database = self.root / "selected" / "knowledge.sqlite3"
        self.command("--db", database, "init")
        self.assertTrue(database.is_file())
        self.assertFalse(Path(default["data_dir"]).exists())
        result = json.loads(self.command("--db", database, "check").stdout)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(list(self.root.rglob("*.sqlite3")), [database])

    def test_config_file_selects_database_from_another_directory(self):
        config = self.root / "settings.json"
        config.write_text(json.dumps({"database": "store/knowledge.sqlite3"}), encoding="utf-8")
        other = self.root / "other"
        other.mkdir()
        self.command("--config", config, "init", cwd=other)
        self.assertTrue((self.root / "store/knowledge.sqlite3").is_file())
        self.assertFalse((other / "store").exists())

    def test_invalid_config_and_arguments_emit_json_without_writes(self):
        config = self.root / "invalid.json"
        config.write_text('{"unknown": true}', encoding="utf-8")
        for argv in (("--config", config, "init"), ("--config", self.root / "missing.json", "init"),
                     ("--unknown", "init"), ("map", "--port", "invalid", "--no-open")):
            with self.subTest(argv=argv):
                result = self.command(*argv, ok=False)
                self.assertIn("error", json.loads(result.stderr))
        self.assertEqual(list(self.root.rglob("*.sqlite3")), [])

    def test_help_and_version_work_without_a_database(self):
        self.assertIn("map", self.command("--help").stdout)
        self.assertIn("config", self.command("--help").stdout)
        self.assertTrue(self.command("--version").stdout.startswith("Richi "))
        self.assertEqual(list(self.root.rglob("*.sqlite3")), [])

    def test_map_dispatch_is_lazy_and_preserves_global_and_local_options(self):
        with mock.patch("richi.launch_map.main", return_value=0) as launch:
            self.assertEqual(memory.main(["--db", "global.db", "--config", "global.json", "map", "--no-open"]), 0)
            launch.assert_called_once_with(["--db", "global.db", "--config", "global.json", "--no-open"])
        with mock.patch("richi.serve.main", return_value=0) as serve:
            self.assertEqual(memory.main(["--db", "global.db", "map", "serve", "--db", "local.db",
                                       "--config", "local.json", "--port", "8123", "--open"]), 0)
            serve.assert_called_once_with(["--db", "local.db", "--config", "local.json", "--port", "8123", "--open"])
        for args in (["map", "serve", "--no-open"], ["map", "--open"]):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                self.assertEqual(memory.main(args), 1)
            self.assertIn("error", json.loads(stderr.getvalue()))


if __name__ == "__main__":
    unittest.main()
