"""Map workers bound both output streams and reap failed child processes."""
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from richi import serve


class GraphWorkerBoundsTests(unittest.TestCase):
    def launch_fake(self, code, processes):
        launch = subprocess.Popen

        def start(*_args, **kwargs):
            process = launch([sys.executable, "-c", code], **kwargs)
            processes.append(process)
            return process

        return patch.object(serve.subprocess, "Popen", side_effect=start)

    def request(self):
        return serve._run_graph_request(Path("/unread-database"), [], serve.current_runtime())

    def assert_reaped(self, processes):
        self.assertEqual(len(processes), 1)
        process = processes[0]
        self.assertIsNotNone(process.returncode)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)

    def test_graph_cap_counts_stdout_stderr_and_their_combined_size(self):
        for writes in ("os.write(1, b'x'*2048)", "os.write(2, b'x'*2048)",
                       "os.write(1, b'x'*700);os.write(2, b'x'*700)"):
            with self.subTest(writes=writes):
                processes = []
                code = "import os,time;" + writes + ";time.sleep(20)"
                with self.launch_fake(code, processes), patch.object(serve, "GRAPH_WORKER_OUTPUT_LIMIT", 1024):
                    started = time.monotonic()
                    status, payload = self.request()
                self.assertEqual(status, 503)
                self.assertIn("size limit", payload["error"])
                self.assertLess(time.monotonic() - started, 3)
                self.assert_reaped(processes)

    def test_graph_timeout_reaps_worker_and_closes_pipes(self):
        processes = []
        with self.launch_fake("import time;time.sleep(20)", processes), \
                patch.object(serve, "GRAPH_WORKER_TIMEOUT", .1):
            started = time.monotonic()
            status, payload = self.request()
        self.assertEqual((status, payload), (503, {"error": "Graph export timed out"}))
        self.assertLess(time.monotonic() - started, 3)
        self.assert_reaped(processes)

    def test_graph_success_keeps_exact_worker_bytes(self):
        processes = []
        payload = b'{"nodes": [], "edges": [], "title": "synthetic"}\n'
        with self.launch_fake("import os;os.write(1, " + repr(payload) + ")", processes):
            self.assertEqual(self.request(), (200, payload))
        self.assert_reaped(processes)

    def test_graph_failure_preserves_structured_worker_error(self):
        processes = []
        with self.launch_fake("import os,sys;os.write(2,b'{\"error\":\"synthetic\"}');sys.exit(1)", processes):
            self.assertEqual(self.request(), (503, {"error": "synthetic"}))
        self.assert_reaped(processes)


if __name__ == "__main__":
    unittest.main()
