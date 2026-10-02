"""Exercise source reload with a disposable package and a real loopback server."""
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from richi import memory, serve


class MapDevelopmentTests(unittest.TestCase):
    def test_dev_compatibility_requires_same_installed_source(self):
        database = Path("/tmp/synthetic-memory.sqlite3")
        health = {"application": "project-memory-map", "database": str(database),
                  "api_version": serve.API_VERSION, "capabilities": serve.CAPABILITIES}
        self.assertTrue(serve.compatible_health(health, database))
        self.assertFalse(serve.compatible_health(health, database, dev=True))
        health["development"] = {"source": str(serve.BASE / "other"), "reload": True}
        self.assertFalse(serve.compatible_health(health, database, dev=True))
        health["development"]["source"] = str(serve.BASE)
        self.assertTrue(serve.compatible_health(health, database, dev=True))

    @unittest.skipIf(sys.platform == "win32", "Development launcher targets macOS and Linux")
    def test_source_reload_keeps_process_and_url_and_stops_cleanly(self):
        with tempfile.TemporaryDirectory(prefix="richi-map-dev-") as temporary:
            root = Path(temporary).resolve()
            package = root / "richi"
            shutil.copytree(Path(serve.__file__).parent, package,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            database = root / "memory.sqlite3"
            config = root / "settings.json"
            config.write_text(json.dumps({"database": str(database)}))
            with memory.connect(database, create=True) as connection:
                memory.initialize(connection, database)
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            environment = {key: value for key, value in os.environ.items() if not key.startswith("RICHI_")}
            environment["PYTHONPATH"] = str(root)
            bad_config = root / "broken.json"
            bad_config.write_text("not JSON")
            environment["RICHI_CONFIG"] = str(bad_config)
            command = [sys.executable, "-m", "richi", "--config", str(config)]
            with (root / "server.log").open("w+") as log:
                process = subprocess.Popen(command + ["map", "serve", "--dev", "--port", str(port)],
                                           cwd=root, env=environment, stdout=log, stderr=log)
                def request(path):
                    client = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                    try:
                        client.request("GET", path)
                        response = client.getresponse()
                        return response.status, response.getheader("Server"), response.read()
                    finally:
                        client.close()

                def wait_for_version(version):
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline and process.poll() is None:
                        try:
                            status, server, payload = request("/api/health")
                            if status == 200 and server.startswith(version):
                                return json.loads(payload)
                        except (OSError, http.client.HTTPException):
                            pass
                        time.sleep(0.1)
                    log.seek(0)
                    self.fail("Map did not reach " + version + ": " + log.read())

                try:
                    initial = wait_for_version("ProjectMemoryMap/4")
                    self.assertEqual(initial["database"], str(database))
                    self.assertEqual(initial["development"], {"reload": True, "source": str(package)})
                    source = package / "serve.py"
                    original = source.read_text()
                    original_stat = source.stat()
                    # Same length and timestamp reproduces stale bytecode: dev
                    # reload must import the new bytes rather than old .pyc files.
                    source.write_text(original.replace('server_version = "ProjectMemoryMap/4"',
                                                       'server_version = "ProjectMemoryMap/5"'))
                    os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
                    reloaded = wait_for_version("ProjectMemoryMap/5")
                    self.assertEqual(reloaded, initial)
                    self.assertIsNone(process.poll(), "Reload must retain the original process")
                    status, _server, payload = request("/api/graph")
                    self.assertEqual(status, 200, payload)
                    self.assertIn("nodes", json.loads(payload))
                    reused = subprocess.run(command + ["map", "--dev", "--no-open", "--port", str(port)],
                                            cwd=root, env=environment, capture_output=True, text=True, timeout=15)
                    self.assertEqual(reused.returncode, 0, reused.stderr)
                    self.assertEqual(json.loads(reused.stdout),
                                     {"status": "existing", "url": "http://127.0.0.1:%d/" % port})
                    # An incomplete save should not destroy the running map;
                    # fixing the syntax should allow the next reload.
                    source.write_text("this is invalid Python!\n")
                    deadline = time.monotonic() + 15
                    while "waiting for valid source" not in (root / "server.log").read_text():
                        self.assertIsNone(process.poll())
                        self.assertLess(time.monotonic(), deadline, "Invalid source was not detected")
                        time.sleep(0.1)
                    self.assertIsNone(process.poll())
                    self.assertEqual(request("/api/health")[0], 200)
                    source.write_text(original.replace('server_version = "ProjectMemoryMap/4"',
                                                       'server_version = "ProjectMemoryMap/6"'))
                    wait_for_version("ProjectMemoryMap/6")
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                    log.seek(0)
                    output = log.read()
                self.assertEqual(process.returncode, 0, output)
                self.assertIn("waiting for valid source", output)
                with socket.socket() as check:
                    self.assertNotEqual(check.connect_ex(("127.0.0.1", port)), 0)
                self.assertEqual(list((root / "runtime").glob("dev-cache-*")), [])


if __name__ == "__main__":
    unittest.main()
