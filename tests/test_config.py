"""Configuration precedence and failure behavior without touching a real user store."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from richi_launcher import config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-config-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.env = mock.patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        patch = mock.patch.object(Path, "home", return_value=self.home)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(config.sys, "platform", "linux")
        patch.start()
        self.addCleanup(patch.stop)
        self.config_file = self.root / "configuration" / "config.json"

    def write(self, value):
        self.config_file.parent.mkdir(exist_ok=True)
        self.config_file.write_text(json.dumps(value), encoding="utf-8")
        return self.config_file

    @contextmanager
    def cwd(self, path):
        previous = Path.cwd()
        os.chdir(path)
        try:
            yield
        finally:
            os.chdir(previous)

    def test_defaults_are_user_scoped_and_readonly(self):
        settings = config.resolve_settings()
        self.assertEqual(settings.database, self.home / ".local/share/richi/memory.sqlite3")
        self.assertEqual(settings.config_file, self.home / ".config/richi/config.json")
        self.assertEqual(settings.port, 8765)
        self.assertFalse(settings.dev)
        self.assertIsNone(settings.development_source)
        self.assertEqual(settings.as_dict()["development"], {"source": None})
        self.assertFalse(self.home.exists())

    def test_mac_and_windows_defaults(self):
        with mock.patch.object(config.sys, "platform", "darwin"):
            settings = config.resolve_settings()
            self.assertEqual(settings.database, self.home / "Library/Application Support/Richi/memory.sqlite3")
        with mock.patch.object(config.sys, "platform", "win32"):
            settings = config.resolve_settings()
            self.assertEqual(settings.config_file, self.home / "AppData/Roaming/Richi/config.json")
            self.assertEqual(settings.data_dir, self.home / "AppData/Local/Richi")

    def test_config_paths_are_relative_to_config_not_working_directory(self):
        path = self.write({"database": "../stores/knowledge.sqlite3", "data_dir": "./artifacts", "port": 8123})
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        with self.cwd(elsewhere):
            settings = config.resolve_settings(config_file=path)
        self.assertEqual(settings.database, self.root / "stores/knowledge.sqlite3")
        self.assertEqual(settings.data_dir, path.parent / "artifacts")
        self.assertEqual(settings.port, 8123)
        self.assertFalse(settings.data_dir.exists())

    def test_precedence_cli_environment_config_and_defaults(self):
        path = self.write({"database": "configured.sqlite3", "data_dir": "data", "port": 8123})
        with self.cwd(self.root):
            os.environ.update(RICHI_CONFIG=str(path), RICHI_DB="environment.sqlite3",
                              RICHI_DATA_DIR="environment-data", RICHI_PORT="8124")
            env = config.resolve_settings()
            cli = config.resolve_settings(db="cli.sqlite3", port="8125")
        self.assertEqual(env.database, self.root / "environment.sqlite3")
        self.assertEqual(env.data_dir, self.root / "environment-data")
        self.assertEqual(env.port, 8124)
        self.assertEqual(cli.database, self.root / "cli.sqlite3")
        self.assertEqual(cli.port, 8125)
        # A config database remains authoritative when only the data directory changes.
        del os.environ["RICHI_DB"]
        self.assertEqual(config.resolve_settings().database, path.parent / "configured.sqlite3")

    def test_data_directory_supplies_database_when_database_is_not_explicit(self):
        path = self.write({"data_dir": "data"})
        self.assertEqual(config.resolve_settings(config_file=path).database,
                         path.parent / "data/memory.sqlite3")
        os.environ["RICHI_DATA_DIR"] = str(self.root / "env-data")
        self.assertEqual(config.resolve_settings(config_file=path).database,
                         self.root / "env-data/memory.sqlite3")

    def test_explicit_config_overrides_environment_config(self):
        os.environ["RICHI_CONFIG"] = str(self.root / "missing.json")
        path = self.write({"port": 8123})
        self.assertEqual(config.resolve_settings(config_file=path).port, 8123)
        with self.assertRaisesRegex(config.ConfigError, "does not exist"):
            config.resolve_settings()

    def test_missing_explicit_and_corrupt_default_config_fail(self):
        with self.assertRaisesRegex(config.ConfigError, "does not exist"):
            config.resolve_settings(config_file=self.root / "missing.json")
        default = self.home / ".config/richi/config.json"
        default.parent.mkdir(parents=True)
        default.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(config.ConfigError, "Invalid config"):
            config.resolve_settings(db=self.root / "explicit.sqlite3")

    def test_malformed_unknown_or_duplicate_settings_are_rejected(self):
        invalid = [[], {"db": "typo.sqlite3"}, {"database": None}, {"database": ""},
                   {"database": 4}, {"data_dir": True}, {"port": True}, {"port": "8000"},
                   {"port": 0}, {"port": 65536}, {"port": 8.1}]
        for value in invalid:
            with self.subTest(value=value):
                self.write(value)
                with self.assertRaises(config.ConfigError):
                    config.resolve_settings(config_file=self.config_file)
        self.config_file.write_text('{"port": 8000, "port": 8001}', encoding="utf-8")
        with self.assertRaisesRegex(config.ConfigError, "Duplicate"):
            config.resolve_settings(config_file=self.config_file)

    def test_invalid_environment_is_not_silently_ignored(self):
        for key, value in (("RICHI_DB", ""), ("RICHI_CONFIG", ""),
                           ("RICHI_DATA_DIR", ""), ("RICHI_PORT", "no")):
            with self.subTest(key=key), mock.patch.dict(os.environ, {key: value}):
                with self.assertRaises(config.ConfigError):
                    config.resolve_settings()

    def test_empty_or_relative_xdg_values_use_home_defaults(self):
        for value in ("", "relative-directory", "~/unexpanded-directory"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {
                    "XDG_CONFIG_HOME": value, "XDG_DATA_HOME": value}):
                settings = config.resolve_settings()
                self.assertEqual(settings.config_file, self.home / ".config/richi/config.json")
                self.assertEqual(settings.data_dir, self.home / ".local/share/richi")
                selected = config.resolve_settings(db=self.root / "selected.sqlite3")
                self.assertEqual(selected.database, self.root / "selected.sqlite3")
        self.assertFalse(self.home.exists())

    def test_home_expansion_and_xdg_directories(self):
        path = self.write({"database": "~/knowledge.sqlite3"})
        # expanduser consults the environment, independent of Path.home().
        os.environ["HOME"] = str(self.home)
        self.assertEqual(config.resolve_settings(config_file=path).database,
                         self.home / "knowledge.sqlite3")
        os.environ.update(XDG_CONFIG_HOME=str(self.root / "custom-config"),
                          XDG_DATA_HOME=str(self.root / "custom-data"))
        settings = config.resolve_settings()
        self.assertEqual(settings.config_file, self.root / "custom-config/richi/config.json")
        self.assertEqual(settings.data_dir, self.root / "custom-data/richi")

    def test_development_source_is_config_relative_without_inspecting_checkout(self):
        source = self.root / "missing-checkout"
        path = self.write({"dev": True, "development": {"source": "../missing-checkout"}})
        before = path.read_bytes()
        with self.cwd(self.home.parent):
            settings = config.resolve_settings(config_file=path)
        self.assertTrue(settings.dev)
        self.assertEqual(settings.development_source, source)
        self.assertEqual(settings.as_dict()["development"], {"source": str(source)})
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(source.exists())
        self.assertFalse(settings.database.exists())
        self.assertFalse(path.with_name(path.name + ".lock").exists())

    def test_development_values_are_strict_and_only_come_from_config(self):
        invalid = [{"dev": value} for value in (None, 0, 1, "true", [])]
        invalid += [{"development": value} for value in (None, True, [], "source")]
        invalid += [{"development": {"source": value}} for value in (False, 1, [], "", "\x00")]
        invalid += [{"development": {"directory": "typo"}}]
        for value in invalid:
            with self.subTest(value=value):
                path = self.write(value)
                with self.assertRaises(config.ConfigError):
                    config.resolve_settings(config_file=path)
        path = self.write({"dev": False, "development": {"source": None}})
        with mock.patch.dict(os.environ, {"RICHI_DEV": "true", "RICHI_DEVELOPMENT_SOURCE": "ignored"}):
            settings = config.resolve_settings(config_file=path)
        self.assertFalse(settings.dev)
        self.assertIsNone(settings.development_source)
        self.config_file.write_text('{"development":{"source":null,"source":"other"}}', encoding="utf-8")
        with self.assertRaisesRegex(config.ConfigError, "Duplicate"):
            config.resolve_settings(config_file=self.config_file)

    def test_config_set_preserves_storage_values_and_permissions(self):
        original = {"database": "../store.sqlite3", "data_dir": "relative-data", "port": 8123}
        path = self.write(original)
        path.chmod(0o640)
        with self.cwd(self.root):
            selected = config.set_config_value("development.source", "checkout", config_file=path)
        config.set_config_value("dev", True, config_file=path)
        written = json.loads(path.read_text())
        self.assertEqual(selected, path)
        self.assertEqual({key: written[key] for key in original}, original)
        self.assertEqual(written["development"], {"source": str(self.root / "checkout")})
        self.assertIs(written["dev"], True)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        self.assertFalse((self.root / "checkout").exists())
        self.assertFalse((self.root / "store.sqlite3").exists())

    def test_config_set_creates_private_selected_config_and_accepts_unset_source(self):
        os.environ["RICHI_CONFIG"] = str(self.config_file)
        selected = config.set_config_value("dev", True)
        self.assertEqual(selected, self.config_file)
        self.assertEqual(stat.S_IMODE(selected.stat().st_mode), 0o600)
        self.assertEqual(json.loads(selected.read_text()), {"dev": True})
        self.assertTrue(config.resolve_settings().dev)
        self.assertIsNone(config.resolve_settings().development_source)
        config.set_config_value("development.source", None)
        self.assertEqual(json.loads(selected.read_text())["development"], {"source": None})
        self.assertFalse(self.home.exists())

    def test_config_set_false_recovers_from_unavailable_checkout(self):
        broken = self.root / "broken-checkout"
        broken.symlink_to(broken)
        path = self.write({"dev": True, "development": {"source": str(broken)}})
        config.set_config_value("dev", False, config_file=path)
        settings = config.resolve_settings(config_file=path)
        self.assertFalse(settings.dev)
        self.assertEqual(settings.development_source, broken)
        self.assertTrue(broken.is_symlink())
        self.assertFalse(settings.database.exists())

    def test_config_set_rejects_bad_values_without_creating_config(self):
        invalid = [("database", "other.sqlite3"), ("dev", "false"), ("dev", 0),
                   ("dev", None), ("development.source", False), ("development.source", "")]
        for key, value in invalid:
            with self.subTest(key=key, value=value), self.assertRaises(config.ConfigError):
                config.set_config_value(key, value, config_file=self.config_file)
        self.assertFalse(self.config_file.parent.exists())

    def test_config_set_keeps_existing_config_on_rejected_or_failed_update(self):
        path = self.write({"dev": True, "development": {"unknown": "typo"}})
        before = path.read_bytes()
        with self.assertRaisesRegex(config.ConfigError, "Unknown development"):
            config.set_config_value("dev", False, config_file=path)
        self.assertEqual(path.read_bytes(), before)
        path = self.write({"database": "preserved.sqlite3", "dev": True})
        before = path.read_bytes()
        with mock.patch.object(config.os, "replace", side_effect=OSError("simulated failure")):
            with self.assertRaisesRegex(config.ConfigError, "Cannot update config"):
                config.set_config_value("dev", False, config_file=path)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.glob(".config.json.*")), [])


if __name__ == "__main__":
    unittest.main()
