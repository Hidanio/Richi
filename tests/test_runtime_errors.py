"""Unavailable development checkouts must not break stable config recovery."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from richi_launcher import cli, config, runtime


class RuntimeErrorTests(unittest.TestCase):
    def settings(self):
        return config.Settings(database=Path("/synthetic/memory.sqlite3"),
                               data_dir=Path("/synthetic"), port=8765,
                               config_file=Path("/synthetic/config.json"), dev=True,
                               development_source=Path("/synthetic/checkout"))

    def test_public_runtime_inspection_wraps_filesystem_errors(self):
        for error in (PermissionError("checkout denied"), RuntimeError("Symlink loop")):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(Path, "resolve", side_effect=error):
                    with self.assertRaises(config.ConfigError):
                        runtime.describe_runtime(Path("/synthetic/richi"), mode="dev")
                with mock.patch.object(Path, "is_file", side_effect=error):
                    with self.assertRaises(config.ConfigError):
                        runtime.installed_runtime()
                    with self.assertRaises(config.ConfigError):
                        runtime.resolve_runtime(self.settings())

    def test_version_and_release_hash_inspection_wrap_symlink_errors(self):
        with mock.patch.object(Path, "read_bytes", side_effect=RuntimeError("Symlink loop")):
            with self.assertRaises(config.ConfigError):
                runtime.describe_runtime(Path("/synthetic/richi"), mode="dev")
        with mock.patch.object(runtime, "_version", return_value="1.0"), \
                mock.patch.object(Path, "rglob", side_effect=RuntimeError("Symlink loop")):
            with self.assertRaises(config.ConfigError):
                runtime.describe_runtime(Path("/synthetic/richi"))

    def test_config_show_reports_unavailable_and_false_recovers(self):
        fixed = runtime.Runtime("release", Path("/synthetic/installed/richi"), "1.0", "fixed")
        for error in (PermissionError("checkout denied"), RuntimeError("Symlink loop")):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                selected = root / "config.json"
                source = root / "unavailable-checkout"
                selected.write_text(json.dumps({"database": "untouched.sqlite3", "dev": True,
                                                "development": {"source": str(source)}}), encoding="utf-8")
                with mock.patch.dict(os.environ, {}, clear=True), \
                        mock.patch.object(Path, "is_file", side_effect=error), \
                        mock.patch.object(runtime, "installed_runtime", return_value=fixed), \
                        mock.patch.object(cli, "installed_runtime", return_value=fixed):
                    out, err = io.StringIO(), io.StringIO()
                    with redirect_stdout(out), redirect_stderr(err):
                        status = cli.main(["--config", str(selected), "config", "show"])
                    shown = json.loads(out.getvalue())
                    self.assertEqual(status, 0, err.getvalue())
                    self.assertTrue(shown["dev"])
                    self.assertFalse(shown["runtime"]["available"])
                    self.assertIn(str(error), shown["runtime"]["error"])
                    out, err = io.StringIO(), io.StringIO()
                    with redirect_stdout(out), redirect_stderr(err):
                        status = cli.main(["--config", str(selected), "config", "set", "dev", "false"])
                    recovered = json.loads(out.getvalue())
                    self.assertEqual(status, 0, err.getvalue())
                    self.assertFalse(recovered["dev"])
                    self.assertTrue(recovered["runtime"]["available"])
                    self.assertEqual(recovered["runtime"]["mode"], "release")
                written = json.loads(selected.read_text())
                self.assertIs(written["dev"], False)
                self.assertEqual(written["development"]["source"], str(source))
                self.assertFalse((root / "untouched.sqlite3").exists())
                self.assertFalse(source.exists())


if __name__ == "__main__":
    unittest.main()
