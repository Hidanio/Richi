"""Bounded graph views on synthetic stores, preserving full export semantics."""

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from richi import graph, memory, output


class GraphViewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-graph-view-")
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "memory.sqlite3"
        conn = memory.connect(self.db, create=True)
        try:
            memory.initialize(conn, self.db)
        finally:
            conn.close()
        self.put("project", {"id": "a", "name": "Alpha", "source": "test://project"})
        self.put("entry", self.entry("root"))

    @staticmethod
    def entry(identity, **fields):
        value = {"id": identity, "kind": "note", "title": "Entry " + identity,
                 "summary": "Only valid after a successful check. This is recorded evidence.",
                 "project_ids": ["a"], "knowledge_state": "confirmed", "work_state": "done",
                 "sources": [{"reference": "test://" + identity}], "verified_at": "2026-10-01"}
        value.update(fields)
        return value

    @staticmethod
    def edge(identity, source, target, **fields):
        value = {"id": identity, "from_ref": source, "to_ref": target, "kind": "references",
                 "description": "Only this explicit recorded relationship is asserted.",
                 "sources": [{"reference": "test://edge/" + identity}], "knowledge_state": "confirmed"}
        value.update(fields)
        return value

    def put(self, kind, record):
        conn = memory.connect(self.db)
        try:
            with memory.transaction(conn):
                if kind == "project":
                    memory.project_put(conn, record)
                elif kind == "entry":
                    memory.entry_put(conn, record)
                elif kind == "relation":
                    memory.relation_put(conn, record)
                else:
                    graph.put(conn, record, kind, memory)
        finally:
            conn.close()

    def read(self, *flags, compact_json=False, root="entry:root", **kwargs):
        args = self.parser().parse_args(["graph", "neighbors", root, *flags])
        for key, value in kwargs.items():
            setattr(args, key, value)
        before = self.db.read_bytes()
        conn = memory.connect(self.db, readonly=True)
        try:
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
            with memory.transaction(conn, write=False), output.json_mode(compact=compact_json), patch.object(memory, "now", return_value="2026-10-23T12:00:00Z"):
                value = graph.export(conn, args, memory)
                raw = output.rendered(value)
        finally:
            conn.close()
        self.assertEqual(before, self.db.read_bytes())
        if "budget" in value:
            self.assertEqual(value["budget"]["output_chars"], len(raw))
            self.assertLessEqual(len(raw), value["budget"]["max_chars"])
        self.assert_connected(value)
        return value, raw

    @staticmethod
    def parser():
        parser = memory.Parser()
        graph.add_commands(parser.add_subparsers(dest="command", required=True), memory.limit)
        return parser

    def assert_connected(self, value):
        nodes = {item["id"] for item in value["nodes"]}
        self.assertEqual(len(nodes), len(value["nodes"]))
        for edge in value["edges"]:
            self.assertIn(edge["from_ref"], nodes)
            self.assertIn(edge["to_ref"], nodes)
        if not nodes:
            return
        root = value["scope"]["root"]
        self.assertIn(root, nodes)
        reachable = {root}
        while True:
            added = {end for edge in value["edges"] if edge["from_ref"] in reachable or edge["to_ref"] in reachable
                     for end in (edge["from_ref"], edge["to_ref"])}
            if added <= reachable:
                break
            reachable |= added
        self.assertEqual(reachable, nodes)

    def test_full_default_and_explicit_full_keep_legacy_schema(self):
        default, raw = self.read()
        full, full_raw = self.read("--view", "full")
        self.assertEqual(default, full)
        self.assertEqual(raw, full_raw)
        self.assertEqual(set(default), {"schema_version", "generated_at", "nodes", "edges", "truncated", "limits", "scope"})
        self.assertIn("summary", default["nodes"][0])
        self.assertNotIn("summary_excerpt", default["nodes"][0])
        # Map callers historically supply neither new option.
        conn = memory.connect(self.db, readonly=True)
        try:
            args = SimpleNamespace(action="neighbors", ref="entry:root", depth=1, limit=200)
            with patch.object(memory, "now", return_value="2026-10-23T12:00:00Z"):
                self.assertEqual(default, graph.export(conn, args, memory))
        finally:
            conn.close()

    def test_ample_budget_keeps_identities_direction_states_and_provenance(self):
        self.put("entry", self.entry("other"))
        self.put("edge", self.edge("backwards", "entry:other", "entry:root"))
        full, _ = self.read("--depth", "2")
        small, _ = self.read("--view", "compact", "--depth", "2", "--max-chars", "100000")
        self.assertEqual({n["id"] for n in full["nodes"]}, {n["id"] for n in small["nodes"]})
        full_edges = {e["id"]: e for e in full["edges"]}
        self.assertEqual(set(full_edges), {e["id"] for e in small["edges"]})
        for edge in small["edges"]:
            for key in ("from_ref", "to_ref", "kind", "origin", "knowledge_state", "provenance"):
                self.assertEqual(edge[key], full_edges[edge["id"]][key])
            if edge["origin"] == "entry_project":
                self.assertEqual(edge["sources_ref"], edge["from_ref"])
            else:
                self.assertEqual(edge["sources"], full_edges[edge["id"]]["sources"])
                self.assertEqual(edge["read_more"], {"command": "edge get", "id": "backwards"})
        self.assertFalse(small["truncated"])

    def test_memberships_do_not_reload_entry_payload(self):
        for identity in ("b", "c", "d"):
            self.put("project", {"id": identity, "name": identity})
        self.put("entry", self.entry("root", project_ids=["a", "b", "c", "d"]))
        with patch.object(memory, "entry_record", wraps=memory.entry_record) as load:
            value, _ = self.read("--view", "compact", "--max-chars", "100000")
        self.assertEqual(load.call_count, 1)
        self.assertEqual(len(value["edges"]), 4)

    def test_entity_and_project_relation_keep_whole_evidence_and_read_routes(self):
        self.put("project", {"id": "b", "name": "Beta"})
        self.put("entity", {"id": "concept:one", "kind": "concept", "title": "Meaning",
                            "summary": "One recorded definition.", "knowledge_state": "confirmed",
                            "sources": [{"reference": "test://definition", "revision": "v1"}]})
        self.put("edge", self.edge("meaning", "entity:concept:one", "entry:root"))
        self.put("relation", {"id": "ab", "from_project": "a", "to_project": "b", "kind": "depends_on",
                              "description": "A recorded project relation.", "source": "test://relation",
                              "knowledge_state": "confirmed"})
        value, _ = self.read("--view", "compact", "--depth", "2", "--max-chars", "100000")
        concept = next(node for node in value["nodes"] if node["node_type"] == "entity")
        self.assertEqual(concept["read_more"], {"command": "entity get", "id": "concept:one"})
        self.assertEqual(concept["sources"], [{"reference": "test://definition", "revision": "v1"}])
        relation = next(edge for edge in value["edges"] if edge["origin"] == "project_relation")
        self.assertEqual(relation["sources"], [{"reference": "test://relation"}])
        self.assertEqual(relation["provenance"], {"table": "relations", "record_id": "ab"})
        self.assertEqual(relation["read_more"]["args"], ["context", "--project=a", "--limit", "1", "--include-superseded"])
        self.assertEqual(relation["read_more"]["select"], {"collection": "relations", "id": "ab"})
        default, _ = self.read("--view", "compact")
        self.assertEqual(default["budget"]["max_chars"], 16000)

    def test_relation_read_pointer_survives_states_and_neighbor_cap(self):
        self.put("project", {"id": "target", "name": "Target"})
        for index in range(201):
            identity = "neighbor-" + str(index)
            self.put("project", {"id": identity, "name": identity})
            self.put("relation", {"id": "early-" + str(index), "from_project": "a", "to_project": identity,
                                  "kind": "references", "source": "test://fixture", "knowledge_state": "confirmed"})
        for state in ("hypothesis", "superseded"):
            self.put("relation", {"id": "zz-" + state, "from_project": "a", "to_project": "target",
                                  "kind": "references", "description": "The complete " + state + " relation.",
                                  "source": "test://" + state, "knowledge_state": state})
        # The previous pointer loses these late neighbors even with all states.
        capped, _ = self.read("--include-hypotheses", "--include-superseded", root="project:a")
        self.assertTrue(capped["limits"]["nodes_truncated"])
        self.assertNotIn("project:target", {node["id"] for node in capped["nodes"]})
        value, _ = self.read("--view", "compact", "--include-hypotheses", "--include-superseded",
                             "--max-chars", "100000", root="project:target")
        self.assertEqual(len(value["edges"]), 2)
        conn = memory.connect(self.db, readonly=True)
        try:
            with memory.transaction(conn, write=False):
                for edge in value["edges"]:
                    pointer = edge["read_more"]
                    arguments = memory.parser().parse_args(pointer["args"])
                    full = memory.context(conn, arguments)
                    selector = pointer["select"]
                    selected = [item for item in full[selector["collection"]] if item["id"] == selector["id"]]
                    self.assertEqual(len(selected), 1)
                    self.assertEqual(selected[0]["id"], edge["provenance"]["record_id"])
                    self.assertEqual(selected[0]["knowledge_state"], edge["knowledge_state"])
                    self.assertEqual(selected[0]["source"], "test://" + edge["knowledge_state"])
                    self.assertEqual(selected[0]["description"], "The complete " + edge["knowledge_state"] + " relation.")
                    self.assertLessEqual(len(full["entries"]), 1)
                    self.assertGreater(len(full["relations"]), 200)
        finally:
            conn.close()

    def test_oversized_edge_evidence_is_omitted_whole_with_source_count(self):
        self.put("entry", self.entry("other", project_ids=[]))
        source = {"reference": "test://large/" + "x" * 100000}
        self.put("edge", self.edge("huge", "entry:root", "entry:other", sources=[source],
                                   description="Only this edge is evidenced. " * 1000))
        value, _ = self.read("--view", "compact", "--max-chars", "12000")
        edge = next(edge for edge in value["edges"] if edge["origin"] == "explicit")
        self.assertEqual(edge["sources"], [])
        self.assertEqual(edge["source_count"], 1)
        self.assertEqual(edge["sources_omitted"], 1)
        self.assertEqual(edge["read_more"], {"command": "edge get", "id": "huge"})
        self.assertTrue(edge["description_is_excerpt"])
        self.assertTrue(value["truncated"])

    def test_hidden_root_and_endpoints_are_not_false_no_match(self):
        self.put("entry", self.entry("guess", knowledge_state="hypothesis"))
        self.put("entry", self.entry("old", knowledge_state="superseded"))
        for identity in ("guess", "old"):
            self.put("edge", self.edge(identity, "entry:root", "entry:" + identity))
        default, _ = self.read("--view", "compact", "--max-chars", "100000")
        self.assertEqual({n["id"] for n in default["nodes"]}, {"entry:root", "project:a"})
        hidden, _ = self.read("--view", "compact", root="entry:guess")
        self.assertEqual(hidden["nodes"], [])
        self.assertFalse(hidden["scope"]["root_visible"])
        self.assertNotIn("no_match", hidden)
        all_states, _ = self.read("--view", "compact", "--include-hypotheses", "--include-superseded", "--max-chars", "100000")
        self.assertTrue({"entry:guess", "entry:old"} <= {n["id"] for n in all_states["nodes"]})

    def test_dense_cycles_are_bounded_in_both_renderers_and_keep_paths(self):
        for index in range(12):
            self.put("entry", self.entry("n" + str(index), summary="Only a test; not a production claim. " * 100,
                                         sources=[{"reference": "test://source/" + str(i)} for i in range(8)]))
        refs = ["entry:root"] + ["entry:n" + str(i) for i in range(12)]
        for index, ref in enumerate(refs):
            for offset in (1, 2, 3):
                self.put("edge", self.edge(str(index) + "-" + str(offset), ref, refs[(index + offset) % len(refs)]))
        for format_compact in (False, True):
            for budget in (2000, 4000, 16000, 100000):
                with self.subTest(compact=format_compact, budget=budget):
                    value, _ = self.read("--view", "compact", "--depth", "3", "--max-chars", str(budget), compact_json=format_compact)
                    self.assertEqual(value["nodes"][0]["id"], "entry:root")
                    self.assertTrue(value["truncated"])
                    self.assertEqual(value["budget"]["omitted_nodes"], 14 - len(value["nodes"]))
        limited, _ = self.read("--view", "compact", "--depth", "3", "--limit", "1", "--max-chars", "100000")
        self.assertTrue(limited["limits"]["nodes_truncated"])
        self.assertEqual(len(limited["nodes"]), 1)

    def test_huge_sources_and_maximum_escaped_id_have_honest_fallback(self):
        identity = "\\" * 200
        source = {"reference": "test://" + "x" * 100000, "label": "evidence"}
        self.put("entry", self.entry(identity, title="Huge title " * 10000, summary="Long sentence " * 10000, sources=[source]))
        for format_compact in (False, True):
            for budget in (2000, 4000, 16000):
                with self.subTest(compact=format_compact, budget=budget):
                    value, _ = self.read("--view", "compact", "--max-chars", str(budget), root="entry:" + identity, compact_json=format_compact)
                    self.assertTrue(value["truncated"])
                    self.assertEqual(value["scope"]["root"], "entry:" + identity)
                    self.assertEqual(value["nodes"][0]["id"], "entry:" + identity)
                    for node in value["nodes"]:
                        self.assertTrue(node.get("payload_omitted") or node.get("sources_omitted", 0) or node["node_type"] == "project")
                        for displayed in node.get("sources", []):
                            self.assertIn(displayed, [source, {"reference": "test://project"}])

    def test_edge_cap_cannot_leave_disconnected_nodes(self):
        for identity in ("one", "two", "three"):
            self.put("entry", self.entry(identity))
        with patch.object(graph, "MAX_EDGES", 1):
            value, _ = self.read("--view", "compact", "--depth", "2", "--max-chars", "100000")
        self.assertTrue(value["limits"]["edges_truncated"])
        self.assertGreater(value["limits"]["disconnected_nodes_omitted"], 0)
        self.assertEqual(value["budget"]["omitted_nodes"], 0)
        self.assertEqual(value["budget"]["omitted_edges"], 0)

    def test_invalid_arguments_fail_and_full_never_ignores_budget(self):
        for flags in (("--max-chars", "4000"), ("--view", "full", "--max-chars", "4000"),
                      ("--view", "compact", "--max-chars", "1999"), ("--view", "compact", "--max-chars", "100001"),
                      ("--view", "tiny"), ("--depth", "4"), ("--limit", "0")):
            with self.subTest(flags=flags), self.assertRaises(memory.MemoryError):
                self.read(*flags)
        with self.assertRaises(memory.MemoryError):
            self.read("--view", "compact", root="entry:missing")
        with self.assertRaises(memory.MemoryError):
            self.parser().parse_args(["graph", "export", "--view", "compact"])


if __name__ == "__main__":
    unittest.main()
