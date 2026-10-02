"""Real maps keep their workspace while another CLI selects a different default."""
import http.client
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import urlencode, urlsplit

from richi import git_evidence, git_sources, memory, serve
import richi_launcher


@unittest.skipIf(sys.platform == "win32", "Map launch/reload targets macOS and Linux")
class WorkspaceMapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-workspace-map-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.installed = self.root / "installed"
        self.checkout = self.root / "checkout"
        self.package = self.checkout / "src" / "richi"
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
        shutil.copytree(Path(serve.__file__).parent, self.installed / "richi", ignore=ignore)
        shutil.copytree(Path(richi_launcher.__file__).parent, self.installed / "richi_launcher", ignore=ignore)
        shutil.copytree(Path(serve.__file__).parent, self.package, ignore=ignore)
        (self.checkout / "pyproject.toml").write_text('[project]\nname = "richi"\nversion = "0.1.0"\n')
        runner = "import sys;sys.path.insert(0,sys.argv[1]);from richi_launcher.cli import main;sys.exit(main(sys.argv[2:]))"
        self.command = [sys.executable, "-I", "-B", "-c", runner, str(self.installed)]
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith("RICHI_")}
        self.environment.update(HOME=str(self.root / "home"), XDG_CONFIG_HOME=str(self.root / "config"),
                                XDG_DATA_HOME=str(self.root / "data"))
        self.processes = []
        self.detached = []
        self.addCleanup(self.stop_maps)

    def stop_maps(self):
        for process, _log in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        for pid, _port in self.detached:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        # Reap foreground processes and wait for detached servers to close before
        # TemporaryDirectory removes their copied packages and databases.
        deadline = time.monotonic() + 5
        for _pid, port in self.detached:
            while time.monotonic() < deadline:
                with socket.socket() as check:
                    if check.connect_ex(("127.0.0.1", port)) != 0:
                        break
                time.sleep(0.05)

    def cli(self, *arguments):
        result = subprocess.run(self.command + list(arguments), cwd=self.root, env=self.environment,
                                capture_output=True, text=True, timeout=25)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def create_workspace(self, name):
        self.cli("workspace", "create", name)
        self.cli("-w", name, "init")
        return self.cli("-w", name, "config", "show")

    @staticmethod
    def free_port():
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            return reservation.getsockname()[1]

    def start_foreground(self, arguments, port):
        log_path = self.root / ("server-%d.log" % port)
        with log_path.open("w") as log:
            process = subprocess.Popen(self.command + list(arguments) + ["map", "serve", "--port", str(port)],
                                       cwd=self.root, env=self.environment, stdout=log, stderr=log)
        self.processes.append((process, log_path))
        return process

    @staticmethod
    def request(port, path):
        client = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            client.request("GET", path)
            response = client.getresponse()
            return response.status, response.getheader("Server"), response.read()
        finally:
            client.close()

    def health(self, port, mode="release", server_version="ProjectMemoryMap/4"):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                status, server, payload = self.request(port, "/api/health")
                health = json.loads(payload)
                if status == 200 and health["runtime"]["mode"] == mode and server.startswith(server_version):
                    return health
            except (OSError, ValueError, http.client.HTTPException):
                pass
            if any(process.poll() is not None for process, _log in self.processes):
                break
            time.sleep(0.1)
        self.fail("Map transition failed: " + "\n".join(log.read_text() for _process, log in self.processes))

    def git(self, repository, *arguments):
        result = subprocess.run(["git", "-C", str(repository), *arguments], capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def seed(self, settings, name, repository):
        database = Path(settings["database"])
        (repository / "code.txt").write_text(name + " captured worktree\n")
        captured = git_evidence.capture(repository, "shared", "code.txt", worktree=True,
                                         artifact_dir=git_sources._artifacts(database))
        with memory.connect(database) as connection:
            memory.project_put(connection, {"id": "shared", "name": name + " project", "repo_path": str(repository)})
            memory.entry_put(connection, {"id": "task:same", "kind": "experiment", "title": name + " experiment",
                "summary": name + " isolated knowledge", "project_ids": ["shared"], "sources": [captured],
                "work_state": "implemented", "knowledge_state": "confirmed", "verified_at": "2026-10-02"})
            return memory.entry_record(connection, "task:same")

    def assert_knowledge(self, port, name, record):
        status, _server, payload = self.request(port, "/api/graph")
        graph = json.loads(payload)
        self.assertEqual(status, 200, graph)
        projects = [node for node in graph["nodes"] if node["id"] == "project:shared"]
        self.assertEqual(projects[0]["title"], name + " project")
        query = urlencode({"action": "show", "ref": "entry:task:same", "source": "1",
                           "expected_updated_at": record["updated_at"]})
        status, _server, payload = self.request(port, "/api/git?" + query)
        shown = json.loads(payload)
        self.assertEqual(status, 200, shown)
        self.assertEqual(shown["result"]["content"], name + " captured worktree\n")

    def test_maps_workers_and_dev_reload_remain_in_original_workspace(self):
        alpha = self.create_workspace("alpha")
        beta = self.create_workspace("beta")
        self.assertNotEqual(Path(alpha["database"]).parent, Path(beta["database"]).parent)
        repository = self.root / "shared repository"
        repository.mkdir()
        self.git(repository, "init", "-q")
        self.git(repository, "config", "user.name", "Fixture")
        self.git(repository, "config", "user.email", "fixture@example.invalid")
        (repository / "code.txt").write_text("Committed baseline\n")
        self.git(repository, "add", "code.txt")
        self.git(repository, "commit", "-qm", "Baseline")
        alpha_record = self.seed(alpha, "Alpha", repository)
        beta_record = self.seed(beta, "Beta", repository)
        self.cli("workspace", "use", "alpha")
        port_a = self.free_port()
        process_a = self.start_foreground([], port_a)
        initial_a = self.health(port_a)
        self.assertEqual(initial_a["workspace"], "alpha")
        self.assertEqual(initial_a["config_file"], alpha["config_file"])
        self.cli("workspace", "use", "beta")
        result_b = self.cli("map", "--no-open", "--port", str(port_a))
        self.assertEqual(result_b["status"], "started")
        port_b = urlsplit(result_b["url"]).port
        self.detached.append((result_b["pid"], port_b))
        self.assertNotEqual(port_b, port_a)
        initial_b = self.health(port_b)
        self.assertEqual(initial_b["workspace"], "beta")
        self.assertEqual(initial_b["database"], beta["database"])
        self.assertEqual(self.health(port_a), initial_a)
        self.assert_knowledge(port_a, "Alpha", alpha_record)
        self.assert_knowledge(port_b, "Beta", beta_record)
        reused = self.cli("-w", "alpha", "map", "--no-open", "--port", str(port_a))
        self.assertEqual(reused["status"], "existing")
        self.assertEqual(urlsplit(reused["url"]).port, port_a)
        self.cli("-w", "alpha", "config", "set", "development.source", str(self.checkout))
        self.cli("-w", "alpha", "config", "set", "dev", "true")
        development = self.health(port_a, "dev")
        self.assertEqual(development["workspace"], "alpha")
        self.assertEqual(development["database"], alpha["database"])
        self.assertIsNone(process_a.poll(), "Runtime transitions must preserve the original process")
        source = self.package / "serve.py"
        source.write_text(source.read_text().replace('server_version = "ProjectMemoryMap/4"',
                                                    'server_version = "ProjectMemoryMap/5"'))
        reloaded = self.health(port_a, "dev", "ProjectMemoryMap/5")
        self.assertEqual(reloaded, development)
        self.assertIsNone(process_a.poll())
        self.assert_knowledge(port_a, "Alpha", alpha_record)
        self.assertEqual(self.health(port_b), initial_b)
        self.cli("-w", "alpha", "config", "set", "dev", "false")
        self.assertEqual(self.health(port_a), initial_a)
        self.assertIsNone(process_a.poll())
        self.assert_knowledge(port_b, "Beta", beta_record)

    def test_missing_default_config_stays_pinned_after_workspace_use(self):
        database = self.root / "default-memory.sqlite3"
        with memory.connect(database, create=True) as connection:
            memory.initialize(connection, database)
            memory.project_put(connection, {"id": "default-only", "name": "Default memory"})
        port = self.free_port()
        process = self.start_foreground(["--db", str(database)], port)
        initial = self.health(port)
        self.assertEqual(initial["workspace"], "default")
        config = Path(initial["config_file"])
        self.assertFalse(config.exists())
        self.create_workspace("beta")
        self.cli("workspace", "use", "beta")
        self.assertEqual(self.health(port), initial)
        status, _server, payload = self.request(port, "/api/graph")
        graph = json.loads(payload)
        self.assertEqual(status, 200, graph)
        self.assertEqual([node["id"] for node in graph["nodes"]], ["project:default-only"])
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"dev": True, "development": {"source": str(self.checkout)}}))
        development = self.health(port, "dev")
        self.assertEqual(development["workspace"], "default")
        self.assertEqual(development["database"], str(database))
        self.assertEqual(development["config_file"], str(config))
        self.assertIsNone(process.poll())


if __name__ == "__main__":
    unittest.main()
