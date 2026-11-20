"""A current decision can be read and revised without inferring approval from prose."""
import copy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from richi import compact, graph, measurements, memory, recall, search_index, task_brief


class DecisionCardTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "memory.sqlite3"
        self.conn = memory.connect(self.db, create=True)
        self.addCleanup(self.conn.close)
        memory.initialize(self.conn, self.db)
        self.task = {"id": "task:DEMO-72", "kind": "task", "jira_key": "DEMO-72", "title": "Batch encoding",
                     "summary": "The package check passed. " * 1800,
                     "sources": [{"reference": "https://example.invalid/notes/{}".format(i)} for i in range(96)],
                     "knowledge_state": "confirmed", "expected_updated_at": None}
        self.card = {"question": "Enable batch encoding in production?",
                     "observations": [{"text": "Encoding was faster. End-to-end latency is still unverified.",
                                       "reference": "entry:experiment:batch-trial"}],
                     "recommendation": {"text": "Limit the first release to small batches.", "by": "agent",
                                        "reference": "entry:experiment:batch-trial"},
                     "decision": {"status": "pending"},
                     "reconsider_when": "Compare paired deadline completion and end-to-end tail latency."}

    def put(self, payload):
        with memory.transaction(self.conn):
            return memory.entry_put(self.conn, payload)

    def brief(self, budget):
        args = SimpleNamespace(query="Should we enable DEMO-72?", limit=1, max_chars=budget)
        with memory.transaction(self.conn, write=False):
            result = task_brief.brief(self.conn, args, memory, self.db)
        self.assertLessEqual(len(compact.rendered(result)), budget)
        self.assertEqual(result["budget"]["output_chars"], len(compact.rendered(result)))
        return result

    def test_first_brief_on_long_task_preserves_entire_pending_decision_and_links(self):
        self.put({"id": "experiment:batch-trial", "kind": "experiment", "title": "Batch trial",
                  "summary": self.card["recommendation"]["text"], "knowledge_state": "confirmed"})
        self.put(dict(self.task, decision_card=self.card))
        for budget in (4000, 16000):
            result = self.brief(budget)
            card = result["results"][0]
            self.assertEqual(card["ref"], "entry:task:DEMO-72")
            self.assertEqual(card["decision_card"], self.card)
            self.assertTrue(card["is_excerpt"])
            self.assertGreater(card["sources_omitted"], 90)
            ref = card["decision_card"]["recommendation"]["reference"]
            self.assertEqual(memory.entry_get(self.conn, ref.partition(":")[2])["summary"],
                             self.card["recommendation"]["text"])

    def test_tiny_budget_omits_whole_decision_with_full_pointer(self):
        self.card["reconsider_when"] = "Evidence from the open trial. " * 140
        self.put(dict(self.task, decision_card=self.card))
        result = self.brief(4000)
        self.assertTrue(result["truncated"])
        card = result["results"][0]
        self.assertTrue(card["decision_card_omitted"])
        self.assertNotIn("decision_card", card)
        self.assertEqual(memory.entry_get(self.conn, card["read_more"]["id"])["decision_card"], self.card)

    def test_shared_budget_keeps_whole_decision_or_measurement_when_only_one_fits(self):
        row = {"metric": "Tail latency", "value": "12.4", "unit": "ms", "sample": "200 batches"}
        huge_ref = "https://example.invalid/" + "long-file/" * 500
        source = {"reference": huge_ref, "measurements": [row]}
        self.put(dict(self.task, decision_card=self.card, sources=[source]))
        card = self.brief(4000)["results"][0]
        self.assertEqual(card["decision_card"], self.card)
        self.assertEqual(card["measurements"], [])
        self.assertEqual(card["measurements_omitted"], 1)

        source["reference"] = "https://example.invalid/batches.csv"
        self.card["reconsider_when"] = "Compare paired observations. " * 140
        version = memory.entry_get(self.conn, self.task["id"])["updated_at"]
        self.put(dict(self.task, decision_card=self.card, sources=[source], expected_updated_at=version))
        card = self.brief(4000)["results"][0]
        self.assertTrue(card["decision_card_omitted"])
        self.assertEqual(card["measurements"], [dict(row, source_index=1, source={"reference": source["reference"]})])
        self.assertEqual(card["measurements_omitted"], 0)

    def test_add_to_legacy_update_remove_and_replay_preserve_history_and_cas(self):
        self.put(dict(self.task, event_id="legacy"))
        legacy = memory.entry_get(self.conn, self.task["id"])
        self.assertNotIn("decision_card", legacy)
        self.assertNotIn("decision_card", self.brief(4000)["results"][0])
        proposal = dict(self.task, decision_card=self.card, expected_updated_at=legacy["updated_at"],
                        event_id="proposal")
        self.put(proposal)
        self.assertEqual(self.put(dict(self.task, event_id="legacy"))["status"], "replayed")
        pending = memory.entry_get(self.conn, self.task["id"])
        decided = copy.deepcopy(self.card)
        decided["decision"] = {"status": "rejected", "by": "release owner", "text": "Defer the rollout.",
                               "reference": "https://example.invalid/reviews/72"}
        with self.assertRaisesRegex(memory.MemoryError, "conflict"):
            self.put(dict(self.task, decision_card=decided, expected_updated_at=legacy["updated_at"]))
        self.put(dict(self.task, decision_card=decided, expected_updated_at=pending["updated_at"]))
        self.assertEqual(self.put(proposal)["status"], "replayed")
        self.assertEqual(self.brief(4000)["results"][0]["decision_card"], decided)
        history = memory.entry_get(self.conn, self.task["id"], history=True)
        self.assertEqual(history["history"][-1]["before"]["decision_card"], self.card)
        self.assertEqual(history["history"][-1]["after"]["decision_card"], decided)
        self.assertEqual(self.put(dict(self.task, decision_card=decided))["status"], "unchanged")
        self.put(dict(self.task, expected_updated_at=history["updated_at"]))
        self.assertNotIn("decision_card", memory.entry_get(self.conn, self.task["id"]))
        self.assertFalse(self.conn.execute("SELECT count(*) FROM metadata WHERE key LIKE 'decision-card:%'").fetchone()[0])

    def test_invalid_outcomes_and_unattributed_claims(self):
        for change in ({"decision": {"status": "pending", "text": "Release it"}},
                       {"decision": {"status": "accepted", "by": "owner", "text": "Release it"}},
                       {"decision": {"status": "rejected", "text": "Defer it", "reference": "meeting"}},
                       {"recommendation": {"text": "Roll out", "reference": "review"}},
                       {"observations": [{"text": "Faster"}]}, {"unknown": "field"}):
            with self.subTest(change=change), self.assertRaises(memory.MemoryError):
                self.put(dict(self.task, decision_card=dict(self.card, **change)))
        with self.assertRaisesRegex(memory.MemoryError, "Only a task"):
            self.put(dict(self.task, kind="experiment", decision_card=self.card))

    def test_older_runtime_revocation_cannot_resurrect_accepted_decision(self):
        accepted = dict(self.card, decision={"status": "accepted", "by": "owner",
                                             "text": "Enable small batches.", "reference": "review:72"})
        self.put(dict(self.task, decision_card=accepted))
        # The old runtime only writes entry columns and the corresponding
        # immutable history. Its view has no decision_card field.
        before = memory.entry_get(self.conn, self.task["id"])
        del before["decision_card"]
        after = dict(before, summary="The owner revoked approval after the trial.",
                     updated_at=memory.next_updated_at(before["updated_at"]))
        with memory.transaction(self.conn):
            self.conn.execute("UPDATE entries SET summary = ?, updated_at = ? WHERE id = ?",
                              (after["summary"], after["updated_at"], after["id"]))
            self.conn.execute("INSERT INTO entry_history(entry_id, changed_at, before_json, after_json) "
                              "VALUES (?, ?, ?, ?)", (after["id"], after["updated_at"],
                                                      memory.dumps(before), memory.dumps(after)))
        self.assertNotIn("decision_card", self.brief(4000)["results"][0])
        current = memory.entry_get(self.conn, after["id"], history=True)
        self.assertNotIn("decision_card", current)
        self.assertEqual(current["history"][0]["after"]["decision_card"], accepted)
        self.put(dict(self.task, summary=after["summary"], decision_card=self.card,
                      expected_updated_at=current["updated_at"]))
        self.assertEqual(self.brief(4000)["results"][0]["decision_card"]["decision"], {"status": "pending"})

    def test_structured_fields_select_current_evidence_before_brief_ranking(self):
        def found(query, **options):
            args = SimpleNamespace(query=query, limit=5, max_chars=16000, **options)
            return {card["ref"]: card for card in task_brief.brief(self.conn, args, memory, self.db)["results"]}

        field = {"metric": "Zstd throughput", "value": "57", "unit": "Mbps", "sample": "regional canary"}
        sources = [{"reference": "synthetic://table/unique-locator", "measurements": [field]}]
        self.card["question"] = "Enable Brotli for payloads?"
        self.put(dict(self.task, decision_card=self.card, sources=sources))
        self.put(dict(self.task, id="task:DEMO-73", jira_key="DEMO-73", knowledge_state="hypothesis",
                      decision_card=self.card))
        task = "entry:task:DEMO-72"
        hidden = "entry:task:DEMO-73"
        graph.put(self.conn, {"id": "codec", "kind": "concept", "title": "Codec", "summary": "Trial",
                              "sources": sources, "knowledge_state": "confirmed"}, "entity", memory)
        graph.put(self.conn, {"id": "trial", "kind": "informs", "from_ref": "entity:codec", "to_ref": task,
                              "description": "Trial results", "sources": sources, "knowledge_state": "confirmed"},
                  "edge", memory)
        with memory.transaction(self.conn):
            search_index.build(self.conn, memory)
        for term in ("Brotli", "Zstd", "Mbps", "regional", "57"):
            card = found(term)[task]
            self.assertIn("decision_card" if term == "Brotli" else "measurement", card["query_match_reasons"])
        measured = found("Zstd")
        self.assertEqual(set(measured), {task, "entity:codec", "edge:trial"})
        self.assertEqual(measured["edge:trial"]["from_ref"], "entity:codec")
        self.assertEqual(measured["entity:codec"]["measurements"][0]["source"]["reference"], sources[0]["reference"])
        self.assertNotIn(hidden, found("Brotli"))
        self.assertIn(hidden, found("Brotli", include_hypotheses=True))
        self.assertNotIn(task, found("DEMO-73 Brotli", include_hypotheses=True))
        self.assertEqual(found("unique-locator"), {})

        # A pre-card writer changes the owner without touching the sidecar.
        self.conn.execute("UPDATE entries SET updated_at = ? WHERE id = ?", ("2030-01-01", self.task["id"]))
        self.assertNotIn(task, found("Brotli"))

    def test_issue_key_routes_the_card_without_crowding_out_the_requested_measurement(self):
        keys = ("#662", "DEMO-72")
        target = {"metric": "Duration", "value": "57", "unit": "ms", "sample": "canary"}
        source = {"reference": "synthetic://table"}
        for key in keys:
            rows = [{"metric": "Duration", "value": str(i), "unit": "ms", "sample": key} for i in range(8)]
            self.put(dict(self.task, id="task:" + key, jira_key=key, summary="Trial results",
                          sources=[dict(source, measurements=rows + [target])]))

        # The last query puts "canary" beyond recall's 16-term diagnostic excerpt.
        qualifiers = ("regional remote primary secondary paired steady warm cold peak busy quiet daily "
                      "monthly sampled filtered raw final")
        for indexed in (False, True):
            if indexed:
                with memory.transaction(self.conn):
                    search_index.build(self.conn, memory)
            for key in (*keys, ""):
                queries = (f"{key} canary", f"What was the duration for the canary in {key}?",
                           f"{key} 57", f"{key} {qualifiers} canary") if key else ("What was the canary duration?",)
                for query in queries:
                    with self.subTest(indexed=indexed, query=query):
                        args = SimpleNamespace(query=query, limit=2, max_chars=16000)
                        cards = task_brief.brief(self.conn, args, memory, self.db)["results"]
                        expected_keys = [key] if key else keys
                        self.assertCountEqual([card["ref"] for card in cards], ["entry:task:" + k for k in expected_keys])
                        for card in cards:
                            self.assertEqual(len(card["measurements"]), 8)
                            self.assertEqual(card["measurements_omitted"], 1)
                            self.assertEqual(card["measurements"][0], dict(target, source_index=1, source=source))

    def test_one_digit_measurement_is_searchable_without_weakening_issue_routing(self):
        rows = [{"metric": "Latency", "value": str(n), "unit": "ms", "sample": "cohort"}
                for n in (*range(11, 19), 7)]
        self.put(dict(self.task, summary="Trial results", sources=[{"reference": "trial", "measurements": rows}]))
        other = dict(self.task, id="task:DEMO-7", jira_key="DEMO-7", summary="Other trial", sources=[],
                     decision_card=dict(self.card, question="Which of the 7 options?"))
        self.put(other)

        def args(query):
            return SimpleNamespace(query=query, limit=2, max_chars=16000)

        for indexed in (False, True):
            if indexed:
                with memory.transaction(self.conn):
                    search_index.build(self.conn, memory)
            for query in ("7", "DEMO-72 7 ms"):
                with self.subTest(indexed=indexed, query=query):
                    card, = task_brief.brief(self.conn, args(query), memory, self.db)["results"]
                    self.assertEqual(card["id"], self.task["id"])
                    self.assertEqual(card["measurements"][0]["value"], "7")
            routed, = task_brief.brief(self.conn, args("DEMO-7"), memory, self.db)["results"]
            self.assertEqual(routed["id"], other["id"])
            self.assertTrue(recall.recall(self.conn, args("7"), memory)["no_match"])

    def test_measurement_match_keeps_relevant_row_with_legacy_inline_sources(self):
        rows = [{"metric": "queue depth", "value": str(n), "unit": "items", "sample": "The trial is complete"}
                for n in range(10)]
        target = {"metric": "Throughput", "value": "57", "unit": "Mbps", "sample": "paired runs"}
        rows.append(target)
        sources = [{"reference": "synthetic://table", "measurements": rows}]
        self.put(dict(self.task, sources=sources))
        for inline in (False, True):
            if inline:
                self.conn.execute("DELETE FROM metadata WHERE key = ?", (measurements.KEY_PREFIX + "entry:" + self.task["id"],))
                self.conn.execute("UPDATE entries SET sources = ? WHERE id = ?", (memory.dumps(sources), self.task["id"]))
            for word in ("throughput", "throughp"):
                with self.subTest(inline=inline, word=word):
                    args = SimpleNamespace(query="What is the " + word + "?", limit=2, max_chars=16000)
                    result = task_brief.brief(self.conn, args, memory, self.db)
                    self.assertFalse(result["no_match"])
                    self.assertEqual(result["results"][0]["measurements"][0]["metric"], "Throughput")

    def test_brief_matches_whole_numeric_observations_in_lookup_and_row_selection(self):
        def row(value, sample):
            return {"metric": "Latency", "value": value, "unit": "ms", "sample": sample}

        def cards(question):
            args = SimpleNamespace(query=question, limit=3, max_chars=16000)
            return task_brief.brief(self.conn, args, memory, self.db)["results"]

        cases = (
            ("5", "0.5", ("5", "5 ms", "+5ms")),
            ("0.5", "5", ("0.5", "0,5", ".5ms")),
            (",5", "-0.5", ("0.5", "0,5 ms")),
            ("−5", "5", ("-5", "−5ms")),
            ("5e+3", "5", ("5e3", "5e+3ms")),
            ("3.2e−5", "5", ("3,2e-5", "3.2e−5ms")),
            ("5", "3.2e-5", ("5", "5ms")),
            ("5", "v0.5", ("5", "5ms")),
            ("5", "EX-5", ("5", "5ms")),
            ("5", "5abc", ("5", "5ms")),
            ("5ms", "0.5ms", ("5", "5ms")),
            ("234.56", "1,234.56", ("234.56ms",)),
            ("1", "1,234.56", ("1ms",)),
            ("0.56", "1.234,56", ("0.56ms",)),
            ("1,234.56", "234.56", ("1234.56ms", "1.234,56ms", "1\u202f234.56ms")),
            ("1.234,56", "234.56", ("1234.56ms", "1,234.56ms")),
            ("1 234 567,89", "567.89", ("1234567.89ms", "1,234,567.89ms")),
            ("−1\u00a0234,56e−2", "-234.56e-2", ("-1234.56e-2ms",)),
            ("1,234,567", "567", ("1234567ms",)),
            ("567", "1.234.567", ("567ms",)),
            ("12 → 240", "12240", ("12ms", "240ms")),
        )
        for indexed in (False, True):
            for wanted, other, queries in cases:
                distractors = [row(other, "cohort " + chr(97 + i)) for i in range(8)]
                target = row(wanted, "canary")
                for identity, rows in (("DEMO-72", distractors + [target]), ("DEMO-73", distractors)):
                    previous = memory.entry_record(self.conn, "task:" + identity)
                    self.put(dict(self.task, id="task:" + identity, jira_key=identity,
                                  summary="Observed results.", sources=[{"reference": "test://trial", "measurements": rows}],
                                  expected_updated_at=previous["updated_at"] if previous else None))
                if indexed:
                    with memory.transaction(self.conn):
                        search_index.build(self.conn, memory)
                for query in queries:
                    with self.subTest(wanted=wanted, other=other, query=query, indexed=indexed):
                        # A unit alone may find both records. A bare or attached
                        # quantity can only discover the one with the requested value.
                        if " " not in query:
                            self.assertEqual([card["id"] for card in cards(query)], ["task:DEMO-72"])
                        card, = cards("DEMO-72 " + query)
                        self.assertEqual(card["measurements"][0]["value"], wanted)
                        self.assertEqual(card["measurements_omitted"], 1)

    def test_quantity_units_retain_case_in_discovery_and_measurement_selection(self):
        def source(unit):
            row = {"metric": "Power", "value": "5", "unit": unit, "sample": "trial"}
            return {"reference": "synthetic://trial/" + unit, "measurements": [row]}

        for identity, units in (("a", ["mW"]), ("z", ["mW", "MW"]),
                                ("bits", ["Mb/s"]), ("bytes", ["Mb/s", "MB/s"])):
            self.put(dict(self.task, id=identity, jira_key=None, title="Trial", summary="Observed results.",
                          sources=[source(unit) for unit in units]))
        for indexed in (False, True):
            if indexed:
                with memory.transaction(self.conn):
                    search_index.build(self.conn, memory)
            for query, identity, unit in (("5MW", "z", "MW"), ("5 MW", "z", "MW"),
                                          ("5mW", "a", "mW"), ("5 mW", "a", "mW"),
                                          ("5MB/s", "bytes", "MB/s"), ("5 MB/s", "bytes", "MB/s"),
                                          ("5Mb/s", "bits", "Mb/s")):
                with self.subTest(indexed=indexed, query=query):
                    args = SimpleNamespace(query=query, limit=1, max_chars=16000)
                    card, = task_brief.brief(self.conn, args, memory, self.db)["results"]
                    winner = card["measurements"][0]
                    self.assertEqual((card["id"], winner["unit"], winner["source"]["reference"]),
                                     (identity, unit, source(unit)["reference"]))
            args = SimpleNamespace(query="5MW", limit=2, max_chars=16000)
            result = task_brief.brief(self.conn, args, memory, self.db)
            self.assertEqual([card["id"] for card in result["results"]], ["z"])

        self.put(dict(self.task, sources=[source("mW"), source("MW")]))
        terms, question, keys = recall._routed_terms(self.conn, "Straße DEMO-72 5MW")
        self.assertEqual((terms, keys), (["strasse", "5mw"], ["demo-72"]))
        self.assertEqual(measurements.query_terms(question), ["strasse", "5MW"])

    def test_structured_match_applies_the_entity_project_boost(self):
        memory.project_put(self.conn, {"id": "service", "name": "Service"})
        evidence = {"reference": "synthetic://trial", "measurements": [
            {"metric": "Bandwidth", "value": "57", "unit": "Mbps", "sample": "10 trials"}]}
        for identity in ("a", "z"):
            graph.put(self.conn, {"id": identity, "kind": "metric", "title": "Connectivity", "summary": "Trial",
                                  "sources": [evidence], "knowledge_state": "confirmed"}, "entity", memory)
        graph.put(self.conn, {"id": "scope", "kind": "informs", "from_ref": "entity:z", "to_ref": "project:service",
                              "description": "Local trial", "sources": [{"reference": "synthetic://scope"}],
                              "knowledge_state": "confirmed"}, "edge", memory)
        for word in ("connectivity", "bandwidth"):
            args = SimpleNamespace(query=word, project="service", limit=1, max_chars=4000)
            card, = task_brief.brief(self.conn, args, memory, self.db)["results"]
            self.assertEqual(card["ref"], "entity:z")
            self.assertIn("explicit_project_link", card["query_match_reasons"])
