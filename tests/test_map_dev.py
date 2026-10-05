"""Real runtime switching and reload with disposable installed/dev packages."""
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from richi import memory, serve
import richi_launcher
from richi_launcher.runtime import current_runtime, describe_runtime


class MapDevelopmentTests(unittest.TestCase):
    def test_compatibility_requires_runtime_mode_source_version_and_config(self):
        database = Path("/tmp/synthetic-memory.sqlite3")
        runtime = current_runtime()
        health = {"application": "project-memory-map", "database": str(database),
                  "api_version": serve.API_VERSION, "capabilities": serve.CAPABILITIES,
                  "runtime": runtime.as_dict(), "config_file": None}
        self.assertTrue(serve.compatible_health(health, database, runtime))
        for key, value in (("mode", "dev"), ("package", "/other/package"),
                           ("version", "99.0.0"), ("identity", "different"),
                           ("source", "/other/checkout")):
            changed = dict(health, runtime=dict(health["runtime"], **{key: value}))
            with self.subTest(key=key):
                self.assertFalse(serve.compatible_health(changed, database, runtime))
        self.assertFalse(serve.compatible_health(health, database, runtime, Path("/another/config.json")))

    @staticmethod
    def copied_installation(root):
        installed = root / "installed"
        checkout = root / "checkout"
        package = checkout / "src" / "richi"
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
        shutil.copytree(Path(serve.__file__).parent, installed / "richi", ignore=ignore)
        shutil.copytree(Path(richi_launcher.__file__).parent, installed / "richi_launcher", ignore=ignore)
        shutil.copytree(Path(serve.__file__).parent, package, ignore=ignore)
        (checkout / "pyproject.toml").write_text('[project]\nname = "richi"\nversion = "0.1.0"\n')
        return installed, checkout, package

    @staticmethod
    def isolated_command(installed):
        runner = "import sys;sys.path.insert(0,sys.argv[1]);from richi_launcher.cli import main;sys.exit(main(sys.argv[2:]))"
        return [sys.executable, "-I", "-B", "-c", runner, str(installed)]

    def test_graph_worker_uses_pinned_runtime_when_config_selects_another(self):
        with tempfile.TemporaryDirectory(prefix="richi-map-pinned-") as temporary:
            root = Path(temporary).resolve()
            _installed, checkout, package = self.copied_installation(root)
            graph_source = package / "graph.py"
            graph_source.write_text(graph_source.read_text().replace(
                'return {"schema_version": 2, "generated_at":',
                'return {"dev_fixture": True, "schema_version": 2, "generated_at":'))
            runtime = describe_runtime(package, "dev", checkout)
            database = root / "memory.sqlite3"
            config = root / "config.json"
            config.write_text(json.dumps({"database": str(database), "dev": False}))
            with memory.connect(database, create=True) as connection:
                memory.initialize(connection, database)
            server = serve.LoopbackHTTPServer(("127.0.0.1", 0),
                serve.handler_for(database, config, runtime, config_required=True))
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            client = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            try:
                client.request("GET", "/api/graph")
                response = client.getresponse()
                payload = json.loads(response.read())
                self.assertEqual(response.status, 200, payload)
                self.assertTrue(payload["dev_fixture"])
            finally:
                client.close()
                server.shutdown()
                server.server_close()
                worker.join(timeout=5)
            self.assertFalse(worker.is_alive())

    @unittest.skipIf(sys.platform == "win32", "Map runtime targets macOS and Linux")
    def test_missing_default_config_can_be_created_to_switch_running_map(self):
        with tempfile.TemporaryDirectory(prefix="richi-map-default-config-") as temporary:
            root = Path(temporary).resolve()
            installed, checkout, _package = self.copied_installation(root)
            database = root / "memory.sqlite3"
            with memory.connect(database, create=True) as connection:
                memory.initialize(connection, database)
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            environment = {key: value for key, value in os.environ.items() if not key.startswith("RICHI_") and key != "CODEX_THREAD_ID"}
            environment.update(HOME=str(root / "home"), XDG_CONFIG_HOME=str(root / "config"),
                               XDG_DATA_HOME=str(root / "data"))
            command = self.isolated_command(installed) + ["--db", str(database), "map", "serve", "--port", str(port)]
            log_path = root / "server.log"
            with log_path.open("w") as log:
                process = subprocess.Popen(command, cwd=root, env=environment, stdout=log, stderr=log)
                def wait_for(mode):
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline and process.poll() is None:
                        client = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                        try:
                            client.request("GET", "/api/health")
                            response = client.getresponse()
                            health = json.loads(response.read())
                            if response.status == 200 and health["runtime"]["mode"] == mode:
                                return health
                        except (OSError, ValueError, http.client.HTTPException):
                            pass
                        finally:
                            client.close()
                        time.sleep(0.1)
                    self.fail("Default-config transition failed: " + log_path.read_text())
                try:
                    health = wait_for("release")
                    config = Path(health["config_file"])
                    self.assertIn(root, config.parents)
                    self.assertFalse(config.exists())
                    config.parent.mkdir(parents=True, exist_ok=True)
                    config.write_text(json.dumps({"dev": True, "development": {"source": str(checkout)}}))
                    development = wait_for("dev")
                    self.assertEqual(development["database"], str(database))
                    self.assertIsNone(process.poll())
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                self.assertEqual(process.returncode, 0, log_path.read_text())

    @unittest.skipIf(sys.platform == "win32", "Development launcher targets macOS and Linux")
    def test_config_switch_and_source_reload_keep_process_url_and_fixed_release(self):
        with tempfile.TemporaryDirectory(prefix="richi-map-runtime-") as temporary:
            root = Path(temporary).resolve()
            installed, checkout, package = self.copied_installation(root)
            database = root / "memory.sqlite3"
            config = root / "settings.json"
            config.write_text(json.dumps({"database": str(database), "dev": False,
                                          "development": {"source": str(checkout)}}))
            with memory.connect(database, create=True) as connection:
                memory.initialize(connection, database)
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            environment = {key: value for key, value in os.environ.items() if not key.startswith("RICHI_") and key != "CODEX_THREAD_ID"}
            bad_config = root / "broken.json"
            bad_config.write_text("not JSON")
            environment["RICHI_CONFIG"] = str(bad_config)
            # Exercise an isolated fixed installation, independently of the
            # interpreter/environment used to run this test suite.
            command = self.isolated_command(installed) + ["--config", str(config)]
            log_path = root / "server.log"
            with log_path.open("w+") as log:
                process = subprocess.Popen(command + ["map", "serve", "--port", str(port)],
                                           cwd=root, env=environment, stdout=log, stderr=log)
                def request(path):
                    client = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                    try:
                        client.request("GET", path)
                        response = client.getresponse()
                        return response.status, response.getheader("Server"), response.read()
                    finally:
                        client.close()

                def wait_for_runtime(mode, server_version="ProjectMemoryMap/4"):
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline and process.poll() is None:
                        try:
                            status, server, payload = request("/api/health")
                            health = json.loads(payload)
                            if status == 200 and server.startswith(server_version) and health["runtime"]["mode"] == mode:
                                return health
                        except (OSError, ValueError, http.client.HTTPException):
                            pass
                        time.sleep(0.1)
                    self.fail("Map did not reach " + mode + ": " + log_path.read_text())

                def change_mode(value):
                    changed = subprocess.run(command + ["config", "set", "dev", value],
                                             cwd=root, env=environment, capture_output=True, text=True, timeout=10)
                    self.assertEqual(changed.returncode, 0, changed.stderr)

                def assert_reused():
                    reused = subprocess.run(command + ["map", "--no-open", "--port", str(port)],
                                            cwd=root, env=environment, capture_output=True, text=True, timeout=20)
                    self.assertEqual(reused.returncode, 0, reused.stderr + log_path.read_text())
                    self.assertEqual(json.loads(reused.stdout),
                                     {"status": "existing", "url": "http://127.0.0.1:%d/" % port})
                    self.assertIsNone(process.poll(), "Switch/reload must retain the original process")

                try:
                    fixed = wait_for_runtime("release")
                    self.assertEqual(fixed["runtime"]["package"], str(installed / "richi"))
                    self.assertEqual(fixed["database"], str(database))
                    self.assertEqual(fixed["config_file"], str(config))
                    change_mode("true")
                    # Launch immediately while the old server is still watching
                    # the edit. It must wait/reuse, never create another map.
                    assert_reused()
                    initial = wait_for_runtime("dev")
                    self.assertEqual(initial["runtime"]["package"], str(package))
                    self.assertEqual(initial["runtime"]["source"], str(checkout))
                    source = package / "serve.py"
                    original = source.read_text()
                    original_stat = source.stat()
                    source.write_text(original.replace('server_version = "ProjectMemoryMap/4"',
                                                       'server_version = "ProjectMemoryMap/5"'))
                    os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
                    reloaded = wait_for_runtime("dev", "ProjectMemoryMap/5")
                    self.assertEqual(reloaded, initial)
                    self.assertIsNone(process.poll())
                    status, _server, payload = request("/api/graph")
                    self.assertEqual(status, 200, payload)
                    self.assertIn("nodes", json.loads(payload))
                    # Syntactically valid but broken imports must fail preflight
                    # before the working server is stopped. Stable config CLI
                    # must still recover to the fixed release.
                    source.write_text('raise RuntimeError("broken development import")\n' + original)
                    deadline = time.monotonic() + 20
                    while "broken development import" not in log_path.read_text():
                        self.assertIsNone(process.poll())
                        self.assertLess(time.monotonic(), deadline, "Broken runtime was not rejected")
                        time.sleep(0.1)
                    self.assertEqual(request("/api/health")[0], 200)
                    change_mode("false")
                    shutil.rmtree(checkout)
                    assert_reused()
                    restored = wait_for_runtime("release")
                    self.assertEqual(restored, fixed)
                    status, _server, payload = request("/api/graph")
                    self.assertEqual(status, 200, payload)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                    output = log_path.read_text()
                self.assertEqual(process.returncode, 0, output)
                self.assertIn("Runtime reload waiting", output)
                with socket.socket() as check:
                    self.assertNotEqual(check.connect_ex(("127.0.0.1", port)), 0)


if __name__ == "__main__":
    unittest.main()
