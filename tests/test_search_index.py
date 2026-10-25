"""Derived index contracts against disposable public fixtures, never live memory."""

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from richi import graph, memory, recall, search_index


class SearchIndexTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-derived-search-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "synthetic.sqlite3"
        self.conn = memory.connect(self.path, create=True)
        self.addCleanup(self.conn.close)
        memory.initialize(self.conn, self.path)

    def insert(self, family, identity, **changes):
        """A plain SQL writer deliberately bypasses the new refresh API."""
        record = {
            "id": identity, "kind": "note" if family == "entry" else "component",
            "title": "Neutral title", "summary": "Stored observation", "knowledge_state": "confirmed",
            "sources": [{"reference": "test://synthetic/evidence"}], "aliases": [], "tags": [],
            "created_at": "2026-09-20T10:00:00Z", "updated_at": "2026-09-21T10:00:00Z", "verified_at": None,
        }
        if family == "entry":
            record.update(jira_key=None, work_state=None)
        record.update(changes)
        stored = {key: json.dumps(value, ensure_ascii=False) if key in {"sources", "aliases", "tags"} else value
                  for key, value in record.items()}
        table = "entries" if family == "entry" else "entities"
        self.conn.execute("INSERT INTO " + table + " (" + ",".join(stored) + ") VALUES (" +
                          ",".join("?" for _ in stored) + ")", list(stored.values()))
        return record

    def build(self):
        with memory.transaction(self.conn):
            return search_index.build(self.conn, memory)

    def candidates(self, family, query, conn=None):
        terms, keys = recall.terms_for(query)
        if family == "entry":
            return search_index.entry_ids(conn or self.conn, query, keys)
        return search_index.entity_ids(conn or self.conn, query, terms, keys)

    def snapshot(self, tables):
        return {table: [tuple(row) for row in self.conn.execute("SELECT * FROM " + table + " ORDER BY 1")]
                for table in tables}

    def test_absent_index_reads_and_refresh_do_not_create_schema(self):
        self.insert("entry", "one")
        before = self.snapshot(["sqlite_master", "metadata", "entries"])
        changes = self.conn.total_changes
        self.assertFalse(search_index.status(self.conn)["available"])
        self.assertIsNone(self.candidates("entry", "one"))
        self.assertIsNone(self.candidates("entity", "one"))
        search_index.refresh_record(self.conn, "entries", "one")
        self.assertEqual(self.conn.total_changes, changes)
        self.assertEqual(before, self.snapshot(before))

    def test_build_is_additive_and_excludes_sources_and_history(self):
        huge_evidence = "provenanceonlyneedle " * 30000
        with memory.transaction(self.conn):
            memory.entry_put(self.conn, {"id": "note", "kind": "note", "title": "Visible title",
                                        "summary": "Visible summary", "aliases": ["Search alias"],
                                        "sources": [{"reference": huge_evidence}], "knowledge_state": "confirmed"})
            graph.put(self.conn, {"id": "component", "kind": "component", "title": "Visible entity",
                                  "summary": "Entity summary", "sources": [{"reference": huge_evidence}],
                                  "knowledge_state": "confirmed"}, "entity", memory)
        authoritative = ["entries", "entities", "entry_history", "entity_history", "graph_nodes"]
        before = self.snapshot(authoritative)
        backend_before = memory.backend(self.conn)
        result = self.build()
        self.assertTrue(result["available"])
        self.assertEqual(result["dirty"], {"entry": 0, "entity": 0})
        self.assertLess(result["key_bytes"], 1000)
        self.assertEqual(self.snapshot(authoritative), before)
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(memory.backend(self.conn), backend_before)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM " + search_index.KEYS +
                                           " WHERE instr(key, 'provenanceonlyneedle') > 0").fetchone()[0], 0)
        self.assertNotIn("provenanceonlyneedle", json.dumps(result))
        with memory.connect(self.path, readonly=True) as readonly:
            self.assertEqual(self.candidates("entry", "SEARCH ALIAS", readonly), {"note"})
            self.assertEqual(search_index.status(readonly)["keys"], result["keys"])

    def test_entry_routing_matches_existing_unicode_jira_and_phrase_rules(self):
        for identity, changes in [
            ("Straße:Ёлка", {}), ("task:demo-42", {}), ("jira:demo42", {}),
            ("ordinary", {"jira_key": "DEMO-42"}),
            ("unicode-alias", {"aliases": ["СЕВЕРНЫЙ ЁЖ", "Straße", "DATA::Layer", "Y"]}),
            ("embedded-alias", {"aliases": ["cache routing"]}),
            ("single-alias", {"aliases": ["cache"]}),
            ("ambiguous-alias", {"aliases": ["cache routing"], "knowledge_state": "superseded"}),
        ]:
            self.insert("entry", identity, **changes)
        self.build()
        rows = list(self.conn.execute("SELECT id, jira_key, aliases FROM entries"))
        for query in ["straße:ёлка", "ENTRY:STRASSE:ЕЛКА", "DEMO42", "DEMO-42", "DEMO-420",
                      "почему северный еж изменился", "Straße", "what uses DATA::Layer", "Y",
                      "why cache routing changed", "why cache changed", "unknown",
                      " ".join(["word" + str(i) for i in range(60)] + ["cache", "routing"])]:
            with self.subTest(query=query):
                _, keys = recall.terms_for(query)
                variants = {variant for key in keys for variant in recall.key_variants(key)}
                expected = {row["id"] for row in rows if recall.entry_routing_match(
                    row, recall.normalize(query).strip(), recall.phrase(query), variants)}
                self.assertEqual(self.candidates("entry", query), expected)

    def test_entity_candidates_cover_every_positive_lexical_score(self):
        records = []
        examples = ["repo.foo/bar-baz_quux", "FOO::bar", "C++ needle", "x\u0338Alpha", "İstanbul",
                    "ЁЛКА Straße", "Data::Metric", "DEMO-42", "TASK123", "Привет_мир",
                    "alpha + beta", "a💡neon", "Ｆｏｏ", "Δείγμα-κόσμος"]
        for field in ("id", "title", "summary", "aliases", "tags"):
            for number, text in enumerate(examples):
                identity = text if field == "id" else field + ":" + str(number)
                values = {} if field == "id" else {field: [text] if field in {"aliases", "tags"} else text}
                records.append(self.insert("entity", identity, **values))
        records.extend([
            self.insert("entity", "symbol", kind="concept", aliases=["Y", "why"]),
            self.insert("entity", "concept-one", kind="concept", aliases=["cache routing"]),
            self.insert("entity", "concept-two", kind="concept", aliases=["cache routing"]),
            self.insert("entity", "task:demo42"),
        ])
        self.build()
        queries = examples + ["foo", "bar", "baz", "quux", "needle", "Alpha", "stanbul", "ел", "STRASS",
                              "metric", "DEMO42", "TASK-123", "мир", "beta", "ne", "κόσ",
                              "what is Y", "why", "why cache routing", "entity:concept-one", "no matches"]
        # Cross-check against the authoritative scoring function, not an index
        # tokenization reimplementation. Over-inclusion is deliberately allowed.
        for query in queries:
            with self.subTest(query=query):
                terms, keys = recall.terms_for(query)
                expected = {record["id"] for record in records
                            if recall.lexical(record, query, terms, keys, "entity")[0] > 0}
                self.assertTrue(expected.issubset(self.candidates("entity", query)), expected)
        self.assertTrue({"concept-one", "concept-two"}.issubset(self.candidates("entity", "why cache routing")))

    def test_entity_candidates_never_cap_or_return_early_on_exact_id(self):
        for number in range(400):
            self.insert("entity", "item" + str(number), summary="Sharedmeaning explanation")
        self.insert("entity", "sharedmeaning", summary="Exact identity")
        self.build()
        self.assertEqual(len(self.candidates("entity", "sharedmeaning")), 401)

    def test_old_writer_dirty_records_are_always_candidates_until_refreshed(self):
        self.insert("entry", "old-entry", aliases=["Before alias"])
        self.insert("entity", "old-entity", title="Beforeterm")
        self.build()
        # Separate stock sqlite connection: persistent triggers need no Python
        # functions and an older CLI can still write to schema version 2.
        with sqlite3.connect(str(self.path)) as old_writer:
            old_writer.execute("UPDATE entries SET aliases = ? WHERE id = 'old-entry'", ('["After alias"]',))
            old_writer.execute("UPDATE entities SET title = 'Afterterm' WHERE id = 'old-entity'")
        self.insert("entity", "new-entity", title="Newterm")
        self.assertEqual(search_index.status(self.conn)["dirty"], {"entry": 1, "entity": 2})
        self.assertIn("old-entry", self.candidates("entry", "After alias"))
        self.assertEqual(self.candidates("entity", "unrelated"), {"old-entity", "new-entity"})
        search_index.refresh_record(self.conn, "entries", "old-entry")
        search_index.refresh_record(self.conn, "entities", "old-entity")
        search_index.refresh_record(self.conn, "entities", "new-entity")
        self.assertEqual(search_index.status(self.conn)["dirty"], {"entry": 0, "entity": 0})
        self.assertEqual(self.candidates("entry", "Before alias"), set())
        self.assertEqual(self.candidates("entry", "After alias"), {"old-entry"})
        self.assertEqual(self.candidates("entity", "beforeterm"), set())
        self.assertEqual(self.candidates("entity", "afterterm"), {"old-entity"})

    def test_new_write_refresh_rolls_back_with_authoritative_record(self):
        self.insert("entry", "one", aliases=["Before alias"])
        self.build()
        before = self.snapshot(["entries", search_index.KEYS, search_index.DIRTY])
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with memory.transaction(self.conn):
                self.conn.execute("UPDATE entries SET aliases = '[\"After alias\"]' WHERE id = 'one'")
                search_index.refresh_record(self.conn, "entries", "one")
                self.assertEqual(self.candidates("entry", "After alias"), {"one"})
                raise RuntimeError("abort")
        self.assertEqual(self.snapshot(before), before)

    def test_upsert_after_old_writer_dirty_row_has_no_trigger_conflict(self):
        self.insert("entry", "one", aliases=["Before alias"])
        self.insert("entity", "two", title="Beforeterm")
        self.build()
        self.conn.execute("UPDATE entries SET summary = 'Old writer changed it' WHERE id = 'one'")
        self.conn.execute("UPDATE entities SET summary = 'Old writer changed it' WHERE id = 'two'")
        with memory.transaction(self.conn):
            memory.entry_put(self.conn, {"id": "one", "kind": "note", "title": "Title",
                                        "summary": "New writer", "aliases": ["After alias"]})
            graph.put(self.conn, {"id": "two", "kind": "component", "title": "Afterterm",
                                  "summary": "New writer"}, "entity", memory)
        self.assertEqual(search_index.status(self.conn)["dirty"], {"entry": 0, "entity": 0})
        self.assertEqual(self.candidates("entry", "After alias"), {"one"})
        self.assertEqual(self.candidates("entity", "Afterterm"), {"two"})

    def test_failed_refresh_preserves_dirty_fallback_and_previous_index(self):
        self.insert("entity", "one", title="Beforeterm")
        self.build()
        self.conn.execute("UPDATE entities SET title = 'Afterterm' WHERE id = 'one'")
        before = self.snapshot([search_index.KEYS, search_index.DIRTY])
        # Failure after a key has been inserted exercises savepoint rollback,
        # including callers using autocommit instead of an outer transaction.
        with mock.patch.object(search_index, "_record_keys", return_value={("alias", "first"), ("broken",)}):
            with self.assertRaises(ValueError):
                search_index.refresh_record(self.conn, "entities", "one")
        self.assertEqual(self.snapshot(before), before)
        self.assertEqual(self.candidates("entity", "afterterm"), {"one"})
        self.assertFalse(self.conn.in_transaction)

    def test_build_and_rebuild_failures_are_atomic_even_if_caller_catches(self):
        self.insert("entry", "one", aliases=["First alias"])
        self.insert("entity", "two", title="Secondterm")
        with self.assertRaises(memory.MemoryError):
            search_index.build(self.conn, memory)
        original = search_index._record_keys

        def fail_second(record, family):
            if record["id"] == "two":
                raise RuntimeError("synthetic failure")
            return original(record, family)

        for already_built in (False, True):
            with self.subTest(already_built=already_built):
                if already_built:
                    self.build()
                    self.conn.execute("UPDATE entities SET title = 'Updatedterm' WHERE id = 'two'")
                tables = ["sqlite_master", "metadata", "entries", "entities"]
                if already_built:
                    tables += [search_index.KEYS, search_index.DIRTY]
                before = self.snapshot(tables)
                with memory.transaction(self.conn):
                    with mock.patch.object(search_index, "_record_keys", side_effect=fail_second):
                        with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                            search_index.build(self.conn, memory)
                    self.assertEqual(self.snapshot(tables), before)
                self.assertEqual(self.snapshot(tables), before)
        self.assertEqual(self.build()["dirty"], {"entry": 0, "entity": 0})
        self.assertEqual(self.candidates("entity", "updatedterm"), {"two"})

    def test_oversized_records_and_long_queries_use_complete_fallback(self):
        self.insert("entity", "huge", summary="x" * search_index.MAX_RECORD_CHARS + " finalneedle")
        self.insert("entry", "large-alias", aliases=["a" * (search_index.MAX_KEY_CHARS + 1)])
        result = self.build()
        self.assertEqual(result["fallback_records"], {"entry": 1, "entity": 1})
        self.assertEqual(self.candidates("entity", "finalneedle"), {"huge"})
        self.assertEqual(self.candidates("entity", "unrelated"), {"huge"})
        self.assertEqual(self.candidates("entry", "unrelated"), {"large-alias"})
        for query in ("x" * (search_index.MAX_QUERY_CHARS + 1), "word " * (search_index.MAX_QUERY_WORDS + 1)):
            self.assertIsNone(self.candidates("entry", query))
            self.assertIsNone(self.candidates("entity", query))
        for threshold in ("MAX_RECORD_KEYS", "MAX_RECORD_KEY_BYTES"):
            with self.subTest(threshold=threshold), mock.patch.object(search_index, threshold, 1):
                self.assertIsNone(search_index._record_keys({"id": "x", "jira_key": None, "aliases": "[]"}, "entry"))

    def test_missing_trigger_or_future_version_falls_back_readonly(self):
        self.insert("entity", "one")
        self.build()
        self.conn.execute("DROP TRIGGER richi_search_entities_update")
        self.assertFalse(search_index.status(self.conn)["available"])
        self.assertIsNone(self.candidates("entity", "one"))
        self.build()
        self.conn.execute("UPDATE metadata SET value = '999' WHERE key = ?", (search_index.VERSION_KEY,))
        before = self.snapshot(["sqlite_master", "metadata", search_index.KEYS])
        with memory.connect(self.path, readonly=True) as readonly:
            self.assertIsNone(self.candidates("entry", "one", readonly))
            self.assertEqual(search_index.status(readonly)["status"], "unsupported")
        with memory.transaction(self.conn), self.assertRaises(memory.MemoryError):
            search_index.build(self.conn, memory)
        self.assertEqual(self.snapshot(before), before)

    def test_first_build_refuses_unowned_schema_collision(self):
        self.conn.execute("CREATE TABLE " + search_index.KEYS + " (valuable TEXT)")
        self.conn.execute("INSERT INTO " + search_index.KEYS + " VALUES ('keep')")
        before = self.snapshot(["sqlite_master", search_index.KEYS, "metadata"])
        with memory.transaction(self.conn), self.assertRaises(memory.MemoryError):
            search_index.build(self.conn, memory)
        self.assertEqual(self.snapshot(before), before)

    def test_prefix_and_state_kind_filters_have_index_plans(self):
        from richi import query_filters

        self.insert("entity", "unicode", title="Ёлка Straße")
        self.insert("entry", "earlier", updated_at="2026-09-21T09:00:00+02:00")
        self.insert("entry", "later", updated_at="2026-09-21T08:00:00Z")
        self.build()
        plan = " ".join(row[3] for row in self.conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM " + search_index.KEYS +
            " WHERE family = ? AND field = ? AND key >= ? AND key < ?", ("entity", "token", "str", "sts")))
        self.assertIn("SEARCH", plan)
        self.assertIn("PRIMARY KEY", plan)
        self.assertNotIn("SCAN", plan)
        self.assertEqual(self.candidates("entity", "STRASS"), {"unicode"})
        self.assertEqual(self.candidates("entity", "ел"), {"unicode"})
        selected = query_filters.parse(
            "knowledge_state = 'confirmed' AND kind = 'note' AND updated_at >= '2026-09-21T07:30:00Z'",
            memory)
        selected.prepare(self.conn)
        clause, params = selected.sql("entry")
        sql = "SELECT e.id FROM entries e WHERE " + clause
        self.assertEqual([row[0] for row in self.conn.execute(sql, params)], ["later"])
        plan = " ".join(row[3] for row in self.conn.execute("EXPLAIN QUERY PLAN " + sql, params))
        self.assertIn("richi_search_entries_state_kind", plan)
        self.assertIn("knowledge_state=? AND kind=?", plan)

    def test_v1_writers_rename_delete_and_explicit_v2_migration_remain_safe(self):
        path = self.path.with_name("v1.sqlite3")
        conn = memory.connect(path, create=True)
        self.addCleanup(conn.close)
        memory.execute_script(conn, (memory.BASE / "schema.sql").read_text())
        conn.execute("INSERT INTO metadata VALUES ('search_backend', 'scan')")
        conn.execute("PRAGMA user_version = 1")
        conn.execute("INSERT INTO entries (id, kind, title, summary, knowledge_state, created_at, updated_at) "
                     "VALUES ('old', 'note', 'Title', 'Summary', 'hypothesis', '2026-09-20', '2026-09-20')")
        with memory.transaction(conn):
            self.assertEqual(search_index.build(conn, memory)["dirty"], {"entry": 0})
        conn.execute("UPDATE entries SET id = 'new' WHERE id = 'old'")
        self.assertEqual({row[0] for row in conn.execute("SELECT id FROM " + search_index.DIRTY)}, {"old", "new"})
        self.assertEqual(search_index.entry_ids(conn, "unknown", []), {"old", "new"})
        search_index.refresh_record(conn, "entries", "old")
        search_index.refresh_record(conn, "entries", "new")
        conn.execute("DELETE FROM entries WHERE id = 'new'")
        self.assertIn("new", search_index.entry_ids(conn, "unknown", []))
        search_index.refresh_record(conn, "entries", "new")
        self.assertEqual(search_index.entry_ids(conn, "new", []), set())
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        graph.migrate(conn, path, memory)
        self.assertFalse(search_index.status(conn)["available"])
        with memory.transaction(conn):
            self.assertTrue(search_index.build(conn, memory)["available"])
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
