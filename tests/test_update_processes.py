"""Update routing and real runtime children keep installation leases intact."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import http.client
from http.server import BaseHTTPRequestHandler
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from richi import launch_map, memory, serve
from richi_launcher import cli, runtime, status, update
from richi_launcher.config import ConfigError


def isolated_environment(root):
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("RICHI_") and key != "CODEX_THREAD_ID"}
    environment.update(HOME=str(root / "home"), XDG_CONFIG_HOME=str(root / "config"),
                       XDG_DATA_HOME=str(root / "data"))
    return environment


class UpdateCliRoutingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-update-routing-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"dev": True, "development": {
            "source": str(self.root / "missing checkout")}}), encoding="utf-8")
        self.database = self.root / "memory.sqlite3"
        self.database.write_bytes(b"synthetic store must never be opened")
        environment = isolated_environment(self.root)
        environment.update(CODEX_THREAD_ID="unbound-update-chat", RICHI_WORKSPACE="missing",
                           RICHI_CONFIG=str(self.config), RICHI_DB=str(self.database))
        stack = self.enterContext(ExitStack())
        stack.enter_context(mock.patch.dict(os.environ, environment, clear=True))
        for name in ("resolve_settings", "resolve_runtime", "pin_chat", "detect_chat"):
            stack.enter_context(mock.patch.object(cli, name, side_effect=AssertionError(
                "Installation update consulted workspace or development code: " + name)))
        stack.enter_context(mock.patch("sqlite3.connect", side_effect=AssertionError(
            "Installation update opened a knowledge store")))
        self.local = {"supported": True, "current": {"version": "0.1.0"}}
        self.local_status = stack.enter_context(mock.patch.object(
            update.installation, "status", return_value=self.local))
        self.fetch = stack.enter_context(mock.patch.object(update.releases, "fetch_release"))
        self.apply = stack.enter_context(mock.patch.object(update.installation, "apply_release",
                                                           return_value={"status": "applied"}))
        self.rollback = stack.enter_context(mock.patch.object(update.installation, "rollback",
                                                              return_value={"status": "rolled_back"}))
        self.cleanup = stack.enter_context(mock.patch.object(update.installation, "cleanup",
                                                             return_value={"status": "cleaned"}))
        self.original = self.snapshot()

    # TestCase.enterContext was added after the minimum supported Python 3.9.
    def enterContext(self, context):
        result = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return result

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    def command(self, *arguments, ok=True):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(list(arguments))
        self.assertEqual(code, 0 if ok else 1, output.getvalue() + errors.getvalue())
        self.assertEqual(self.snapshot(), self.original)
        return json.loads(output.getvalue() if ok else errors.getvalue())

    @staticmethod
    def release(version="0.2.0", checksum="a" * 64):
        return {"version": version, "tag": "v" + version,
                "html_url": "https://github.com/Hidanio/Richi/releases/tag/v" + version,
                "manifest": {"wheel": {"sha256": checksum}}}

    def test_check_works_without_chat_binding_or_working_workspace_and_dev(self):
        self.fetch.return_value = None
        report = self.command("--chat", "another-unbound-chat", "update", "check")
        self.assertEqual(report["status"], "no_release")
        self.fetch.assert_called_once_with(None)
        self.apply.assert_not_called()
        self.rollback.assert_not_called()
        self.cleanup.assert_not_called()

    def test_check_distinguishes_newer_older_and_unverified_same_version(self):
        for version, verified, expected, can_apply in (
                ("0.2.0", False, "update_available", True),
                ("0.0.9", False, "older_release", False),
                ("0.1.0", False, "release_available", True),
                ("0.1.0", True, "up_to_date", False)):
            with self.subTest(version=version, verified=verified):
                self.local["current"] = {"version": "0.1.0"}
                if verified:
                    self.local["current"]["wheel_sha256"] = "a" * 64
                self.fetch.return_value = self.release(version)
                report = self.command("update", "check")
                self.assertEqual(report["status"], expected)
                self.assertEqual(report["can_apply"], can_apply)
        self.apply.assert_not_called()

    def test_explicit_mutations_route_globally_without_workspace_resolution(self):
        release = self.release()
        self.fetch.return_value = release
        self.assertEqual(self.command("update", "apply", "--version", "0.2.0")["status"], "applied")
        self.fetch.assert_called_once_with("0.2.0")
        self.apply.assert_called_once_with(release)
        self.fetch.reset_mock()
        self.local_status.reset_mock()
        self.assertEqual(self.command("update", "rollback")["status"], "rolled_back")
        self.assertEqual(self.command("update", "cleanup")["status"], "cleaned")
        self.rollback.assert_called_once_with()
        self.cleanup.assert_called_once_with()
        self.fetch.assert_not_called()
        self.local_status.assert_not_called()

    def test_auto_commands_route_without_workspace_or_dev_access(self):
        from richi_launcher import auto_update
        for action in ("enable", "disable", "status", "run"):
            with self.subTest(action=action), mock.patch.object(auto_update, action,
                    return_value={"status": action}) as operation:
                result = self.command("--chat", "unbound", "update", "auto", action)
                self.assertEqual(result["status"], action)
                if action == "enable":
                    operation.assert_called_once_with(interval_seconds=21600, auto_install=False)
                else:
                    operation.assert_called_once_with()
        with mock.patch.object(auto_update, "enable", return_value={"status": "enabled"}) as enable:
            self.command("update", "auto", "enable", "--interval", "1d")
            enable.assert_called_once_with(interval_seconds=86400, auto_install=False)
        with mock.patch.object(auto_update, "enable", return_value={"status": "enabled"}) as enable:
            self.command("update", "auto", "enable", "--install", "--interval", "15m")
            enable.assert_called_once_with(interval_seconds=900, auto_install=True)
        self.fetch.assert_not_called()
        self.local_status.assert_not_called()

    def test_auto_invalid_interval_cannot_register_a_job(self):
        from richi_launcher import auto_update
        with mock.patch.object(auto_update, "enable") as enable:
            for value in ("1m", "8d", "0h", "1.5h", "-1h", "hour"):
                with self.subTest(value=value):
                    self.command("update", "auto", "enable", "--interval=" + value, ok=False)
            enable.assert_not_called()

    def test_apply_rejects_unsupported_installation_before_network_access(self):
        self.local_status.return_value = {"supported": False, "reason": "dedicated venv required"}
        self.assertIn("dedicated venv required", self.command("update", "apply", ok=False)["error"])
        self.fetch.assert_not_called()
        self.apply.assert_not_called()

    def test_network_failure_remains_actionable_and_does_not_change_settings(self):
        self.fetch.side_effect = ConfigError("Release server unavailable")
        self.assertIn("Release server unavailable", self.command("update", "check", ok=False)["error"])
        self.apply.assert_not_called()

    def test_storage_selectors_cannot_make_an_installation_update_look_workspace_scoped(self):
        selectors = (("-w", "work"), ("-wwork",), ("--workspace=work",),
                     ("--config", str(self.config)), ("--db", str(self.database)),
                     ("--project-path", str(self.root)))
        for selection in selectors:
            for action in ("check", "apply", "rollback", "cleanup", "auto"):
                with self.subTest(selection=selection, action=action):
                    report = self.command(*selection, "update", action, ok=False)
                    self.assertIn("installation", report["error"])
        self.fetch.assert_not_called()
        self.local_status.assert_not_called()
        self.apply.assert_not_called()
        self.rollback.assert_not_called()
        self.cleanup.assert_not_called()


@unittest.skipIf(sys.platform == "win32", "Map launcher targets macOS and Linux")
class MapPortReuseTests(unittest.TestCase):
    def test_restart_reuses_server_time_wait_port_but_never_an_active_listener(self):
        with socket.socket() as listener:
            # Match the map server's listening socket. An accepted connection
            # actively closed by the server leaves its port in TIME_WAIT.
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.settimeout(3)
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(3)
                    connection.shutdown(socket.SHUT_WR)
                    self.assertEqual(client.recv(1), b"")
                    client.close()
                    self.assertEqual(connection.recv(1), b"")
        self.assertTrue(launch_map.available(port), "A restarting map can reuse TIME_WAIT")
        with serve.LoopbackHTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler) as server:
            self.assertEqual(server.server_address[1], port)
            self.assertFalse(launch_map.available(port), "An active listener still owns its port")


@unittest.skipIf(sys.platform == "win32", "Runtime leases require macOS or Linux flock")
class RuntimeLeasePropagationTests(unittest.TestCase):
    def setUp(self):
        import fcntl
        self.fcntl = fcntl
        temporary = tempfile.TemporaryDirectory(prefix="richi-child-leases-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        checkout = self.root / "checkout"
        package = checkout / "src/richi"
        shutil.copytree(Path(memory.__file__).parent, package,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (checkout / "pyproject.toml").write_text('[project]\nname="richi"\nversion="0.1.0"\n')
        self.lease_path = self.root / "generation.lease"
        self.lease = self.lease_path.open("w+b")
        self.addCleanup(self.lease.close)
        fcntl.flock(self.lease, fcntl.LOCK_SH)
        # An inherited fd must survive close_fds=True in each real worker.
        # Checking inode/device rules out reuse of the same descriptor number.
        package.joinpath("__init__.py").write_text(
            package.joinpath("__init__.py").read_text(encoding="utf-8") + '\n'
            'import os as _lease_os\n'
            '_lease_fd = int(_lease_os.environ["RICHI_INSTALLATION_LEASE_FD"])\n'
            '_lease_actual = _lease_os.fstat(_lease_fd)\n'
            '_lease_expected = _lease_os.stat(' + repr(str(self.lease_path)) + ')\n'
            'assert (_lease_actual.st_dev, _lease_actual.st_ino) == '
            '(_lease_expected.st_dev, _lease_expected.st_ino), "wrong runtime lease"\n',
            encoding="utf-8")
        self.runtime = runtime.describe_runtime(package, "dev", checkout)
        self.database = self.root / "memory.sqlite3"
        with memory.connect(self.database, create=True) as connection:
            memory.initialize(connection, self.database)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"database": str(self.database), "dev": True,
                                         "development": {"source": str(checkout)}}), encoding="utf-8")
        environment = isolated_environment(self.root)
        environment["RICHI_INSTALLATION_LEASE_FD"] = str(self.lease.fileno())
        environment["RICHI_INSTALLATION_GENERATION"] = "v0.1.0-process-fixture"
        patcher = mock.patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_preflight_status_and_git_workers_inherit_the_selected_generation_lease(self):
        self.assertTrue(runtime.preflight(self.runtime, self.database, self.config))
        report = status._worker(self.runtime, {"database": str(self.database),
                                              "project_path": None, "all_projects": False})
        self.assertEqual(report["database"]["status"], "ready")
        code, payload = serve._run_git_request(self.database, {"ref": "entry:missing"},
                                               self.runtime, self.config, config_required=True)
        self.assertEqual(code, 404, payload)
        self.assertEqual(payload["code"], "record_unavailable")

    def assert_locked(self):
        with self.lease_path.open("rb") as candidate:
            with self.assertRaises(BlockingIOError):
                self.fcntl.flock(candidate, self.fcntl.LOCK_EX | self.fcntl.LOCK_NB)

    def test_detached_map_keeps_lease_after_launcher_exits_and_passes_it_to_graph_worker(self):
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        children = []
        popen = subprocess.Popen

        def remember_child(*arguments, **options):
            child = popen(*arguments, **options)
            children.append(child)
            return child

        # Keep the actual Popen handle so the test can reap its detached child.
        with mock.patch.object(launch_map.subprocess, "Popen", side_effect=remember_child):
            launched = launch_map.launch(self.database, port, self.config, self.runtime,
                                         config_required=True)
        self.assertEqual(launched["status"], "started")
        self.assertEqual(len(children), 1)
        child = children[0]
        self.assertEqual(child.pid, launched["pid"])
        try:
            # The detached server is now the sole holder, just as after the
            # short-lived `richi map` launcher exits during a release update.
            self.lease.close()
            self.assert_locked()
            from urllib.parse import urlsplit
            address = urlsplit(launched["url"])
            client = http.client.HTTPConnection(address.hostname, address.port, timeout=15)
            try:
                client.request("GET", "/api/health")
                health_response = client.getresponse()
                health = json.loads(health_response.read())
                self.assertEqual(health_response.status, 200, health)
                self.assertEqual(health["pid"], child.pid)
                self.assertEqual(health["installation_generation"], "v0.1.0-process-fixture")
                client.request("GET", "/api/graph")
                response = client.getresponse()
                payload = json.loads(response.read())
                self.assertEqual(response.status, 200, payload)
                self.assertIn("nodes", payload)
            finally:
                client.close()
            self.assert_locked()
        finally:
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
                self.fail("Detached map did not stop within five seconds")
        with self.lease_path.open("rb") as candidate:
            self.fcntl.flock(candidate, self.fcntl.LOCK_EX | self.fcntl.LOCK_NB)


if __name__ == "__main__":
    unittest.main()
