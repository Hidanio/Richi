"""Progressive retrieval contracts against synthetic, disposable stores."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from richi import graph, memory, output, recall, retrieval_tools
from cli_environment import cli_environment


class RetrievalToolsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-progressive-test-")
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "synthetic.sqlite3"
        self.conn = memory.connect(self.db, create=True)
        self.addCleanup(self.conn.close)
        memory.initialize(self.conn, self.db)
        for identity in ("alpha", "beta"):
            memory.project_put(self.conn, {"id": identity, "name": identity})

    def entry(self, identity, **changes):
        value = dict(id=identity, kind="note", title="Recorded observation", summary="A measured outcome.",
                     project_ids=["alpha"], sources=[{"reference": "https://example.test/source"}],
                     aliases=[], tags=[], knowledge_state="confirmed", verified_at="2026-10-01")
        value.update(changes)
        memory.entry_put(self.conn, value)
        return value

    def entity(self, identity, **changes):
        value = dict(id=identity, kind="concept", title="Recorded definition", summary="A scoped meaning.",
                     sources=[{"reference": "https://example.test/source"}],
                     aliases=[], tags=[], knowledge_state="confirmed", verified_at="2026-10-01")
        value.update(changes)
        graph.put(self.conn, value, "entity", memory)
        return value

    def edge(self, identity, source, target, **changes):
        value = dict(id=identity, from_ref=source, to_ref=target, kind="informs", description="Evidence link.",
                     sources=[{"reference": "https://example.test/edge"}],
                     knowledge_state="confirmed", verified_at="2026-10-01")
        value.update(changes)
        graph.put(self.conn, value, "edge", memory)
        return value

    def args(self, command, **changes):
        args = dict(command=command, query="routingneedle", project=None, limit=8,
                    max_chars=100000, include_hypotheses=False, include_superseded=False,
                    refs=[], offset=0)
        args.update(changes)
        return SimpleNamespace(**args)

    def run_tool(self, command, compact=False, **changes):
        args = self.args(command, **changes)
        with output.json_mode(compact=compact):
            result = retrieval_tools.run(self.conn, args, memory)
            serialized = output.rendered(result)
            self.assertLessEqual(len(serialized), args.max_chars)
            self.assertEqual(result["budget"]["output_chars"], len(serialized))
            self.assertTrue(serialized.endswith("\n"))
        return result

    def test_discover_same_ranking_states_jira_and_project_boost(self):
        self.entry("experiment:native-12", aliases=["routingneedle"], jira_key="DEMO-12")
        self.entry("other", aliases=["routingneedle"], project_ids=["beta"], jira_key="DEMO-120")
        self.entry("guess", aliases=["routingneedle"], knowledge_state="hypothesis")
        self.entry("old", aliases=["routingneedle"], knowledge_state="superseded")
        for flags in ({}, {"project": "beta"}, {"include_hypotheses": True},
                      {"include_superseded": True}, {"query": "DEMO-12"},
                      {"query": "entry:experiment:native-12"}):
            with self.subTest(flags=flags):
                expected = recall.recall(self.conn, self.args("discover", **flags), memory, bounded=False)
                actual = self.run_tool("discover", **flags)
                self.assertEqual([(c["ref"], c["retrieval_score"], c["knowledge_state"]) for c in actual["results"]],
                                 [(c["ref"], c["retrieval_score"], c["knowledge_state"]) for c in expected["results"]])
                self.assertEqual(actual["selection"], expected["selection"])
                self.assertEqual(actual["candidate_count"], expected["candidate_count"])

    def test_discover_omits_sources_without_losing_exact_edge_provenance(self):
        evidence = [{"reference": "https://example.test/" + "x" * 50000} for _ in range(3)]
        self.entry("task:native-id", aliases=["routingneedle"], sources=evidence,
                   summary="Leading condition. " + "A measured result. " * 100)
        self.entity("concept:neighbor")
        self.edge("relation:edge-id", "entity:concept:neighbor", "entry:task:native-id", sources=evidence)
        result = self.run_tool("discover", max_chars=8000)
        self.assertFalse(result["full_records"])
        self.assertIn("before conclusions", result["notice"])
        for card in result["results"]:
            self.assertNotIn("sources", card)
            self.assertLessEqual(len(card["summary_excerpt"]), 300)
            self.assertFalse(card["full_record"])
        edge = result["edges"][0]
        self.assertEqual(edge["id"], "edge:relation:edge-id")
        self.assertEqual(edge["from_ref"], "entity:concept:neighbor")
        self.assertEqual(edge["to_ref"], "entry:task:native-id")
        self.assertEqual(edge["origin"], "explicit")
        self.assertEqual(edge["provenance"], {"table": "graph_edges", "record_id": "relation:edge-id"})
        self.assertEqual(edge["read_more"]["args"], ["edge", "get", "--", "relation:edge-id"])
        self.assertEqual(edge["source_count"], 3)
        self.assertEqual(edge["sources_omitted"], 3)
        self.assertNotIn("sources", edge)

    def test_ambiguity_and_no_match_survive_minimum_budget(self):
        for number in range(4):
            self.entity("concept:" + str(number) + "x" * 180, aliases=["routingneedle"], title="T" * 500)
        for compact in (False, True):
            result = self.run_tool("discover", compact=compact, max_chars=2000, limit=1)
            self.assertFalse(result["no_match"])
            self.assertEqual(result["candidate_count"], 4)
            self.assertTrue(result["ambiguity"]["detected"])
            self.assertEqual(result["ambiguity"]["concept_count"], 4)
            self.assertEqual(result["ambiguity"]["omitted_candidates"],
                             4 - len(result["ambiguity"]["candidates"]))
            empty = self.run_tool("discover", compact=compact, max_chars=2000, query="unmatchedzq")
            self.assertTrue(empty["no_match"])
            self.assertIn("never happened", empty["coverage_note"])

    def test_oversized_discovery_metadata_does_not_report_false_no_match(self):
        project_ids = []
        for number in range(8):
            identity = str(number) + "p" * 199
            memory.project_put(self.conn, {"id": identity, "name": identity})
            project_ids.append(identity)
        self.entry("n" * 200, aliases=["routingneedle"], project_ids=project_ids)
        result = self.run_tool("discover", max_chars=2000)
        self.assertFalse(result["no_match"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["results"], [])
        self.assertEqual(result["read_more"][0]["args"], ["entry", "get", "--", "n" * 200])

    def test_fetch_whole_records_equal_individual_get_without_history(self):
        self.entry("task:alpha-12")
        self.entry("task:alpha-12", summary="Updated observation.")
        self.entity("concept:beta-13", knowledge_state="superseded")
        self.edge("edge:tricky-14", "entry:task:alpha-12", "entity:concept:beta-13")
        expected = [memory.entry_get(self.conn, "task:alpha-12"),
                    graph.get(self.conn, "entity", "concept:beta-13", memory, history=False),
                    graph.get(self.conn, "edge", "edge:tricky-14", memory, history=False),
                    dict(memory.require_project(self.conn, "alpha"))]
        refs = ["entry:task:alpha-12", "entity:concept:beta-13", "edge:edge:tricky-14", "project:alpha"]
        for compact in (False, True):
            result = self.run_tool("fetch", compact=compact, refs=refs + refs[:1])
            self.assertEqual([r["record"] for r in result["results"]], expected)
            self.assertEqual([r["ref"] for r in result["results"]], refs)
            self.assertEqual(result["requested_count"], 4)
            self.assertTrue(result["all_requested_records_returned"])
            self.assertTrue(result["request_complete"])
            self.assertIsNone(result["next_offset"])
            self.assertTrue(all("history" not in r["record"] for r in result["results"]))

    def test_fetch_mixed_missing_oversize_and_unbounded_source_without_loading(self):
        self.entry("small")
        self.entry("big", summary="x" * 100001)
        self.entry("source-huge", sources=[{"reference": "x" * 1000000}])
        original = memory.entry_get
        loaded = []
        def tracked(conn, identity, history=False):
            loaded.append(identity)
            return original(conn, identity, history=history)
        with patch.object(memory, "entry_get", side_effect=tracked):
            result = self.run_tool("fetch", refs=["entry:small", "entry:missing", "entry:big", "entry:source-huge"])
        self.assertEqual(loaded, ["small"])
        self.assertEqual([r["status"] for r in result["results"]], ["found", "missing", "oversize", "read_limit"])
        self.assertFalse(result["all_requested_records_returned"])
        self.assertTrue(result["request_complete"])
        for row in result["results"][1:]:
            self.assertNotIn("record", row)
            self.assertEqual(row["read_more"]["args"][-1], row["ref"].partition(":")[2])

    def test_fetch_long_escaped_refs_progress_through_minimum_budget(self):
        refs = ["entry:" + str(number).zfill(2) + '"' * 198 for number in range(32)]
        for compact in (False, True):
            offset, returned = 0, []
            while offset is not None:
                result = self.run_tool("fetch", compact=compact, refs=refs, offset=offset, max_chars=2000)
                self.assertGreater(result["processed_count"], 0)
                self.assertEqual(result["remaining_count"], 32 - offset - result["processed_count"])
                returned.extend(row["ref"] for row in result["results"])
                offset = result["next_offset"]
            self.assertEqual(returned, refs)

    def test_fetch_continuation_keeps_whole_records_and_input_order(self):
        refs = []
        for number in range(6):
            identity = "task:" + str(number)
            self.entry(identity, summary="Sentence. " * 20)
            refs.append("entry:" + identity)
        for compact in (False, True):
            offset, returned = 0, []
            while offset is not None:
                result = self.run_tool("fetch", compact=compact, refs=refs, offset=offset, max_chars=2300)
                self.assertGreater(result["processed_count"], 0)
                for row in result["results"]:
                    self.assertEqual(row["status"], "found")
                    self.assertEqual(row["record"], memory.entry_get(self.conn, row["record"]["id"]))
                    returned.append(row["ref"])
                offset = result["next_offset"]
            self.assertEqual(returned, refs)

    def test_snapshot_is_consistent_and_no_writes_or_history_reads(self):
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.entry("first")
        self.entry("second", summary="Before concurrent update.")
        writer = memory.connect(self.db)
        self.addCleanup(writer.close)
        statements, original = [], memory.entry_get
        def concurrent(conn, identity, history=False):
            value = original(conn, identity, history=history)
            if identity == "first":
                writer.execute("UPDATE entries SET summary = 'After concurrent update.' WHERE id = 'second'")
            return value
        self.conn.set_trace_callback(statements.append)
        with patch.object(memory, "entry_get", side_effect=concurrent):
            result = self.run_tool("fetch", refs=["entry:first", "entry:second"])
        self.conn.set_trace_callback(None)
        self.assertEqual(result["results"][1]["record"]["summary"], "Before concurrent update.")
        self.assertEqual(original(self.conn, "second")["summary"], "After concurrent update.")
        self.assertEqual(sum(s == "BEGIN" for s in statements), 1)
        self.assertEqual(sum(s == "COMMIT" for s in statements), 1)
        self.assertFalse(any("history" in s.lower() for s in statements))
        self.assertFalse(any(s.lstrip().split(" ", 1)[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"} for s in statements))

    def test_caller_transaction_and_invalid_arguments(self):
        self.entry("-native:dash-id")
        with memory.transaction(self.conn, write=False):
            result = self.run_tool("fetch", refs=["entry:-native:dash-id"])
            self.assertTrue(self.conn.in_transaction)
            self.assertEqual(result["results"][0]["record"]["id"], "-native:dash-id")
        for changes in ({"refs": ["wrong:id"]}, {"refs": ["entry:"]},
                        {"refs": ["entry:" + str(i) for i in range(33)]},
                        {"refs": ["entry:a"], "offset": -1}, {"refs": ["entry:a"], "offset": 2},
                        {"refs": ["entry:a"], "max_chars": 1999}):
            with self.subTest(changes=changes), self.assertRaises(memory.MemoryError):
                retrieval_tools.run(self.conn, self.args("fetch", **changes), memory)

    def test_installed_cli_emits_actual_bounded_json(self):
        self.entry("task:cli-42", aliases=["routingneedle"])
        expected = memory.entry_get(self.conn, "task:cli-42")
        for compact in (False, True):
            flags = ["--compact-json"] if compact else []
            for command in (["discover", "routingneedle"], ["fetch", "--ref", "entry:task:cli-42"]):
                result = subprocess.run(
                    [sys.executable, "-m", "richi", "--db", str(self.db)] + flags + command +
                    ["--max-chars", "2000"], capture_output=True, text=True, timeout=20,
                    env=cli_environment(self.db.parent, PYTHONDONTWRITEBYTECODE="1"))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertLessEqual(len(result.stdout), 2000)
                value = json.loads(result.stdout)
                self.assertEqual(value["budget"]["output_chars"], len(result.stdout))
                if command[0] == "fetch":
                    self.assertEqual(value["results"][0]["record"], expected)

    def test_cli_parser_defaults_and_safe_read_pointer(self):
        import argparse
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="command", required=True)
        retrieval_tools.add_commands(commands, memory)
        args = parser.parse_args(["discover", "needle"])
        self.assertEqual(args.max_chars, 8000)
        self.assertEqual(args.limit, 8)
        args = parser.parse_args(["fetch", "--ref", "entry:-native:id"])
        self.assertEqual(args.max_chars, 16000)
        self.assertEqual(args.offset, 0)
        self.assertEqual(memory.parser().parse_args(["entry", "get", "--", "-native:id"]).id, "-native:id")


if __name__ == "__main__":
    unittest.main()
