"""Stop, restart and replace running maps without signalling unverified processes."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from richi import launch_map, serve
import richi_bootstrap
import richi_launcher
from richi_launcher import maps

from cli_environment import cli_environment


def free_port():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return reservation.getsockname()[1]


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class ForgedHealth(BaseHTTPRequestHandler):
    payload = {}

    def do_GET(self):
        body = json.dumps(self.payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class ArgumentTests(unittest.TestCase):
    runtime = {"mode": "release", "package": "/synthetic/site/richi", "version": "1.0.1",
               "identity": "a" * 64, "source": None}

    def runner(self, action, *rest, runtime=None):
        payload = json.dumps(runtime or self.runtime)
        return ["/synthetic/bin/python", "-I", "-B", "-c", 'x\nos.environ["RICHI_ACTIVE_RUNTIME"] = payload\n',
                "/synthetic/site", payload, action, *rest]

    def test_only_the_exact_runtime_runner_serving_a_map_is_recognized(self):
        self.assertEqual(maps.runner_launcher(self.runner("serve", "--db", "/x", "--port", "1"), self.runtime),
                         "/synthetic/site")
        self.assertEqual(maps.runner_launcher(self.runner("cli", "--db", "/x", "map", "serve", "--port", "1"),
                                              self.runtime), "/synthetic/site")
        rejected = [self.runner("cli", "--db", "/x", "recall", "map"),
                    self.runner("cli", "--db", "/x", "map", "--no-open"),
                    self.runner("git_worker"),
                    self.runner("serve", runtime=dict(self.runtime, identity="b" * 64)),
                    ["/bin/sleep", "60"],
                    ["python", "-c", "print()", "/synthetic/site", json.dumps(self.runtime), "serve"]]
        for arguments in rejected:
            with self.subTest(arguments=arguments):
                self.assertIsNone(maps.runner_launcher(arguments, self.runtime))

    @unittest.skipUnless(sys.platform == "darwin" or sys.platform.startswith("linux"), "Process arguments")
    def test_process_arguments_are_exact_including_newlines(self):
        code = "import time\n# synthetic é line\ntime.sleep(30)\n"
        child = subprocess.Popen([sys.executable, "-I", "-c", code, "/with space/path", "--port", "1"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        deadline = time.monotonic() + 5
        while maps.process_arguments(child.pid) is None or "-c" not in maps.process_arguments(child.pid):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)
        self.assertEqual(maps.process_arguments(child.pid)[1:],
                         ["-I", "-c", code, "/with space/path", "--port", "1"])
        self.assertEqual(maps.process_uid(child.pid), os.getuid())


@unittest.skipIf(sys.platform == "win32", "Detached maps target macOS and Linux")
class MapControlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-map-control-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.database = self.root / "memory.sqlite3"
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"database": str(self.database), "data_dir": str(self.root)}))
        self.environment = cli_environment(self.root)
        self.port = free_port()
        self.command = [sys.executable, "-B", "-m", "richi", "--config", str(self.config)]
        self.run_cli("init")
        self.started = set()
        self.addCleanup(self.stop_started)

    def run_cli(self, *arguments, ok=True):
        result = subprocess.run(self.command + list(arguments), capture_output=True, text=True,
                                timeout=90, env=self.environment, cwd=self.root)
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return json.loads(result.stdout if ok else result.stderr) if (result.stdout or result.stderr).strip() else None

    def map(self, *arguments, ok=True):
        result = self.run_cli("map", *arguments, "--port", str(self.port), ok=ok)
        if ok and isinstance(result, dict) and result.get("pid"):
            self.started.add(result["pid"])
        return result

    def stop_started(self):
        for pid in self.started:
            if alive(pid) and maps.process_arguments(pid):
                os.kill(pid, signal.SIGTERM)

    def health(self, port=None):
        return maps.read_health(port or self.port)

    def assert_no_installation_files(self):
        # Before a first update nothing is written to the installation's data
        # directory; managed registries are covered by test_update_e2e.
        self.assertEqual(list(self.root.rglob("installations")), [])

    def test_stop_restart_keep_the_url_and_wait_for_the_old_process(self):
        self.assertEqual(self.map("stop"), {"status": "not_running", "url": maps.url(self.port)})
        started = self.map("--no-open")
        self.assertEqual((started["status"], started["url"]), ("started", maps.url(self.port)))
        self.assertIn(maps.HANDOFF_CAPABILITY, self.health()["capabilities"])
        restarted = self.map("restart", "--no-open")
        self.assertEqual((restarted["status"], restarted["url"]), ("restarted", maps.url(self.port)))
        self.assertEqual(restarted["previous"]["pid"], started["pid"])
        self.assertTrue(restarted["previous"]["exited"])
        self.assertFalse(alive(started["pid"]) and maps.process_arguments(started["pid"]))
        self.assertEqual(self.health()["pid"], restarted["pid"])
        stopped = self.map("stop")
        self.assertEqual((stopped["status"], stopped["pid"], stopped["exited"]), ("stopped", restarted["pid"], True))
        self.assertTrue(maps.port_free(self.port))
        self.assertEqual(self.map("stop")["status"], "not_running")
        again = self.map("restart", "--no-open")
        self.assertEqual((again["status"], again["url"]), ("started", maps.url(self.port)))
        self.assertEqual(self.map("stop")["status"], "stopped")
        for arguments in (("stop", "--no-open"), ("stop", "--open"), ("--open",)):
            with self.subTest(arguments=arguments):
                self.assertIn("error", self.map(*arguments, ok=False))
        self.assert_no_installation_files()

    def test_foreign_and_forged_listeners_are_never_signalled(self):
        sleeper = subprocess.Popen(["/bin/sleep", "60"])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        # A raw TCP service: no HTTP health at all.
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", self.port))
            listener.listen()
            refused = self.map("stop", ok=False)
            self.assertEqual((refused["status"], refused["reason"]), ("refused", "not_richi_map"))
        forged = {"application": "project-memory-map", "pid": sleeper.pid, "database": str(self.database),
                  "config_file": str(self.config), "api_version": serve.API_VERSION,
                  "capabilities": list(serve.CAPABILITIES), "workspace": None,
                  "runtime": {"mode": "release", "package": "/synthetic/richi", "version": "0.0.1",
                              "identity": "f" * 64, "source": None}}
        for payload, reason in ((forged, "unverified_process"),
                                (dict(forged, database=str(self.root / "other.sqlite3")), "other_workspace")):
            with self.subTest(reason=reason):
                handler = type("Handler", (ForgedHealth,), {"payload": payload})
                server = HTTPServer(("127.0.0.1", self.port), handler)
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    refused = self.map("stop", ok=False)
                    self.assertEqual((refused["status"], refused["reason"]), ("refused", reason))
                    if reason == "unverified_process":
                        # richi map replaces stale maps only after verification too.
                        launched = self.map("--no-open", ok=False)
                        self.assertEqual(launched["reason"], "unverified_process")
                    self.assertEqual(self.health()["pid"], sleeper.pid)
                finally:
                    server.shutdown()
                    server.server_close()
                    worker.join(timeout=5)
                self.assertIsNone(sleeper.poll())
        self.assertIsNone(sleeper.poll(), "A process named only by forged health must survive")

    def copied_launcher(self, version):
        site = self.root / ("site-" + version)
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
        shutil.copytree(Path(serve.__file__).parent, site / "richi", ignore=ignore)
        shutil.copytree(Path(richi_launcher.__file__).parent, site / "richi_launcher", ignore=ignore)
        shutil.copy2(richi_bootstrap.__file__, site / "richi_bootstrap.py")
        init = site / "richi" / "__init__.py"
        init.write_text(re.sub(r'(?m)^__version__\s*=.*$', '__version__ = "' + version + '"', init.read_text()))
        return site

    def test_richi_map_replaces_a_map_of_another_environment_on_the_same_url(self):
        site = self.copied_launcher("0.0.1")
        runner = "import sys;sys.path.insert(0,sys.argv[1]);from richi_launcher.cli import main;sys.exit(main(sys.argv[2:]))"
        log = (self.root / "stale.log").open("w")
        self.addCleanup(log.close)
        stale = subprocess.Popen([sys.executable, "-I", "-B", "-c", runner, str(site), "--config", str(self.config),
                                  "map", "serve", "--port", str(self.port)],
                                 env=self.environment, stdout=log, stderr=log, cwd=self.root)
        self.addCleanup(lambda: stale.poll() is None and stale.kill())
        deadline = time.monotonic() + 20
        while not (maps.is_map(self.health()) and self.health()["runtime"]["version"] == "0.0.1"):
            self.assertIsNone(stale.poll(), (self.root / "stale.log").read_text())
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.1)
        replaced = self.map("--no-open")
        self.assertEqual((replaced["status"], replaced["url"]), ("restarted", maps.url(self.port)))
        self.assertEqual((replaced["previous"]["pid"], replaced["previous"]["version"]), (stale.pid, "0.0.1"))
        self.assertEqual(stale.wait(timeout=10), 0)
        current = self.health()
        self.assertEqual(current["runtime"]["package"], str(Path(serve.__file__).parent))
        self.assertEqual(self.map("--no-open")["status"], "existing")

    def test_handoff_signal_reexecutes_the_map_in_place(self):
        started = self.map("--no-open")
        before = self.health()
        os.kill(started["pid"], signal.SIGUSR1)
        deadline = time.monotonic() + 20
        log = self.root / "runtime" / "map-server.log"
        while "Restart requested; restarting map on the same URL." not in log.read_text():
            self.assertLess(time.monotonic(), deadline, log.read_text())
            time.sleep(0.1)
        while not maps.is_map(self.health()):
            self.assertLess(time.monotonic(), deadline, log.read_text())
            time.sleep(0.1)
        after = self.health()
        self.assertEqual(after["pid"], started["pid"])
        self.assertEqual(after["runtime"], before["runtime"])
        self.assertEqual(self.map("--no-open")["status"], "existing")
        self.assert_no_installation_files()


class LaunchRaceTests(unittest.TestCase):
    def test_a_map_changing_under_richi_map_is_rescanned_instead_of_failing(self):
        with tempfile.TemporaryDirectory(prefix="richi-map-race-") as temporary:
            database = Path(temporary) / "memory.sqlite3"
            database.write_bytes(b"")
            refusal = maps.refusal("changed", 4321, pid=99999)
            outcomes = [launch_map.MapRefused(refusal), {"url": maps.url(4321), "status": "existing"}]

            def scan(*_arguments):
                outcome = outcomes.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

            with mock.patch.object(launch_map, "_launch_locked", side_effect=scan) as scanned, \
                    mock.patch.object(launch_map, "_settle") as settled:
                result = launch_map.launch(database, 4321, runtime=mock.sentinel.runtime)
            self.assertEqual(result["status"], "existing")
            self.assertEqual(scanned.call_count, 2)
            settled.assert_called_once_with(4321)
            for reason in ("unverified_process", "foreign_user"):
                with self.subTest(reason=reason):
                    failing = launch_map.MapRefused(maps.refusal(reason, 4321, pid=99999))
                    with mock.patch.object(launch_map, "_launch_locked", side_effect=failing), \
                            self.assertRaises(launch_map.MapRefused):
                        launch_map.launch(database, 4321, runtime=mock.sentinel.runtime)


if __name__ == "__main__":
    unittest.main()
