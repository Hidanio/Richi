"""Read-only map Git navigation uses temporary repositories and attached evidence only."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from richi import git_evidence
from richi import git_sources
from richi import graph
from richi import memory
from richi import serve


class GitMapAPITests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="git-map-api-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / "repository with spaces"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.file = self.repo / "code.txt"
        self.file.write_text("first implementation\n", encoding="utf-8")
        self.git("add", "code.txt")
        self.git("commit", "-qm", "Initial code")
        self.head = self.git("rev-parse", "HEAD").strip()
        self.db = self.root / "memory.sqlite3"
        self.conn = memory.connect(self.db, create=True)
        self.addCleanup(self.conn.close)
        memory.initialize(self.conn, self.db)
        memory.project_put(self.conn, {"id": "repo", "name": "Repo", "repo_path": str(self.repo)})
        self.source = git_evidence.capture(self.repo, "repo", "code.txt")
        self.sources = [{"reference": "report://before"}, self.source]
        memory.entry_put(self.conn, {"id": "task:one", "kind": "experiment", "title": "Saved code",
            "summary": "Known experiment", "project_ids": ["repo"], "sources": self.sources,
            "work_state": "implemented", "knowledge_state": "confirmed", "verified_at": "2026-09-30"})
        graph.put(self.conn, {"id": "term", "kind": "concept", "title": "Term", "summary": "Meaning",
            "sources": self.sources, "knowledge_state": "hypothesis"}, "entity", memory)
        graph.put(self.conn, {"id": "supports", "from_ref": "entry:task:one", "to_ref": "entity:term",
            "kind": "supports", "description": "Evidence", "sources": self.sources,
            "knowledge_state": "superseded"}, "edge", memory)
        self.server = serve.LoopbackHTTPServer(("127.0.0.1", 0), serve.handler_for(self.db))
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True,
                                text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def record(self, ref="entry:task:one"):
        return git_sources._record(self.conn, ref, memory)[1]

    def options(self, action="show", ref="entry:task:one", **extra):
        result = dict(action=action, ref=ref, source="2", expected_updated_at=self.record(ref)["updated_at"])
        result.update(extra)
        return result

    def request(self, path, method="GET", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=35)
        try:
            conn.request(method, path, headers=headers or {})
            response = conn.getresponse()
            body = response.read()
            return response.status, json.loads(body) if body else None, dict(response.getheaders())
        finally:
            conn.close()

    def api(self, action="show", ref="entry:task:one", **extra):
        return self.request("/api/git?" + urlencode(self.options(action, ref, **extra)))

    def replace_entry(self, **changes):
        record = self.record()
        payload = {key: value for key, value in record.items() if key not in {"created_at", "updated_at", "history"}}
        payload.update(changes, expected_updated_at=record["updated_at"])
        memory.entry_put(self.conn, payload)

    def test_show_history_diff_and_selected_source_check(self):
        status, shown, headers = self.api()
        self.assertEqual(status, 200, shown)
        self.assertEqual(shown["result"]["content"], "first implementation\n")
        self.assertEqual(shown["captured_commit"], self.head)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.file.write_text("second implementation\n")
        self.git("add", "code.txt")
        self.git("commit", "-qm", "Changed code")
        current = self.git("rev-parse", "HEAD").strip()
        for action in ("diff", "check"):
            status, result, _ = self.api(action, target="HEAD")
            self.assertEqual(status, 200, result)
            self.assertEqual(result["result"]["status"], "changed")
            self.assertEqual(result["result"]["target"]["commit"], current)
            self.assertEqual(result["result"]["ancestry"], "ahead")
        self.assertIn("+second implementation", self.api("diff")[1]["result"]["patch"])
        captured_history = self.api("history")[1]["result"]
        self.assertEqual(captured_history["target"], self.head)
        self.assertEqual(len(captured_history["commits"]), 1)
        head_history = self.api("history", target="HEAD", limit="1")[1]["result"]
        self.assertEqual(head_history["target"], current)
        self.assertTrue(head_history["truncated"])
        self.assertEqual(head_history["commits"][0]["subject"], "Changed code")

    def test_full_source_index_and_all_record_states(self):
        self.assertEqual(self.api(source="1")[0], 400)
        self.assertEqual(self.api(source="3")[0], 400)
        for ref in ("entry:task:one", "entity:term", "edge:supports"):
            status, result, _ = self.api("check", ref)
            self.assertEqual(status, 200, result)
            self.assertEqual(result["ref"], ref)
            self.assertEqual(result["result"]["status"], "unchanged")
        memory.entry_put(self.conn, {"id": "task:with spaces", "kind": "experiment", "title": "Saved code",
            "summary": "An accepted ID", "project_ids": ["repo"], "sources": self.sources})
        self.assertEqual(self.api(ref="entry:task:with spaces")[0], 200)

    def test_stale_card_rejected_before_changed_source_index_is_read(self):
        stale = self.options()
        self.replace_entry(sources=[self.source, {"reference": "report://different"}])
        status, result, _ = self.request("/api/git?" + urlencode(stale))
        self.assertEqual(status, 409)
        self.assertEqual(result["code"], "stale_record")
        self.assertEqual(self.api(source="1")[0], 200)

    def test_worktree_snapshot_survives_later_changes_and_missing_snapshot_is_explicit(self):
        self.file.write_text("saved dirty experiment\n")
        source = git_evidence.capture(self.repo, "repo", "code.txt", worktree=True,
                                      artifact_dir=git_sources._artifacts(self.db))
        self.replace_entry(sources=[{"reference": "report://before"}, source])
        self.file.write_text("later experiment\n")
        status, result, _ = self.api()
        self.assertEqual(status, 200, result)
        self.assertTrue(result["captured_dirty"])
        self.assertEqual(result["result"]["content"], "saved dirty experiment\n")
        self.assertEqual(self.api("check", worktree="1")[1]["result"]["status"], "changed")
        self.assertIn("+later experiment", self.api("diff", worktree="1")[1]["result"]["patch"])
        (git_sources._artifacts(self.db) / source["git"]["snapshot"]).unlink()
        self.assertEqual(self.api()[0], 503)
        self.assertEqual(self.api("check")[1]["result"]["status"], "unavailable")

    def test_deleted_file_remains_readable_from_captured_commit(self):
        self.git("rm", "-q", "code.txt")
        self.git("commit", "-qm", "Remove code")
        self.assertEqual(self.api()[0], 200)
        for action in ("check", "diff"):
            status, result, _ = self.api(action)
            self.assertEqual(status, 200, result)
            self.assertEqual(result["result"]["status"], "deleted")

    def test_missing_repository_record_and_revision(self):
        missing = self.options()
        missing["ref"] = "entry:absent"
        self.assertEqual(self.request("/api/git?" + urlencode(missing))[0], 404)
        self.assertEqual(self.api("history", target="refs/heads/absent")[0], 503)
        self.assertEqual(self.api("check", target="refs/heads/absent")[1]["result"]["status"], "unavailable")
        self.repo.rename(self.root / "moved-repo")
        status, result, _ = self.api()
        self.assertEqual(status, 503, result)
        self.assertEqual(result["code"], "git_unavailable")

    def test_strict_query_validation_does_not_accept_paths_or_mutations(self):
        base = self.options()
        invalid = [dict(base, action="capture"), dict(base, action="attach"),
            dict(base, repo=str(self.repo)), dict(base, path="code.txt"), dict(base, json="source.json"),
            dict(base, source="0"), dict(base, source="1.0"), dict(base, source="-1"),
            dict(base, source="2" + "0" * 100), dict(base, expected_updated_at="not a timestamp"),
            dict(base, action="history", limit="101"), dict(base, action="history", worktree="1"),
            dict(base, action="diff", target="HEAD", worktree="1"), dict(base, action="diff", worktree="0"),
            dict(base, action="check", target="--help"), dict(base, action="check", target="HEAD\n"),
            dict(base, action="show", target="HEAD"), dict(base, max_chars="100001"),
            dict(base, max_chars="1999"), dict(base, ref="project:repo")]
        for options in invalid:
            with self.subTest(options=options):
                self.assertEqual(self.request("/api/git?" + urlencode(options))[0], 400)
        query = urlencode(base)
        for bad in (query + "&source=2", query + "&ref=entry:task:one", query + "&x",
                    query.replace("action=show", "action=%FF"), ""):
            self.assertEqual(self.request("/api/git?" + bad)[0], 400)
        self.assertEqual(self.request("/api/git?" + query + "&padding=" + "x" * 8200)[0], 414)

    def test_origin_protection_and_mutation_methods(self):
        path = "/api/git?" + urlencode(self.options())
        for headers in ({"Host": "attacker.test"}, {"Origin": "https://attacker.test"},
                        {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.request(path, headers=headers)[0], 403)
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            self.assertEqual(self.request(path, method=method)[0], 405)

    def test_budget_and_binary_output(self):
        self.file.write_bytes(b"\0binary\xffpayload")
        self.git("add", "code.txt")
        self.git("commit", "-qm", "Binary bytes")
        source = git_evidence.capture(self.repo, "repo", "code.txt")
        self.replace_entry(sources=[{"reference": "report://before"}, source])
        self.assertEqual(self.api()[1]["result"]["encoding"], "base64")
        self.file.write_text("x" * 60000)
        source = git_evidence.capture(self.repo, "repo", "code.txt", worktree=True,
                                      artifact_dir=git_sources._artifacts(self.db))
        self.replace_entry(sources=[{"reference": "report://before"}, source])
        status, result, _ = self.api(max_chars="3000")
        self.assertEqual(status, 200, result)
        self.assertTrue(result["result"]["truncated"])
        self.assertTrue(result["budget"]["truncated"])
        self.assertEqual(len(git_sources._render(result)), result["budget"]["output_chars"])
        self.assertLessEqual(result["budget"]["output_chars"], 3000)

    def test_requests_preserve_database_worktree_index_and_artifacts(self):
        before_db = list(self.conn.iterdump())
        before_index = (self.repo / ".git" / "index").read_bytes()
        before_file = self.file.read_bytes()
        for action in ("show", "history", "diff", "check"):
            self.assertEqual(self.api(action)[0], 200)
        self.assertEqual(list(self.conn.iterdump()), before_db)
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), before_index)
        self.assertEqual(self.file.read_bytes(), before_file)
        self.assertFalse(git_sources._artifacts(self.db).exists())
        options = serve._git_options(urlencode(self.options()))
        original = git_sources.navigate
        def check_readonly(conn, *args):
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM entries")
            return original(conn, *args)
        with patch.object(git_sources, "navigate", side_effect=check_readonly):
            serve._git_response(self.db, options)

    def test_health_and_existing_graph_endpoint(self):
        status, health, _ = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(serve.compatible_health(health, self.db))
        self.assertFalse(serve.compatible_health({"application": "project-memory-map", "database": str(self.db)}, self.db))
        self.assertFalse(serve.compatible_health(dict(health, capabilities=None), self.db))
        self.assertEqual(health["api_version"], 4)
        status, exported, _ = self.request("/api/graph")
        self.assertEqual(status, 200)
        self.assertIn("entry:task:one", {item["id"] for item in exported["nodes"]})

    def test_concurrent_navigation_replies_busy_without_blocking_health(self):
        started, release = threading.Event(), threading.Event()
        path = "/api/git?" + urlencode(self.options())
        def slow(_database, _options, _runtime, _config, _workspace, _required):
            started.set()
            release.wait(5)
            return 200, {"result": "done"}
        with patch.object(serve, "_run_git_request", side_effect=slow):
            with ThreadPoolExecutor(max_workers=1) as executor:
                first = executor.submit(self.request, path)
                try:
                    self.assertTrue(started.wait(5))
                    status, result, _ = self.api()
                    self.assertEqual(status, 503)
                    self.assertEqual(result["code"], "busy")
                    self.assertEqual(self.request("/api/health")[0], 200)
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=5)[0], 200)


class GitWorkerBoundsTests(unittest.TestCase):
    def launch_fake(self, code):
        original = subprocess.Popen
        def launch(*_args, **kwargs):
            return original([sys.executable, "-c", code], **kwargs)
        return patch.object(serve.subprocess, "Popen", side_effect=launch)

    def test_worker_deadline_terminates_stuck_process(self):
        with self.launch_fake("import sys,time;sys.stdin.buffer.read();time.sleep(20)"), \
                patch.object(serve, "GIT_WORKER_TIMEOUT", .1):
            started = time.monotonic()
            status, result = serve._run_git_request(Path("/unread-database"), {})
            self.assertEqual(status, 503)
            self.assertEqual(result["code"], "timeout")
            self.assertLess(time.monotonic() - started, 3)

    def test_worker_output_cap_covers_stderr_and_stdout(self):
        with self.launch_fake("import sys;sys.stdin.buffer.read();sys.stderr.write('x'*100000)"), \
                patch.object(serve, "GIT_WORKER_OUTPUT_LIMIT", 1024):
            status, result = serve._run_git_request(Path("/unread-database"), {})
            self.assertEqual(status, 503)
            self.assertIn("size limit", result["error"])

    @unittest.skipUnless(hasattr(serve.signal, "SIGALRM"), "Unix worker alarm")
    def test_worker_alarm_covers_total_operation(self):
        code = ("import time;from richi import serve;serve.GIT_OPERATION_TIMEOUT=.1;"
                "serve._git_response=lambda *args: time.sleep(20);serve._git_worker()")
        with self.launch_fake(code):
            started = time.monotonic()
            status, result = serve._run_git_request(Path("/unread-database"), {})
            self.assertEqual(status, 503)
            self.assertEqual(result["code"], "timeout")
            self.assertLess(time.monotonic() - started, 3)


if __name__ == "__main__":
    unittest.main()
