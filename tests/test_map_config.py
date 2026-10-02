"""Explicit configuration must survive both detached map and graph workers."""
from contextlib import redirect_stdout
import http.client
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import urlsplit

from richi import memory, serve


@unittest.skipIf(sys.platform == "win32", "Detached launcher currently uses flock")
class MapConfigTests(unittest.TestCase):
    def test_foreground_startup_and_health_do_not_require_reverse_dns(self):
        with tempfile.TemporaryDirectory(prefix="richi-map-dns-") as temporary:
            root = Path(temporary).resolve()
            database = root / "memory.sqlite3"
            config = root / "config.json"
            config.write_text("{}", encoding="utf-8")
            with memory.connect(database, create=True) as connection:
                memory.initialize(connection, database)
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]

            def check_health(server):
                worker = threading.Thread(target=server.handle_request, daemon=True)
                server.timeout = 5
                worker.start()
                client = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    client.request("GET", "/api/health")
                    response = client.getresponse()
                    health = json.loads(response.read())
                    self.assertEqual(response.status, 200, health)
                    self.assertTrue(serve.compatible_health(health, database))
                    self.assertEqual(server.server_name, "127.0.0.1")
                    self.assertEqual(server.server_port, port)
                finally:
                    client.close()
                    worker.join(timeout=5)
                self.assertFalse(worker.is_alive())

            with mock.patch("socket.getfqdn", side_effect=AssertionError("DNS must not delay startup")), \
                    mock.patch("richi.serve.ThreadingHTTPServer.serve_forever", autospec=True,
                               side_effect=check_health) as running, redirect_stdout(io.StringIO()):
                self.assertEqual(serve.main(["--db", str(database), "--config", str(config),
                                             "--port", str(port)]), 0)
                running.assert_called_once()

    def test_explicit_config_overrides_bad_environment_in_all_workers(self):
        with tempfile.TemporaryDirectory(prefix="richi-map-config-") as temporary:
            root = Path(temporary).resolve()
            database = root / "memory.sqlite3"
            good_config = root / "good config.json"
            good_config.write_text(json.dumps({"database": str(database), "data_dir": str(root)}))
            bad_config = root / "invalid.json"
            bad_config.write_text("this deliberately is not JSON")
            environment = {key: value for key, value in os.environ.items() if not key.startswith("RICHI_")}
            environment["RICHI_CONFIG"] = str(bad_config)
            command = [sys.executable, "-B", "-m", "richi", "--config", str(good_config)]
            initialized = subprocess.run(command + ["init"], capture_output=True, text=True,
                                         timeout=10, env=environment)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            seeded = subprocess.run(command + ["project", "upsert", "--json", "-"],
                                    input=json.dumps({"id": "demo", "name": "Demo"}),
                                    capture_output=True, text=True, timeout=10, env=environment)
            self.assertEqual(seeded.returncode, 0, seeded.stderr)
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            child_pid = None
            try:
                launched = subprocess.run(command + ["map", "--no-open", "--port", str(port)],
                                          capture_output=True, text=True, timeout=20, env=environment)
                self.assertEqual(launched.returncode, 0, launched.stderr)
                result = json.loads(launched.stdout)
                self.assertEqual(result["status"], "started")
                child_pid = result["pid"]
                address = urlsplit(result["url"])
                connection = http.client.HTTPConnection(address.hostname, address.port, timeout=10)
                try:
                    connection.request("GET", "/api/health")
                    health_response = connection.getresponse()
                    health = json.loads(health_response.read())
                    self.assertEqual(health_response.status, 200, health)
                    self.assertEqual(health["database"], str(database))
                    self.assertIn("standalone_runtime", health["capabilities"])
                    connection.request("GET", "/api/graph")
                    graph_response = connection.getresponse()
                    graph = json.loads(graph_response.read())
                    self.assertEqual(graph_response.status, 200, graph)
                    self.assertEqual([node["id"] for node in graph["nodes"]], ["project:demo"])
                finally:
                    connection.close()
                self.assertEqual(bad_config.read_text(), "this deliberately is not JSON")
            finally:
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main()
