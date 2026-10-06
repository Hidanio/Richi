"""Workspace status through the fixed launcher and selected disposable runtimes."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ("import sys; sys.path.insert(0,sys.argv[1]); "
             "from richi_launcher.cli import main; sys.exit(main(sys.argv[2:]))")
NO_ACCESS = '''
import sys
from unittest import mock
sys.path.insert(0, sys.argv[1])
from richi_launcher import cli, status
with mock.patch.object(status, "resolve_settings", side_effect=AssertionError("settings accessed")), \\
     mock.patch.object(status, "resolve_runtime", side_effect=AssertionError("runtime accessed")), \\
     mock.patch.object(status.subprocess, "run", side_effect=AssertionError("worker started")), \\
     mock.patch("pathlib.Path.cwd", side_effect=AssertionError("project path accessed")):
    result = cli.main(sys.argv[2:])
sys.exit(result)
'''


class StatusCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-status-cli-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.installed = self.root / "fixed package"
        ignored = shutil.ignore_patterns("__pycache__", "*.pyc")
        for source, target in ((ROOT / "launcher/richi_launcher", "richi_launcher"),
                               (ROOT / "src/richi", "richi")):
            shutil.copytree(source, self.installed / target, ignore=ignored)
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith("RICHI_")
                            and key not in {"CODEX_THREAD_ID", "CODEX_SESSION_ID"}}
        self.environment.update(HOME=str(self.root / "home"),
                                XDG_CONFIG_HOME=str(self.root / "config"),
                                XDG_DATA_HOME=str(self.root / "data"))

    def run_command(self, *args, chat=None, env=None, ok=True, payload=None,
                    runner=BOOTSTRAP, cwd=None):
        environment = dict(self.environment, **(env or {}))
        if chat is not None:
            environment["CODEX_THREAD_ID"] = chat
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", runner, str(self.installed), *map(str, args)],
            cwd=cwd or self.root, env=environment, capture_output=True, text=True, timeout=25,
            input=json.dumps(payload) if payload is not None else None)
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return result

    def command(self, *args, **kwargs):
        result = self.run_command(*args, **kwargs)
        return json.loads(result.stdout if result.returncode == 0 else result.stderr)

    def snapshot(self):
        # SQLite read-only connections still update WAL reader bookkeeping in
        # an existing -shm file. Its presence/size matter; its mtime does not.
        return sorted((str(path.relative_to(self.root)), path.stat().st_size,
                       None if path.name.endswith("-shm") else path.stat().st_mtime_ns,
                       hashlib.sha256(path.read_bytes()).hexdigest()
                       if path.name.endswith((".sqlite3", ".sqlite3-wal", ".json")) else None)
                      for path in self.root.rglob("*") if path.is_file())

    def create(self, name, initialized=False):
        result = self.command("workspace", "create", name)
        if initialized:
            self.command("-w", name, "init")
        return result

    def project(self, workspace, project_id, path=None, name=None):
        self.command("-w", workspace, "project", "upsert", "--json", "-", payload={
            "id": project_id, "name": name or project_id,
            "repo_path": str(path) if path is not None else None})

    def checkout(self):
        checkout = self.root / "development checkout"
        shutil.copytree(self.installed / "richi", checkout / "src/richi")
        (checkout / "pyproject.toml").write_text("[project]\nname='richi'\nversion='0.1.0'\n")
        return checkout

    def test_unbound_chat_reports_choice_without_reading_settings_runtime_or_project_path(self):
        before = self.snapshot()
        for arguments in (("status",),
                          ("--project-path", self.root / "never inspected", "-w", "default",
                           "status", "--projects", "--project", "shared")):
            with self.subTest(arguments=arguments):
                report = self.command(*arguments, chat="new-chat", runner=NO_ACCESS)
                self.assertEqual(report["status"], "selection_required")
                self.assertEqual(report["schema_version"], 1)
                self.assertTrue(report["read_only"])
                self.assertTrue(report["chat"]["detected"])
                self.assertFalse(report["chat"]["bound"])
                for key in ("workspace", "selection", "storage", "runtime", "diagnostics"):
                    self.assertIsNone(report[key], key)
                self.assertTrue(report["next_actions"])
                self.assertTrue(all(isinstance(action["argv"], list) for action in report["next_actions"]))
        self.assertEqual(self.snapshot(), before)

    def test_missing_store_status_does_not_initialize_or_write_any_files(self):
        before = self.snapshot()
        report = self.command("status")
        self.assertEqual(report["workspace"], "default")
        self.assertEqual(report["selection"], "cli_default")
        self.assertEqual(report["status"], "attention")
        self.assertEqual(report["runtime"]["mode"], "release")
        self.assertTrue(report["runtime"]["available"])
        self.assertEqual(report["diagnostics"]["database"]["status"], "missing")
        self.assertEqual(report["diagnostics"]["projects"]["status"], "not_checked")
        self.assertFalse(Path(report["storage"]["database"]).exists())
        self.assertEqual(self.snapshot(), before)

    def test_same_checkout_two_bound_chats_stay_in_their_stores_after_global_switch(self):
        repository = self.root / "shared repository"
        repository.mkdir()
        stores = {}
        for name in ("alpha", "beta"):
            stores[name] = self.create(name, initialized=True)
            self.project(name, "shared", repository, name="marker_" + name)
            self.command("chat", "bind", name, chat=name)
        self.command("use", "beta")
        before = self.snapshot()
        for name in stores:
            report = self.command("status", "--projects", chat=name, cwd=repository)
            self.assertEqual(report["workspace"], name)
            self.assertEqual(report["chat"]["workspace"], name)
            self.assertEqual(report["selection"], "chat")
            self.assertEqual(report["cli"]["default"], "beta")
            self.assertEqual(report["storage"]["database"], stores[name]["database"])
            self.assertEqual(report["diagnostics"]["project"]["project"]["id"], "shared")
            self.assertEqual(report["diagnostics"]["project"]["project"]["name"], "marker_" + name)
            items = report["diagnostics"]["projects"]["items"]
            self.assertEqual([item["project"]["name"] for item in items], ["marker_" + name])
        self.assertEqual(self.snapshot(), before)

    def test_status_keeps_one_selection_snapshot_if_chat_and_cli_change_mid_invocation(self):
        alpha = self.create("alpha", initialized=True)
        self.create("beta", initialized=True)
        self.project("alpha", "shared", self.root, name="marker_alpha")
        self.project("beta", "shared", self.root, name="marker_beta")
        self.command("use", "alpha")
        self.command("chat", "bind", "alpha", chat="chat")
        runner = """
import sys
sys.path.insert(0, sys.argv[1])
from richi_launcher import cli, status, chats, workspaces
original = status.current_chat
def concurrent_selection(chat_id):
    snapshot = original(chat_id)
    chats.bind_chat(chat_id, "beta", replace=True)
    workspaces.use_workspace("beta")
    return snapshot
status.current_chat = concurrent_selection
sys.exit(cli.main(sys.argv[2:]))
"""
        report = self.command("status", chat="chat", runner=runner)
        self.assertEqual(report["workspace"], "alpha")
        self.assertEqual(report["chat"]["workspace"], "alpha")
        self.assertEqual(report["storage"]["database"], alpha["database"])
        self.assertEqual(report["diagnostics"]["project"]["project"]["name"], "marker_alpha")
        next_report = self.command("status", chat="chat")
        self.assertEqual(next_report["workspace"], "beta")
        self.assertEqual(next_report["cli"]["default"], "beta")
        self.assertEqual(next_report["diagnostics"]["project"]["project"]["name"], "marker_beta")

    def test_terminal_status_keeps_snapshot_if_global_default_changes_during_resolution(self):
        alpha = self.create("alpha", initialized=True)
        self.create("beta", initialized=True)
        self.command("use", "alpha")
        runner = """
import sys
sys.path.insert(0, sys.argv[1])
from richi_launcher import cli, status, workspaces
original = status.current_chat
def concurrent_selection(chat_id):
    snapshot = original(chat_id)
    workspaces.use_workspace("beta")
    return snapshot
status.current_chat = concurrent_selection
sys.exit(cli.main(sys.argv[2:]))
"""
        report = self.command("status", runner=runner)
        self.assertEqual(report["workspace"], "alpha")
        self.assertEqual(report["cli"]["current"], "alpha")
        self.assertEqual(report["storage"]["database"], alpha["database"])
        self.assertEqual(self.command("status")["workspace"], "beta")

    def test_worker_failures_return_bounded_unavailable_report_without_retry_or_fallback(self):
        runner = """
import json, os, sys
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from richi_launcher import cli, status
calls = []
def worker(execution, *, stdout, stderr, **kwargs):
    calls.append(execution)
    case = os.environ["STATUS_WORKER_CASE"]
    if case == "timeout":
        raise status.subprocess.TimeoutExpired(execution, 30)
    if case == "os_error":
        raise OSError("synthetic worker unavailable")
    if case == "oversized":
        stdout.write(b"x" * (status.MAX_OUTPUT + 1))
    elif case == "invalid_json":
        stdout.write(b"not json")
    elif case == "incomplete_json":
        stdout.write(b"{}")
    elif case in {"invalid_issue", "invalid_issue_string", "invalid_project"}:
        payload = {"database": {"status": "ready"}, "project": None,
                   "projects": {"status": "not_checked"}, "issues": []}
        if case == "invalid_project":
            payload["project"] = "oops"
        else:
            payload["issues"] = [None if case == "invalid_issue" else "oops"]
        stdout.write(json.dumps(payload).encode())
    return SimpleNamespace(returncode=0)
status.subprocess.run = worker
result = cli.main(sys.argv[2:])
assert len(calls) == 1, "status retried or fell back to another runtime"
sys.exit(result)
"""
        for failure in ("timeout", "os_error", "invalid_json", "incomplete_json", "oversized",
                        "invalid_issue", "invalid_issue_string", "invalid_project"):
            with self.subTest(failure=failure):
                result = self.run_command("status", runner=runner, env={"STATUS_WORKER_CASE": failure})
                self.assertLess(len(result.stdout), 10000)
                report = json.loads(result.stdout)
                self.assertEqual(report["status"], "unavailable")
                self.assertEqual(report["runtime"]["mode"], "release")
                self.assertFalse(report["runtime"]["available"])
                self.assertIsNone(report["diagnostics"])

    def test_bound_default_chat_rejects_workspace_and_storage_overrides(self):
        self.create("alpha")
        bound = self.command("chat", "bind", "default", chat="chat")
        for args, code in ((("-w", "alpha", "status"), "chat_workspace_conflict"),
                           (("--db", bound["database"], "status"), "chat_storage_override"),
                           (("--config", bound["config_file"], "status"), "chat_storage_override")):
            with self.subTest(args=args):
                self.assertEqual(self.command(*args, chat="chat", ok=False)["code"], code)
        for key, value, code in (("RICHI_WORKSPACE", "alpha", "chat_workspace_conflict"),
                                 ("RICHI_DB", bound["database"], "chat_storage_override"),
                                 ("RICHI_CONFIG", bound["config_file"], "chat_storage_override"),
                                 ("RICHI_DATA_DIR", bound["data_dir"], "chat_storage_override")):
            with self.subTest(environment=key):
                self.assertEqual(self.command("status", chat="chat", env={key: value}, ok=False)["code"], code)
        self.assertEqual(self.command("-w", "default", "status", chat="chat")["selection"], "chat")

    def test_broken_development_reports_unavailable_without_falling_back_to_release(self):
        self.create("alpha", initialized=True)
        self.command("chat", "bind", "alpha", chat="chat")
        self.command("config", "set", "development.source", self.root / "missing", chat="chat")
        self.command("config", "set", "dev", "true", chat="chat")
        missing = self.command("status", "--projects", chat="chat")
        self.assertEqual(missing["status"], "unavailable")
        self.assertEqual(missing["runtime"]["mode"], "dev")
        self.assertFalse(missing["runtime"]["available"])
        self.assertIsNone(missing["diagnostics"])
        checkout = self.checkout()
        (checkout / "src/richi/status.py").write_text("raise ImportError('synthetic selected runtime failure')\n")
        self.command("config", "set", "development.source", checkout, chat="chat")
        failure = self.command("status", chat="chat")
        self.assertEqual(failure["status"], "unavailable")
        self.assertFalse(failure["runtime"]["available"])
        self.assertEqual(failure["runtime"]["mode"], "dev")
        self.assertIsNone(failure["diagnostics"])
        self.assertIn("synthetic selected runtime failure", json.dumps(failure))

    def test_selected_development_status_loads_same_size_edits_without_reinstall(self):
        self.command("init")
        checkout = self.checkout()
        source = checkout / "src/richi/status.py"
        source.write_text(source.read_text() + '''
_base_inspect = inspect
def inspect(*args, **kwargs):
    result = _base_inspect(*args, **kwargs)
    result["database"]["fixture_marker"] = "alpha"
    return result
''')
        self.command("config", "set", "development.source", checkout)
        self.command("config", "set", "dev", "true")
        first = self.command("status")
        self.assertEqual(first["runtime"]["mode"], "dev")
        self.assertEqual(first["diagnostics"]["database"]["fixture_marker"], "alpha")
        previous = source.stat()
        source.write_text(source.read_text().replace('"fixture_marker"] = "alpha"',
                                                     '"fixture_marker"] = "bravo"'))
        os.utime(source, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        second = self.command("status")
        self.assertEqual(second["diagnostics"]["database"]["fixture_marker"], "bravo")
        self.assertEqual(first["runtime"]["identity"], second["runtime"]["identity"])

    def test_project_expectation_reports_mismatch_and_audit_output_is_bounded(self):
        self.command("init")
        repository = self.root / "registered folder"
        repository.mkdir()
        self.project("default", "shared", repository)
        self.project("default", "offline", self.root / "missing folder")
        self.project("default", "remote-only")
        before = self.snapshot()
        ordinary = self.command("--project-path", repository, "status")
        self.assertEqual(ordinary["diagnostics"]["project"]["status"], "matched")
        self.assertEqual(ordinary["diagnostics"]["projects"]["status"], "not_checked")
        mismatch = self.command("--project-path", repository, "status", "--project", "offline")
        self.assertEqual(mismatch["diagnostics"]["project"]["status"], "mismatch")
        self.assertEqual(mismatch["status"], "attention")
        audited = self.command("--project-path", repository, "status", "--projects", "--limit", "1")
        audit = audited["diagnostics"]["projects"]
        self.assertEqual(audit["total"], 3)
        self.assertEqual(audit["checked"], 3)
        self.assertEqual(audit["omitted"], 2)
        self.assertEqual(len(audit["items"]), 1)
        self.assertEqual(audited["status"], "attention")
        self.assertEqual(self.snapshot(), before)

    def test_status_argument_validation_and_help_do_not_open_store(self):
        before = self.snapshot()
        for tail in (("--limit", "0"), ("--limit", "201"), ("--limit", "-1"),
                     ("--limit", "many"), ("--unexpected",), ("--projects=yes",),
                     ("--", "--help"), ("--project",), ("--project", ""),
                     ("--project", "bad\nvalue"), ("--project", "p" * 201)):
            with self.subTest(tail=tail):
                self.assertIn("error", self.command("status", *tail, chat="new", ok=False))
        help_result = self.run_command("status", "--help", chat="new")
        self.assertIn("--projects", help_result.stdout)
        self.assertIn("--limit", help_result.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_ordinary_terminal_reports_selector_origin_and_supports_custom_configuration(self):
        alpha = self.create("alpha")
        self.create("beta")
        self.command("use", "alpha")
        current = self.command("status")
        self.assertEqual(current["selection"], "cli_default")
        self.assertEqual(current["workspace"], "alpha")
        environment = self.command("status", env={"RICHI_WORKSPACE": "beta"})
        self.assertEqual(environment["selection"], "environment")
        self.assertEqual(environment["workspace"], "beta")
        argument = self.command("-walpha", "status", env={"RICHI_WORKSPACE": "beta"})
        self.assertEqual(argument["selection"], "argument")
        self.assertEqual(argument["storage"]["database"], alpha["database"])
        custom = self.root / "custom.json"
        self.command("--config", custom, "config", "set", "dev", "false")
        before = self.snapshot()
        report = self.command("--config", custom, "status")
        self.assertEqual(report["selection"], "custom_config")
        self.assertIsNone(report["workspace"])
        self.assertEqual(report["storage"]["config_file"], str(custom))
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
