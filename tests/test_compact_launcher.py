"""Explicit stdout formatting must not alter launcher selection or recovery."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from richi import memory
import richi_bootstrap
from richi_launcher import cli
from richi_launcher.runtime import bootstrap_command

from cli_environment import cli_environment


class CompactLauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-compact-launcher-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.environment = mock.patch.dict(os.environ, cli_environment(self.root), clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def invoke(self, arguments, expected=0):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = cli.main(arguments)
        self.assertEqual(status, expected, stderr.getvalue())
        return stdout.getvalue(), stderr.getvalue()

    def assert_compact(self, text, value):
        self.assertEqual(text, json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")

    def test_flag_is_consumed_only_in_global_prefix(self):
        selectors, command, tail = cli._selectors([
            "--compact-json", "-w", "alpha", "--chat=synthetic", "--compact-json",
            "recall", "--", "--compact-json"])
        self.assertEqual(selectors, {"compact_json": True, "workspace": "alpha", "chat_id": "synthetic"})
        self.assertEqual((command, tail), ("recall", ["--", "--compact-json"]))
        self.assertEqual(cli._selectors(["--config", "--compact-json", "check"]),
                         ({"config_file": "--compact-json"}, "check", []))

    def test_tail_and_unknown_boolean_assignment_remain_runtime_arguments(self):
        self.assertEqual(cli._selectors(["entry", "get", "--", "--compact-json"]),
                         ({}, "entry", ["get", "--", "--compact-json"]))
        self.assertEqual(cli._selectors(["recall", "--compact-json"]),
                         ({}, "recall", ["--compact-json"]))
        self.assertEqual(cli._selectors(["--compact-json=false", "check"]),
                         ({}, "--compact-json=false", ["check"]))

    def test_early_workspace_command_does_not_receive_a_storage_selector(self):
        value = {"current": "default", "workspaces": [{"name": "default", "label": "Работа"}]}
        with mock.patch.object(cli, "list_workspaces", return_value=value):
            compact, error = self.invoke(["--compact-json", "workspace", "list"])
            pretty, _ = self.invoke(["workspace", "list"])
        self.assertEqual(error, "")
        self.assert_compact(compact, value)
        self.assertEqual(pretty, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def test_early_chat_bind_preserves_binding_arguments(self):
        value = {"chat_id": "synthetic", "workspace": "alpha", "bound": True}
        with mock.patch.object(cli, "bind_chat", return_value=value) as bind:
            text, error = self.invoke(["--compact-json", "--chat", "synthetic", "chat", "bind", "--current"])
        self.assertEqual(error, "")
        self.assert_compact(text, value)
        bind.assert_called_once_with("synthetic", workspace=None, current=True, replace=False)

    def test_compact_flag_does_not_bypass_chat_global_workspace_guard(self):
        with mock.patch.object(cli, "current_chat", return_value={"workspace": "alpha"}), \
                mock.patch.object(cli, "use_workspace") as use:
            stdout, stderr = self.invoke(["--compact-json", "--chat", "synthetic", "use", "beta"], expected=1)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["code"], "chat_global_use_forbidden")
        use.assert_not_called()

    def test_status_receives_original_selectors_without_format_flag(self):
        value = {"workspace": "alpha", "health": {"status": "ok"}}
        with mock.patch("richi_launcher.status.command", return_value=value) as status:
            stdout, stderr = self.invoke([
                "--compact-json", "-w", "alpha", "--project-path", "/synthetic/repo",
                "status", "--projects"])
        self.assertEqual(stderr, "")
        self.assert_compact(stdout, value)
        status.assert_called_once_with(["--projects"], {"workspace": "alpha"}, None, "/synthetic/repo")

    def test_update_is_still_independent_of_chat_and_storage(self):
        value = {"status": "up_to_date", "available": False}
        with mock.patch("richi_launcher.update.command", return_value=value) as update, \
                mock.patch.object(cli, "detect_chat", side_effect=AssertionError("update must not resolve a chat")), \
                mock.patch.object(cli, "resolve_settings", side_effect=AssertionError("update must not resolve storage")):
            stdout, stderr = self.invoke(["--compact-json", "update", "check"])
            rejected, error = self.invoke(["--compact-json", "-w", "alpha", "update", "check"], expected=1)
        self.assertEqual(stderr, "")
        self.assert_compact(stdout, value)
        self.assertEqual(rejected, "")
        self.assertIn("not a workspace", json.loads(error)["error"])
        update.assert_called_once_with(["check"])

    def test_emergency_rollback_accepts_non_storage_global_options(self):
        for prefix in ([], ["--compact-json"], ["--chat", "synthetic"],
                       ["--chat=synthetic", "--compact-json"],
                       ["--compact-json", "--chat", "synthetic", "--compact-json"]):
            with self.subTest(prefix=prefix):
                self.assertTrue(richi_bootstrap._rollback_requested(prefix + ["update", "rollback"]))
        for arguments in (["--db", "synthetic.db", "update", "rollback"],
                          ["-wdefault", "update", "rollback"],
                          ["--project-path", "/synthetic", "update", "rollback"],
                          ["--compact-json=false", "update", "rollback"],
                          ["update", "rollback", "--help"],
                          ["update", "rollback", "--compact-json"],
                          ["--chat", "--compact-json", "update", "rollback", "extra"]):
            with self.subTest(arguments=arguments):
                self.assertFalse(richi_bootstrap._rollback_requested(arguments))

    def test_chat_help_keeps_fixed_runtime_and_text_output(self):
        selected = object()
        with mock.patch.object(cli, "installed_runtime", return_value=selected), \
                mock.patch.object(cli, "runtime_command", return_value=["/synthetic/python"]) as command, \
                mock.patch.object(cli.os, "execve", side_effect=SystemExit(0)) as execute, \
                mock.patch.object(cli, "pin_chat", side_effect=AssertionError("help must not require binding")), \
                mock.patch.object(cli, "resolve_settings", side_effect=AssertionError("help must not resolve dev")):
            with self.assertRaises(SystemExit):
                cli.main(["--compact-json", "--chat", "synthetic", "--help"])
        command.assert_called_once_with(selected, ["--help"])
        execute.assert_called_once()


class CompactLauncherInstalledTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="richi-compact-installed-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name).resolve()
        cls.database = cls.root / "synthetic.sqlite3"
        cls.config = cls.root / "release.json"
        cls.config.write_text(json.dumps({"database": str(cls.database)}), encoding="utf-8")
        cls.development = cls.root / "development.json"
        cls.development.write_text(json.dumps({
            "database": str(cls.database), "dev": True,
            "development": {"source": str(Path(__file__).resolve().parents[1])}}), encoding="utf-8")
        connection = memory.connect(cls.database, create=True)
        try:
            memory.initialize(connection, cls.database)
            memory.project_put(connection, {"id": "alpha", "name": "Synthetic", "source": "test"})
            memory.entry_put(connection, {
                "id": "--compact-json", "kind": "note", "title": "Точная запись",
                "summary": "Один источник.\nУсловие остаётся в тексте.", "knowledge_state": "confirmed",
                "sources": [{"reference": "test://synthetic"}], "project_ids": ["alpha"]})
        finally:
            connection.close()

    def command(self, *arguments, config=None, expected=0):
        result = subprocess.run(bootstrap_command([
            "--config", str(config or self.config), *arguments]),
            capture_output=True, text=True, encoding="utf-8", timeout=20,
            env=cli_environment(self.root))
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def assert_compact(self, text):
        value = json.loads(text)
        self.assertEqual(text, json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
        return value

    def test_release_and_development_runtime_receive_global_flag(self):
        for config in (self.config, self.development):
            with self.subTest(config=config.name):
                pretty = self.command("check", config=config)
                compact = self.command("--compact-json", "check", config=config)
                value = self.assert_compact(compact.stdout)
                self.assertEqual(json.loads(pretty.stdout), value)
                self.assertEqual(pretty.stdout, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
                self.assertEqual(compact.stderr, "")

    def test_flag_looking_record_id_is_not_reinterpreted(self):
        pretty = self.command("entry", "get", "--", "--compact-json")
        compact = self.command("--compact-json", "entry", "get", "--", "--compact-json")
        value = self.assert_compact(compact.stdout)
        self.assertEqual(value["id"], "--compact-json")
        self.assertEqual(json.loads(pretty.stdout), value)
        self.assertEqual(pretty.stdout, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def test_config_recovery_works_with_unavailable_development_runtime(self):
        broken = self.root / "broken.json"
        broken.write_text(json.dumps({"dev": True, "development": {
            "source": str(self.root / "does-not-exist")}}), encoding="utf-8")
        result = self.command("--compact-json", "config", "show", config=broken)
        value = self.assert_compact(result.stdout)
        self.assertFalse(value["runtime"]["available"])
        self.assertTrue(Path(value["installed_runtime"]["package"]).is_dir())
        self.assertEqual(result.stderr, "")

    def test_misplaced_assignment_and_unknown_command_are_errors(self):
        for arguments in (("check", "--compact-json"), ("--compact-json=false", "check"),
                          ("--compact-json", "no-such-command")):
            with self.subTest(arguments=arguments):
                result = self.command(*arguments, expected=1)
                self.assertEqual(result.stdout, "")
                self.assertIn("error", json.loads(result.stderr))


if __name__ == "__main__":
    unittest.main()
