"""Tool usage navigation on synthetic stores, independently of graph-export caps."""

import http.client
import json
import sqlite3
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_recall as fixtures
from richi import memory, serve, tool_catalog


class ToolCatalogTests(unittest.TestCase):
    setUp = fixtures.RecallTests.setUp
    process = fixtures.RecallTests.process
    cli = fixtures.RecallTests.cli
    put = fixtures.RecallTests.put
    entry = fixtures.RecallTests.entry
    entity = fixtures.RecallTests.entity
    edge = fixtures.RecallTests.edge

    def populate(self):
        self.put("entity", [
            self.entity("tool:tracekit", kind="tool", title="TraceKit", aliases=["TraceKit"]),
            self.entity("tool:benchkit", kind="tool", title="BenchKit", aliases=["BenchKit"]),
            self.entity("concept:tracekit", title="TraceKit terminology", aliases=["TraceKit"]),
        ])
        self.put("entry", [
            self.entry("task:recovery", kind="task", title="Verify recovery", project_ids=["alpha", "beta"]),
            self.entry("task:unrelated", kind="task", title="Unrelated task", jira_key="DEMO-1"),
            self.entry("procedure:recovery", kind="procedure", title="Recovery scenario",
                       project_ids=["alpha", "beta"], jira_key="DEMO-1"),
            self.entry("experiment:recovery", kind="experiment", title="Recovery run"),
        ])
        self.put("edge", [
            self.edge("trace-scenario", "entry:procedure:recovery", "entity:tool:tracekit", kind="uses_tool"),
            self.edge("bench-scenario", "entry:procedure:recovery", "entity:tool:benchkit", kind="uses_tool"),
            self.edge("trace-run", "entry:experiment:recovery", "entity:tool:tracekit", kind="uses_tool"),
            self.edge("scenario-task", "entry:procedure:recovery", "entry:task:recovery", kind="part_of"),
            self.edge("run-scenario", "entry:experiment:recovery", "entry:procedure:recovery", kind="part_of"),
            self.edge("trace-gamma", "entity:tool:tracekit", "project:gamma", kind="used_in"),
        ])

    def tool(self, identity="tool:tracekit", *flags):
        result = self.cli("tools", "get", identity, *flags)
        tool = result["tools"][0]
        records = result["records"]
        tool["records"] = [records[ref] for ref in tool["records"]]
        for project in tool["projects"]:
            project["records"] = [records[ref] for ref in project["records"]]
            project["tasks"] = [dict(records[task["ref"]], usage_paths=task["usage_paths"],
                                     usage_records=[records[ref] for ref in task["usage_refs"]])
                                for task in project["tasks"]]
        return tool

    def test_catalog_is_distinct_from_concepts_and_groups_shared_scenario(self):
        self.populate()
        value = self.cli("tools", "list")
        self.assertEqual([tool["id"] for tool in value["tools"]], ["tool:benchkit", "tool:tracekit"])
        self.assertFalse(value["truncated"])
        tool = self.tool()
        self.assertEqual(tool["project_count"], 3)
        self.assertEqual(tool["task_count"], 1)
        projects = {project["id"]: project for project in tool["projects"]}
        for project_id in ("alpha", "beta"):
            self.assertEqual([task["id"] for task in projects[project_id]["tasks"]], ["task:recovery"])
        self.assertEqual(projects["gamma"]["tasks"], [])
        self.assertEqual(projects["gamma"]["records"], [])
        self.assertEqual(len(projects["alpha"]["tasks"][0]["usage_records"]), 2)
        self.assertEqual(len(projects["beta"]["tasks"][0]["usage_records"]), 1)
        self.assertEqual(projects["alpha"]["tasks"][0]["usage_paths"][0]["record_ref"],
                         "entry:experiment:recovery")
        self.assertTrue(tool["records"][0]["usage_evidence"][0]["sources"])
        self.assertTrue(tool["updated_at"])

    def test_arbitrary_links_reverse_usage_and_matching_jira_do_not_add_tasks(self):
        self.populate()
        self.put("edge", [
            self.edge("related", "entry:task:unrelated", "entity:tool:tracekit", kind="related_to"),
            self.edge("reverse", "entity:tool:tracekit", "entry:task:unrelated", kind="uses_tool"),
            self.edge("guess", "entry:task:unrelated", "entity:tool:tracekit", kind="uses_tool", knowledge_state="hypothesis"),
            self.edge("reverse-parent", "entry:task:unrelated", "entry:procedure:recovery", kind="part_of"),
        ])
        tool = self.tool("tool:tracekit", "--include-hypotheses")
        self.assertEqual({task["id"] for project in tool["projects"] for task in project["tasks"]}, {"task:recovery"})

    def test_task_and_application_must_both_belong_to_selected_project(self):
        self.populate()
        self.put("entry", self.entry("task:recovery", kind="task", project_ids=["beta"]))
        projects = {project["id"]: project for project in self.tool()["projects"]}
        self.assertEqual(projects["alpha"]["tasks"], [])
        self.assertEqual(len(projects["alpha"]["records"]), 2)
        self.assertEqual(len(projects["beta"]["tasks"]), 1)

    def test_direct_task_usage_without_project_and_part_of_cycles(self):
        self.populate()
        self.put("entry", self.entry("task:unscoped", kind="task", project_ids=[]))
        self.put("edge", [
            self.edge("direct", "entry:task:unscoped", "entity:tool:tracekit", kind="uses_tool"),
            self.edge("cycle", "entry:procedure:recovery", "entry:experiment:recovery", kind="part_of"),
        ])
        tool = self.tool()
        self.assertIn("task:unscoped", [record["id"] for record in tool["records"]])
        self.assertEqual(tool["task_count"], 1)
        self.assertFalse(tool["truncated"])

    def test_unsourced_usage_and_unsourced_parent_are_not_evidence(self):
        self.populate()
        with sqlite3.connect(str(self.db)) as conn:
            conn.execute("UPDATE graph_edges SET sources = '[]' WHERE id IN ('trace-run', 'scenario-task')")
        tool = self.tool()
        self.assertEqual(tool["record_count"], 1)
        self.assertEqual(tool["task_count"], 0)

    def test_hidden_historical_and_hypothesis_nodes_require_opt_in(self):
        self.populate()
        self.put("entity", self.entity("tool:old", kind="tool", knowledge_state="superseded"))
        self.put("entry", self.entry("procedure:recovery", kind="procedure", knowledge_state="hypothesis"))
        self.assertNotIn("tool:old", [tool["id"] for tool in self.cli("tools", "list")["tools"]])
        self.assertEqual(self.tool()["task_count"], 0)
        self.assertEqual(self.tool("tool:tracekit", "--include-hypotheses")["task_count"], 1)
        self.assertEqual(self.tool("tool:old", "--include-superseded")["id"], "tool:old")
        old = next(tool for tool in self.cli("tools", "list", "--include-superseded")["tools"] if tool["id"] == "tool:old")
        self.assertTrue(self.cli(*old["read_more"]["args"])["tools"][0]["detail"])

    def test_list_is_lightweight_and_detail_deduplicates_full_records(self):
        self.populate()
        project_description = "shared-project-background " * 20000
        self.put("project", {"id": "alpha", "name": "Alpha", "description": project_description,
                             "repo_path": "/synthetic/project", "source": "synthetic evidence"}, action="upsert")
        self.put("entity", self.entity("tool:tracekit", kind="tool", summary="description " * 1000))
        self.put("entry", self.entry("procedure:recovery", kind="procedure", project_ids=["alpha", "beta"],
                                    summary="unique-long-scenario " * 500))
        listing = self.cli("tools", "list")
        tool = next(tool for tool in listing["tools"] if tool["id"] == "tool:tracekit")
        self.assertFalse(tool["detail"])
        self.assertTrue(tool["summary_is_excerpt"])
        self.assertEqual(len(tool["summary"]), 1000)
        self.assertEqual(tool["sources_omitted"], 1)
        self.assertEqual(listing["records"], {})
        for listed_tool in listing["tools"]:
            for project in listed_tool["projects"]:
                self.assertFalse({"description", "repo_path", "source", "verified_at"} & project.keys())
        self.assertLess(len(json.dumps(listing)), 15000)
        detail = self.cli("tools", "get", "tool:tracekit")
        alpha = next(project for project in detail["tools"][0]["projects"] if project["id"] == "alpha")
        self.assertEqual(alpha["description"], project_description)
        self.assertEqual(alpha["repo_path"], "/synthetic/project")
        self.assertEqual(alpha["source"], "synthetic evidence")
        self.assertEqual(json.dumps(detail).count("unique-long-scenario"), 500)
        self.assertEqual(detail["tools"][0]["projects"][0]["records"],
                         ["entry:experiment:recovery", "entry:procedure:recovery"])

    def test_project_limit_counts_destinations_and_bounds_evidence_separately(self):
        self.put("entity", self.entity("tool:tracekit", kind="tool"))
        self.put("edge", [self.edge("scope-alpha-" + str(i), "entity:tool:tracekit",
                                    "project:alpha", kind="used_in") for i in range(100)] + [
            self.edge("scope-beta", "entity:tool:tracekit", "project:beta", kind="used_in")])
        for action in (("list",), ("get", "tool:tracekit")):
            with self.subTest(action=action):
                result = self.cli("tools", *action)
                tool = result["tools"][0]
                projects = {project["id"]: project for project in tool["projects"]}
                self.assertEqual(set(projects), {"alpha", "beta"})
                self.assertEqual(tool["project_count"], 2)
                self.assertEqual(len(projects["alpha"]["usage_evidence"]), 20)
                self.assertTrue(projects["alpha"]["usage_evidence_truncated"])
                self.assertFalse(projects["beta"]["usage_evidence_truncated"])
                self.assertEqual(projects["beta"]["usage_evidence"], ["edge:scope-beta"])
                self.assertTrue(result["truncated"])
                self.assertEqual(result["limits"]["evidence_edges_per_project"], 20)

    def test_ancestor_limit_counts_destinations_instead_of_parallel_edges(self):
        self.put("entity", self.entity("tool:tracekit", kind="tool"))
        self.put("entry", [self.entry("usage", kind="procedure"), self.entry("first", kind="task"),
                           self.entry("last", kind="task")])
        self.put("edge", [self.edge("use", "entry:usage", "entity:tool:tracekit", kind="uses_tool")] + [
            self.edge("parent-" + str(i).zfill(3), "entry:usage", "entry:first", kind="part_of")
            for i in range(200)] + [self.edge("parent-last", "entry:usage", "entry:last", kind="part_of")])
        result = self.cli("tools", "get", "tool:tracekit")
        tool = result["tools"][0]
        tasks = tool["projects"][0]["tasks"]
        self.assertEqual({task["ref"] for task in tasks}, {"entry:first", "entry:last"})
        first = next(task for task in tasks if task["ref"] == "entry:first")
        self.assertEqual(first["usage_paths"][0]["edge_refs"], ["edge:parent-000"])
        self.assertFalse(result["truncated"])

    def test_ancestor_limit_excludes_the_usage_record_itself(self):
        self.put("entity", self.entity("tool:tracekit", kind="tool"))
        self.put("entry", [self.entry("usage", kind="procedure")] + [
            self.entry("task:" + str(i), kind="task") for i in range(200)])
        self.put("edge", [self.edge("use", "entry:usage", "entity:tool:tracekit", kind="uses_tool")] + [
            self.edge("parent-" + str(i), "entry:usage", "entry:task:" + str(i), kind="part_of")
            for i in range(200)])
        result = self.cli("tools", "get", "tool:tracekit")
        self.assertEqual(result["tools"][0]["task_count"], 200)
        self.assertFalse(result["truncated"])

    def test_usage_and_task_limits_are_reported_without_fabricated_counts(self):
        self.populate()
        conn = memory.connect(self.db, readonly=True)
        self.addCleanup(conn.close)
        args = SimpleNamespace(action="get", id="tool:tracekit", include_hypotheses=False, include_superseded=False)
        with patch.object(tool_catalog, "MAX_RECORDS", 1):
            result = tool_catalog.run(conn, args, memory)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["tools"][0]["record_count"], 1)
        self.assertTrue(all(project["truncated"] for project in result["tools"][0]["projects"]))

    def test_unrelated_entries_do_not_turn_usage_navigation_into_full_scans(self):
        self.populate()
        with sqlite3.connect(str(self.db)) as conn:
            conn.execute("""
                WITH RECURSIVE numbers(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM numbers WHERE n < 10000)
                INSERT INTO entries(id,kind,title,summary,knowledge_state,created_at,updated_at)
                SELECT 'unrelated-' || n, 'note', 'Background', '', 'confirmed', '2026-01-01', '2026-01-01'
                FROM numbers
            """)
        conn = memory.connect(self.db, readonly=True)
        self.addCleanup(conn.close)
        steps = [0]
        def budget():
            steps[0] += 100
            return int(steps[0] > 100000)
        conn.set_progress_handler(budget, 100)
        args = SimpleNamespace(action="get", id="tool:tracekit", include_hypotheses=False, include_superseded=False)
        result = tool_catalog.run(conn, args, memory)
        self.assertEqual(result["tools"][0]["task_count"], 1)
        self.assertLess(steps[0], 100000)

    def test_task_limit_preserves_two_hundred_unique_tasks_and_marks_partial(self):
        self.populate()
        self.put("entry", [self.entry("task:many-" + str(i), kind="task") for i in range(201)])
        self.put("edge", [self.edge("parent-" + str(i),
            "entry:procedure:recovery" if i < 100 else "entry:experiment:recovery",
            "entry:task:many-" + str(i), kind="part_of") for i in range(201)])
        result = self.cli("tools", "get", "tool:tracekit")
        tool = result["tools"][0]
        self.assertEqual(tool["task_count"], 200)
        self.assertTrue(tool["truncated"])
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["records"]), 202)

    def test_limits_missing_and_empty_catalog(self):
        self.assertEqual(self.cli("tools", "list")["tools"], [])
        self.populate()
        value = self.cli("tools", "list", "--limit", "1")
        self.assertTrue(value["truncated"])
        self.assertEqual(value["total_tools"], 2)
        for identity in ("missing", "concept:tracekit"):
            with self.subTest(identity=identity):
                error = self.cli("tools", "get", identity, ok=False)
                self.assertEqual(error["code"], "not_found", error)
        self.assertNotIn("code", self.cli("tools", "get", "x" * 201, ok=False))
        self.cli("tools", "list", "--limit", "201", ok=False)

    def test_recall_and_strict_filter_share_confirmed_usage_scope(self):
        self.populate()
        value = self.cli("recall", "TraceKit", "--project", "alpha")
        tool = next(record for record in value["results"] if record["id"] == "tool:tracekit")
        self.assertEqual(tool["project_ids"], ["alpha", "beta", "gamma"])
        self.assertIn("confirmed_tool_usage", tool["match_reasons"])
        for predicate, expected in (("project = 'alpha'", 2), ("project = 'gamma'", 1),
                                    ("project != 'gamma'", 1), ("project IS NULL", 0),
                                    ("project IS NOT NULL", 2), ("project IN ('alpha', 'beta')", 2)):
            with self.subTest(predicate=predicate):
                result = self.cli("query", "count", "--family", "entity", "--where", "kind = 'tool' AND (" + predicate + ")")
                self.assertEqual(result["count"], expected, result)

    def test_catalog_api_uses_separate_bounded_worker_and_rejects_bad_queries(self):
        server = serve.LoopbackHTTPServer(("127.0.0.1", 0), serve.handler_for(self.db))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        self.addCleanup(connection.close)
        with patch.object(serve, "_run_graph_request", return_value=(200, b'{"tools":[]}')) as worker:
            connection.request("GET", "/api/tools?include_superseded=1")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {"tools": []})
            self.assertTrue(worker.call_args.kwargs["catalog"])
            self.assertEqual(worker.call_args.args[1], ["--include-superseded"])
            connection.request("GET", "/api/tools?tool=tool%3Atracekit&include_hypotheses=1")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            self.assertEqual(worker.call_args.kwargs["tool_id"], "tool:tracekit")
            for query in ("tool=", "tool=x&tool=y", "include_hypotheses=2", "include_hypotheses=1&include_hypotheses=0"):
                connection.request("GET", "/api/tools?" + query)
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                response.read()


if __name__ == "__main__":
    unittest.main()
