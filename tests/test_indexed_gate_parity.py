"""Seeded conservative-gate audit: 240 records, 346 queries, 83,040 comparisons.

Expected matches come from the existing lexical/guard and entry-routing APIs,
not the new index tokenizer. False positives are allowed in a candidate gate;
false negatives are not. This is a bounded synthetic regression corpus rather
than a claim of exhaustive language coverage.
"""
import json
from pathlib import Path
import random
import tempfile
import unittest

from richi import graph, memory, recall, search_index


class IndexedGateParityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-index-gate-parity-")
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "synthetic.sqlite3"
        self.conn = memory.connect(path, create=True)
        self.addCleanup(self.conn.close)
        memory.initialize(self.conn, path)

    def put_both(self, value):
        memory.entry_put(self.conn, dict(value, kind="note", project_ids=[], jira_key=None))
        graph.put(self.conn, dict(value, kind="concept"), "entity", memory)

    def build(self):
        with memory.transaction(self.conn):
            search_index.build(self.conn, memory)

    def test_seeded_unicode_punctuation_corpus_has_no_candidate_false_negatives(self):
        rng = random.Random(25102026)
        words = ["Straße", "STRASSE", "Ёлка", "елка", "КС", "Y", "the", "foo", "bar", "HTTP2",
                 "DEMO-42", "café", "cafe\u0301", "İstanbul", "αβγ", "中文", "cache", "pool"]
        separators = [".", "/", "_", ":", "-", " ", "  ", " + ", "🙂", "\n", "..", "\\", "(", ")", "@", "\u0301"]
        for number in range(120):
            mixed = "".join(rng.choice(words) + rng.choice(separators) for _ in range(rng.randint(1, 4)))
            title = rng.choice(words) + rng.choice(separators) + mixed
            common = dict(id="item:" + str(number), title=title, summary=mixed,
                          aliases=[rng.choice(words), mixed], tags=[rng.choice(words)],
                          knowledge_state="confirmed", sources=[{"reference": "test://synthetic"}])
            memory.entry_put(self.conn, dict(common, kind="note", project_ids=[],
                                            jira_key="DEMO-42" if number % 13 == 0 else None))
            graph.put(self.conn, dict(common, kind="concept" if number % 2 else "component"), "entity", memory)
        self.build()
        entities = [recall.decode(row) for row in self.conn.execute("SELECT * FROM entities ORDER BY rowid")]
        entries = list(self.conn.execute("SELECT id, jira_key, aliases FROM entries ORDER BY rowid"))
        queries = list(words) + [word.casefold() for word in words]
        for record in entities[:70]:
            queries.extend([record["id"], "entity:" + record["id"], record["aliases"][1]])
        for _ in range(100):
            queries.append(rng.choice(words) + rng.choice(separators) + rng.choice(words))
        self.assertEqual(len(queries), 346)
        self.assertEqual(len(entries) + len(entities), 240)
        failures = []
        for query in queries:
            terms, keys = recall.terms_for(query)
            entity_ids = search_index.entity_ids(self.conn, query, terms, keys)
            entry_ids = search_index.entry_ids(self.conn, query, keys)
            # This corpus fits the index limits; do not silently bypass the gate
            # under test through a full-scan fallback.
            self.assertIsNotNone(entity_ids)
            self.assertIsNotNone(entry_ids)
            subjects, symbols = recall.technical_subjects(query), recall.short_subjects(query)
            variants = {variant for key in keys for variant in recall.key_variants(key)}
            for record in entries:
                expected = recall.entry_routing_match(record, recall.normalize(query).strip(),
                                                      recall.phrase(query), variants)
                if expected and record["id"] not in entry_ids:
                    failures.append(("entry", query, record["id"]))
            for record in entities:
                score, reasons = recall.lexical(record, query, terms, keys, "entity")
                expected = recall.lexical_rejection(record, score, reasons, keys, subjects, symbols) is None
                if expected and record["id"] not in entity_ids:
                    failures.append(("entity", query, record["id"]))
        self.assertEqual(failures, [])

    def test_repeated_old_writer_updates_keep_changed_unicode_routes(self):
        self.put_both(dict(id="old-writer", title="Before", summary="Recorded.", aliases=["before"],
                           tags=[], sources=[{"reference": "test://source"}], knowledge_state="confirmed"))
        self.build()
        for table, family in (("entries", "entry"), ("entities", "entity")):
            for route in ("route-αβγ", "Straße route", "Ёлка.route"):
                self.conn.execute("UPDATE " + table + " SET aliases = ? WHERE id = ?",
                                  (json.dumps([route]), "old-writer"))
                terms, keys = recall.terms_for(route)
                ids = (search_index.entry_ids(self.conn, route, keys) if family == "entry" else
                       search_index.entity_ids(self.conn, route, terms, keys))
                self.assertIn("old-writer", ids)
        self.assertEqual(search_index.status(self.conn)["dirty"], {"entry": 1, "entity": 1})


if __name__ == "__main__":
    unittest.main()
