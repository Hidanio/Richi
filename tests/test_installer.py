"""First-install failures must not claim or overwrite existing environments."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from richi_launcher import installer
from richi_launcher.config import ConfigError


class FirstInstallFailureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-installer-test-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        environment = patch.dict(os.environ, {"HOME": str(self.home)}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.prefix = self.home / "venv"
        self.args = argparse.Namespace(yes=True, dry_run=False, prefix=self.prefix,
                                       version=None, wheel=None, manifest=None,
                                       no_path=True, replace_skill=False)

    def run_install(self):
        with contextlib.redirect_stderr(io.StringIO()):
            return installer.install(self.args)

    def test_unowned_environment_is_preserved(self):
        self.prefix.mkdir()
        (self.prefix / "important.txt").write_text("mine")
        with self.assertRaisesRegex(ConfigError, "not created by this installer"):
            self.run_install()
        self.assertEqual(list(self.prefix.iterdir()), [self.prefix / "important.txt"])

    def test_symlink_prefix_is_rejected_without_following_it(self):
        original = self.home / "existing"
        original.mkdir()
        self.prefix.symlink_to(original)
        with self.assertRaisesRegex(ConfigError, "symlink"):
            self.run_install()
        self.assertEqual(list(original.iterdir()), [])

    def test_unpublished_or_failed_release_never_creates_environment(self):
        for failure in (None, ConfigError("TLS failed")):
            with patch.object(installer.releases, "fetch_release", return_value=None,
                              side_effect=failure):
                with self.assertRaises(ConfigError):
                    self.run_install()
            self.assertFalse(self.prefix.exists())

    def test_invalid_offline_pair_fails_before_confirming_or_writing(self):
        self.args.wheel = self.home / "missing.whl"
        with patch.object(installer, "_confirm") as confirm:
            with self.assertRaisesRegex(ConfigError, "--wheel with --manifest"):
                self.run_install()
            confirm.assert_not_called()
        self.assertFalse(self.prefix.exists())

    def test_interrupted_owned_environment_is_kept_as_a_backup(self):
        self.prefix.mkdir()
        marker = {"format_version": 1, "prefix": str(self.prefix), "phase": "creating"}
        (self.prefix / installer.MARKER).write_text(json.dumps(marker))
        (self.prefix / "partial.txt").write_text("earlier attempt")
        manifest = {"version": "0.1.0", "wheel": {"sha256": "a" * 64}}
        with patch.object(installer.releases, "fetch_release", return_value={"manifest": manifest}), \
                patch.object(installer.releases, "download_wheel", return_value=self.home / "wheel"), \
                patch("richi_launcher.installation._compatible"), \
                patch.object(installer, "_run", side_effect=ConfigError("simulated venv failure")):
            with self.assertRaisesRegex(ConfigError, "simulated"):
                self.run_install()
        backup, = self.home.glob("venv.interrupted-*")
        self.assertEqual((backup / "partial.txt").read_text(), "earlier attempt")
        self.assertEqual(json.loads((self.prefix / installer.MARKER).read_text())["phase"], "creating")


if __name__ == "__main__":
    unittest.main()
