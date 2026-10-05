"""Exercise real launcher processes with disposable databases and local ports."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

from richi_launcher.runtime import bootstrap_command

from cli_environment import cli_environment



class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="map-launcher-")
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name).resolve() / "memory.sqlite3"
        self.config = self.db.parent / "config.json"
        self.config.write_text(json.dumps({"database": str(self.db), "dev": False}))
        subprocess.run(bootstrap_command(["--config", str(self.config), "init"]),
                       capture_output=True, check=True, env=cli_environment(self.db.parent))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.children = []
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for pid in self.children:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def launch(self, db=None):
        result = subprocess.run(bootstrap_command(["--config", str(self.config), "--db", str(db or self.db),
                                                  "map", "--port", str(self.port), "--no-open"]),
                                capture_output=True, text=True, timeout=20, env=cli_environment(self.db.parent))
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        if "pid" in record:
            self.children.append(record["pid"])
        return record

    def test_concurrent_clicks_start_once_then_reuse(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            first, second = list(executor.map(lambda _: self.launch(), [1, 2]))
        self.assertEqual({first["status"], second["status"]}, {"started", "existing"})
        self.assertEqual(first["url"], second["url"])
        third = self.launch()
        self.assertEqual(third["status"], "existing")
        self.assertEqual(third["url"], first["url"])
        self.assertEqual(len(self.children), 1)

    def test_busy_port_uses_another_without_touching_owner(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", self.port))
            occupied.listen()
            result = self.launch()
            self.assertEqual(result["status"], "started")
            self.assertNotEqual(result["url"], "http://127.0.0.1:%d/" % self.port)
            self.assertEqual(occupied.getsockname()[1], self.port)

    def test_missing_database_does_not_create_one(self):
        missing = self.db.parent / "missing.sqlite3"
        result = subprocess.run(bootstrap_command(["--config", str(self.config), "--db", str(missing), "map", "--no-open"]),
                                 capture_output=True, timeout=10, env=cli_environment(self.db.parent))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(missing.exists())

    def assert_old_server_kept(self, **health_fields):
        health = {"application": "project-memory-map", "database": str(self.db), **health_fields}

        class OldHandler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                data = json.dumps(health).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        old_server = ThreadingHTTPServer(("127.0.0.1", self.port), OldHandler)
        worker = threading.Thread(target=old_server.serve_forever, daemon=True)
        worker.start()
        def stop_old():
            old_server.shutdown()
            old_server.server_close()
            worker.join(timeout=3)
        self.addCleanup(stop_old)
        result = self.launch()
        self.assertEqual(result["status"], "started")
        self.assertNotEqual(result["url"], "http://127.0.0.1:%d/" % self.port)
        self.assertTrue(worker.is_alive())
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request("GET", "/api/health")
            self.assertEqual(json.loads(connection.getresponse().read()), health)
        finally:
            connection.close()

    def test_old_server_is_kept_and_new_capability_starts_on_next_port(self):
        self.assert_old_server_kept()

    def test_api4_flat_runtime_is_not_reused_even_for_the_same_database(self):
        self.assert_old_server_kept(api_version=4,
                                   capabilities=["git_source_viewer", "legacy_git_source_viewer"])


if __name__ == "__main__":
    unittest.main()
