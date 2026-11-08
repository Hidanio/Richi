"""Stop, restart and replace running maps without signalling unverified processes."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import pty
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

from richi import cli_parser, launch_map, memory, serve
import richi_bootstrap
import richi_launcher
from richi_launcher import maps

from cli_environment import cli_environment, free_port


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
        accepted = [self.runner("serve", "--db", "/x", "--port", "1"),
                    self.runner("cli", "--db", "/x", "map", "serve", "--port", "1"),
                    self.runner("cli", "--db", "/x", "--config", "/c", "map", "--port", "8766", "serve"),
                    self.runner("cli", "--db=/x", "--compact-json", "-wteam", "map", "--port=1", "serve"),
                    self.runner("cli", "--project-path", "/p", "map", "--open", "serve")]
        development = self.runner("cli", "--db", "/x", "map", "serve")
        development[3:3] = ["-X", "pycache_prefix=/tmp/richi-bytecode-1-2"]
        for arguments in accepted + [development]:
            with self.subTest(arguments=arguments):
                self.assertEqual(maps.runner_launcher(arguments, self.runtime), "/synthetic/site")
        rejected = [self.runner("cli", "--db", "/x", "recall", "map"),
                    self.runner("cli", "--db", "/x", "recall", "map", "serve"),
                    self.runner("cli", "--db", "/x", "entry", "put", "map", "serve"),
                    self.runner("cli", "--db", "/x", "map", "--no-open"),
                    self.runner("cli", "--db", "/x", "map", "stop", "serve"),
                    self.runner("cli", "--unknown", "map", "serve"),
                    self.runner("git_worker"),
                    self.runner("serve", runtime=dict(self.runtime, identity="b" * 64)),
                    ["/bin/sleep", "60"],
                    ["python", "-c", "print()", "/synthetic/site", json.dumps(self.runtime), "serve"],
                    ["python", "-I", "-c", self.runner("serve")[4]] + self.runner("serve")[5:]]
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

    @unittest.skipUnless(sys.platform == "darwin" or sys.platform.startswith("linux"), "Process terminals")
    def test_a_map_attached_to_a_terminal_is_told_apart_from_a_background_map(self):
        background = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
        self.addCleanup(background.wait)
        self.addCleanup(background.kill)
        pid, controller = pty.fork()
        if pid == 0:
            os.execv("/bin/sleep", ["/bin/sleep", "30"])
        self.addCleanup(os.close, controller)
        self.addCleanup(os.waitpid, pid, 0)
        self.addCleanup(os.kill, pid, signal.SIGKILL)
        self.assertEqual(maps.process_owner(background.pid), (os.getuid(), False))
        self.assertEqual(maps.process_owner(pid), (os.getuid(), True))


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
            # Nothing of this workspace runs there; the other program is left alone.
            self.assertEqual(self.map("stop"), {"status": "not_running", "url": maps.url(self.port),
                                                "note": "Another program uses this port; it was not touched"})
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
                    if reason == "other_workspace":
                        self.assertEqual(self.map("stop")["note"], "The map on this port serves another "
                                         "workspace; select that workspace to stop it")
                    else:
                        refused = self.map("stop", ok=False)
                        self.assertEqual((refused["status"], refused["reason"]), ("refused", reason))
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

    def test_a_handoff_request_without_an_installation_change_is_ignored(self):
        # Managed handoffs are covered by test_update_e2e; here the signal alone
        # must neither restart nor terminate the map.
        started = self.map("--no-open")
        before = self.health()
        os.kill(started["pid"], signal.SIGUSR1)
        deadline = time.monotonic() + 20
        log = self.root / "runtime" / "map-server.log"
        while "Restart request ignored" not in log.read_text():
            self.assertLess(time.monotonic(), deadline, log.read_text())
            time.sleep(0.1)
        self.assertEqual(self.health(), before)
        self.assertNotIn("restarting map", log.read_text())
        self.assertEqual(self.map("--no-open")["status"], "existing")
        self.assert_no_installation_files()

    def test_a_map_whose_launcher_cannot_hand_off_does_not_advertise_it(self):
        # A development runtime paired with an installed launcher from before
        # the handoff, or with one speaking another map protocol: the signal's
        # default action would terminate that map.
        runner = "import sys;sys.path.insert(0,sys.argv[1]);from richi_launcher.cli import main;sys.exit(main(sys.argv[2:]))"
        for variant in ("without_maps", "other_protocol"):
            with self.subTest(variant=variant):
                site = self.copied_launcher("0.0.2-" + variant.replace("_", "-"))
                package = site / "richi_launcher" / "maps"
                if variant == "without_maps":
                    shutil.rmtree(package)
                else:
                    module = package / "protocol.py"
                    source = module.read_text()
                    self.assertIn("\nAPI_VERSION = %d\n" % maps.API_VERSION, source)
                    module.write_text(source.replace("\nAPI_VERSION = %d\n" % maps.API_VERSION, "\nAPI_VERSION = 99\n"))
                log_path = self.root / (variant + ".log")
                log = log_path.open("w")
                self.addCleanup(log.close)
                old = subprocess.Popen([sys.executable, "-I", "-B", "-c", runner, str(site), "--config",
                                        str(self.config), "map", "serve", "--port", str(self.port)],
                                       env=self.environment, stdout=log, stderr=log, cwd=self.root)
                self.addCleanup(lambda process=old: process.poll() is None and process.terminate())
                deadline = time.monotonic() + 20
                while not maps.is_map(self.health()):
                    self.assertIsNone(old.poll(), log_path.read_text())
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.1)
                self.assertNotIn(maps.HANDOFF_CAPABILITY, self.health()["capabilities"])
                self.assertEqual(self.health()["launcher"], str(site))
                old.terminate()
                self.assertEqual(old.wait(timeout=10), 0)
                while not maps.port_free(self.port):
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.05)


class InstallationHookTests(unittest.TestCase):
    """The first command of a generation never waits for maps and starts servers only from a terminal."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-map-hook-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "state.json").write_text(json.dumps({"current": "g2"}))
        environment = mock.patch.dict(os.environ, {maps.ROOT_ENV: str(self.root), maps.GENERATION_ENV: "g2"})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(maps.PASSIVE_ENV, None)
        self.marker = self.root / maps.REGISTRY / maps.MARKER

    def run_hook(self, port, interactive, found):
        with mock.patch.object(maps.reconcile, "nudge", return_value=found) as nudge:
            maps.first_invocation(port, interactive=interactive)
        return nudge

    def test_pending_replacements_wait_for_a_terminal_and_each_range_is_scanned_once(self):
        old = maps.reconcile.Report(9000, 4242, maps.Status.RESTART_REQUIRED, error="older map")
        agent = self.run_hook(9000, False, [old])
        self.assertEqual(set(agent.call_args.args[0]), set(range(9000, 9011)))
        self.assertEqual(agent.call_args.kwargs, {"replace": False})
        recorded = json.loads(self.marker.read_text())
        self.assertEqual((recorded["generation"], recorded["scanned"], recorded["pending"]), ("g2", [9000], [9000]))
        self.assertFalse(self.run_hook(9000, False, []).called)
        # A terminal command for another workspace's port also takes the pending one.
        terminal = self.run_hook(8800, True, [maps.reconcile.Report(9000, 4242, maps.Status.REPLACING)])
        self.assertEqual(set(terminal.call_args.args[0]), set(range(8800, 8811)) | {9000})
        self.assertEqual(terminal.call_args.kwargs, {"replace": True})
        recorded = json.loads(self.marker.read_text())
        self.assertEqual((recorded["scanned"], recorded["pending"]), ([8800, 9000], []))
        self.assertEqual(recorded["maps"], [{"url": maps.url(9000), "pid": 4242, "status": "replacing",
                                             "from_version": None, "from_generation": None}])
        self.assertFalse(self.run_hook(9000, True, []).called)
        self.assertFalse(self.run_hook(8800, True, []).called)

    def test_maps_in_a_terminal_are_not_pending_and_interrupted_scans_repeat(self):
        foreground = maps.reconcile.Report(9000, 4242, maps.Status.RESTART_REQUIRED, terminal=True)
        self.run_hook(9000, False, [foreground])
        self.assertEqual(json.loads(self.marker.read_text())["pending"], [])
        with mock.patch.object(maps.reconcile, "nudge", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            maps.first_invocation(7000, interactive=True)
        self.assertEqual(json.loads(self.marker.read_text())["scanned"], [9000])
        self.assertTrue(self.run_hook(7000, True, []).called)

    def test_entry_point_checks_and_other_generations_never_scan(self):
        with mock.patch.dict(os.environ, {maps.PASSIVE_ENV: "1"}):
            self.assertFalse(self.run_hook(9000, True, []).called)
        with mock.patch.dict(os.environ, {maps.GENERATION_ENV: "g1"}):
            self.assertFalse(self.run_hook(9000, True, []).called)
        self.assertFalse(self.marker.exists())

    def test_richi_map_waits_while_a_registered_map_reexecutes(self):
        sleeper = subprocess.Popen(["/bin/sleep", "30"])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        port = free_port()
        directory = self.root / maps.REGISTRY
        directory.mkdir()
        entry = directory / ("%d.json" % port)
        entry.write_text(json.dumps({"pid": sleeper.pid, "port": port, "restarting": int(time.time())}))
        started = time.monotonic()
        self.assertEqual(maps.wait_for_restarts([port], timeout=0.6), [port])
        self.assertGreaterEqual(time.monotonic() - started, 0.5)
        sleeper.kill()
        sleeper.wait()
        started = time.monotonic()
        self.assertEqual(maps.wait_for_restarts([port], timeout=5), [])
        self.assertLess(time.monotonic() - started, 1)
        entry.write_text(json.dumps({"pid": os.getpid(), "port": port}))
        started = time.monotonic()
        self.assertEqual(maps.wait_for_restarts([port], timeout=5), [])
        self.assertLess(time.monotonic() - started, 1)

    def test_a_restart_mark_older_than_any_restart_is_ignored(self):
        sleeper = subprocess.Popen(["/bin/sleep", "30"])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        port = free_port()
        (self.root / maps.REGISTRY).mkdir()
        (self.root / maps.REGISTRY / ("%d.json" % port)).write_text(json.dumps(
            {"pid": sleeper.pid, "restarting": int(time.time()) - maps.protocol.RESTART_STALE - 1}))
        started = time.monotonic()
        self.assertEqual(maps.wait_for_restarts([port], timeout=5), [])
        self.assertLess(time.monotonic() - started, 1)


class RestartingRefusalTests(unittest.TestCase):
    """richi map never starts a second map while one of the range is still re-executing."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-map-restarting-")
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "memory.sqlite3"
        self.database.write_bytes(b"")
        self.port = free_port()
        self.health = {"application": maps.APPLICATION, "pid": 4242, "runtime": {"identity": "x"}}
        start = mock.patch.object(launch_map, "_start")
        self.start = start.start()
        self.addCleanup(start.stop)

    def scan(self, restarting, own=False, quick=()):
        answers = {self.port: self.health} if own else {}
        return [mock.patch.object(maps, "wait_for_restarts",
                                  side_effect=lambda _ports, timeout=None: list(quick) if timeout == 0 else restarting),
                mock.patch.object(launch_map, "read_health", side_effect=lambda port: answers.get(port)),
                mock.patch.object(launch_map, "compatible_health", side_effect=lambda health, *_a: health is not None),
                mock.patch.object(launch_map, "managed_health", side_effect=lambda health, *_a: health is not None)]

    def run_with(self, patches, call):
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return call()

    def test_without_its_own_map_richi_map_and_stop_refuse(self):
        with self.assertRaisesRegex(RuntimeError, "перезапускается"):
            self.run_with(self.scan([self.port + 1]), lambda: launch_map.launch(
                self.database, self.port, runtime=mock.sentinel.runtime))
        with self.assertRaisesRegex(RuntimeError, "перезапускается"):
            launch_map.stop(self.database, self.port)
        self.assertFalse(self.start.called)

    def test_another_workspace_restarting_does_not_block_this_map(self):
        self.assertEqual(self.run_with(self.scan([self.port + 1], own=True), lambda: launch_map.launch(
            self.database, self.port, runtime=mock.sentinel.runtime))["status"], "existing")
        stopped = maps.Stopped(self.port, 4242, "1", True)
        with mock.patch.object(maps, "stop", return_value=stopped) as stop:
            self.assertEqual(launch_map.stop(self.database, self.port), stopped.as_dict())
        stop.assert_called_once_with(self.port, self.health)

    def test_a_map_that_began_to_restart_during_the_scan_is_looked_at_again(self):
        with self.assertRaises(launch_map.MapRefused) as refused:
            self.run_with(self.scan([], quick=[self.port]), lambda: launch_map._launch_locked(
                self.database, self.port, None, mock.sentinel.runtime, False, None, False, Path("/unused")))
        self.assertIs(refused.exception.reason, maps.Reason.CHANGED)
        self.assertTrue(refused.exception.retryable)
        self.assertFalse(self.start.called)


class FollowerTests(unittest.TestCase):
    """When a map re-executes on its installation's current generation."""

    def setUp(self):
        environment = mock.patch.dict(os.environ, {maps.ROOT_ENV: "/synthetic/root", maps.GENERATION_ENV: "g1",
                                                   maps.GRACE_ENV: "600"})
        environment.start()
        self.addCleanup(environment.stop)
        self.current = "g1"
        patcher = mock.patch.object(maps, "current_generation", side_effect=lambda _root: self.current)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.capabilities = list(serve.CAPABILITIES)
        self.follower = serve.InstallationFollower(Path("/synthetic/memory.sqlite3"), ["--db", "/synthetic/memory.sqlite3"],
                                                   ["map", "serve", "--port", "1"], {}, self.capabilities)

    def due(self, command=("python", "entry"), problem=None, features=frozenset()):
        handoff = maps.Handoff(command, features) if command is not None else None
        with mock.patch.object(maps, "handoff_command", return_value=(handoff, problem)) as prepared, \
                mock.patch("sys.stdout"):
            return self.follower.due(), prepared

    def test_requests_follow_only_a_new_generation_after_the_entry_point_check(self):
        self.follower.requested.set()
        self.assertEqual(self.due()[0], False)  # Same generation: ignored.
        self.assertFalse(self.follower.requested.is_set())
        self.current = "g2"
        self.assertEqual(self.due()[0], False)  # No request yet and the grace has not passed.
        self.follower.requested.set()
        followed, prepared = self.due()
        self.assertTrue(followed)
        self.assertEqual(prepared.call_args.args[:3], (["--db", "/synthetic/memory.sqlite3"],
                                                        ["map", "serve", "--port", "1"], {}))
        self.assertEqual((self.follower.handoff.command, self.follower.reason), (("python", "entry"), "Restart requested"))
        self.assertFalse(self.follower.takes_listener())

    def test_the_socket_goes_only_to_the_runtime_that_answered_for_the_current_generation(self):
        self.current = "g2"
        self.follower.requested.set()
        self.assertTrue(self.due(features=frozenset({maps.LISTEN_FD}))[0])
        self.assertTrue(self.follower.takes_listener())
        # Another change while requests finish: the entry point would start a runtime nobody checked.
        self.current = "g3"
        self.assertFalse(self.follower.takes_listener())

    def test_a_release_that_cannot_start_is_not_followed_and_rechecked_later(self):
        self.current = "g2"
        self.follower.requested.set()
        self.assertEqual(self.due(None, "the current release did not start: boom")[0], False)
        self.follower.requested.set()
        followed, prepared = self.due()
        self.assertFalse(followed)
        self.assertFalse(prepared.called)
        self.follower.blocked = ("g2", time.monotonic() - serve.HANDOFF_RECHECK, "old")
        self.follower.requested.set()
        self.assertTrue(self.due()[0])

    def test_a_launcher_it_cannot_use_withdraws_the_capability(self):
        self.current = "g2"
        self.follower.requested.set()
        with mock.patch.object(maps, "handoff_command", side_effect=TypeError("changed API")), \
                mock.patch("sys.stdout"):
            self.assertFalse(self.follower.due())
        self.assertNotIn(maps.HANDOFF_CAPABILITY, self.capabilities)
        self.follower.requested.set()
        self.assertEqual(self.due()[0], False)


class HandoffCommandTests(unittest.TestCase):
    def check(self, code, managed=True):
        command = [sys.executable, "-I", "-c", code]
        with mock.patch.object(maps.handoff, "handoff_problem", return_value=None), \
                mock.patch.object(maps.handoff, "stable_command", return_value=(command, managed)):
            return maps.handoff_command(["--db", "/x"], ["map", "serve"],
                                        {maps.LEASE_ENV: "7", "PATH": os.environ.get("PATH", "")})

    def test_the_entry_point_must_start_the_release_before_a_map_replaces_itself(self):
        passive = ("import os, sys; sys.exit(0 if os.environ.get('RICHI_MAPS_PASSIVE') == '1' "
                   "and 'RICHI_INSTALLATION_LEASE_FD' not in os.environ else 4)")
        handoff, problem = self.check(passive)
        self.assertIsNone(problem)
        self.assertEqual((handoff.command[-1], handoff.features), (passive, frozenset()))
        self.assertEqual(self.check("import sys; sys.stderr.write('broken'); sys.exit(1)"),
                         (None, "the current release did not start: broken"))
        self.assertEqual(self.check("pass", managed=False),
                         (None, "the installation entry point is unavailable; repair the installation"))
        with mock.patch.object(maps.handoff, "handoff_problem", return_value="release 9 is incomplete"):
            self.assertEqual(maps.handoff_command([], [], {}), (None, "release 9 is incomplete"))

    def test_the_check_tells_which_runtimes_take_over_the_listening_socket(self):
        # This runtime answers the check; 1.1.0 and earlier say nothing.
        answering = ("import os, sys; sys.path[:0] = [sys.argv[1], sys.argv[2]]; from richi import cli_parser, memory; "
                     "assert os.environ.get(cli_parser.MAP_PROBE_ENV) == '1'; memory.main(['--version'])")
        command = [sys.executable, "-I", "-c", answering, str(Path(launch_map.__file__).parents[1]),
                   str(Path(maps.__file__).parents[2])]
        with mock.patch.object(maps.handoff, "handoff_problem", return_value=None), \
                mock.patch.object(maps.handoff, "stable_command", return_value=(command, True)):
            handoff, problem = maps.handoff_command([], [], {"PATH": os.environ.get("PATH", "")})
        self.assertIsNone(problem)
        self.assertEqual(handoff.features, frozenset({maps.LISTEN_FD}))
        self.assertEqual(maps.handoff.declared_features("Richi 1.1.0\nrichi-map-handoff: listen_fd other\n"),
                         {"listen_fd", "other"})
        self.assertEqual(maps.handoff.declared_features("richi-map-handoff:\n"), frozenset())


class ListenerHandoffTests(unittest.TestCase):
    """A map takes over only the loopback socket its previous process left listening on its port."""

    def setUp(self):
        self.port = free_port()
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)

    def listening(self, port=None, listen=True):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        sock.bind(("127.0.0.1", port or self.port))
        if listen:
            sock.listen(1)
        return sock

    def inherit(self, descriptor):
        os.environ[serve.LISTEN_FD_ENV] = str(descriptor)
        listener = serve.inherited_listener(self.port)
        self.assertNotIn(serve.LISTEN_FD_ENV, os.environ)
        return listener

    def test_the_listening_socket_is_taken_over_and_served(self):
        descriptor = os.dup(self.listening().fileno())
        os.set_inheritable(descriptor, True)
        listener = self.inherit(descriptor)
        self.assertEqual((listener.fileno(), listener.getsockname()), (descriptor, ("127.0.0.1", self.port)))
        self.assertFalse(os.get_inheritable(descriptor))

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(204)
                self.end_headers()

            def log_message(self, *_args):
                pass

        server = serve.LoopbackHTTPServer(("127.0.0.1", self.port), Handler, listener=listener)
        self.addCleanup(server.server_close)
        self.assertIs(server.socket, listener)
        self.assertEqual(server.server_port, self.port)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        connection = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        with connection:
            connection.sendall(b"GET / HTTP/1.0\r\n\r\n")
            self.assertTrue(connection.recv(64).startswith(b"HTTP/1.0 204"))

    def test_clients_that_gave_up_while_the_map_restarted_are_not_logged_as_errors(self):
        server = serve.LoopbackHTTPServer(("127.0.0.1", self.port), BaseHTTPRequestHandler)
        self.addCleanup(server.server_close)
        for error, logged in ((BrokenPipeError(32, "Broken pipe"), False),
                              (ConnectionResetError(54, "reset"), False), (ValueError("bug"), True)):
            with self.subTest(error=type(error).__name__), mock.patch("sys.stderr") as stderr:
                try:
                    raise error
                except Exception:
                    server.handle_error(None, ("127.0.0.1", 1))
                self.assertEqual(stderr.write.called, logged)

    def test_anything_else_is_left_alone_and_the_port_is_bound_as_usual(self):
        self.assertIsNone(serve.inherited_listener(self.port))
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        self.addCleanup(os.close, write)
        other = free_port()
        # A connection that happens to use this port as its local address.
        server = self.listening(other)
        client = self.listening(listen=False)
        client.connect(("127.0.0.1", other))
        # Freed last, so no descriptor opened above reuses its number.
        closed = os.dup(write)
        os.close(closed)
        for name, descriptor in (("pipe", read), ("closed", closed), ("not a number", "x"),
                                 ("other port", server.fileno()), ("connected", client.fileno())):
            with self.subTest(name):
                self.assertIsNone(self.inherit(descriptor))
                if name in {"pipe", "other port", "connected"}:
                    os.fstat(descriptor)  # Still open: it was not ours to close.


@unittest.skipUnless(hasattr(signal, "SIGUSR1"), "The handoff signal")
class RestartSequenceTests(unittest.TestCase):
    """What a map does between deciding to re-execute and exec (exec itself is recorded)."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-map-restart-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.database = self.root / "memory.sqlite3"
        self.config = self.root / "config.json"
        self.config.write_text("{}", encoding="utf-8")
        with memory.connect(self.database, create=True) as connection:
            memory.initialize(connection, self.database)
        installation = self.root / "installation"
        installation.mkdir()
        (installation / "state.json").write_text(json.dumps({"current": "g2"}))
        self.port = free_port()
        self.entry = installation / maps.REGISTRY / ("%d.json" % self.port)
        environment = mock.patch.dict(os.environ, {maps.ROOT_ENV: str(installation), maps.GENERATION_ENV: "g1"})
        environment.start()
        self.addCleanup(environment.stop)
        previous = signal.getsignal(signal.SIGUSR1)
        self.addCleanup(signal.signal, signal.SIGUSR1, previous)
        self.events = []

    def restart(self, listener_answers, terminate_during_drain=False):
        events, answers = self.events, list(listener_answers)

        class Follower:
            reason, handoff = "Installation changed", maps.Handoff(["/synthetic/entry"], frozenset({maps.LISTEN_FD}))

            def __init__(self, *_arguments):
                self.requested = threading.Event()

            def due(self):
                return True

            def takes_listener(self):
                return answers.pop(0) if len(answers) > 1 else answers[0]

        real_mark, real_close = maps.mark_restarting, serve.LoopbackHTTPServer.server_close

        def mark(path, pid=None):
            events.append("mark")
            real_mark(path, pid)

        def close(server):
            events.append("drain")
            if terminate_during_drain:
                os.kill(os.getpid(), signal.SIGTERM)
            real_close(server)

        with mock.patch.object(serve, "InstallationFollower", Follower), \
                mock.patch.object(maps, "mark_restarting", side_effect=mark), \
                mock.patch.object(serve.LoopbackHTTPServer, "server_close", close), \
                mock.patch("os.execve") as execve, mock.patch("sys.stdout"):
            self.assertEqual(serve.main(["--db", str(self.database), "--config", str(self.config),
                                         "--port", str(self.port)]), 0)
        return execve

    def test_the_entry_is_marked_before_requests_drain_and_the_socket_crosses_exec(self):
        execve = self.restart([True])
        self.assertEqual(self.events, ["mark", "drain"])
        self.assertIn("restarting", json.loads(self.entry.read_text()))
        command, environment = execve.call_args.args[1], execve.call_args.args[2]
        self.assertEqual(command, ["/synthetic/entry"])
        descriptor = int(environment[maps.LISTEN_FD_ENV])
        self.addCleanup(os.close, descriptor)
        self.assertTrue(os.get_inheritable(descriptor))
        listener = socket.socket(fileno=os.dup(descriptor))
        with listener:
            self.assertEqual(listener.getsockname(), ("127.0.0.1", self.port))
        self.assertFalse(maps.port_free(self.port))

    def test_after_another_installation_change_the_port_closes_instead(self):
        execve = self.restart([True, False])
        self.assertNotIn(maps.LISTEN_FD_ENV, execve.call_args.args[2])
        self.assertTrue(maps.port_free(self.port))

    def test_a_stop_during_the_drain_wins_and_removes_the_entry(self):
        execve = self.restart([True], terminate_during_drain=True)
        self.assertFalse(execve.called)
        self.assertFalse(self.entry.exists())
        self.assertTrue(maps.port_free(self.port))


class ProtocolTests(unittest.TestCase):
    """The runtime and the launcher agree on one map protocol, defined once on each side."""

    def test_both_sides_use_the_same_names_ranges_and_version(self):
        self.assertEqual((serve.LEASE_ENV, serve.ROOT_ENV, serve.GENERATION_ENV),
                         (richi_bootstrap.LEASE_ENV, richi_bootstrap.ROOT_ENV, richi_bootstrap.GENERATION_ENV))
        self.assertEqual((maps.LEASE_ENV, maps.ROOT_ENV, maps.GENERATION_ENV),
                         (serve.LEASE_ENV, serve.ROOT_ENV, serve.GENERATION_ENV))
        self.assertEqual(launch_map.PORT_RANGE, maps.PORT_RANGE)
        self.assertEqual(serve.MAPS_API, maps.API_VERSION)
        # Between two map processes, whichever launchers they run with.
        self.assertEqual((maps.PROBE_ENV, maps.PROBE_ANSWER, maps.LISTEN_FD, maps.LISTEN_FD_ENV),
                         (cli_parser.MAP_PROBE_ENV, serve.PROBE_ANSWER, serve.LISTEN_FD, serve.LISTEN_FD_ENV))
        self.assertIn(maps.LISTEN_FD, serve.HANDOFF_FEATURES)
        self.assertIs(serve.maps, maps)
        self.assertIs(launch_map.maps, maps)
        # Waiting less than a map spends on its check makes callers give up on it.
        self.assertGreater(maps.HANDOFF_TIMEOUT, maps.HANDOFF_CHECK_TIMEOUT + 5)

    def test_a_launcher_speaking_another_protocol_is_treated_as_absent(self):
        self.assertIs(serve._launcher_maps(), maps)
        # A launcher without the module is covered with a real map in MapControlTests.
        with mock.patch.object(maps, "API_VERSION", serve.MAPS_API + 1):
            self.assertIsNone(serve._launcher_maps())


class InPlaceHandoffTests(unittest.TestCase):
    """richi map waits for a map switching in place as long as its own check may take."""

    health = {"application": maps.APPLICATION, "pid": 4242, "capabilities": [maps.HANDOFF_CAPABILITY],
              "runtime": {"identity": "old", "version": "0.1.0"}, "installation_generation": "g1"}

    def hand_off(self, answers):
        answers, started = list(answers), time.monotonic()
        self.addCleanup(lambda: self.assertLess(time.monotonic() - started, 5,
                                                "The wait must come from maps.HANDOFF_TIMEOUT"))
        patches = [mock.patch.object(maps, "HANDOFF_TIMEOUT", 0.3),
                   mock.patch.object(maps, "running_current_generation", return_value=True),
                   mock.patch.object(maps, "inspect", return_value=(maps.Verified("/synthetic/launcher", False), None)),
                   mock.patch.object(maps, "installation_owns", return_value=True),
                   mock.patch.object(maps, "handoff_problem", return_value=None),
                   mock.patch.object(maps, "request_handoff", return_value=None),
                   mock.patch.object(launch_map, "database_schema", return_value=3),
                   mock.patch.object(launch_map, "read_health",
                                     side_effect=lambda _port: answers.pop(0) if len(answers) > 1 else answers[0]),
                   mock.patch.object(launch_map, "compatible_health",
                                     side_effect=lambda health, *_args: bool(health and health.get("compatible")))]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return launch_map._hand_off_in_place(4321, self.health, Path("/synthetic/memory.sqlite3"), None, None)

    def test_a_switched_map_is_reported_with_its_previous_process(self):
        switched = dict(self.health, compatible=True, installation_generation="g2")
        result = self.hand_off([None, switched])
        self.assertEqual((result["status"], result["pid"], result["previous"]),
                         ("restarted", 4242, {"pid": 4242, "version": "0.1.0", "exited": False}))

    def test_a_map_that_kept_its_version_is_replaced_and_a_moving_one_rescanned(self):
        self.assertIsNone(self.hand_off([dict(self.health)]))
        # Each of these alone means the port no longer holds the map that was asked.
        for moving in (None, dict(self.health, pid=4243), dict(self.health, installation_generation="g2"),
                       dict(self.health, runtime={"identity": "new", "version": "0.1.0"})):
            with self.subTest(moving=moving):
                with self.assertRaises(launch_map.MapRefused) as refused:
                    self.hand_off([moving])
                self.assertIs(refused.exception.reason, maps.Reason.CHANGED)


class StartFailureTests(unittest.TestCase):
    def test_the_error_names_what_this_map_wrote_to_the_shared_log(self):
        with tempfile.TemporaryDirectory(prefix="richi-map-log-") as temporary:
            log = Path(temporary) / "map-server.log"
            log.write_text("Earlier map: unrelated\n")
            offset = log.stat().st_size
            self.assertEqual(launch_map._reason(log, offset), "")
            with log.open("a") as stream:
                stream.write("Project memory map: http://127.0.0.1:1/\n\n"
                             '{"error": "Cannot listen on port 1: [Errno 48] Address already in use."}\n')
            self.assertEqual(launch_map._reason(log, offset),
                             ': {"error": "Cannot listen on port 1: [Errno 48] Address already in use."}')
            self.assertEqual(launch_map._reason(Path(temporary) / "missing.log", 0), "")

    def test_only_maps_reporting_another_launcher_are_not_waited_for(self):
        runtime = {"mode": "release", "package": str(launch_map.LAUNCHER_ROOT / "richi")}
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(serve.GENERATION_ENV, None)
            self.assertTrue(launch_map.follows_itself({"runtime": runtime}))
            self.assertTrue(launch_map.follows_itself({"runtime": runtime, "launcher": str(launch_map.LAUNCHER_ROOT)}))
            self.assertFalse(launch_map.follows_itself({"runtime": runtime, "launcher": "/other/site"}))


class LaunchRaceTests(unittest.TestCase):
    def test_a_map_changing_under_richi_map_is_rescanned_instead_of_failing(self):
        with tempfile.TemporaryDirectory(prefix="richi-map-race-") as temporary:
            database = Path(temporary) / "memory.sqlite3"
            database.write_bytes(b"")
            outcomes = [maps.Refused(maps.Reason.CHANGED, 4321, 99999), {"url": maps.url(4321), "status": "existing"}]

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
            self.assertIs(launch_map.MapRefused, maps.Refused)
            for reason in (maps.Reason.UNVERIFIED_PROCESS, maps.Reason.FOREIGN_USER):
                with self.subTest(reason=reason):
                    failing = maps.Refused(reason, 4321, 99999)
                    with mock.patch.object(launch_map, "_launch_locked", side_effect=failing), \
                            self.assertRaises(launch_map.MapRefused):
                        launch_map.launch(database, 4321, runtime=mock.sentinel.runtime)


if __name__ == "__main__":
    unittest.main()
