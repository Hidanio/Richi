"""Release transactions and process leases use only disposable environments."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import patch

import richi_bootstrap as bootstrap
from richi_launcher import installation
from richi_launcher.config import ConfigError


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="richi-update-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.prefix = self.base / "original"
        self.site = self.prefix / "lib" / "python3.9" / "site-packages"
        self.site.mkdir(parents=True)
        self._package(self.site, "0.1.0")
        (self.site / "richi_bootstrap.py").write_text("# Permanent bootstrap lease anchor\n")
        (self.prefix / "bin").mkdir()
        (self.prefix / "bin" / "python").write_text("fixture interpreter")
        (self.prefix / "pyvenv.cfg").write_text("include-system-site-packages = false\n")
        metadata = self.site / "richi-0.1.0.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: richi\nVersion: 0.1.0\n")
        self.ctx = {"prefix": str(self.prefix), "original_prefix": str(self.prefix),
                    "site": str(self.site), "bootstrap": str(self.site / "richi_bootstrap.py"),
                    "root": str(self.base / "data" / "installations" / "test")}
        self.root = Path(self.ctx["root"])
        self.patches = [patch.object(installation, "_context", return_value=self.ctx),
                        patch.object(installation, "_stage", side_effect=self._stage),
                        patch("richi_launcher.releases.download_wheel", side_effect=self._download),
                        patch("richi_launcher.releases.verify_wheel")]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _package(self, site, version):
        for name in ("richi", "richi_launcher"):
            package = site / name
            package.mkdir(parents=True)
            (package / "__init__.py").write_text('__version__ = "' + version + '"\n')
        (site / "richi" / "memory.py").write_text("VERSION = 2\n")
        (site / "richi_launcher" / "cli.py").write_text("def main(argv=None): return 0\n")

    def _download(self, release, directory):
        wheel = directory / release["manifest"]["wheel"]["filename"]
        wheel.write_bytes(b"wheel fixture")
        return wheel

    def _stage(self, ctx, generation, wheel, manifest):
        prefix = self.root / "versions" / generation
        prefix.mkdir(parents=True)
        installation._atomic_json(prefix / bootstrap.OWNER_FILE, installation._owner(ctx, generation))
        site = prefix / "lib" / "python3.9" / "site-packages"
        self._package(site, manifest["version"])
        return {"kind": "release", "prefix": str(prefix), "site": str(site),
                "version": manifest["version"], "wheel_sha256": manifest["wheel"]["sha256"]}

    def release(self, version):
        return {"version": version, "manifest": {"format_version": 1, "version": version,
                "updater_protocol": 1, "python_min": [3, 9], "database_schemas": [1, 2], "map_schema": 2,
                "wheel": {"filename": "richi-" + version + "-py3-none-any.whl", "sha256": version * 10, "size": 13}}}

    def state(self):
        return bootstrap.read_state(self.ctx)

    def apply(self, version):
        # The real public bootstrap imports the current installation. Fixtures
        # retain the seed module for convenience, so bypass its missing package
        # check after its intentionally tested removal.
        with patch.object(installation, "_validate_layout"):
            return installation.apply_release(self.release(version))

    def test_status_is_read_only_and_independent_of_workspace_overrides(self):
        before = set(self.base.rglob("*"))
        with patch.dict(os.environ, {"RICHI_CONFIG": "/does/not/exist", "RICHI_DATA_DIR": "/invalid",
                                     "RICHI_DB": "/missing.sqlite3", "RICHI_WORKSPACE": "missing"}):
            result = installation.status()
        self.assertTrue(result["supported"], result)
        self.assertEqual(result["current"]["version"], "0.1.0")
        self.assertFalse(result["managed"])
        self.assertEqual(set(self.base.rglob("*")), before)

    def test_refuses_shared_or_editable_environment_without_creating_state(self):
        extra = self.site / "unrelated-1.dist-info"
        extra.mkdir()
        (extra / "METADATA").write_text("Name: unrelated\nVersion: 1\n")
        self.assertFalse(installation.status()["supported"])
        with self.assertRaisesRegex(ConfigError, "dedicated"):
            installation.apply_release(self.release("0.2.0"))
        self.assertFalse(self.root.exists())
        (extra / "METADATA").unlink()
        extra.rmdir()
        (self.site / "richi-0.1.0.dist-info" / "direct_url.json").write_text(json.dumps({"dir_info": {"editable": True}}))
        with self.assertRaisesRegex(ConfigError, "editable"):
            installation.apply_release(self.release("0.2.0"))

    def test_background_compare_and_swap_and_busy_never_stage(self):
        release = self.release("0.2.0")
        with patch.object(installation, "_stage") as stage:
            result = installation.apply_release(release, expected_generation="not-current")
            self.assertEqual(result["status"], "superseded")
            with installation._locked(self.ctx):
                result = installation.apply_release(release, blocking=False)
            self.assertEqual(result["status"], "busy")
            result = installation.apply_release(release, before_apply=lambda: False)
            self.assertEqual(result["status"], "cancelled")
            stage.assert_not_called()
        self.assertFalse((self.root / "state.json").exists())

    def test_cancel_staged_background_candidate_preserves_current(self):
        first = self.apply("0.2.0")
        before = (self.root / "state.json").read_bytes()
        @contextmanager
        def denied():
            yield False
        result = installation.apply_release(self.release("0.3.0"),
                  expected_generation=first["current"]["generation"], activation_guard=denied)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual((self.root / "state.json").read_bytes(), before)
        self.assertEqual(list((self.root / "versions").iterdir()), [Path(first["current"]["prefix"])])

    def test_background_release_requires_auto_capability_before_activation(self):
        with patch.object(installation, "_run", side_effect=ConfigError("Automatic release self-check failed")):
            with self.assertRaisesRegex(ConfigError, "Automatic release self-check"):
                installation.apply_release(self.release("0.2.0"), require_auto=True)
        self.assertFalse((self.root / "state.json").exists())
        self.assertEqual(list((self.root / "versions").iterdir()), [])

    def test_activation_stays_inside_guard_and_rollback_pauses_first(self):
        guarded = []
        @contextmanager
        def guard():
            guarded.append(True)
            try:
                yield True
            finally:
                guarded.pop()
        activate = installation._activate
        def checked_activate(ctx, state, action):
            self.assertEqual(guarded, [True])
            return activate(ctx, state, action)
        with patch.object(installation, "_activate", side_effect=checked_activate):
            installation.apply_release(self.release("0.2.0"), activation_guard=guard)
        before = self.state()["current"]
        def paused(ctx, generation):
            self.assertEqual(self.state()["current"], before)
            self.assertEqual(generation, "seed")
        with patch("richi_launcher.auto_update.pause_for_rollback", side_effect=paused) as pause:
            self.assertEqual(installation.rollback()["current"]["generation"], "seed")
            pause.assert_called_once()

    def test_same_version_seed_can_be_replaced_then_verified_version_is_noop(self):
        first = self.apply("0.1.0")
        self.assertEqual(first["status"], "updated")
        self.assertEqual(first["previous"]["generation"], "seed")
        second = self.apply("0.1.0")
        self.assertEqual(second["status"], "already_current")
        self.assertEqual(second["current"], first["current"])

    def test_failed_selfcheck_preserves_current_and_removes_candidate(self):
        first = self.apply("0.2.0")
        before = (self.root / "state.json").read_bytes()
        stage = self._stage
        def fail(*args):
            stage(*args)
            raise ConfigError("Release self-check disagrees with manifest")
        with patch.object(installation, "_stage", side_effect=fail):
            with self.assertRaisesRegex(ConfigError, "self-check"):
                self.apply("0.3.0")
        self.assertEqual((self.root / "state.json").read_bytes(), before)
        self.assertEqual(list((self.root / "versions").iterdir()), [Path(first["current"]["prefix"])])

    def test_incompatible_protocol_or_schema_never_creates_state(self):
        for key, value in (("updater_protocol", 2), ("database_schemas", [2, 3]), ("python_min", [99, 0])):
            with self.subTest(key=key):
                release = self.release("0.2.0")
                release["manifest"][key] = value
                with self.assertRaises(ConfigError):
                    installation.apply_release(release)
                self.assertFalse(self.root.exists())

    def test_downgrade_refused_and_rollback_swaps_existing_generations(self):
        first = self.apply("0.2.0")
        with self.assertRaisesRegex(ConfigError, "downgrade"):
            self.apply("0.1.0")
        result = installation.rollback()
        self.assertEqual(result["current"]["generation"], "seed")
        self.assertEqual(result["previous"], first["current"])
        again = installation.rollback()
        self.assertEqual(again["current"], first["current"])

    def test_seed_retired_only_after_lease_ends_and_bootstrap_is_preserved(self):
        _, generation, descriptor = bootstrap.select(self.ctx)
        self.assertEqual(generation, "seed")
        self.assertTrue(os.get_inheritable(descriptor))
        try:
            self.apply("0.2.0")
            result = self.apply("0.3.0")
            self.assertIn("seed", result["cleanup"]["deferred"])
            self.assertTrue((self.site / "richi" / "memory.py").is_file())
            self.assertTrue((self.site / "richi_launcher" / "cli.py").is_file())
        finally:
            os.close(descriptor)
        result = installation.cleanup()
        self.assertIn("seed", result["cleanup"]["removed"])
        self.assertFalse((self.site / "richi" / "memory.py").exists())
        self.assertFalse((self.site / "richi_launcher").exists())
        self.assertTrue((self.site / "richi" / "__main__.py").is_file())
        self.assertTrue((self.site / "richi_bootstrap.py").is_file())
        self.assertTrue((self.site / "richi-0.1.0.dist-info").is_dir())
        self.assertTrue((self.prefix / "bin" / "python").is_file())

    def test_leased_release_is_deferred_then_removed_without_touching_other_files(self):
        first = self.apply("0.2.0")
        _, generation, descriptor = bootstrap.select(self.ctx)
        self.assertEqual(generation, first["current"]["generation"])
        outside = self.base / "unrelated"
        outside.mkdir()
        (outside / "keep").write_text("do not touch")
        (self.root / "versions" / "unknown").symlink_to(outside)
        try:
            self.apply("0.3.0")
            third = self.apply("0.4.0")
            self.assertIn(generation, third["cleanup"]["deferred"])
            self.assertTrue(Path(first["current"]["prefix"]).is_dir())
        finally:
            os.close(descriptor)
        with patch.object(installation, "_validate_layout"):
            result = installation.cleanup()
        self.assertIn(generation, result["cleanup"]["removed"])
        self.assertFalse(Path(first["current"]["prefix"]).exists())
        self.assertEqual((outside / "keep").read_text(), "do not touch")
        self.assertEqual(len(result["retired"]), 0)
        self.assertEqual(len([p for p in (self.root / "versions").iterdir() if not p.is_symlink()]), 2)

    def test_inherited_lease_survives_parent_close(self):
        first = self.apply("0.2.0")
        _, generation, descriptor = bootstrap.select(self.ctx)
        child = subprocess.Popen([sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.readline()"],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, pass_fds=(descriptor,))
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            os.close(descriptor)
            descriptor = None
            self.apply("0.3.0")
            result = self.apply("0.4.0")
            self.assertIn(generation, result["cleanup"]["deferred"])
            self.assertTrue(Path(first["current"]["prefix"]).is_dir())
        finally:
            if descriptor is not None:
                os.close(descriptor)
            child.communicate("done\n", timeout=10)
        with patch.object(installation, "_validate_layout"):
            self.assertIn(generation, installation.cleanup()["cleanup"]["removed"])

    def test_tampered_generation_paths_and_owner_markers_are_never_deleted(self):
        first = self.apply("0.2.0")
        self.apply("0.3.0")
        marker = Path(first["current"]["prefix"]) / bootstrap.OWNER_FILE
        marker.write_text("{}")
        result = self.apply("0.4.0")
        self.assertTrue(result["cleanup"]["failures"])
        self.assertTrue(Path(first["current"]["prefix"]).is_dir())
        state = self.state()
        state["generations"][state["previous"]]["prefix"] = str(self.base)
        installation._atomic_json(self.root / "state.json", state)
        with self.assertRaisesRegex(bootstrap.InstallationError, "ownership"):
            bootstrap.read_state(self.ctx)

    def test_cleanup_recovers_owned_interrupted_staging_only(self):
        self.apply("0.2.0")
        orphan = "v0.3.0-interrupted"
        self._stage(self.ctx, orphan, None, self.release("0.3.0")["manifest"])
        unknown = self.root / "versions" / "not-owned"
        unknown.mkdir()
        result = installation.cleanup()
        self.assertIn(orphan, result["cleanup"]["removed"])
        self.assertTrue(unknown.is_dir())

    def test_select_state_and_first_run_make_no_files(self):
        before = set(self.base.rglob("*"))
        _, _, descriptor = bootstrap.select(self.ctx)
        os.close(descriptor)
        self.assertEqual(set(self.base.rglob("*")), before)
        self.apply("0.2.0")
        before = set(self.base.rglob("*"))
        record, generation, descriptor = bootstrap.select(self.ctx)
        os.close(descriptor)
        self.assertEqual(record["version"], "0.2.0")
        self.assertEqual(generation, self.state()["current"])
        self.assertEqual(set(self.base.rglob("*")), before)

    def test_bootstrap_rollback_does_not_import_corrupted_current_launcher(self):
        first = self.apply("0.2.0")
        current_site = Path(first["current"]["site"])
        (current_site / "richi_launcher" / "cli.py").unlink()
        with self.assertRaisesRegex(bootstrap.InstallationError, "missing"):
            bootstrap.select(self.ctx)
        with patch.object(bootstrap, "context", return_value=self.ctx), \
                patch.object(bootstrap.os, "execve", side_effect=RuntimeError("exec captured")) as execute, \
                patch.dict(os.environ):
            try:
                with self.assertRaisesRegex(RuntimeError, "exec captured"):
                    bootstrap.main(["update", "rollback"])
                self.assertEqual(execute.call_args[0][0], str(self.prefix / "bin" / "python"))
                self.assertEqual(execute.call_args[0][1][-2:], ["update", "rollback"])
                self.assertEqual(os.environ[bootstrap.GENERATION_ENV], "seed")
            finally:
                for descriptor in bootstrap.lease_fds():
                    os.close(descriptor)

    def test_bootstrap_recovery_accepts_chat_prefix_without_reading_registry(self):
        self.assertTrue(bootstrap._rollback_requested(["update", "rollback"]))
        self.assertTrue(bootstrap._rollback_requested(["--chat", "missing", "update", "rollback"]))
        self.assertTrue(bootstrap._rollback_requested(["--chat=missing", "update", "rollback"]))
        self.assertFalse(bootstrap._rollback_requested(["--workspace", "other", "update", "rollback"]))
        self.assertFalse(bootstrap._rollback_requested(["update", "apply"]))

    def test_interrupted_download_does_not_change_selected_generation(self):
        first = self.apply("0.2.0")
        before = (self.root / "state.json").read_bytes()
        with patch("richi_launcher.releases.download_wheel", side_effect=ConfigError("connection interrupted")):
            with self.assertRaisesRegex(ConfigError, "interrupted"):
                self.apply("0.3.0")
        self.assertEqual((self.root / "state.json").read_bytes(), before)
        self.assertEqual(self.state()["current"], first["current"]["generation"])

    def test_staging_does_not_block_new_bootstrap_invocations(self):
        self.apply("0.2.0")
        staging = threading.Event()
        release = threading.Event()
        def paused_stage(*args):
            staging.set()
            if not release.wait(10):
                raise ConfigError("Test staging timed out")
            return self._stage(*args)
        code = ("import json,sys; sys.path.insert(0,sys.argv[1]); import richi_bootstrap as b; "
                "record,generation,fd=b.select(json.loads(sys.argv[2])); print(record['version'])")
        with ThreadPoolExecutor(max_workers=1) as executor, patch.object(installation, "_stage", side_effect=paused_stage):
            future = executor.submit(self.apply, "0.3.0")
            try:
                self.assertTrue(staging.wait(5))
                result = subprocess.run([sys.executable, "-I", "-B", "-c", code,
                                         str(Path(bootstrap.__file__).parent), json.dumps(self.ctx)],
                                        capture_output=True, text=True, timeout=3)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "0.2.0")
            finally:
                release.set()
            self.assertEqual(future.result(timeout=5)["status"], "updated")

    def test_post_replace_fsync_failure_never_deletes_activated_candidate(self):
        self.apply("0.2.0")
        write = installation._atomic_json
        def fail_after_replace(path, data):
            write(path, data)
            if path.name == "state.json":
                raise OSError("directory fsync failed after replace")
        with patch.object(installation, "_atomic_json", side_effect=fail_after_replace):
            with self.assertRaisesRegex(ConfigError, "fsync"):
                self.apply("0.3.0")
        state = self.state()
        record = state["generations"][state["current"]]
        self.assertEqual(record["version"], "0.3.0")
        self.assertTrue((Path(record["site"]) / "richi" / "memory.py").is_file())
        selected, _, descriptor = bootstrap.select(self.ctx)
        os.close(descriptor)
        self.assertEqual(selected["version"], "0.3.0")

    def test_pre_replace_failure_removes_unactivated_candidate(self):
        first = self.apply("0.2.0")
        before = (self.root / "state.json").read_bytes()
        write = installation._atomic_json
        def fail_before_replace(path, data):
            if path.name == "state.json":
                raise OSError("state write failed before replace")
            write(path, data)
        with patch.object(installation, "_atomic_json", side_effect=fail_before_replace):
            with self.assertRaisesRegex(ConfigError, "state write"):
                self.apply("0.3.0")
        self.assertEqual((self.root / "state.json").read_bytes(), before)
        self.assertEqual(list((self.root / "versions").iterdir()), [Path(first["current"]["prefix"])])


if __name__ == "__main__":
    unittest.main()
