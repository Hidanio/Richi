"""Workspace selection, isolation and registry transactions use disposable paths."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from richi_launcher import config, workspaces


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-workspaces-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        for patch in (mock.patch.dict(os.environ, {}, clear=True),
                      mock.patch.object(Path, "home", return_value=self.home),
                      mock.patch.object(config.sys, "platform", "linux")):
            patch.start()
            self.addCleanup(patch.stop)
        self.registry = self.home / ".config/richi/workspaces.json"
        self.default_config = self.registry.parent / "config.json"
        self.data = self.home / ".local/share/richi"

    def write(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content), encoding="utf-8")
        return path

    def test_implicit_default_reads_do_not_create_files(self):
        self.assertEqual(workspaces.choose_config(), (self.default_config, False, "default"))
        settings = config.resolve_settings()
        self.assertEqual(settings.workspace, "default")
        self.assertEqual(settings.database, self.data / "memory.sqlite3")
        self.assertEqual(settings.as_dict()["workspace"], "default")
        listed = workspaces.list_workspaces()
        self.assertEqual(listed["current"], "default")
        self.assertEqual([x["name"] for x in listed["workspaces"]], ["default"])
        self.assertFalse(listed["workspaces"][0]["initialized"])
        self.assertFalse(self.home.exists())

    def test_create_inherits_dev_but_preserves_default_bytes_and_does_not_init(self):
        self.write(self.default_config, {"database": "existing.sqlite3", "dev": True,
                                        "development": {"source": "missing-checkout"}})
        old_bytes = self.default_config.read_bytes()
        self.default_config.parent.joinpath("existing.sqlite3").write_bytes(b"existing knowledge")
        created = workspaces.create_workspace("client-a")
        path = self.data / "workspaces/client-a/config.json"
        self.assertEqual(created["config_file"], str(path))
        self.assertEqual(created["current"], "default")
        self.assertFalse(created["initialized"])
        self.assertFalse(Path(created["database"]).exists())
        selected = config.resolve_settings(workspace="client-a")
        self.assertEqual(selected.workspace, "client-a")
        self.assertEqual(selected.database, path.parent / "memory.sqlite3")
        self.assertEqual(selected.data_dir, path.parent)
        self.assertTrue(selected.dev)
        self.assertEqual(selected.development_source, self.default_config.parent / "missing-checkout")
        self.assertEqual(self.default_config.read_bytes(), old_bytes)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.registry.stat().st_mode), 0o600)
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_create_and_use_are_separate_and_updates_preserve_records(self):
        workspaces.create_workspace("alpha")
        workspaces.create_workspace("beta")
        self.assertEqual(config.resolve_settings().workspace, "default")
        self.assertEqual(workspaces.use_workspace("alpha")["current"], "alpha")
        self.assertEqual(config.resolve_settings().workspace, "alpha")
        self.assertEqual(workspaces.use_workspace("beta")["current"], "beta")
        self.assertEqual(config.resolve_settings().workspace, "beta")
        config.set_config_value("dev", True)
        self.assertTrue(config.resolve_settings(workspace="beta").dev)
        self.assertFalse(config.resolve_settings(workspace="alpha").dev)
        workspaces.use_workspace("default")
        self.assertEqual(config.resolve_settings().database, self.data / "memory.sqlite3")
        self.assertEqual([x["name"] for x in workspaces.list_workspaces()["workspaces"]],
                         ["default", "alpha", "beta"])

    def test_explicit_and_environment_precedence(self):
        workspaces.create_workspace("alpha")
        workspaces.create_workspace("beta")
        workspaces.use_workspace("beta")
        custom = self.write(self.root / "custom.json", {})
        os.environ["RICHI_WORKSPACE"] = "alpha"
        self.assertEqual(config.resolve_settings().workspace, "alpha")
        os.environ["RICHI_CONFIG"] = str(custom)
        self.assertEqual(config.resolve_settings().config_file, custom)
        self.assertIsNone(config.resolve_settings().workspace)
        self.assertEqual(config.resolve_settings(workspace="beta").workspace, "beta")
        os.environ["RICHI_WORKSPACE"] = "missing"
        self.assertEqual(config.resolve_settings(config_file=custom).config_file, custom)
        with self.assertRaisesRegex(config.ConfigError, "cannot be combined"):
            config.resolve_settings(workspace="alpha", config_file=custom)
        del os.environ["RICHI_CONFIG"]
        with self.assertRaisesRegex(config.ConfigError, "Unknown workspace"):
            config.resolve_settings()
        with self.assertRaisesRegex(config.ConfigError, "Unknown workspace"):
            config.resolve_settings(workspace="missing")

    def test_named_storage_rejects_overrides_for_every_selection_method(self):
        workspaces.create_workspace("alpha")
        for selection in ({"workspace": "alpha"}, {}):
            workspaces.use_workspace("alpha")
            with self.subTest(selection=selection):
                with self.assertRaisesRegex(config.ConfigError, "isolated storage"):
                    config.resolve_settings(db=self.root / "other.sqlite3", **selection)
                for key in ("RICHI_DB", "RICHI_DATA_DIR"):
                    with mock.patch.dict(os.environ, {key: str(self.root / "other")}):
                        with self.assertRaisesRegex(config.ConfigError, key):
                            config.resolve_settings(**selection)
        # Existing default/custom configuration behavior remains available.
        with mock.patch.dict(os.environ, {"RICHI_DB": str(self.root / "other.sqlite3")}):
            self.assertEqual(config.resolve_settings(workspace="default").database,
                             self.root / "other.sqlite3")
        selected = workspaces.choose_config(workspace="alpha")[0]
        self.assertEqual(config.resolve_settings(config_file=selected, db=self.root / "explicit.db").database,
                         self.root / "explicit.db")

    def test_empty_or_redirected_named_configs_cannot_share_storage(self):
        first = Path(workspaces.create_workspace("alpha")["config_file"])
        second = Path(workspaces.create_workspace("beta")["config_file"])
        self.write(first, {})
        self.assertEqual(config.resolve_settings(workspace="alpha").database,
                         first.parent / "memory.sqlite3")
        for values in ({"database": str(second.parent / "memory.sqlite3")},
                       {"data_dir": str(second.parent)}, {"database": "alternate.db"}):
            self.write(first, values)
            with self.assertRaisesRegex(config.ConfigError, "isolated storage"):
                config.resolve_settings(workspace="alpha")
            with self.assertRaisesRegex(config.ConfigError, "isolated storage"):
                config.set_config_value("dev", False, workspace="alpha")
        self.write(first, {})
        first.parent.joinpath("memory.sqlite3").symlink_to(second.parent / "memory.sqlite3")
        with self.assertRaisesRegex(config.ConfigError, "isolated storage"):
            config.resolve_settings(workspace="alpha")

    def test_default_store_cannot_already_point_at_new_workspace(self):
        directory = self.data / "workspaces/alpha"
        self.write(self.default_config, {"database": str(directory / "memory.sqlite3")})
        with self.assertRaisesRegex(config.ConfigError, "overlaps the default"):
            workspaces.create_workspace("alpha")
        self.assertFalse(directory.exists())
        self.assertFalse(self.registry.exists())

    def test_missing_named_config_does_not_fall_back_or_get_recreated(self):
        path = Path(workspaces.create_workspace("alpha")["config_file"])
        path.unlink()
        workspaces.use_workspace("alpha")
        with self.assertRaisesRegex(config.ConfigError, "does not exist"):
            config.resolve_settings()
        with self.assertRaisesRegex(config.ConfigError, "does not exist"):
            config.set_config_value("dev", False)
        self.assertFalse(path.exists())
        self.assertIn("error", workspaces.list_workspaces()["workspaces"][1])
        workspaces.use_workspace("default")
        self.assertEqual(config.resolve_settings().workspace, "default")

    def test_invalid_names_do_not_create_any_files(self):
        for name in ("", "A", "../escape", "a/b", "a.b", "1name", "a" * 49, None, "alpha\n"):
            with self.subTest(name=name):
                with self.assertRaises(config.ConfigError):
                    workspaces.create_workspace(name)
        with self.assertRaisesRegex(config.ConfigError, "already exists"):
            workspaces.create_workspace("default")
        self.assertFalse(self.home.exists())

    def test_existing_name_directory_and_dangling_link_are_never_overwritten(self):
        created = workspaces.create_workspace("alpha")
        before = Path(created["config_file"]).read_bytes()
        with self.assertRaisesRegex(config.ConfigError, "already exists"):
            workspaces.create_workspace("alpha")
        self.assertEqual(Path(created["config_file"]).read_bytes(), before)
        directory = self.data / "workspaces/beta"
        directory.mkdir()
        sentinel = directory / "keep"
        sentinel.write_text("important", encoding="utf-8")
        with self.assertRaisesRegex(config.ConfigError, "already exists"):
            workspaces.create_workspace("beta")
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "important")
        link = self.data / "workspaces/link"
        link.symlink_to(self.root / "missing")
        with self.assertRaisesRegex(config.ConfigError, "already exists"):
            workspaces.create_workspace("link")
        self.assertTrue(link.is_symlink())
        self.assertFalse(self.root.joinpath("missing").exists())

    def test_concurrent_creates_preserve_all_records_and_single_winner(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(workspaces.create_workspace, ["alpha", "beta", "gamma", "delta"]))
        self.assertEqual(len(results), 4)
        self.assertEqual(len(workspaces.list_workspaces()["workspaces"]), 5)
        def attempt():
            try:
                workspaces.create_workspace("same")
                return True
            except config.ConfigError:
                return False
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(lambda _: attempt(), range(4))), 1)
        self.assertEqual(len(workspaces.list_workspaces()["workspaces"]), 6)

    def test_registry_write_failure_preserves_previous_registry_and_rolls_back_new_directory(self):
        workspaces.create_workspace("alpha")
        before = self.registry.read_bytes()
        write = workspaces._atomic_json
        def fail_registry(path, value):
            if path == self.registry:
                raise OSError("simulated full disk")
            return write(path, value)
        with mock.patch.object(workspaces, "_atomic_json", side_effect=fail_registry):
            with self.assertRaisesRegex(config.ConfigError, "full disk"):
                workspaces.create_workspace("beta")
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertFalse(self.data.joinpath("workspaces/beta").exists())
        self.assertTrue(self.data.joinpath("workspaces/alpha/config.json").exists())
        with mock.patch.object(workspaces.os, "replace", side_effect=OSError("atomic failure")):
            with self.assertRaisesRegex(config.ConfigError, "atomic failure"):
                workspaces.use_workspace("alpha")
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(list(self.registry.parent.glob(".workspaces.json.*")), [])

    def test_corrupt_registry_fails_without_fallback_but_explicit_config_recovers(self):
        custom = self.write(self.root / "recovery.json", {"dev": True})
        invalid = [{}, [], {"version": True, "current": "default", "workspaces": {}},
                   {"version": 1, "current": "gone", "workspaces": {
                       "default": {"config_file": str(self.default_config)}}},
                   {"version": 1, "current": "default", "workspaces": {
                       "default": {"config_file": "relative"}}}]
        for value in invalid:
            self.write(self.registry, value)
            with self.subTest(value=value):
                with self.assertRaises(config.ConfigError):
                    config.resolve_settings()
                config.set_config_value("dev", False, config_file=custom)
                self.assertFalse(config.resolve_settings(config_file=custom).dev)
        self.registry.write_text('{"version":1,"version":2}', encoding="utf-8")
        with self.assertRaisesRegex(config.ConfigError, "Duplicate"):
            workspaces.list_workspaces()

    def test_registry_cannot_alias_config_or_symlink_workspace_directory(self):
        alpha = workspaces.create_workspace("alpha")
        raw = json.loads(self.registry.read_text())
        raw["workspaces"]["beta"] = {"config_file": alpha["config_file"]}
        self.write(self.registry, raw)
        with self.assertRaises(config.ConfigError):
            workspaces.list_workspaces()
        raw["workspaces"].pop("beta")
        self.write(self.registry, raw)
        directory = Path(alpha["config_file"]).parent
        moved = directory.with_name("moved")
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(config.ConfigError, "managed config directory"):
            config.resolve_settings(workspace="alpha")

    def test_pinned_metadata_never_selects_storage_and_optional_config_stays_pinned(self):
        custom = self.root / "missing.json"
        workspaces.create_workspace("alpha")
        workspaces.use_workspace("alpha")
        with mock.patch.dict(os.environ, {"RICHI_ACTIVE_CONFIG": str(custom),
                                         "RICHI_ACTIVE_WORKSPACE": "alpha"}):
            selected = config.resolve_settings(config_file=custom, config_required=False)
            self.assertEqual(selected.workspace, "alpha")
            self.assertEqual(selected.database, self.data / "memory.sqlite3")
            self.assertEqual(selected.config_file, custom)
            default = config.resolve_settings(config_file=self.default_config, config_required=False)
            self.assertIsNone(default.workspace)
            self.assertEqual(default.config_file, self.default_config)
            with self.assertRaisesRegex(config.ConfigError, "does not exist"):
                config.resolve_settings(config_file=custom)


if __name__ == "__main__":
    unittest.main()
