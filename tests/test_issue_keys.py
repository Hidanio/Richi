"""Issue keys come from what the store records, never from the shape of a token.

Disposable synthetic stores only. Each scenario runs on schemas 1, 2 and 3, with
FTS5 and with the scan backend, without and with the optional search index.
"""
from contextlib import closing
from pathlib import Path
import random
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from richi import graph, memory, recall, search_index


def fts5_available():
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE probe USING fts5(value)")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


LAYOUTS = [(schema, fts, index) for schema in (1, 2, 3)
           for fts in ((True, False) if fts5_available() else (False,)) for index in (False, True)]


def note(identity, **changes):
    value = dict(id=identity, kind="note", title="Neutral title", summary="Stored observation.",
                 sources=[{"reference": "https://example.test/evidence/" + identity}], project_ids=[],
                 aliases=[], tags=[], knowledge_state="confirmed")
    value.update(changes)
    return value


def issue(identity, **changes):
    value = dict(id=identity, kind="jira", title="Neutral title", summary="Stored observation.",
                 sources=[{"reference": "https://example.test/tracker/" + identity}],
                 aliases=[], tags=[], knowledge_state="confirmed")
    value.update(changes)
    return value


class IssueKeyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-issue-keys-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.created = 0

    def store(self, entries=(), entities=(), schema=3, fts=True, index=False):
        """A store of the requested layout; schema 1 has no entities."""
        self.created += 1
        self.path = self.root / "store-{}.sqlite3".format(self.created)
        conn = memory.connect(self.path, create=True)
        self.addCleanup(conn.close)
        memory.execute_script(conn, (memory.BASE / "schema.sql").read_text(encoding="utf-8"))
        if schema >= 2:
            script = (memory.BASE / "graph_schema.sql").read_text(encoding="utf-8")
            memory.execute_script(conn, script if schema == 3 else script.replace(", 'tool'", ""))
        if fts:
            memory.execute_script(conn, memory.FTS_SQL)
        conn.execute("INSERT INTO metadata VALUES ('search_backend', ?)", ("fts5" if fts else "scan",))
        conn.execute("PRAGMA user_version = " + str(schema))
        for value in entries:
            memory.entry_put(conn, value)
        if schema >= 2:
            for value in entities:
                graph.put(conn, value, "entity", memory)
        if index:
            with memory.transaction(conn):
                search_index.build(conn, memory)
        return conn

    def each_layout(self, check, entries=(), entities=()):
        for schema, fts, index in LAYOUTS:
            with self.subTest(schema=schema, fts=fts, index=index):
                check(self.store(entries, entities, schema, fts, index), schema)

    @staticmethod
    def ask(conn, query, **options):
        values = dict(query=query, limit=8, max_chars=16000, project=None)
        values.update(options)
        return recall.recall(conn, SimpleNamespace(**values), memory)

    def words(self, conn, query, found, absent=(), **options):
        """The query is ordinary words: no key mode, and every word takes part."""
        value = self.ask(conn, query, **options)
        selection = value["selection"]
        self.assertEqual(selection["jira_keys"], [], query)
        self.assertNotIn("jira_mode", selection, query)
        self.assertEqual(selection["terms"], recall.phrase(query).split(), query)
        ids = [item["id"] for item in value["results"]]
        self.assertEqual(sorted(ids), sorted(found), query)
        self.assertFalse(set(absent) & set(ids), query)
        self.assertEqual(value["no_match"], not found, query)
        if not found:
            self.assertEqual(selection["abstention_reason"], "no_meaningful_lexical_anchor", query)
        return value

    def keyed(self, conn, query, keys, found, first=None):
        """The query names issue keys: only records with a whole key are answers."""
        value = self.ask(conn, query)
        selection = value["selection"]
        self.assertEqual(selection["jira_keys"], keys, query)
        self.assertEqual(selection["jira_mode"], "required_whole_key", query)
        ids = [item["id"] for item in value["results"]]
        self.assertEqual(sorted(ids), sorted(found), query)
        self.assertEqual(value["no_match"], not found, query)
        if first:
            self.assertEqual(ids[0], first, query)
            self.assertIn("exact_jira_key", value["results"][0]["match_reasons"], query)
        if not found:
            self.assertEqual(value["candidate_count"], 0, query)
            self.assertEqual(selection["abstention_reason"], "no_matching_issue_key", query)
        return value

    def test_store_without_keys_treats_key_shaped_tokens_as_words(self):
        entries = [
            note("warning", title="ResourceWarning for unclosed connections",
                 summary="Every sqlite3 handle is now closed in the suite."),
            note("launcher", title="python3 launcher", summary="The launcher prefers python3 from PATH."),
            note("digest", title="sha256 of the release wheel", summary="The manifest stores the digest."),
            note("longer-digest", title="sha2560 is a different token"),
            note("tokenizer", title="fts5 tokenizer", summary="unicode61 keeps digits inside a token."),
            note("probe", title="gemma-4 embedding probe", summary="Measured on arm64 only."),
            note("later-model", title="gemma-40 is another model"),
        ]

        def check(conn, schema):
            # The reported miss: python3 alone used to decide the whole answer.
            self.words(conn, "python3.13 ResourceWarning", ["warning"], absent=["launcher"])
            self.words(conn, "python3 launcher", ["launcher"])
            self.words(conn, "sha256", ["digest"], absent=["longer-digest"])
            self.words(conn, "sha2560", ["longer-digest"], absent=["digest"])
            self.words(conn, "fts5 tokenizer", ["tokenizer"])
            self.words(conn, "gemma-4 probe", ["probe"], absent=["later-model"])
            self.words(conn, "http404 arm64", ["probe"])
            self.words(conn, "sqlite3 suite", ["warning"])
            self.words(conn, "ubuntu-24 phase-0 mem0 sha-256", [])

        self.each_layout(check, entries)

    def test_recorded_keys_keep_whole_key_strictness(self):
        entries = [
            note("main", kind="task", jira_key="DEMO-42", title="DEMO-42: update ranking"),
            note("sibling", kind="task", jira_key="DEMO-43", title="DEMO-43: update ranking"),
            note("longer", kind="task", jira_key="DEMO-420", title="DEMO-420: update ranking"),
            note("follow-up", kind="decision", summary="After DEMO42 the cache was resized."),
            note("other-follow-up", summary="After DEMO420 nothing changed."),
            note("task:DEMO-7", kind="task", title="Ranking cleanup"),
            note("redis", title="Redis connections", summary="Что сделали в задаче: переиспользовали соединения."),
        ]

        def check(conn, schema):
            self.keyed(conn, "DEMO-42", ["demo-42"], ["main", "follow-up"], first="main")
            self.keyed(conn, "demo42", ["demo-42"], ["main", "follow-up"], first="main")
            self.keyed(conn, "Что следующим ускорять после DEMO-42?", ["demo-42"], ["main", "follow-up"])
            canonical = self.keyed(conn, "DEMO-7", ["demo-7"], ["task:DEMO-7"], first="task:DEMO-7")
            self.assertIn("canonical_issue_record", canonical["results"][0]["match_reasons"])
            # An unknown number of a recorded series never falls back to the other words.
            unknown = self.keyed(conn, "Что сделали в задаче DEMO-999999 Redis?", ["demo-999999"], [])
            self.assertEqual(unknown["selection"]["terms"], ["задаче", "redis"])
            self.assertEqual(unknown["edges"], [])

        self.each_layout(check, entries)

    def test_keys_of_other_trackers_are_exact_in_any_recorded_format(self):
        recorded = {"azure": "AB#123", "github": "#77", "clickup": "CU-8669abc",
                    "cyrillic": "ПРОЕКТ-12", "dotted": "APP.WEB-12"}
        near = {"azure-near": "AB#1234", "github-near": "#770", "clickup-near": "CU-8669abcd",
                "cyrillic-near": "ПРОЕКТ-120", "dotted-near": "APP.WEB-120"}
        entries = [note(identity, kind="task", jira_key=key, title="Recorded tracker item")
                   for identity, key in list(recorded.items()) + list(near.items())]
        entries.append(note("mentions", summary="Follows AB#123, #77, CU-8669abc, проект-12 and APP.WEB-12."))

        def check(conn, schema):
            for identity, key in recorded.items():
                canonical = recall.normalize(key)
                self.keyed(conn, key, [canonical], [identity, "mentions"], first=identity)
                self.keyed(conn, "что решили в " + key.lower() + "?", [canonical], [identity, "mentions"], first=identity)
            self.keyed(conn, "ПРОЕКТ12", ["проект-12"], ["cyrillic", "mentions"], first="cyrillic")
            # Another number of a recorded series is an unknown issue, in every format.
            self.keyed(conn, "AB#999 tracker item", ["ab#999"], [])
            self.keyed(conn, "issue #999", ["#999"], [])
            self.keyed(conn, "ПРОЕКТ-999", ["проект-999"], [])
            self.keyed(conn, "APP.WEB-999", ["app.web-999"], [])
            # No number ends CU-8669abc, so it starts no series; other tokens stay words.
            self.words(conn, "CU-0000zzz", [])
            self.words(conn, "sha256 python3", [])
            self.words(conn, "recorded tracker item", list(recorded) + list(near), absent=["mentions"], limit=20)

        self.each_layout(check, entries)

    def test_ids_declare_keys_and_a_key_holds_a_number_and_more(self):
        entries = [
            note("task:OPS-7", kind="task", title="Rotate the signing certificate"),
            note("task:backup", kind="task", title="Backup rotation"),
            note("restore", title="Restore drill", summary="The backup was restored on a spare host."),
            note("placeholder", jira_key="N/A", title="Internal tooling"),
            note("named", jira_key="internal", title="Internal audit"),
            note("numbered", jira_key="17", title="Seventeenth item"),
            note("task:30", kind="task", title="Thirtieth item"),
        ]
        entities = [issue("jira:NET-5", title="Gateway timeout")]

        def check(conn, schema):
            self.keyed(conn, "OPS-7", ["ops-7"], ["task:OPS-7"], first="task:OPS-7")
            self.keyed(conn, "ops7 certificate", ["ops-7"], ["task:OPS-7"], first="task:OPS-7")
            self.keyed(conn, "OPS-8 certificate", ["ops-8"], [])
            if schema >= 2:
                self.keyed(conn, "NET-5", ["net-5"], ["jira:NET-5"], first="jira:NET-5")
                self.keyed(conn, "net-6 gateway", ["net-6"], [])
            else:
                self.words(conn, "NET-5", [])
            # task:backup names the work and jira_key "internal" is a word, not an issue.
            self.words(conn, "backup restored", ["task:backup", "restore"])
            self.words(conn, "internal tooling", ["placeholder", "named"])
            # A bare number is never an issue key, or "top 17" and "30 days" would be issues.
            self.words(conn, "backup 17 30", ["task:backup", "restore", "task:30"])
            self.words(conn, "item 17", ["numbered", "task:30"])

        self.each_layout(check, entries, entities)

    def test_technical_token_in_a_recorded_series_is_an_issue_key(self):
        entries = [
            note("gateway", kind="task", jira_key="HTTP-12", title="HTTP-12: gateway timeout"),
            note("push", title="http2 push was disabled", summary="Server push stays off."),
            note("digest", title="sha256 digest of the wheel"),
        ]

        def check(conn, schema):
            # Documented: the store records the HTTP series, so http2 is one of its issues.
            self.keyed(conn, "http2 push", ["http-2"], ["push"])
            self.keyed(conn, "http404 gateway timeout", ["http-404"], [])
            self.keyed(conn, "HTTP-12", ["http-12"], ["gateway"], first="gateway")
            self.words(conn, "sha256 digest", ["digest"])
            self.words(conn, "gateway timeout", ["gateway"])

        self.each_layout(check, entries)

    def test_key_is_found_inside_urls_branches_and_punctuation(self):
        entries = [note("main", kind="task", jira_key="DEMO-42", title="DEMO-42: update ranking")]

        def check(conn, schema):
            for query, terms in (("https://tracker.example.test/browse/DEMO-42", ["https", "tracker.example.test/browse"]),
                                 ("feature/DEMO-42-fix-login", ["feature", "fix-login"]),
                                 ("(DEMO-42).", []), ("«demo-42»,", []), ("DEMO-42/DEMO-43", [])):
                value = self.ask(conn, query)
                self.assertIn("demo-42", value["selection"]["jira_keys"], query)
                self.assertEqual(value["selection"]["terms"], terms, query)
                self.assertEqual([item["id"] for item in value["results"]], ["main"], query)
            # Glued by a word character, the token is one different word.
            for query in ("x_DEMO-42", "DEMO-42x", "DEMO-42_"):
                self.assertEqual(self.ask(conn, query)["selection"]["jira_keys"], [], query)

        self.each_layout(check, entries)

    def test_run_longer_than_the_limit_is_not_an_issue_key(self):
        fits = "A" * (recall.ISSUE_KEY_CHARS - 2) + "-1"
        longer = "B" * (recall.ISSUE_KEY_CHARS - 1) + "-1"
        for index in (False, True):
            with self.subTest(index=index):
                conn = self.store([note("fits", jira_key=fits), note("longer", jira_key=longer)], index=index)
                self.keyed(conn, fits, [fits.lower()], ["fits"], first="fits")
                self.assertEqual(self.ask(conn, longer)["selection"]["jira_keys"], [])

    def test_keys_are_recognized_in_every_knowledge_state_and_ignore_filters(self):
        entries = [note("old", kind="task", jira_key="DEMO-42", knowledge_state="superseded"),
                   note("current", title="demo-7 playground", summary="An unrelated sandbox.")]

        def check(conn, schema):
            # The superseded card still tells that DEMO is an issue series.
            self.keyed(conn, "DEMO-42", ["demo-42"], [])
            self.keyed(conn, "demo-7 playground", ["demo-7"], ["current"])
            value = self.ask(conn, "DEMO-42", include_superseded=True)
            self.assertEqual([item["id"] for item in value["results"]], ["old"])
            filtered = self.ask(conn, "DEMO-42", where="kind = 'note'")
            self.assertEqual(filtered["selection"]["jira_keys"], ["demo-42"])
            self.assertTrue(filtered["no_match"])

        self.each_layout(check, entries)

    def test_digit_terms_match_whole_tokens_and_words_keep_prefixes(self):
        for term, text, expected in (
                ("sha256", "the sha256 digest", True), ("sha256", "the sha2560 digest", False),
                ("sha256", "sha256.", True), ("sha256", "hmac-sha256-v2", True),
                ("30", "atl-3010 and 300", False), ("30", "within 30 seconds", True),
                ("python3", "python3.13", True), ("python3", "python39", False),
                ("gemma-4", "gemma-40", False), ("cache", "caching", False),
                ("cach", "caching", True), ("warm", "prewarm", False)):
            with self.subTest(term=term, text=text):
                self.assertEqual(recall.matches_term(term, text), expected)

    def test_recognition_reads_indexes_only_and_writes_nothing(self):
        entries = [note("main", jira_key="DEMO-42"), note("task:OPS-7")]
        for schema, fts, index in LAYOUTS:
            with self.subTest(schema=schema, fts=fts, index=index):
                self.store(entries, [issue("jira:NET-5")], schema, fts, index).close()
                before = self.path.read_bytes()
                conn = memory.connect(self.path, readonly=True)
                self.addCleanup(conn.close)
                statements = []
                conn.set_trace_callback(statements.append)
                keys, spans = recall.issue_keys(conn, "demo-42 ops7 net-5 sha256")
                conn.set_trace_callback(None)
                self.assertEqual(keys, ["demo-42"] + (["net-5"] if schema >= 2 else []) + ["ops-7"])
                self.assertEqual(spans[:2], [(0, 7), (8, 12)])
                scans = [sql for sql in statements if "jira_key IS NOT NULL" in sql or "GLOB '[tT]" in sql]
                if index:
                    self.assertEqual(scans, [], "The search index must answer without scanning records")
                else:
                    self.assertEqual(len(scans), 3 if schema >= 2 else 2)
                    for sql in scans:
                        plan = " ".join(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql))
                        self.assertIn("COVERING INDEX", plan, sql)
                self.assertFalse(any("entries_fts" in sql or "summary" in sql for sql in statements))
                # A question without a number cannot name an issue: nothing is read at all.
                del statements[:]
                conn.set_trace_callback(statements.append)
                self.assertEqual(recall.issue_keys(conn, "why the cache was resized after the incident"), ([], []))
                conn.set_trace_callback(None)
                self.assertEqual(statements, [])
                conn.close()
                self.assertEqual(self.path.read_bytes(), before)

    def test_search_index_and_scan_agree_after_old_writer_changes(self):
        conn = self.store([note("moved", jira_key="DEMO-42"), note("task:OPS-7"), note("removed", jira_key="WEB-3"),
                           note("plain")], [issue("jira:NET-5")], index=True)

        def both(text):
            indexed = recall.issue_keys(conn, recall.normalize(text))
            with mock.patch.object(search_index, "recorded_issue_keys", return_value=None):
                self.assertEqual(recall.issue_keys(conn, recall.normalize(text)), indexed, text)
            return indexed[0]

        self.assertEqual(both("DEMO-42 DEMO-9 OPS-7 NET-5 WEB-3 CORE-1"),
                         ["demo-42", "demo-9", "net-5", "ops-7", "web-3"])
        # A stock connection skips the refresh: the index now holds stale keys.
        with closing(sqlite3.connect(str(self.path))) as old_writer, old_writer:
            old_writer.execute("UPDATE entries SET jira_key = 'CORE-1' WHERE id = 'moved'")
            old_writer.execute("UPDATE entries SET jira_key = 'ПРОЁКТ-5' WHERE id = 'plain'")
            old_writer.execute("UPDATE entries SET jira_key = NULL WHERE id = 'removed'")
            old_writer.execute("DELETE FROM graph_nodes WHERE ref = 'entity:jira:NET-5'")
            old_writer.execute("DELETE FROM entities WHERE id = 'jira:NET-5'")
        self.assertEqual(search_index.status(conn)["dirty"], {"entry": 3, "entity": 1})
        self.assertEqual(both("DEMO-42 DEMO-9 OPS-7 NET-5 WEB-3 CORE-1 core2 проект-5 ПРОЕКТ-6"),
                         ["core-1", "core-2", "ops-7", "проект-5", "проект-6"])
        with memory.transaction(conn):
            search_index.build(conn, memory)
        self.assertEqual(both("DEMO-42 OPS-7 NET-5 WEB-3 CORE-1 проект-5"), ["core-1", "ops-7", "проект-5"])

    def test_seeded_corpus_recognition_matches_a_full_scan_oracle(self):
        rng = random.Random(14112026)
        keys = ["DEMO-42", "demo42", "Demo-7", "ПРОЁКТ-12", "проект12", "AB#123", "#77", "CU-8669abc",
                "APP.WEB-12", "T1234", "12345", "Straße-7", "STRASSE-8", "a_b-3", "x-1-2", "2024-11",
                "v1.2", "İS-5", "MY_PROJ-9", "WEB2-7", "release", "two words-3", " padded-1", "ops"]
        identities = ["task:OPS-7", "TASK:ops-8", "Jira:NET-5", "taſk:LONG-1", "tasK:KEL-2",
                      "task:backup", "jira:плёнка-3", "note:DEMO-99", "task:", "task:x", "tasks:SET-1",
                      "jira:", "xjira:HID-4", "task:richi-recall-candidate-selection-20261016", "TaSk:Mixed-6"]
        conn = self.store()
        for number, identity in enumerate(identities):
            memory.entry_put(conn, note(identity, jira_key=keys[number] if number % 3 == 0 else None))
            graph.put(conn, issue(identity), "entity", memory)
        for number, key in enumerate(keys):
            memory.entry_put(conn, note("record-" + str(number), jira_key=key))
        pool = keys + [identity.partition(":")[2] for identity in identities] + [
            "sha256", "python3.13", "http404", "gemma-4", "ops", "release-2", "x-1-3", "2024-12", "v1.3",
            "t1235", "t-1234", "is-6", "web2-8", "web27", "my_proj10", "long-2", "kel-3", "set-1", "hid-4",
            "mixed-7", "richi-recall-candidate-selection-20261017", "candidate-selection-20261016", "#78",
            "ab#124", "cu-8669abd", "app.web12", "app.web-13", "strasse-7", "straße-9", "плёнка-4", "12346"]
        wrappers = ["{}", "({})", "{}.", "{},", "see/{}/now", "x_{}", "{}x", "{}-fix", "https://example.test/{}"]
        queries = []
        for _ in range(400):
            parts = []
            for _ in range(rng.randint(1, 4)):
                word = rng.choice(pool)
                word = rng.choice([word, word.upper(), word.lower(), word.replace("-", "")])
                parts.append(rng.choice(wrappers).format(word))
            queries.append(" ".join(parts))

        def oracle():
            rows = list(conn.execute("SELECT id, jira_key FROM entries")) + [
                (row[0], None) for row in conn.execute("SELECT id FROM entities")]
            return recall.declared_issue_keys([row[0] for row in rows], [row[1] for row in rows])

        def compare():
            declared = oracle()
            recognized = set()
            for query in queries:
                text = recall.normalize(query)
                with mock.patch.object(recall, "recorded_issue_keys", lambda conn, variants, series:
                                       recall.issue_key_hits(declared, variants, series)):
                    expected = recall.issue_keys(conn, text)
                with mock.patch.object(search_index, "recorded_issue_keys", return_value=None):
                    self.assertEqual(recall.issue_keys(conn, text), expected, query)
                self.assertEqual(recall.issue_keys(conn, text), expected, query)
                recognized.update(expected[0])
            return recognized

        self.assertFalse(search_index.status(conn)["available"])
        scanned = compare()
        self.assertTrue({"demo-42", "проект-12", "ab#123", "#77", "cu-8669abc", "long-1", "kel-2", "ops-8",
                         "richi-recall-candidate-selection-20261016", "mixed-6", "2024-11"} <= scanned)
        self.assertFalse({"sha-256", "http-404", "gemma-4", "backup", "x", "set-1", "hid-4", "release", "ops",
                          "12345", "12346", "candidate-selection-20261016"} & scanned)
        with memory.transaction(conn):
            search_index.build(conn, memory)
        self.assertTrue(search_index.status(conn)["available"])
        self.assertEqual(compare(), scanned)
        # Old writers leave stale index keys behind; recognition must still agree.
        for number in range(0, len(keys), 4):
            conn.execute("UPDATE entries SET jira_key = ? WHERE id = ?",
                         (rng.choice(pool), "record-" + str(number)))
        conn.execute("UPDATE entries SET jira_key = NULL WHERE id = 'record-1'")
        self.assertGreater(search_index.status(conn)["dirty"]["entry"], 0)
        compare()

    def test_query_with_too_many_candidates_scans_with_the_same_answer(self):
        conn = self.store([note("main", jira_key="DEMO-42")], index=True)
        text = recall.normalize("DEMO-42 demo-7 sha256 release/2.0")
        expected = recall.issue_keys(conn, text)
        self.assertEqual(expected[0], ["demo-42", "demo-7"])
        for limit in ("MAX_ISSUE_KEYS", "MAX_ISSUE_SERIES"):
            with self.subTest(limit=limit), mock.patch.object(search_index, limit, 0):
                self.assertIsNone(search_index.recorded_issue_keys(conn, {"demo-42"}, {"demo-"}))
                self.assertEqual(recall.issue_keys(conn, text), expected)

    def test_responses_are_identical_without_and_with_the_search_index(self):
        entries = [
            note("main", kind="task", jira_key="DEMO-42", title="DEMO-42: gateway latency"),
            note("longer", kind="task", jira_key="DEMO-420", title="DEMO-420: gateway latency"),
            note("azure", kind="task", jira_key="AB#123", title="Gateway retries"),
            note("cyrillic", kind="task", jira_key="ПРОЁКТ-12", title="Шлюз и повторы"),
            note("task:OPS-7", kind="task", title="Rotate the gateway certificate"),
            note("follow-up", kind="decision", summary="After DEMO42 and AB#123 the gateway pool was resized."),
            note("digest", title="sha256 digest of the gateway image", summary="Built with python3.13 on arm64."),
            note("old", jira_key="WEB-3", knowledge_state="superseded", title="Retired gateway"),
        ]
        queries = ["DEMO-42", "demo42 latency", "DEMO-999 latency", "AB#123", "ab#124 gateway", "проект-12",
                   "ПРОЕКТ12 шлюз", "OPS-7", "ops8", "WEB-3", "web-4 gateway", "sha256 gateway", "python3.13 arm64",
                   "gateway latency", "http404", "https://tracker.example.test/browse/DEMO-42?focus=7"]
        observed = {}
        for schema in (1, 2, 3):
            for fts in sorted({layout[1] for layout in LAYOUTS}):
                with self.subTest(schema=schema, fts=fts):
                    conn = self.store(entries, schema=schema, fts=fts)
                    before = {query: self.ask(conn, query, include_superseded=number % 2 == 0)
                              for number, query in enumerate(queries)}
                    with memory.transaction(conn):
                        search_index.build(conn, memory)
                    for number, query in enumerate(queries):
                        self.assertEqual(self.ask(conn, query, include_superseded=number % 2 == 0), before[query], query)
                    # Recognition does not depend on the schema or the search backend.
                    summary = {query: (value["selection"]["jira_keys"], value["selection"].get("jira_mode"),
                                       sorted(item["id"] for item in value["results"]),
                                       value["selection"].get("abstention_reason"))
                               for query, value in before.items()}
                    self.assertEqual(observed.setdefault("summary", summary), summary)
        self.assertEqual(observed["summary"]["ab#124 gateway"], (["ab#124"], "required_whole_key", [], "no_matching_issue_key"))
        self.assertEqual(observed["summary"]["sha256 gateway"], ([], None, ["digest"], None))


if __name__ == "__main__":
    unittest.main()
