"""Compatibility of cited measurements with rollback, compact readers and full replacements."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from richi import compact, graph, measurements, memory, recall, retrieval_tools, task_brief


class MeasurementTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "memory.sqlite3"
        self.conn = memory.connect(self.db, create=True)
        self.addCleanup(self.conn.close)
        memory.initialize(self.conn, self.db)
        self.rows = [{"metric": "deadline completion", "value": str(i), "unit": "complete RPCs",
                      "sample": "560 per mode, " + "paired batches " * 10} for i in range(100)]
        self.citation = {"reference": "test://calculation", "locator": "table:3"}
        self.source = dict(self.citation, measurements=self.rows)

    def put(self, kind, payload):
        with memory.transaction(self.conn):
            if kind == "entry":
                return memory.entry_put(self.conn, payload)
            return graph.put(self.conn, payload, kind, memory)

    def get(self, kind, history=False):
        if kind == "entry":
            return memory.entry_get(self.conn, "shared", history)
        return graph.get(self.conn, kind, "shared", memory, history)

    def create(self):
        for kind in ("entry", "entity", "edge"):
            fields = ({"kind": "metric", "title": "Unrelated trial", "summary": "A related experiment."}
                      if kind == "entity" else {"kind": "task", "title": "Lexical needle", "summary": "A package check."})
            if kind == "edge":
                fields = {"kind": "informs", "from_ref": "entry:shared", "to_ref": "entity:shared", "description": "Evidence"}
            self.put(kind, dict(fields, id="shared", sources=[self.source], knowledge_state="confirmed"))

    def legacy_edit(self, kind, **edits):
        # A pre-annotation Richi reads only the base columns. Its normal CAS write
        # replaces those columns and records the before/after it saw in history.
        table = {"entry": "entries", "entity": "entities", "edge": "graph_edges"}[kind]
        history, key = measurements.HISTORY[kind]
        before = self.get(kind)
        raw = self.conn.execute("SELECT sources FROM " + table + " WHERE id = 'shared'").fetchone()[0]
        before["sources"] = json.loads(raw)
        self.assertTrue(all("measurements" not in source for source in before["sources"]))
        after = dict(before, updated_at=memory.next_updated_at(before["updated_at"]), **edits)
        fields = list(edits) + ["updated_at"]
        with memory.transaction(self.conn):
            self.conn.execute("UPDATE " + table + " SET " + ",".join(field + " = ?" for field in fields) +
                              " WHERE id = ? AND updated_at = ?",
                              [memory.dumps(after[field]) if field == "sources" else after[field] for field in fields] +
                              ["shared", before["updated_at"]])
            self.conn.execute("INSERT INTO " + history + "(" + key + ",changed_at,before_json,after_json) "
                              "VALUES (?, ?, ?, ?)", ("shared", after["updated_at"], memory.dumps(before), memory.dumps(after)))

    def test_rollback_can_edit_without_erasing_or_transferring_observations(self):
        self.create()
        for kind in ("entry", "entity", "edge"):
            with self.subTest(kind=kind):
                before = self.get(kind)
                self.legacy_edit(kind, **{("description" if kind == "edge" else "title"): "Editorial change"})
                self.assertEqual(self.get(kind)["sources"], [self.source])
                fields = {k: v for k, v in before.items() if k not in ("updated_at", "created_at")}
                with self.assertRaisesRegex(memory.MemoryError, "conflict"):
                    self.put(kind, dict(fields, expected_updated_at=before["updated_at"]))
                self.legacy_edit(kind, sources=[{"reference": "test://other-calculation"}])
                self.legacy_edit(kind, sources=[self.citation])
                current = self.get(kind, history=True)
                self.assertEqual(current["sources"], [self.citation])
                self.assertEqual(current["history"][0]["after"]["sources"], [self.source])

    def test_compact_summaries_keep_exact_hit_and_cited_neighbor(self):
        self.create()
        args = SimpleNamespace(query="Lexical needle", project=None, limit=8, max_chars=16000,
                               include_hypotheses=False, include_superseded=False)
        witness = dict(self.citation, measurements_omitted=len(self.rows))
        output = recall.recall(self.conn, args, memory)
        self.assertEqual({card["ref"] for card in output["results"]}, {"entry:shared", "entity:shared"})
        self.assertEqual(output["edges"][0]["sources"], [witness])
        self.assertTrue(output["truncated"])
        for card in output["results"]:
            self.assertEqual(card["sources"], [witness])
        view = compact.fit_response({"entry": compact.card(self.get("entry"), "entry:shared")}, 16000)
        self.assertEqual(view["entry"]["sources"], [witness])
        result = task_brief.brief(self.conn, args, memory, self.db)
        self.assertEqual({card["ref"] for card in result["results"]}, {"entry:shared", "entity:shared"})
        for card in result["results"]:
            row = card["measurements"][0]
            self.assertEqual(row["source"], self.citation)
            self.assertEqual(card["connections"][0]["sources"], [witness])

    def test_development_inline_source_is_read_and_normalized_on_unchanged_put(self):
        self.create()
        with memory.transaction(self.conn):
            self.conn.execute("DELETE FROM metadata WHERE key IN (?, ?)", (measurements.KEY_PREFIX + "entry:shared",
                                                                          measurements.INFO_PREFIX + "entry:shared"))
            self.conn.execute("UPDATE entries SET sources = ? WHERE id = 'shared'", (memory.dumps([self.source]),))
        before = self.get("entry")
        self.assertEqual(before["sources"], [self.source])
        payload = {k: v for k, v in before.items() if k not in ("updated_at", "created_at")}
        self.assertEqual(self.put("entry", payload)["status"], "unchanged")
        self.legacy_edit("entry", title="Edited after rollback")
        self.assertEqual(self.get("entry")["sources"], [self.source])

    def test_full_graph_keeps_measurements_once_per_owner_for_both_storage_formats(self):
        self.create()
        projects = ["first", "second"]
        entry = {key: value for key, value in self.get("entry").items() if key not in {"created_at", "updated_at"}}
        sources = [{"reference": "test://background"}, {"reference": "test://notes"}, self.source]
        with memory.transaction(self.conn):
            for project in projects:
                memory.project_put(self.conn, {"id": project, "name": project})
            memory.entry_put(self.conn, dict(entry, sources=sources, project_ids=projects))
        for inline in (False, True):
            if inline:
                with memory.transaction(self.conn):
                    self.conn.execute("DELETE FROM metadata WHERE key IN (?, ?)",
                                      (measurements.KEY_PREFIX + "entry:shared", measurements.INFO_PREFIX + "entry:shared"))
                    self.conn.execute("UPDATE entries SET sources = ? WHERE id = 'shared'", (memory.dumps(sources),))
            with self.subTest(inline=inline), patch.object(memory, "entry_record", wraps=memory.entry_record) as read:
                exported = graph.export(self.conn, SimpleNamespace(action="export"), memory)
                self.assertEqual(read.call_count, 1)
                nodes = {node["id"]: node for node in exported["nodes"]}
                self.assertEqual(nodes["entry:shared"]["sources"], sources)
                self.assertEqual(nodes["entity:shared"]["sources"], [self.source])
                explicit, = [edge for edge in exported["edges"] if edge["origin"] == "explicit"]
                self.assertEqual(explicit["sources"], [self.source])
                memberships = [edge for edge in exported["edges"] if edge["origin"] == "entry_project"]
                self.assertEqual(len(memberships), len(projects))
                for edge in memberships:
                    self.assertEqual(edge["sources_ref"], "entry:shared")
                    self.assertEqual(edge["sources"], sources[:2] + [dict(self.citation, measurements_omitted=100)])

    def test_fetch_measures_detached_observations_before_decoding_any_owner(self):
        self.rows[0]["sample"] = "x" * 900000
        self.create()
        refs = [kind + ":shared" for kind in ("entry", "entity", "edge")]
        args = SimpleNamespace(command="fetch", refs=refs, max_chars=4000)
        with patch.object(memory, "entry_get", side_effect=AssertionError("loaded oversized entry")), \
             patch.object(graph, "get", side_effect=AssertionError("loaded oversized graph record")):
            results = retrieval_tools.run(self.conn, args, memory)["results"]
        self.assertEqual([r["status"] for r in results], ["read_limit"] * 3)
        self.assertTrue(all(r["stored_chars"] > 900000 for r in results))
        statements = []
        self.conn.set_trace_callback(statements.append)
        try:
            args = SimpleNamespace(query="Lexical needle", project=None, limit=8, max_chars=4000,
                                   include_hypotheses=False, include_superseded=False)
            response = recall.recall(self.conn, args, memory)
            self.assertTrue(response["results"])
            compact_record = memory.entry_get(self.conn, "shared", counts_only=True)
            self.assertEqual(compact_record["sources"][0]["measurements_omitted"], 100)
        finally:
            self.conn.set_trace_callback(None)
        self.assertFalse(any("SELECT value FROM metadata" in sql and measurements.KEY_PREFIX in sql
                             for sql in statements))
        self.legacy_edit("entry", sources=[{"reference": "test://replacement"}])
        result = retrieval_tools.run(self.conn, SimpleNamespace(command="fetch", refs=["entry:shared"], max_chars=4000), memory)
        self.assertEqual(result["results"][0]["status"], "found")
        self.assertEqual(result["results"][0]["record"]["sources"], [{"reference": "test://replacement"}])

    def test_fetch_accounts_for_history_read_after_rollback(self):
        self.create()
        self.legacy_edit("entry", title="z" * 900000)
        self.legacy_edit("entry", title="Short again")
        args = SimpleNamespace(command="fetch", refs=["entry:shared"], max_chars=4000)
        with patch.object(memory, "entry_get", side_effect=AssertionError("decoded oversized history")):
            result, = retrieval_tools.run(self.conn, args, memory)["results"]
        self.assertEqual(result["status"], "read_limit")
        self.assertGreater(result["stored_chars"], 900000)
        self.assertEqual(self.get("entry")["sources"], [self.source])
