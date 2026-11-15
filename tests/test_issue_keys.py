"""Issue keys come from what the store records, never from the shape of a token.

Disposable synthetic stores only. Behaviour scenarios run on schemas 1, 2 and 3,
with FTS5 and with the scan backend, without and with the optional search index.
A test of one mechanism names the layouts it needs.
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


FTS5 = fts5_available()
LAYOUTS = [(schema, fts, index) for schema in (1, 2, 3)
           for fts in ((True, False) if FTS5 else (False,)) for index in (False, True)]


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

    def store(self, entries=(), entities=(), schema=3, fts=FTS5, index=False):
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

    def words(self, conn, query, found, **options):
        """The query is ordinary words: no key mode, and every word takes part."""
        value = self.ask(conn, query, **options)
        selection = value["selection"]
        self.assertEqual(selection["jira_keys"], [], query)
        self.assertNotIn("jira_mode", selection, query)
        self.assertEqual(selection["terms"], recall.phrase(query).split(), query)
        ids = [item["id"] for item in value["results"]]
        self.assertEqual(sorted(ids), sorted(found), query)
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
            self.words(conn, "python3.13 ResourceWarning", ["warning"])
            self.words(conn, "python3 launcher", ["launcher"])
            self.words(conn, "sha256", ["digest"])
            self.words(conn, "sha2560", ["longer-digest"])
            self.words(conn, "fts5 tokenizer", ["tokenizer"])
            self.words(conn, "gemma-4 probe", ["probe"])
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
            self.words(conn, "recorded tracker item", list(recorded) + list(near), limit=20)

        self.each_layout(check, entries)

    def test_only_a_letters_number_key_has_a_compact_spelling(self):
        entries = [
            note("item", kind="task", jira_key="REL-1-2", title="Release item"),
            note("other", kind="task", jira_key="REL12", title="Other issue"),
            note("clickup", kind="task", jira_key="CU-8669abc", title="Export format"),
            note("mentions", summary="See REL12 and CU8669abc."),
        ]

        def check(conn, schema):
            # REL-1-2 without its hyphens would be another issue, REL12.
            self.keyed(conn, "REL-1-2", ["rel-1-2"], ["item"], first="item")
            self.keyed(conn, "REL12", ["rel-12"], ["other", "mentions"], first="other")
            self.keyed(conn, "rel-12", ["rel-12"], ["other", "mentions"], first="other")
            self.keyed(conn, "CU-8669abc", ["cu-8669abc"], ["clickup"], first="clickup")
            self.words(conn, "CU8669abc", ["mentions"])
            self.assertEqual(recall.key_variants("rel-1-2"), ("rel-1-2",))
            self.assertEqual(recall.key_variants("проект-12"), ("проект-12", "проект12"))
            self.assertEqual(recall.key_variants("#77"), ("#77",))

        self.each_layout(check, entries)

    def test_series_recorded_only_in_its_compact_spelling(self):
        entries = [note("compact", kind="task", jira_key="DEMO42", title="Cache warmup"),
                   note("task:ops7", kind="task", title="Rotate the certificate")]

        def check(conn, schema):
            self.keyed(conn, "DEMO-42", ["demo-42"], ["compact"], first="compact")
            self.keyed(conn, "demo-7 warmup", ["demo-7"], [])
            self.keyed(conn, "demo8", ["demo-8"], [])
            self.keyed(conn, "OPS-7", ["ops-7"], ["task:ops7"], first="task:ops7")
            self.keyed(conn, "ops-8 certificate", ["ops-8"], [])

        self.each_layout(check, entries)

    def test_several_keys_in_one_question(self):
        entries = [note("main", kind="task", jira_key="DEMO-42", title="Ranking"),
                   note("sibling", kind="task", jira_key="DEMO-43", title="Caching"),
                   note("both", summary="DEMO-42 and DEMO-43 were released together."),
                   note("one", summary="Blocked by DEMO-43."),
                   note("other", kind="task", jira_key="DEMO-44", title="Unrelated")]

        def check(conn, schema):
            value = self.keyed(conn, "DEMO-42/DEMO-43", ["demo-42", "demo-43"], ["main", "sibling", "both", "one"])
            # A record is credited with the keys it mentions; its own jira_key is no mention.
            mentioned = {item["id"]: [reason for reason in item["match_reasons"] if reason.startswith("keywords:")]
                         for item in value["results"]}
            self.assertEqual(mentioned, {"main": [], "sibling": [], "both": ["keywords:demo-42,demo-43"],
                                         "one": ["keywords:demo-43"]})

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
                   note("current", title="demo-7 playground", summary="An unrelated sandbox."),
                   note("web", kind="task", jira_key="WEB-5", title="Web item")]

        def check(conn, schema):
            # The superseded card still tells that DEMO is an issue series.
            self.keyed(conn, "DEMO-42", ["demo-42"], [])
            self.keyed(conn, "demo-7 playground", ["demo-7"], ["current"])
            value = self.ask(conn, "DEMO-42", include_superseded=True)
            self.assertEqual([item["id"] for item in value["results"]], ["old"])
            # The filter hides the only record that declares WEB-5; it is still a key.
            self.keyed(conn, "WEB-5 item", ["web-5"], ["web"], first="web")
            filtered = self.ask(conn, "WEB-5 item", where="kind = 'note'")
            self.assertEqual(filtered["selection"]["jira_keys"], ["web-5"])
            self.assertEqual(filtered["selection"]["abstention_reason"], "no_matching_issue_key")

        self.each_layout(check, entries)

    def test_digit_terms_match_whole_tokens_and_words_keep_prefixes(self):
        for term, text, expected in (
                ("sha256", "the sha256 digest", True), ("sha256", "the sha2560 digest", False),
                ("sha256", "sha256.", True), ("sha256", "hmac-sha256-v2", True),
                ("30", "atl-3010 and 300", False), ("30", "within 30 seconds", True),
                ("python3", "python3.13", True), ("python3", "python39", False),
                ("gemma-4", "gemma-40", False), ("cach", "caching", True),
                ("sha256", "the_sha256_digest", True), ("30", "timeout_30", True)):
            with self.subTest(term=term, text=text):
                self.assertEqual(recall.matches_term(term, text), expected)

    def test_recognition_only_reads_indexes_on_a_read_only_connection(self):
        entries = [note("main", jira_key="DEMO-42"), note("task:OPS-7")]
        for schema, fts, index in LAYOUTS:
            with self.subTest(schema=schema, fts=fts, index=index):
                self.store(entries, [issue("jira:NET-5")], schema, fts, index).close()
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
                self.assertTrue(all(sql.lstrip().startswith("SELECT") for sql in statements), statements)
                # A question without a number cannot name an issue: nothing is read at all.
                del statements[:]
                conn.set_trace_callback(statements.append)
                self.assertEqual(recall.issue_keys(conn, "why the cache was resized after the incident"), ([], []))
                conn.set_trace_callback(None)
                self.assertEqual(statements, [])
                conn.close()

    def test_search_index_and_scan_agree_after_old_writer_changes(self):
        conn = self.store([note("moved", jira_key="DEMO-42"), note("task:OPS-7"), note("removed", jira_key="WEB-3"),
                           note("plain")], [issue("jira:NET-5")], index=True)

        def both(text):
            indexed = recall.issue_keys(conn, recall.normalize(text))
            with mock.patch.object(search_index, "indexed_issue_keys", return_value=None):
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

    def test_index_and_scan_recognize_the_same_keys_in_a_seeded_corpus(self):
        rng = random.Random(14112026)
        keys = ["DEMO-42", "demo42", "Demo-7", "ПРОЁКТ-12", "проект12", "AB#123", "#77", "CU-8669abc",
                "APP.WEB-12", "T1234", "12345", "Straße-7", "STRASSE-8", "a_b-3", "x-1-2", "2024-11",
                "v1.2", "İS-5", "MY_PROJ-9", "WEB2-7", "release", "two words-3", " padded-1", "ops"]
        identities = ["task:OPS-7", "TASK:ops-8", "Jira:NET-5", "taſk:LONG-1", "tasK:KEL-2",
                      "task:backup", "jira:плёнка-3", "note:DEMO-99", "task:", "task:x", "tasks:SET-1",
                      "jira:", "xjira:HID-4", "task:atlas-cache-warmup-review-20260106", "TaSk:Mixed-6"]
        conn = self.store()
        for number, identity in enumerate(identities):
            memory.entry_put(conn, note(identity, jira_key=keys[number] if number % 3 == 0 else None))
            graph.put(conn, issue(identity), "entity", memory)
        for number, key in enumerate(keys):
            memory.entry_put(conn, note("record-" + str(number), jira_key=key))
        pool = keys + [identity.partition(":")[2] for identity in identities] + [
            "sha256", "python3.13", "http404", "gemma-4", "ops", "release-2", "x-1-3", "2024-12", "v1.3",
            "t1235", "t-1234", "is-6", "web2-8", "web27", "my_proj10", "long-2", "kel-3", "set-1", "hid-4",
            "mixed-7", "atlas-cache-warmup-review-20260107", "warmup-review-20260106", "#78",
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
            # Every record read in full: independent of the index and of the SQL
            # prefilter of the scan, though not of the declaration rules themselves.
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
                self.assertEqual(recall.issue_keys(conn, text), expected, query)
                recognized.update(expected[0])
            return recognized

        self.assertFalse(search_index.status(conn)["available"])
        scanned = compare()
        self.assertTrue({"demo-42", "проект-12", "ab#123", "#77", "cu-8669abc", "long-1", "kel-2", "ops-8",
                         "atlas-cache-warmup-review-20260106", "mixed-6", "2024-11"} <= scanned)
        self.assertFalse({"sha-256", "http-404", "gemma-4", "backup", "x", "set-1", "hid-4", "release", "ops",
                          "12345", "12346", "warmup-review-20260106"} & scanned)
        with memory.transaction(conn):
            search_index.build(conn, memory)
        self.assertTrue(search_index.status(conn)["available"])
        self.assertEqual(compare(), scanned)

    def test_only_an_extreme_query_leaves_the_index_for_the_scan(self):
        conn = self.store([note("main", jira_key="DEMO-42")], index=True)
        path = "see /srv/app-1/releases/2026-01-02/v1.2.3/lib/python3.9/site-packages/x1/y2 DEMO-42 demo-7"
        extreme = "DEMO-42 demo-7 " + "/".join("v{}.{}".format(number, number + 1) for number in range(400))
        for query, indexed in ((path, True), (extreme, False)):
            with self.subTest(indexed=indexed):
                with mock.patch.object(search_index, "indexed_issue_keys",
                                       wraps=search_index.indexed_issue_keys) as lookup:
                    keys, _ = recall.issue_keys(conn, recall.normalize(query))
                self.assertEqual(keys, ["demo-42", "demo-7"])
                variants, series = lookup.call_args[0][1:]
                self.assertEqual(len(variants) <= search_index.MAX_ISSUE_KEYS, indexed)
                self.assertEqual(search_index.indexed_issue_keys(conn, variants, series) is not None, indexed)

    def test_entities_that_mention_a_key_pass_the_index_gate(self):
        # AB#123, #77 and C++-7 are several lexical tokens each in the entity index.
        entries = [note("azure", kind="task", jira_key="AB#123", title="Gateway retries"),
                   note("github", kind="task", jira_key="#77", title="Upload retries"),
                   note("plus", kind="task", jira_key="C++-7", title="Port the parser")]
        entities = [issue("svc-a", kind="component", summary="Changed by AB#123 last week."),
                    issue("svc-b", kind="component", title="Regression after #77"),
                    issue("svc-c", kind="component", aliases=["C++-7 port"]),
                    issue("svc-d", kind="component", summary="Mentions 123, 77 and 7 without any key.")]

        for schema, fts, index in LAYOUTS:
            if schema >= 2:
                with self.subTest(schema=schema, fts=fts, index=index):
                    conn = self.store(entries, entities, schema, fts, index)
                    self.keyed(conn, "AB#123", ["ab#123"], ["azure", "svc-a"], first="azure")
                    self.keyed(conn, "#77", ["#77"], ["github", "svc-b"], first="github")
                    self.keyed(conn, "C++-7", ["c++-7"], ["plus", "svc-c"], first="plus")

    def test_graph_neighbour_without_the_key_is_not_added(self):
        entries = [note("main", kind="task", jira_key="DEMO-42", title="Ranking"),
                   note("neighbour", title="Capacity plan"),
                   note("keyed-neighbour", summary="Planned together with DEMO-42.")]
        for schema, fts, index in LAYOUTS:
            if schema >= 2:
                with self.subTest(schema=schema, fts=fts, index=index):
                    conn = self.store(entries, schema=schema, fts=fts, index=index)
                    for target in ("neighbour", "keyed-neighbour"):
                        graph.put(conn, dict(id="main-" + target, from_ref="entry:main", to_ref="entry:" + target,
                                             kind="informs", description="Recorded link.", knowledge_state="confirmed",
                                             sources=[{"reference": "https://example.test/link/" + target}]), "edge", memory)
                    self.keyed(conn, "DEMO-42", ["demo-42"], ["main", "keyed-neighbour"], first="main")
                    explained = self.ask(conn, "DEMO-42", expect="entry:neighbour")["explain"]["expected"]
                    self.assertEqual(explained["graph_stage"], "jira_guard")

    def test_key_mode_shortlist_holds_only_records_with_the_key(self):
        # FTS5 keeps only "12" of the key #12, and every date with a 12 matches that.
        holders = [note("github", kind="task", jira_key="#12", title="Upload retries"),
                   note("in-summary", kind="decision", summary="Decided after #12 to cap the retries."),
                   note("in-title", title="Follow-up to #12"),
                   note("in-alias", aliases=["#12"], title="Retry budget"),
                   note("in-shared-key", kind="task", jira_key="#12, #13", title="Two issues in one card"),
                   note("retro-#12", title="Retrospective")]
        ids = [value["id"] for value in holders]
        for schema, fts, index in LAYOUTS:
            if schema == 3:
                with self.subTest(fts=fts, index=index):
                    scores = []
                    for unrelated in (recall.CANDIDATE_LIMIT - 20, recall.CANDIDATE_LIMIT + 40):
                        dated = [note("dated-{:03d}".format(number), title="Report of 2026-01-12, part {}".format(number))
                                 for number in range(unrelated)]
                        conn = self.store(holders + dated, schema=schema, fts=fts, index=index)
                        value = self.keyed(conn, "#12", ["#12"], ids, first="github")
                        self.assertFalse(value["truncated"])
                        scores.append([(item["id"], item["retrieval_score"]) for item in value["results"]])
                        if fts:
                            self.assertEqual(value["selection"]["discarded_weak_matches"], unrelated)
                        filtered = self.ask(conn, "#12", where="kind = 'decision'", expect="entry:in-summary")
                        self.assertEqual([item["id"] for item in filtered["results"]], ["in-summary"])
                        self.assertEqual(filtered["explain"]["expected"]["outcome"], "returned")
                    # Unrelated rows that cross the shortlist limit change nothing for the holders.
                    self.assertEqual(scores[0], scores[1])
                    with mock.patch.object(recall, "CANDIDATE_LIMIT", 3):
                        capped = self.ask(conn, "#12")
                    self.assertEqual(capped["truncated"], fts)
                    self.assertLessEqual({item["id"] for item in capped["results"]}, set(ids))

    def test_digit_term_shortlist_is_not_filled_by_longer_tokens(self):
        # "sha256"* would shortlist 170 rows with sha2560, and lexical() rejects them all.
        entries = [note("digest", summary="The manifest stores a sha256 of the wheel."),
                   note("timeout", summary="The gateway waits 30 seconds.")]
        entries += [note("longer-{:03d}".format(number), title="sha2560 probe with 300 requests")
                    for number in range(recall.CANDIDATE_LIMIT + 10)]
        for schema, fts, index in LAYOUTS:
            if schema == 3:
                with self.subTest(fts=fts, index=index):
                    conn = self.store(entries, schema=schema, fts=fts, index=index)
                    self.assertFalse(self.words(conn, "sha256", ["digest"])["truncated"])
                    self.assertFalse(self.words(conn, "30 seconds", ["timeout"])["truncated"])

    def test_record_that_holds_a_key_is_an_answer_wherever_it_holds_it(self):
        entries = [note("github", kind="task", jira_key="#77", title="Upload retries"),
                   note("aliased", aliases=["#77"], title="Retry budget"),
                   note("series", kind="task", jira_key="DEMO-7", title="Cache warmup"),
                   note("shared", kind="task", jira_key="DEMO-45, DEMO-46", title="Two issues in one card")]

        def check(conn, schema):
            # Other words in the question must not hide a card that passed the key guard.
            self.keyed(conn, "issue #77 status", ["#77"], ["github", "aliased"], first="github")
            self.keyed(conn, "DEMO-45 status", ["demo-45"], ["shared"])

        self.each_layout(check, entries)

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
