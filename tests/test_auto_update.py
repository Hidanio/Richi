"""Background policy uses isolated installation metadata, never user workspaces."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from richi_launcher import auto_update, installation, releases, update_scheduler
from richi_launcher.config import ConfigError


class AutoUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-auto-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "installation"
        self.ctx = {"root": str(self.root), "prefix": str(self.base / "venv"),
                    "original_prefix": str(self.base / "venv")}
        self.current = {"version": "0.1.0", "generation": "seed"}
        self.clock = 100000
        for module, name, kwargs in [
                (installation, "_context", {"return_value": self.ctx}),
                (installation, "status", {"side_effect": self.local}),
                (auto_update, "_now", {"side_effect": lambda: self.clock}),
                (update_scheduler, "install", {"return_value": {"status": "installed"}}),
                (update_scheduler, "remove", {"return_value": {"status": "removed"}}),
                (update_scheduler, "status", {"return_value": {"supported": True, "installed": True}}),
                (releases, "fetch_release", {"return_value": self.release()}),
                (installation, "apply_release", {"side_effect": self.apply})]:
            patcher = patch.object(module, name, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def local(self):
        return {"supported": True, "root": str(self.root), "current": dict(self.current)}

    def release(self, version="0.2.0", checksum="a" * 64):
        return {"version": version, "manifest": {"version": version,
                "wheel": {"sha256": checksum}}}

    def apply(self, release, *, expected_generation, blocking, before_apply, activation_guard, require_auto):
        self.assertFalse(blocking)
        self.assertTrue(require_auto)
        if expected_generation != self.current["generation"]:
            return {"status": "superseded"}
        if not before_apply():
            return {"status": "cancelled"}
        with activation_guard() as allowed:
            if not allowed:
                return {"status": "cancelled"}
            self.current = {"version": release["version"], "generation": "release-" + release["version"],
                            "wheel_sha256": release["manifest"]["wheel"]["sha256"]}
            return dict(self.local(), status="updated")

    def state(self):
        return json.loads((self.root / "auto-state.json").read_text())

    def save(self, state):
        installation._atomic_json(self.root / "auto-state.json", state)

    def due(self):
        self.clock = max(self.clock + 1, self.state()["next_due"])

    def start_tick(self):
        result = []
        def tick():
            try:
                result.append(auto_update.run())
            except BaseException as exc:
                result.append(exc)
        thread = threading.Thread(target=tick)
        thread.start()
        self.addCleanup(lambda: thread.join(5))
        return thread, result

    def test_disabled_status_and_tick_are_read_only(self):
        before = list(self.base.rglob("*"))
        self.assertEqual(auto_update.status()["status"], "disabled")
        self.assertEqual(auto_update.run(), {"status": "disabled"})
        self.assertEqual(list(self.base.rglob("*")), before)
        releases.fetch_release.assert_not_called()

    def test_enable_installs_scheduler_default_interval_and_current_baseline(self):
        result = auto_update.enable()
        self.assertTrue(result["enabled"])
        self.assertFalse(result["auto_install"])
        self.assertEqual(result["interval_seconds"], 21600)
        self.assertEqual(self.state()["approved_generation"], "seed")
        update_scheduler.install.assert_called_once_with(self.ctx)
        self.assertEqual((self.root / "auto.json").stat().st_mode & 0o777, 0o600)
        releases.fetch_release.assert_not_called()

    def test_default_mode_reports_update_without_downloading_or_installing(self):
        auto_update.enable()
        with patch.object(releases, "download_wheel") as download, \
                patch("richi_launcher.config.resolve_settings", side_effect=AssertionError("workspace access")):
            result = auto_update.run()
        self.assertEqual(result["status"], "update_available")
        self.assertEqual(result["action"], "richi update apply --version 0.2.0")
        self.assertEqual(self.state()["last_outcome"]["action"], result["action"])
        self.assertEqual(self.current["generation"], "seed")
        self.assertFalse((self.root / "state.json").exists())
        download.assert_not_called()
        installation.apply_release.assert_not_called()

    def test_default_mode_distinguishes_unverified_same_version_release(self):
        auto_update.enable()
        releases.fetch_release.return_value = self.release("0.1.0")
        result = auto_update.run()
        self.assertEqual(result["status"], "release_available")
        self.assertEqual(result["action"], "richi update apply --version 0.1.0")
        installation.apply_release.assert_not_called()

    def legacy_policy(self):
        path = self.root / "auto.json"
        config = json.loads(path.read_text())
        config["format_version"] = 1
        config.pop("auto_install")
        installation._atomic_json(path, config)
        return path

    def test_legacy_policy_defaults_to_checks_and_migrates_without_installation(self):
        auto_update.enable(auto_install=True)
        path = self.legacy_policy()
        installation._atomic_json(self.root / "auto-pause.json",
                                  {"format_version": 1, "reason": "manual_rollback"})
        before = path.read_bytes()
        snapshot = auto_update.status()
        self.assertFalse(snapshot["auto_install"])
        self.assertEqual(snapshot["status"], "enabled")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(auto_update.run()["status"], "update_available")
        migrated = json.loads(path.read_text())
        self.assertEqual(migrated["format_version"], 2)
        self.assertFalse(migrated["auto_install"])
        # Previous launchers accept only format 1; they must not reinterpret
        # an enabled check-only policy as permission to install after rollback.
        with self.assertRaisesRegex(ConfigError, "Unsupported"):
            auto_update._read(self.ctx, "auto.json", None)
        installation.apply_release.assert_not_called()

    def test_legacy_policy_migrates_even_when_next_check_is_not_due(self):
        auto_update.enable()
        releases.fetch_release.return_value = None
        auto_update.run()
        path = self.legacy_policy()
        self.assertEqual(auto_update.run()["status"], "not_due")
        self.assertEqual(json.loads(path.read_text())["format_version"], 2)

    def test_legacy_rollback_migrates_before_old_launcher_can_read_policy(self):
        auto_update.enable()
        path = self.legacy_policy()
        with installation._locked(self.ctx):
            auto_update.pause_for_rollback(self.ctx, "seed")
        self.assertEqual(json.loads(path.read_text())["format_version"], 2)
        self.assertEqual(auto_update.status()["status"], "enabled")
        self.assertEqual(auto_update.run()["status"], "update_available")

    def test_checking_continues_after_manual_install_and_rollback(self):
        auto_update.enable()
        self.current = {"version": "0.2.0", "generation": "manual-release", "wheel_sha256": "a" * 64}
        self.assertEqual(auto_update.run()["status"], "up_to_date")
        self.due()
        with installation._locked(self.ctx):
            auto_update.pause_for_rollback(self.ctx, "seed")
        self.current = {"version": "0.1.0", "generation": "seed"}
        self.assertEqual(auto_update.run()["status"], "update_available")
        self.assertEqual(auto_update.status()["status"], "enabled")
        installation.apply_release.assert_not_called()

    def test_reenable_and_disable_revoke_installation_consent(self):
        self.assertTrue(auto_update.enable(auto_install=True)["auto_install"])
        self.assertFalse(auto_update.enable()["auto_install"])
        self.assertEqual(auto_update.run()["status"], "update_available")
        auto_update.enable(auto_install=True)
        self.assertFalse(auto_update.disable()["auto_install"])
        config = json.loads((self.root / "auto.json").read_text())
        self.assertFalse(config["auto_install"])
        installation.apply_release.assert_not_called()

    def test_plain_enable_revokes_before_waiting_for_a_real_installation_lock(self):
        auto_update.enable(auto_install=True)
        staged, continue_stage, revoked, enabled = [threading.Event() for unused in range(4)]
        self.addCleanup(continue_stage.set)
        failures = []
        write = auto_update._write
        def record_write(ctx, name, value):
            write(ctx, name, value)
            if name == "auto.json" and value.get("enabled") and value.get("auto_install") is False:
                revoked.set()
        def apply(release, **kwargs):
            with installation._locked(self.ctx):
                self.assertTrue(kwargs["before_apply"]())
                staged.set()
                self.assertTrue(continue_stage.wait(5))
                return self.apply(release, **kwargs)
        def enable_checks():
            try:
                auto_update.enable()
                enabled.set()
            except BaseException as exc:
                failures.append(exc)
        installation.apply_release.side_effect = apply
        with patch.object(auto_update, "_write", side_effect=record_write):
            tick, result = self.start_tick()
            self.assertTrue(staged.wait(5))
            control = threading.Thread(target=enable_checks)
            control.start()
            self.assertTrue(revoked.wait(5))
            self.assertFalse(enabled.wait(0.05))
            continue_stage.set()
            tick.join(5)
            control.join(5)
        self.assertFalse(tick.is_alive() or control.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(result[0]["status"], "cancelled")
        self.assertEqual(self.current["generation"], "seed")
        self.assertFalse(auto_update.status()["auto_install"])

    def test_failed_reenable_keeps_revocation_and_retires_old_attempt(self):
        auto_update.enable(auto_install=True)
        self.pending()
        update_scheduler.install.side_effect = ConfigError("OS registration unavailable")
        with self.assertRaises(ConfigError):
            auto_update.enable()
        self.assertFalse(auto_update.status()["auto_install"])
        self.assertEqual(auto_update.run()["status"], "cancelled")
        self.assertIsNone(self.state()["in_progress"])
        self.due()
        self.assertEqual(auto_update.run()["status"], "update_available")
        installation.apply_release.assert_not_called()

    def test_legacy_interrupted_installation_is_retired_without_resuming_it(self):
        auto_update.enable(auto_install=True)
        self.pending()
        self.legacy_policy()
        self.assertEqual(auto_update.run()["status"], "cancelled")
        self.assertIsNone(self.state()["in_progress"])
        self.assertIsNone(self.state()["quarantine"])
        self.assertEqual(self.state()["approved_generation"], "seed")
        self.due()
        self.assertEqual(auto_update.run()["status"], "update_available")
        installation.apply_release.assert_not_called()

    def test_invalid_installation_consent_never_registers_scheduler(self):
        for value in ("true", 1, None):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                auto_update.enable(auto_install=value)
        update_scheduler.install.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_enable_failure_preserves_previous_policy_and_pause(self):
        auto_update.enable(auto_install=True)
        auto_update.pause_for_rollback(self.ctx, "seed")
        before = {name: (self.root / name).read_bytes() for name in
                  ("auto.json", "auto-state.json", "auto-pause.json")}
        update_scheduler.install.side_effect = ConfigError("OS registration unavailable")
        with self.assertRaises(ConfigError):
            auto_update.enable(3600, auto_install=True)
        self.assertEqual(before, {name: (self.root / name).read_bytes() for name in before})

    def test_interval_validation_never_registers_or_writes(self):
        for value in (True, "3600", 0, 899, 604801):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                auto_update.enable(value)
        update_scheduler.install.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_no_release_records_normal_outcome_then_due_check_is_read_only(self):
        auto_update.enable(3600)
        releases.fetch_release.return_value = None
        result = auto_update.run()
        self.assertEqual(result["status"], "no_release")
        self.assertEqual(result["next_due"], self.clock + 3600)
        before = {path: path.read_bytes() for path in self.root.iterdir() if path.is_file()}
        self.assertEqual(auto_update.run()["status"], "not_due")
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.iterdir() if path.is_file()})
        installation.apply_release.assert_not_called()

    def test_successful_update_tracks_generation_without_changing_workspaces(self):
        workspace = self.base / "workspace"
        workspace.mkdir()
        (workspace / "config.json").write_text('{"dev":true,"development":{"source":"/source"}}')
        (workspace / "memory.sqlite3").write_bytes(b"private synthetic database sentinel")
        before = {path: path.read_bytes() for path in workspace.iterdir()}
        with patch.dict(os.environ, {"RICHI_DB": str(workspace / "memory.sqlite3"),
                                     "RICHI_CONFIG": str(workspace / "config.json"),
                                     "RICHI_WORKSPACE": "absent", "CODEX_THREAD_ID": "unbound"}), \
                patch("richi_launcher.config.resolve_settings", side_effect=AssertionError("workspace access")):
            auto_update.enable(auto_install=True)
            result = auto_update.run()
            self.assertEqual(result["status"], "updated")
            self.assertEqual(self.state()["approved_generation"], self.current["generation"])
            self.due()
            self.assertEqual(auto_update.run()["status"], "up_to_date")
        self.assertEqual(before, {path: path.read_bytes() for path in workspace.iterdir()})
        self.assertEqual(installation.apply_release.call_count, 1)

    def test_feed_errors_back_off_exponentially_and_reset_after_success(self):
        auto_update.enable(3600)
        releases.fetch_release.side_effect = ConfigError("offline")
        for expected in (900, 1800, 3600, 3600):
            result = auto_update.run()
            self.assertEqual(result["status"], "check_failed")
            self.assertEqual(result["next_due"] - self.clock, expected)
            self.due()
        releases.fetch_release.side_effect = None
        releases.fetch_release.return_value = None
        auto_update.run()
        self.assertEqual(self.state()["failure_count"], 0)

    def test_download_network_failure_retries_without_quarantine(self):
        auto_update.enable(auto_install=True)
        installation.apply_release.side_effect = releases.TransientReleaseError("connection reset")
        result = auto_update.run()
        self.assertEqual(result["status"], "download_failed")
        self.assertEqual(result["next_due"] - self.clock, 900)
        self.assertIsNone(self.state()["quarantine"])
        self.due()
        installation.apply_release.side_effect = self.apply
        self.assertEqual(auto_update.run()["status"], "updated")

    def test_bad_candidate_quarantined_until_newer_release_or_explicit_enable(self):
        auto_update.enable(auto_install=True)
        installation.apply_release.side_effect = ConfigError("self-check failed")
        self.assertEqual(auto_update.run()["status"], "candidate_failed")
        self.due()
        # Replacing bytes within the same version does not silently bypass the
        # quarantine. An explicit enable can request retrying that release.
        releases.fetch_release.return_value = self.release(checksum="b" * 64)
        self.assertEqual(auto_update.run()["status"], "quarantined")
        self.assertEqual(installation.apply_release.call_count, 1)
        auto_update.enable(auto_install=True)
        installation.apply_release.side_effect = self.apply
        self.assertEqual(auto_update.run()["status"], "updated")
        self.due()
        releases.fetch_release.return_value = self.release("0.3.0")
        installation.apply_release.side_effect = ConfigError("self-check failed")
        self.assertEqual(auto_update.run()["status"], "candidate_failed")
        self.due()
        releases.fetch_release.return_value = self.release("0.4.0")
        installation.apply_release.side_effect = self.apply
        self.assertEqual(auto_update.run()["status"], "updated")
        self.assertIsNone(self.state()["quarantine"])

    def test_older_published_version_does_not_downgrade(self):
        self.current = {"version": "0.9.0", "generation": "release-0.9.0"}
        auto_update.enable(auto_install=True)
        self.assertEqual(auto_update.run()["status"], "older_release")
        installation.apply_release.assert_not_called()

    def test_disable_persists_even_when_service_removal_fails(self):
        auto_update.enable(auto_install=True)
        update_scheduler.remove.side_effect = ConfigError("OS removal unavailable")
        with self.assertRaises(ConfigError):
            auto_update.disable()
        self.assertEqual(auto_update.run()["status"], "disabled")
        self.assertFalse(auto_update.status()["enabled"])
        releases.fetch_release.assert_not_called()

    def test_disable_during_fetch_prevents_installation_and_overlap_is_busy(self):
        auto_update.enable(auto_install=True)
        entered, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        def fetch():
            entered.set()
            self.assertTrue(resume.wait(5))
            return self.release()
        releases.fetch_release.side_effect = fetch
        thread, result = self.start_tick()
        self.assertTrue(entered.wait(5))
        before = (self.root / "auto-state.json").read_bytes()
        self.assertEqual(auto_update.run()["status"], "busy")
        self.assertEqual((self.root / "auto-state.json").read_bytes(), before)
        self.assertFalse(auto_update.disable()["enabled"])
        resume.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0]["status"], "cancelled")
        installation.apply_release.assert_not_called()

    def test_reenable_during_fetch_owns_new_policy_and_old_tick_cannot_overwrite(self):
        auto_update.enable(auto_install=True)
        entered, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        def fetch():
            entered.set()
            self.assertTrue(resume.wait(5))
            return self.release()
        releases.fetch_release.side_effect = fetch
        thread, result = self.start_tick()
        self.assertTrue(entered.wait(5))
        auto_update.enable(1800)
        before = self.state()
        resume.set()
        thread.join(5)
        self.assertEqual(result[0]["status"], "cancelled")
        self.assertEqual(self.state(), before)

    def test_disable_after_staging_is_atomic_with_activation(self):
        auto_update.enable(auto_install=True)
        staged, activate = threading.Event(), threading.Event()
        self.addCleanup(activate.set)
        def apply(release, **kwargs):
            self.assertTrue(kwargs["before_apply"]())
            staged.set()
            self.assertTrue(activate.wait(5))
            return self.apply(release, **kwargs)
        installation.apply_release.side_effect = apply
        thread, result = self.start_tick()
        self.assertTrue(staged.wait(5))
        auto_update.disable()
        activate.set()
        thread.join(5)
        self.assertEqual(result[0]["status"], "cancelled")
        self.assertEqual(self.current["generation"], "seed")

    def test_enable_waits_until_concurrent_disable_finishes_removing_scheduler(self):
        auto_update.enable(auto_install=True)
        removing, resume, enabling, enabled = [threading.Event() for unused in range(4)]
        self.addCleanup(resume.set)
        order, failures = [], []
        def remove(ctx):
            removing.set()
            if not resume.wait(5):
                raise AssertionError("remove barrier expired")
            order.append("removed")
        def install(ctx):
            order.append("installed")
        def disable():
            try:
                auto_update.disable()
            except BaseException as exc:
                failures.append(exc)
        def enable():
            enabling.set()
            try:
                auto_update.enable(auto_install=True)
                enabled.set()
            except BaseException as exc:
                failures.append(exc)
        update_scheduler.remove.side_effect = remove
        update_scheduler.install.side_effect = install
        stopping = threading.Thread(target=disable)
        starting = threading.Thread(target=enable)
        stopping.start()
        self.assertTrue(removing.wait(5))
        starting.start()
        self.assertTrue(enabling.wait(5))
        self.assertFalse(enabled.wait(0.05))
        resume.set()
        stopping.join(5)
        starting.join(5)
        self.assertFalse(stopping.is_alive() or starting.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(order, ["removed", "installed"])
        self.assertTrue(auto_update.status()["enabled"])

    def test_rollback_pause_and_external_generation_change_require_reenable(self):
        auto_update.enable(auto_install=True)
        auto_update.pause_for_rollback(self.ctx, "seed")
        self.assertEqual(auto_update.run()["status"], "paused")
        releases.fetch_release.assert_not_called()
        auto_update.enable(auto_install=True)
        self.current = {"version": "0.3.0", "generation": "manual-release"}
        self.assertEqual(auto_update.run()["status"], "paused")
        self.assertEqual(auto_update.status()["pause"]["reason"], "installation_changed")
        releases.fetch_release.assert_not_called()
        auto_update.enable(auto_install=True)
        self.assertEqual(auto_update.run()["status"], "older_release")

    def pending(self, phase="applying"):
        state = self.state()
        state["in_progress"] = {"attempt_id": "crashed", "phase": phase, "started_at": self.clock,
                "expected_generation": "seed", "version": "0.2.0", "wheel_sha256": "a" * 64,
                "policy_id": json.loads((self.root / "auto.json").read_text())["policy_id"]}
        self.save(state)

    def test_crash_after_verified_activation_recovers_without_reinstalling(self):
        auto_update.enable(auto_install=True)
        self.pending()
        self.current = {"version": "0.2.0", "generation": "release-0.2.0", "wheel_sha256": "a" * 64}
        state = self.state()
        # The activation guard approved this generation before interruption;
        # only the final outcome journal write was lost.
        state["approved_generation"] = self.current["generation"]
        self.save(state)
        self.assertEqual(auto_update.run()["status"], "recovered_updated")
        self.assertEqual(self.state()["approved_generation"], "release-0.2.0")
        releases.fetch_release.assert_not_called()
        installation.apply_release.assert_not_called()

    def test_manual_same_wheel_after_interruption_requires_explicit_reenable(self):
        auto_update.enable(auto_install=True)
        self.pending()
        self.current = {"version": "0.2.0", "generation": "manual-same-wheel", "wheel_sha256": "a" * 64}
        self.assertEqual(auto_update.run()["status"], "superseded")
        self.assertEqual(auto_update.status()["status"], "paused")
        self.assertEqual(self.state()["approved_generation"], "seed")
        self.assertEqual(auto_update.run()["status"], "paused")
        releases.fetch_release.assert_not_called()
        installation.apply_release.assert_not_called()

    def test_crash_before_activation_quarantines_incomplete_candidate(self):
        auto_update.enable(auto_install=True)
        self.pending()
        self.assertEqual(auto_update.run()["status"], "interrupted_candidate")
        self.due()
        self.assertEqual(auto_update.run()["status"], "quarantined")
        installation.apply_release.assert_not_called()

    def test_crash_during_feed_check_uses_retry_schedule(self):
        auto_update.enable(auto_install=True)
        self.pending("checking")
        result = auto_update.run()
        self.assertEqual(result["status"], "interrupted_check")
        self.assertEqual(result["next_due"] - self.clock, 900)
        self.assertIsNone(self.state()["quarantine"])

    def test_interrupted_attempt_does_not_override_manual_generation_change(self):
        auto_update.enable(auto_install=True)
        self.pending()
        self.current = {"version": "0.1.0", "generation": "manually-restored", "wheel_sha256": "b" * 64}
        self.assertEqual(auto_update.run()["status"], "superseded")
        self.assertEqual(auto_update.status()["status"], "paused")
        installation.apply_release.assert_not_called()

    def test_malformed_attempt_journal_fails_closed(self):
        auto_update.enable(auto_install=True)
        state = self.state()
        state["in_progress"] = {"phase": "applying"}
        self.save(state)
        with self.assertRaisesRegex(ConfigError, "journal"):
            auto_update.run()
        releases.fetch_release.assert_not_called()

    def test_recovery_cannot_overwrite_policy_after_explicit_reenable(self):
        auto_update.enable(auto_install=True)
        self.pending()
        stale = self.state()["in_progress"]
        self.current = {"version": "0.2.0", "generation": "release-0.2.0", "wheel_sha256": "a" * 64}
        auto_update.enable(3600)
        before = self.state()
        self.assertEqual(auto_update._recovery(self.ctx, stale)["status"], "cancelled")
        self.assertEqual(self.state(), before)

    def test_recovery_rechecks_manual_rollback_pause_before_approving_generation(self):
        auto_update.enable(auto_install=True)
        self.pending()
        stale = self.state()["in_progress"]
        self.current = {"version": "0.2.0", "generation": "release-0.2.0", "wheel_sha256": "a" * 64}
        auto_update.pause_for_rollback(self.ctx, "seed")
        before = self.state()
        self.assertEqual(auto_update._recovery(self.ctx, stale)["status"], "cancelled")
        self.assertEqual(self.state(), before)

    def test_history_and_error_messages_are_bounded(self):
        auto_update.enable(900)
        releases.fetch_release.side_effect = ConfigError("x" * 2000)
        for unused in range(25):
            auto_update.run()
            self.due()
        state = self.state()
        self.assertEqual(len(state["history"]), 20)
        self.assertLessEqual(len(state["last_outcome"]["error"]), 500)
        self.assertLess((self.root / "auto-state.json").stat().st_size, 20000)

    def test_corrupt_or_symlinked_policy_fails_closed(self):
        self.root.mkdir()
        target = self.base / "foreign.json"
        target.write_text('{"format_version":1,"enabled":true}')
        (self.root / "auto.json").symlink_to(target)
        with self.assertRaises(ConfigError):
            auto_update.run()
        self.assertEqual(target.read_text(), '{"format_version":1,"enabled":true}')
        releases.fetch_release.assert_not_called()


class TransientReleaseTests(unittest.TestCase):
    def test_http_and_transport_failures_are_classified(self):
        url = "https://api.github.com/repos/Hidanio/Richi/releases/latest"
        for error, expected in ((URLError("offline"), releases.TransientReleaseError),
                (HTTPError(url, 429, "rate limit", {}, None), releases.TransientReleaseError),
                (HTTPError(url, 503, "unavailable", {}, None), releases.TransientReleaseError),
                (HTTPError(url, 403, "forbidden", {}, None), ConfigError)):
            with self.subTest(error=error), patch.object(releases, "build_opener") as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaises(expected) as caught:
                    releases._stream_response(url, 1024, lambda block: True)
                if expected is ConfigError:
                    self.assertNotIsInstance(caught.exception, releases.TransientReleaseError)


if __name__ == "__main__":
    unittest.main()
