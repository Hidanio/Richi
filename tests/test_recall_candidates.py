"""Candidate routing parity and observed work on disposable stores.

legacy_candidates freezes the pre-optimization selection pass from e1265bb.
Only unchanged lexical, scope and rendering helpers are shared; the optimized
router is never used to calculate the expected candidate set.
"""
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from richi import compact, graph, memory, recall


def legacy_candidates(conn, query, terms, keys, states, project, api, trace=None):
    states_sql = ",".join("?" for _ in states)
    candidates_by_ref, fts_ranks, discarded_weak = {}, {}, 0
    subjects = recall.technical_subjects(query)
    symbols = recall.short_subjects(query)
    has_fts = recall.table_exists(conn, "entries_fts")
    fts_truncated = False
    if has_fts and (terms or keys):
        expression = " OR ".join('"' + word.replace('"', '""') + '"*' for word in terms)
        key_expressions = ['"' + variant.replace('"', '""') + '"' for key in keys for variant in recall.key_variants(key)]
        expression = " OR ".join(key_expressions) if keys else expression
        rows = conn.execute("""
            SELECT e.id
            FROM entries e
                JOIN entries_fts ON entries_fts.rowid = e.rowid
            WHERE 1=1 AND entries_fts MATCH ? AND e.knowledge_state IN (""" + states_sql + """ )
            ORDER BY bm25(entries_fts, 8.0, 6.0, 1.0, 10.0, 3.0, 5.0), e.id
            LIMIT ?
        """, [expression] + states + [160 + 1]).fetchall()
        fts_truncated = len(rows) > 160
        fts_ranks = {row[0]: rank for rank, row in enumerate(rows[:160])}
        if trace:
            trace.counts.update(fts_rows_fetched=len(rows), fts_shortlisted=len(fts_ranks))

    # A light scan supplies exact aliases/IDs that tokenization can miss. When FTS
    # is absent the same scan also acts as the Unicode lexical fallback.
    rows = conn.execute("SELECT * FROM entries WHERE 1=1 AND knowledge_state IN (" + states_sql + ")", states)
    for row in rows:
        record = recall.decode(row)
        node_ref = "entry:" + record["id"]
        if trace:
            trace.counts["entries_scanned"] += 1
        score, reasons = recall.lexical(record, query, terms, keys, "entry")
        rank = fts_ranks.get(record["id"])
        # FTS rank and project proximity can rank evidence, but cannot turn an
        # absent substantive lexical anchor into evidence for the question.
        rejection = recall.lexical_rejection(record, score, reasons, keys, subjects, symbols)
        if rejection:
            discarded_weak += int(rank is not None)
            if trace:
                trace.direct(node_ref, rejection)
            continue
        exact = any(reason in {"exact_id", "exact_jira_key", "exact_alias", "alias_phrase"} for reason in reasons)
        if has_fts and rank is None and not exact:
            if trace:
                # This proves absence from the executed shortlist, not whether
                # a hypothetical unbounded FTS search would have matched it.
                trace.direct(node_ref, "fts_not_shortlisted")
            continue
        if rank is not None:
            score += 15 / (1 + rank / 8)
            reasons.append("weighted_fts")
        record["project_ids"] = [r[0] for r in conn.execute("SELECT project_id FROM entry_projects WHERE 1=1 AND entry_id = ? ORDER BY project_id", (record["id"],))]
        if project and project in record["project_ids"]:
            score += 18
            reasons.append("project_match")
        candidates_by_ref[node_ref] = {"record": record, "ref": node_ref, "score": score, "reasons": reasons}
        if trace:
            trace.direct(node_ref, "candidate")

    if recall.table_exists(conn, "entities"):
        for row in conn.execute("SELECT * FROM entities WHERE 1=1 AND knowledge_state IN (" + states_sql + ")", states):
            record = recall.decode(row)
            node_ref = "entity:" + record["id"]
            if trace:
                trace.counts["entities_scanned"] += 1
            score, reasons = recall.lexical(record, query, terms, keys, "entity")
            rejection = recall.lexical_rejection(record, score, reasons, keys, subjects, symbols)
            if rejection:
                if trace:
                    trace.direct(node_ref, rejection)
                continue
            record.update(project_ids=[], work_state=None)
            if record["kind"] == "concept":
                record["project_ids"] = recall.concept_projects(conn, node_ref)
                if project and project in record["project_ids"]:
                    score += 18
                    reasons.append("confirmed_concept_scope")
            elif project and recall.table_exists(conn, "graph_edges"):
                # Only an explicitly recorded entity/project link grants this boost.
                linked = conn.execute("SELECT 1 FROM graph_edges WHERE 1=1 AND knowledge_state IN (" + states_sql + ") AND ((from_ref = ? AND to_ref = ?) OR (to_ref = ? AND from_ref = ?)) LIMIT 1",
                                      states + [node_ref, "project:" + project, node_ref, "project:" + project]).fetchone()
                if linked:
                    score += 18
                    reasons.append("explicit_project_link")
            reasons.append("entity_lexical")
            candidates_by_ref[node_ref] = {"record": record, "ref": node_ref, "score": score, "reasons": reasons,
                                           "concept_aliases": recall.concept_alias_matches(record, query)}
            if trace:
                trace.direct(node_ref, "candidate")
    return candidates_by_ref, fts_truncated, "fts5" if has_fts else "scan", discarded_weak


class PayloadCursor:
    """Count real rows delivered to Python, independently of recall's trace."""

    def __init__(self, cursor, audit, sql):
        self.cursor = cursor
        self.audit = audit
        self.columns = {column[0] for column in cursor.description or ()}
        self.full_entry = "entries" in sql.lower() and {"id", "summary", "sources", "aliases"} <= self.columns

    def observe(self, row):
        if row is not None and self.full_entry:
            self.audit.full_entry_ids.append(row["id"])
        if row is not None and self.audit.on_metadata and self.columns == {"id", "jira_key", "aliases"}:
            self.audit.on_metadata(row)
        return row

    def __iter__(self):
        for row in self.cursor:
            yield self.observe(row)

    def fetchone(self):
        return self.observe(self.cursor.fetchone())

    def fetchall(self):
        return [self.observe(row) for row in self.cursor.fetchall()]

    def __getattr__(self, name):
        return getattr(self.cursor, name)


class PayloadAudit:
    def __init__(self, conn, on_metadata=None):
        self.conn = conn
        self.on_metadata = on_metadata
        self.full_entry_ids = []
        self.statements = []

    def execute(self, sql, *arguments):
        self.statements.append(sql)
        return PayloadCursor(self.conn.execute(sql, *arguments), self, sql)

    def __getattr__(self, name):
        return getattr(self.conn, name)


class RecallCandidateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="recall-candidate-parity-")
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "synthetic.sqlite3"
        self.conn = memory.connect(self.db, create=True)
        self.addCleanup(self.conn.close)
        memory.initialize(self.conn, self.db)
        for identity in ("alpha", "beta"):
            memory.project_put(self.conn, {"id": identity, "name": identity})

    def entry(self, identity, **changes):
        value = dict(id=identity, kind="note", title="Separate observation", summary="Recorded evidence.",
                     sources=[{"reference": "https://example.test/evidence/" + identity}],
                     project_ids=["alpha"], aliases=[], tags=[], knowledge_state="confirmed",
                     work_state="done", verified_at="2026-10-01")
        value.update(changes)
        memory.entry_put(self.conn, value)

    def entity(self, identity, **changes):
        value = dict(id=identity, kind="concept", title="Separate definition", summary="A recorded meaning.",
                     sources=[{"reference": "https://example.test/meaning/" + identity}],
                     aliases=[], tags=[], knowledge_state="confirmed", verified_at="2026-10-01")
        value.update(changes)
        graph.put(self.conn, value, "entity", memory)

    def edge(self, identity, source, target, **changes):
        value = dict(id=identity, from_ref=source, to_ref=target, kind="informs",
                     description="Recorded relationship.", knowledge_state="confirmed",
                     sources=[{"reference": "https://example.test/link/" + identity}], verified_at="2026-10-01")
        value.update(changes)
        graph.put(self.conn, value, "edge", memory)

    @staticmethod
    def arguments(query, **changes):
        values = dict(query=query, limit=8, max_chars=16000, project=None,
                      include_hypotheses=False, include_superseded=False)
        values.update(changes)
        return SimpleNamespace(**values)

    def parity(self, query, **changes):
        args = self.arguments(query, **changes)
        with patch.object(recall, "candidates", legacy_candidates):
            expected = recall.recall(self.conn, args, memory)
        actual = recall.recall(self.conn, args, memory)
        self.assertEqual(actual, expected, query)
        self.assertEqual(actual["budget"]["output_chars"], len(compact.rendered(actual)))
        self.assertLessEqual(len(compact.rendered(actual)), args.max_chars)
        return actual

    @staticmethod
    def refs(value):
        return [item["ref"] for item in value["results"]]

    def shortlist(self, query):
        terms, keys = recall.terms_for(query)
        expressions = ['"' + word.replace('"', '""') + '"*' for word in terms]
        if keys:
            expressions = ['"' + variant + '"' for key in keys for variant in recall.key_variants(key)]
        if not expressions:
            return []
        return [row[0] for row in self.conn.execute("""
            SELECT e.id FROM entries e JOIN entries_fts ON entries_fts.rowid = e.rowid
            WHERE entries_fts MATCH ? AND e.knowledge_state = 'confirmed'
            ORDER BY bm25(entries_fts, 8.0, 6.0, 1.0, 10.0, 3.0, 5.0), e.id LIMIT 160
        """, (" OR ".join(expressions),))]

    def crowded(self, title):
        if not recall.table_exists(self.conn, "entries_fts"):
            self.skipTest("SQLite build has no FTS5")
        with memory.transaction(self.conn):
            for number in range(165):
                self.entry("background-{:03d}".format(number), title=title)

    def assert_outside_shortlist(self, query, identities):
        shortlist = self.shortlist(query)
        self.assertEqual(len(shortlist), 160, "Fixture must exercise the actual shortlist cap")
        self.assertTrue(set(identities).isdisjoint(shortlist), "Fixture target unexpectedly entered FTS shortlist")
        value = self.parity(query)
        self.assertTrue({"entry:" + identity for identity in identities} <= set(self.refs(value)))
        self.assertTrue(value["selection"]["fts_candidates_truncated"])
        return value

    def test_exact_id_survives_outside_fts_shortlist(self):
        self.crowded("routeanchor")
        self.entry("routeanchor", summary="padding " * 3000)
        value = self.assert_outside_shortlist("routeanchor", ["routeanchor"])
        self.assertEqual(self.refs(value)[0], "entry:routeanchor")
        self.assertIn("exact_id", value["results"][0]["match_reasons"])

    def test_jira_field_and_canonical_identity_survive_outside_shortlist(self):
        self.crowded("DEMO-42")
        for identity, fields in (("jira-field", {"jira_key": "dEmO-42"}), ("task:DEMO-42", {})):
            self.entry(identity, summary="padding " * 3000, **fields)
        value = self.assert_outside_shortlist("DEMO-42", ["jira-field", "task:DEMO-42"])
        self.assertEqual(self.refs(value)[0], "entry:task:DEMO-42")
        self.assertTrue({"entry:jira-field", "entry:task:DEMO-42"} <= set(self.refs(self.parity("DEMO42"))))
        self.entry("wrong-prefix", jira_key="DEMO-420", aliases=["DEMO-42"], summary="padding " * 3000)
        # The recorded alias is itself a valid whole-key match, just as before.
        self.assertIn("entry:wrong-prefix", self.refs(self.parity("DEMO-42")))

    def test_exact_alias_survives_outside_fts_shortlist(self):
        self.crowded("routeanchor")
        self.entry("alias-target", aliases=["routeanchor"], summary="padding " * 3000)
        value = self.assert_outside_shortlist("routeanchor", ["alias-target"])
        self.assertIn("exact_alias", value["results"][0]["match_reasons"])

    def test_alias_phrase_survives_outside_fts_shortlist(self):
        self.crowded("route anchor")
        self.entry("phrase-target", aliases=["route anchor"], summary="padding " * 3000)
        value = self.assert_outside_shortlist("why route anchor", ["phrase-target"])
        self.assertIn("alias_phrase", value["results"][0]["match_reasons"])

    def test_unicode_alias_id_and_punctuation_routing_preserve_normalization(self):
        self.entry("unicode-alias", aliases=["Straße", "Ёлочный контур", "проверка / границ"])
        self.entry("Straße", title="Unrelated identity")
        self.entry("symbol-only", aliases=["Y"])
        self.entry("the-alias", aliases=["the"])
        self.entry("--leading-dash", title="Unrelated identifier")
        cases = (("STRASSE", "entry:unicode-alias"), ("елочный контур", "entry:unicode-alias"),
                 ("ПРОВЕРКА ... ГРАНИЦ", "entry:unicode-alias"), ("entry:STRASSE", "entry:Straße"),
                 ("Y", "entry:symbol-only"), ("the", "entry:the-alias"),
                 ("entry:--leading-dash", "entry:--leading-dash"))
        for query, expected in cases:
            with self.subTest(query=query):
                self.assertIn(expected, self.refs(self.parity(query)))

    def test_jira_subject_and_symbol_guards_remain_in_force(self):
        self.entry("main", jira_key="DEMO-42", title="latency result", aliases=["latency alias"])
        self.entry("prefix", jira_key="DEMO-420", title="latency result")
        self.entry("compact-mention", summary="After DEMO42 an observation was recorded.")
        self.entry("alias-with-wrong-key", aliases=["latency alias"], summary="DEMO-43 record")
        cases = (("DEMO-42 latency alias", {"entry:main", "entry:compact-mention"}),
                 ("DEMO-99999 latency alias", set()), ("latency QUIC", set()),
                 ("latency Z", set()), ("unrecordedtechnicalsubject", set()))
        for query, expected in cases:
            with self.subTest(query=query):
                value = self.parity(query)
                self.assertEqual(set(self.refs(value)), expected)
                self.assertEqual(value["no_match"], not bool(expected))

    def test_ranked_seeds_graph_evidence_states_and_budgets_match_legacy(self):
        for identity in ("a", "b", "c", "d"):
            self.entry(identity, aliases=["rankanchor"], project_ids=["beta"] if identity == "d" else ["alpha"])
            self.entry("neighbor-" + identity, project_ids=["beta"])
            self.edge("link-" + identity, "entry:neighbor-" + identity, "entry:" + identity)
        self.entry("second-hop")
        self.edge("second-link", "entry:neighbor-a", "entry:second-hop")
        self.entry("hypothesis", aliases=["rankanchor"], knowledge_state="hypothesis")
        self.entry("superseded", aliases=["rankanchor"], knowledge_state="superseded")
        for project in (None, "beta"):
            for limit in (1, 3, 8):
                for maximum in (2000, 16000):
                    with self.subTest(project=project, limit=limit, maximum=maximum):
                        self.parity("rankanchor", project=project, limit=limit, max_chars=maximum,
                                    include_hypotheses=True, include_superseded=True)
        normal = self.parity("rankanchor")
        self.assertTrue({"entry:neighbor-a", "entry:neighbor-b"} <= set(self.refs(normal)))
        self.assertTrue({"entry:second-hop", "entry:neighbor-d", "entry:hypothesis", "entry:superseded"}.isdisjoint(self.refs(normal)))

    def test_entities_keep_all_ambiguity_candidates_and_confirmed_scope(self):
        for identity, state in (("meaning-a", "confirmed"), ("meaning-b", "confirmed"),
                                ("meaning-old", "superseded"), ("meaning-idea", "hypothesis")):
            self.entity(identity, aliases=["Y"], knowledge_state=state)
        self.edge("scope-b", "entity:meaning-b", "project:beta", kind="used_in")
        self.edge("wrong-direction", "project:beta", "entity:meaning-a", kind="used_in")
        for maximum in (2000, 16000):
            value = self.parity("What is Y?", project="beta", limit=1, max_chars=maximum,
                                include_hypotheses=True, include_superseded=True)
            self.assertEqual(value["ambiguity"]["concept_count"], 4)
        value = self.parity("What is Y?", project="beta", limit=1)
        self.assertEqual(self.refs(value), ["entity:meaning-b"])
        self.assertEqual(value["ambiguity"]["concept_count"], 2)

    def test_fts_shortlist_bounds_full_payloads_and_scoring_as_background_grows(self):
        if not recall.table_exists(self.conn, "entries_fts"):
            self.skipTest("SQLite build has no FTS5")
        self.entry("target", title="payloadneedle")
        previous = 0
        observed = []
        for total in (100, 1000):
            with memory.transaction(self.conn):
                for number in range(previous, total):
                    self.entry("unrelated-{:04d}".format(number), summary="unrelated payload " * 200)
            previous = total
            audit = PayloadAudit(self.conn)
            with patch.object(recall, "lexical", wraps=recall.lexical) as score:
                value = recall.recall(audit, self.arguments("payloadneedle", explain=True), memory)
            self.assertEqual(self.refs(value), ["entry:target"])
            scored_ids = [call.args[0]["id"] for call in score.call_args_list if call.args[4] == "entry"]
            self.assertEqual(scored_ids, ["target"])
            self.assertEqual(audit.full_entry_ids, ["target"])
            self.assertEqual(sum("entries_fts MATCH" in sql for sql in audit.statements), 1)
            counts = value["explain"]["counts"]
            self.assertEqual(counts["entries_scanned"], total + 1)
            self.assertEqual(counts["entries_prefiltered"], total)
            self.assertEqual(counts["entries_scored"], 1)
            self.assertEqual(counts["expected_entry_probes"], 0)
            observed.append((len(scored_ids), len(audit.full_entry_ids)))
        self.assertEqual(observed[0], observed[1])

    def test_expected_probe_scores_only_skipped_target_without_affecting_selection(self):
        if not recall.table_exists(self.conn, "entries_fts"):
            self.skipTest("SQLite build has no FTS5")
        self.entry("hit", title="probecase")
        self.entry("expected-miss", title="Another subject")
        self.entry("unrelated", title="Another subject")
        ordinary = self.parity("probecase")
        audit = PayloadAudit(self.conn)
        with patch.object(recall, "lexical", wraps=recall.lexical) as score:
            explained = recall.recall(audit, self.arguments("probecase", expect="entry:expected-miss"), memory)
        for key in ("results", "edges", "candidate_count", "selection", "no_match"):
            self.assertEqual(explained[key], ordinary[key])
        self.assertEqual(explained["explain"]["expected"]["outcome"], "weak_lexical")
        self.assertEqual(explained["explain"]["counts"]["expected_entry_probes"], 1)
        self.assertEqual(explained["explain"]["counts"]["entries_scored"], 1)
        self.assertCountEqual([call.args[0]["id"] for call in score.call_args_list], ["hit", "expected-miss"])
        self.assertCountEqual(audit.full_entry_ids, ["hit", "expected-miss"])
        self.assertEqual(sum("entries_fts MATCH" in sql for sql in audit.statements), 1)

    def test_scan_fallback_still_scores_full_corpus_and_preserves_unicode_matches(self):
        self.entry("unicode", title="ёлочный источник")
        self.entry("ordinary", title="Different topic")
        if recall.table_exists(self.conn, "entries_fts"):
            self.conn.execute("DROP TABLE entries_fts")
        ordinary = self.parity("елочный источник")
        self.assertEqual(self.refs(ordinary), ["entry:unicode"])
        audit = PayloadAudit(self.conn)
        with patch.object(recall, "lexical", wraps=recall.lexical) as score:
            value = recall.recall(audit, self.arguments("елочный источник", explain=True), memory)
        self.assertEqual(value["selection"]["search_backend"], "scan")
        self.assertEqual(score.call_count, 2)
        self.assertCountEqual(audit.full_entry_ids, ["unicode", "ordinary"])
        self.assertEqual(value["explain"]["counts"]["entries_scored"], 2)
        self.assertEqual(value["explain"]["counts"]["entries_prefiltered"], 0)
        self.assertFalse(any("entries_fts MATCH" in sql for sql in audit.statements))

    def test_fts_no_match_reads_no_full_payload_and_keeps_current_schema_read_only(self):
        if not recall.table_exists(self.conn, "entries_fts"):
            self.skipTest("SQLite build has no FTS5")
        self.entry("ordinary", title="A separate subject", summary="Large unrelated text " * 1000)
        self.conn.close()
        before = self.db.read_bytes()
        self.conn = memory.connect(self.db, readonly=True)
        self.addCleanup(self.conn.close)
        for query in ("completelyunknown", "Z"):
            with self.subTest(query=query):
                self.assertTrue(self.parity(query)["no_match"])
                audit = PayloadAudit(self.conn)
                with patch.object(recall, "lexical", wraps=recall.lexical) as score:
                    value = recall.recall(audit, self.arguments(query, explain=True), memory)
                self.assertTrue(value["no_match"])
                self.assertEqual(score.call_count, 0)
                self.assertEqual(audit.full_entry_ids, [])
                self.assertEqual(value["explain"]["counts"]["entries_scored"], 0)
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 3)
        self.conn.close()
        self.assertEqual(self.db.read_bytes(), before)

    def test_schema_one_read_only_parity_does_not_migrate_or_write(self):
        legacy = self.db.parent / "legacy.sqlite3"
        conn = sqlite3.connect(str(legacy), isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript((memory.BASE / "schema.sql").read_text(encoding="utf-8"))
            conn.execute("PRAGMA user_version = 1")
            conn.execute("INSERT INTO metadata VALUES ('search_backend', 'scan')")
            memory.project_put(conn, {"id": "alpha", "name": "Alpha"})
            original = self.conn
            self.conn = conn
            self.entry("legacy-entry", aliases=["legacyalias"])
            self.conn = original
        finally:
            conn.close()
        before = legacy.read_bytes()
        readonly = memory.connect(legacy, readonly=True)
        try:
            original = self.conn
            self.conn = readonly
            value = self.parity("legacyalias")
            self.assertEqual(self.refs(value), ["entry:legacy-entry"])
            self.assertEqual(readonly.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertFalse(recall.table_exists(readonly, "entities"))
            self.conn = original
        finally:
            readonly.close()
        self.assertEqual(legacy.read_bytes(), before)

    def test_last_routing_row_and_full_payload_share_snapshot_with_concurrent_writer(self):
        if not recall.table_exists(self.conn, "entries_fts"):
            self.skipTest("SQLite build has no FTS5")
        for caller_owned in (False, True):
            with self.subTest(caller_owned_transaction=caller_owned):
                self.entry("last-row", aliases=["snapshotneedle"])
                expected = self.parity("snapshotneedle")
                if caller_owned:
                    self.conn.execute("BEGIN")
                writer = memory.connect(self.db)
                seen = []

                def change_last_row(row):
                    seen.append(row["id"])
                    # This runs after the only metadata row has reached Python,
                    # before the router can fetch that record's full payload.
                    writer.execute("UPDATE entries SET knowledge_state = 'hypothesis', summary = ?, sources = ? WHERE id = ?",
                                   ("Changed after routing", json.dumps([{"reference": "https://example.test/new"}]), row["id"]))

                try:
                    audit = PayloadAudit(self.conn, on_metadata=change_last_row)
                    actual = recall.recall(audit, self.arguments("snapshotneedle"), memory)
                    self.assertEqual(seen, ["last-row"], "Concurrent update must occur at the last metadata row")
                    self.assertEqual(actual, expected)
                    self.assertEqual(self.conn.in_transaction, caller_owned)
                    if caller_owned:
                        self.assertEqual(memory.entry_record(self.conn, "last-row")["knowledge_state"], "confirmed")
                finally:
                    if self.conn.in_transaction:
                        self.conn.rollback()
                    writer.close()
                self.assertEqual(memory.entry_record(self.conn, "last-row")["knowledge_state"], "hypothesis")


if __name__ == "__main__":
    unittest.main()
