"""Graph migration, provenance, concurrency protection, and loopback integration."""
import http.client
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

BASE = Path(__file__).resolve().parents[1] / "src" / "richi"
from richi import memory
from richi import serve

from cli_environment import cli_environment


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="knowledge-graph-")
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "memory with spaces.sqlite3"
        self.cli("init")
        self.put("project", [{"id": p, "name": p, "source": "test fixture"} for p in ("a", "b", "c")], action="upsert")
        self.put("entry", [self.entry("one"), self.entry("two", project_ids=["b"])])

    def cli(self, *args, ok=True, payload=None):
        proc = subprocess.run([sys.executable, "-m", "richi", "--db", str(self.db), *args],
                              input=json.dumps(payload) if payload is not None else None,
                              capture_output=True, text=True, timeout=20, env=cli_environment(self.db.parent))
        self.assertEqual(proc.returncode == 0, ok, proc.stderr + proc.stdout)
        return json.loads(proc.stdout if ok else proc.stderr)

    def put(self, kind, payload, action="put", ok=True):
        return self.cli(kind, action, "--json", "-", payload=payload, ok=ok)

    def entry(self, ident, **kwargs):
        return dict({"id": ident, "kind": "experiment", "title": "Cache " + ident,
                     "summary": "Measured without evidence of production release", "project_ids": ["a"],
                     "knowledge_state": "confirmed", "sources": [{"reference": "test://report"}]}, **kwargs)

    def entity(self, **kwargs):
        return dict({"id": "pr:1", "kind": "pull_request", "title": "A proposal", "summary": "An open proposal",
                     "knowledge_state": "confirmed", "sources": [{"reference": "https://example.test/pr/1"}]}, **kwargs)

    def edge(self, **kwargs):
        return dict({"id": "one-two", "from_ref": "entry:one", "to_ref": "entry:two", "kind": "informs",
                     "description": "The first measurement informed the next experiment",
                     "knowledge_state": "confirmed", "sources": [{"reference": "test://report"}]}, **kwargs)

    def graph(self, *flags):
        return self.cli("graph", "export", *flags)

    def assert_closed(self, result):
        ids = {n["id"] for n in result["nodes"]}
        self.assertEqual(len(ids), len(result["nodes"]))
        for edge in result["edges"]:
            self.assertIn(edge["from_ref"], ids)
            self.assertIn(edge["to_ref"], ids)

    def test_v1_migration_preserves_all_records_fts_and_history(self):
        self.db = Path(self.temp.name) / "legacy.sqlite3"
        with sqlite3.connect(str(self.db)) as conn:
            conn.executescript((BASE / "schema.sql").read_text())
            conn.executescript(memory.FTS_SQL)
            conn.execute("INSERT INTO metadata VALUES ('search_backend', 'fts5')")
            conn.execute("PRAGMA user_version = 1")
        self.put("project", {"id": "a", "name": "A"}, action="upsert")
        self.put("entry", self.entry("old", event_id="initial-write"))
        self.put("entry", self.entry("old", summary="FTSneedlepersist", event_id="changed-write"))
        tables = ["projects", "entries", "entry_projects", "relations", "entry_history", "entry_events", "metadata"]
        with sqlite3.connect(str(self.db)) as conn:
            before = {t: conn.execute("SELECT * FROM " + t).fetchall() for t in tables}
        self.cli("init", ok=False)
        self.cli("graph", "export", ok=False)
        self.assertEqual(self.cli("migrate")["status"], "migrated")
        self.assertEqual(self.cli("migrate")["status"], "existing")
        with sqlite3.connect(str(self.db)) as conn:
            after = {t: conn.execute("SELECT * FROM " + t).fetchall() for t in tables}
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(before, after)
        self.assertIn("old", json.dumps(self.cli("search", "FTSneedlepersist")))
        self.assertEqual(len(self.cli("entry", "get", "old", "--history")["history"]), 2)
        self.assertEqual(len(self.graph()["nodes"]), 2)
        self.cli("check")

    def test_migration_rolls_back_on_partial_schema_collision(self):
        self.db = Path(self.temp.name) / "broken.sqlite3"
        with sqlite3.connect(str(self.db)) as conn:
            conn.executescript((BASE / "schema.sql").read_text())
            conn.execute("INSERT INTO metadata VALUES ('search_backend', 'scan')")
            conn.execute("PRAGMA user_version = 1")
            conn.execute("CREATE TABLE graph_nodes (valuable TEXT)")
            conn.execute("INSERT INTO graph_nodes VALUES ('keep')")
        self.cli("migrate", ok=False)
        with sqlite3.connect(str(self.db)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT * FROM graph_nodes").fetchall(), [("keep",)])
            self.assertFalse(conn.execute("SELECT name FROM sqlite_master WHERE name='entities'").fetchall())

    def test_default_states_no_dangling_edges_and_opt_in(self):
        self.put("entry", [self.entry("guess", knowledge_state="hypothesis"), self.entry("old", knowledge_state="superseded")])
        self.put("edge", [self.edge(), self.edge(id="to-guess", to_ref="entry:guess"),
                          self.edge(id="to-old", to_ref="entry:old"), self.edge(id="guessed-edge", knowledge_state="hypothesis")])
        data = self.graph()
        self.assert_closed(data)
        self.assertNotIn("entry:guess", [n["id"] for n in data["nodes"]])
        self.assertNotIn("entry:old", [n["id"] for n in data["nodes"]])
        self.assertEqual([e["id"] for e in data["edges"] if e["origin"] == "explicit"], ["edge:one-two"])
        all_data = self.graph("--include-hypotheses", "--include-superseded")
        self.assert_closed(all_data)
        self.assertEqual(len(all_data["nodes"]), len(data["nodes"]) + 2)

    def test_memberships_follow_legacy_writes_without_duplicate_entities(self):
        self.put("entry", self.entry("one", project_ids=["b", "c"], title="New title"))
        graph = self.graph()
        node = next(n for n in graph["nodes"] if n["id"] == "entry:one")
        self.assertEqual(node["title"], "New title")
        memberships = [e for e in graph["edges"] if e["from_ref"] == "entry:one"]
        self.assertEqual({e["to_ref"] for e in memberships}, {"project:b", "project:c"})
        self.assertEqual({e["kind"] for e in memberships}, {"belongs_to_project"})
        self.assertEqual({e["origin"] for e in memberships}, {"entry_project"})
        self.put("edge", self.edge(to_ref="project:b", kind="belongs_to_project"), ok=False)

    def test_showing_hypotheses_cannot_hide_confirmed_provenance(self):
        self.put("edge", self.edge(from_ref="project:a", to_ref="project:b", kind="uses_library"))
        self.put("relation", {"id": "later", "from_project": "a", "to_project": "b",
                              "kind": "uses_library", "knowledge_state": "hypothesis"})
        data = self.graph("--include-hypotheses")
        explicit = [e for e in data["edges"] if e["origin"] == "explicit"]
        self.assertEqual(len(explicit), 1)
        self.assertEqual(explicit[0]["knowledge_state"], "confirmed")
        self.assertEqual(explicit[0]["sources"], [{"reference": "test://report"}])

    def test_edges_entities_atomic_validation_cas_and_immutable_history(self):
        self.put("entity", self.entity())
        self.put("edge", self.edge(to_ref="entity:pr:1", kind="references"))
        for kind, payload in (("entity", self.entity()), ("edge", self.edge(to_ref="entity:pr:1", kind="references"))):
            old = self.cli(kind, "get", payload["id"], "--history")
            self.put(kind, payload)
            self.assertEqual(old["history"], self.cli(kind, "get", payload["id"], "--history")["history"])
            field = "summary" if kind == "entity" else "description"
            updated = dict(payload, **{field: "New information"}, expected_updated_at=old["updated_at"])
            self.put(kind, updated)
            self.put(kind, dict(payload, **{field: "Stale overwrite"}, expected_updated_at=old["updated_at"]), ok=False)
            now = self.cli(kind, "get", payload["id"], "--history")
            self.assertEqual(now[field], "New information")
            self.assertEqual(len(now["history"]), 2)
        self.put("edge", [self.edge(id="atomic"), self.edge(id="bad", to_ref="entity:missing")], ok=False)
        self.cli("edge", "get", "atomic", ok=False)
        self.put("entity", self.entity(id="no-source", sources=[]), ok=False)
        self.put("edge", self.edge(id="no-source", sources=[]), ok=False)
        with sqlite3.connect(str(self.db)) as conn:
            for table in ("entity_history", "edge_history"):
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute("DELETE FROM " + table)

    def test_cycles_cross_project_component_and_limits(self):
        self.put("entity", self.entity())
        self.put("edge", [self.edge(), self.edge(id="two-pr", from_ref="entry:two", to_ref="entity:pr:1"),
                          self.edge(id="pr-one", from_ref="entity:pr:1", to_ref="entry:one")])
        graph = self.graph("--project", "a")
        self.assertEqual({n["id"] for n in graph["nodes"]}, {"project:a", "project:b", "entry:one", "entry:two", "entity:pr:1"})
        limited = self.cli("graph", "neighbors", "entry:one", "--depth", "3", "--limit", "2")
        self.assertEqual(len(limited["nodes"]), 2)
        self.assertTrue(limited["truncated"])
        self.assert_closed(limited)
        self.cli("graph", "neighbors", "entry:missing", ok=False)
        self.cli("graph", "neighbors", "entry:one", "--depth", "1")

    def test_map_http_security_refresh_and_read_only(self):
        server = serve.LoopbackHTTPServer(("127.0.0.1", 0), serve.handler_for(self.db))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        def request(path, method="GET", headers=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
            conn.request(method, path, headers=headers or {})
            response = conn.getresponse()
            result = (response.status, response.read(), dict(response.getheaders()))
            conn.close()
            return result
        self.assertEqual(request("/")[0], 200)
        status, body, headers = request("/api/graph")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        count = len(json.loads(body)["nodes"])
        self.put("entry", self.entry("new-from-other-task"))
        self.assertEqual(len(json.loads(request("/api/graph")[1])["nodes"]), count + 1)
        for path in ("/memory.sqlite3", "/../memory.py", "/api/edge"):
            self.assertEqual(request(path)[0], 404)
        for query in ("x=1", "include_hypotheses=2", "include_hypotheses=1&include_hypotheses=0"):
            self.assertEqual(request("/api/graph?" + query)[0], 400)
        self.assertEqual(request("/api/graph", headers={"Host": "attacker.test"})[0], 403)
        self.assertEqual(request("/api/graph", headers={"Origin": "https://attacker.test"})[0], 403)
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            self.assertEqual(request("/api/graph", method)[0], 405)
        self.cli("check")


if __name__ == "__main__":
    unittest.main()
