"""Recall/compact-read contracts against disposable schema-v2 databases only.

These are CLI integration tests, independent of retrieval/scoring internals.
They deliberately measure the *serialized stdout*, including its final newline.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest




class RecallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="project-memory-recall-test-")
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "isolated knowledge.sqlite3"
        self.cli("init")
        self.put("project", [
            {"id": identity, "name": identity.title(), "source": "test fixture"}
            for identity in ("alpha", "beta", "gamma")
        ], action="upsert")

    def process(self, *args, payload=None, ok=True):
        result = subprocess.run(
            [sys.executable, "-m", "richi", "--db", str(self.db), *args],
            input=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        )
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(result.stderr, "", result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def cli(self, *args, payload=None, ok=True):
        result = self.process(*args, payload=payload, ok=ok)
        return json.loads(result.stdout if ok else result.stderr)

    def put(self, kind, payload, action="put"):
        return self.cli(kind, action, "--json", "-", payload=payload)

    def entry(self, identity, **overrides):
        value = {
            "id": identity,
            "kind": "note",
            "title": "Запись " + identity,
            "summary": "Наблюдение, сохранённое для отдельного исследования.",
            "project_ids": ["alpha"],
            "knowledge_state": "confirmed",
            "work_state": "done",
            "sources": [{"reference": "https://example.test/evidence/" + identity}],
            "tags": [],
            "aliases": [],
            "verified_at": "2026-09-30",
        }
        value.update(overrides)
        return value

    def entity(self, identity, **overrides):
        value = {
            "id": identity,
            "kind": "concept",
            "title": "Понятие " + identity,
            "summary": "Самостоятельное понятие из рабочего контекста.",
            "knowledge_state": "confirmed",
            "sources": [{"reference": "https://example.test/glossary/" + identity}],
            "tags": [],
            "aliases": [],
            "verified_at": "2026-09-30",
        }
        value.update(overrides)
        return value

    def edge(self, identity, source, target, **overrides):
        value = {
            "id": identity,
            "from_ref": source,
            "to_ref": target,
            "kind": "informs",
            "description": "Проверенный результат учитывается в следующем решении.",
            "knowledge_state": "confirmed",
            "sources": [{"reference": "https://example.test/decisions/" + identity}],
            "verified_at": "2026-09-30",
        }
        value.update(overrides)
        return value

    def recall(self, query, *flags):
        value = self.cli("recall", query, *flags)
        self.assertIsInstance(value["results"], list)
        self.assertIsInstance(value["edges"], list)
        self.assertIsInstance(value["no_match"], bool)
        self.assertIsInstance(value["candidate_count"], int)
        self.assertGreaterEqual(value["candidate_count"], len(value["results"]))
        self.assertIn("budget", value)
        self.assertIn("truncated", value)
        return value

    @staticmethod
    def ids(value):
        return {item["id"] for item in value["results"]}

    def test_exact_jira_key_does_not_match_sibling_or_prefix_ticket(self):
        self.put("entry", [
            self.entry("ticket-main", kind="task", jira_key="DEMO-42", title="DEMO-42: обновить ранжирование"),
            self.entry("ticket-sibling", kind="task", jira_key="DEMO-43", title="DEMO-43: обновить ранжирование"),
            self.entry("ticket-prefix", kind="task", jira_key="DEMO-420", title="DEMO-420: обновить ранжирование"),
        ])
        result = self.recall("DEMO-42")
        self.assertFalse(result["no_match"])
        self.assertEqual(result["results"][0]["id"], "ticket-main")
        self.assertNotIn("ticket-sibling", self.ids(result))
        self.assertNotIn("ticket-prefix", self.ids(result))
        card = result["results"][0]
        self.assertEqual(card["ref"], "entry:ticket-main")
        self.assertTrue(card["match_reasons"])
        self.assertEqual(card["read_more"], {"command": "entry get", "id": "ticket-main"})

    def test_native_ids_and_qualified_refs_resolve_without_textual_matches(self):
        entry = self.entry(
            "experiment:pool-detour-89", title="Результат измерения",
            summary="Сохранено проверенное наблюдение о времени ответа.",
        )
        entity = self.entity(
            "concept:semantic-axis-73", title="Отдельное понятие",
            summary="Определение и пример использования.",
        )
        self.put("entry", entry)
        self.put("entity", entity)
        for family, record in (("entry", entry), ("entity", entity)):
            qualified = family + ":" + record["id"]
            for query in (record["id"], qualified):
                with self.subTest(query=query):
                    result = self.recall(query)
                    self.assertFalse(result["no_match"])
                    self.assertEqual(result["results"][0]["id"], record["id"])
                    self.assertEqual(result["results"][0]["ref"], qualified)

    def test_cyrillic_aliases_are_searchable_case_insensitively(self):
        record = self.entry("alias-case", title="Experiment QXZ", summary="Pool measurements.",
                            aliases=["Северный контур"], tags=["внешнийисточник"])
        self.put("entry", record)
        for query in ("СЕВЕРНЫЙ КОНТУР", "северный контур", "внешнийисточник"):
            with self.subTest(query=query):
                result = self.recall(query)
                self.assertIn(record["id"], self.ids(result))
                self.assertFalse(result["no_match"])

    def test_jira_compact_spelling_keeps_related_decisions_without_prefix_noise(self):
        self.put("entry", [
            self.entry("task:DEMO-42", kind="task", jira_key="DEMO-42", title="DEMO-42: первое ускорение"),
            self.entry("decision:next-step", kind="decision", title="Следующее ускорение после DEMO42",
                       summary="После DEMO42 выбран следующий шаг проверки кеша."),
            self.entry("wrong-number", title="Следующее ускорение после DEMO420"),
        ])
        result = self.recall("Что следующим ускорять после DEMO-42?")
        self.assertIn("decision:next-step", self.ids(result))
        self.assertNotIn("wrong-number", self.ids(result))

    def test_project_scope_prioritizes_direct_text_matches(self):
        self.put("entry", [
            self.entry("alpha-hit", aliases=["localprojectneedle"]),
            self.entry("beta-hit", aliases=["localprojectneedle"], project_ids=["beta"]),
        ])
        result = self.recall("localprojectneedle", "--project", "alpha")
        self.assertEqual(result["results"][0]["id"], "alpha-hit")
        beta = self.recall("localprojectneedle", "--project", "beta")
        self.assertEqual(beta["results"][0]["id"], "beta-hit")

    def test_one_explicit_hop_can_cross_project_without_membership_fanout(self):
        self.put("entry", [
            self.entry("seed", aliases=["uniqueseedneedle"]),
            self.entry("cross-project", project_ids=["beta"]),
            self.entry("second-hop", project_ids=["gamma"]),
            self.entry("same-project-bystander"),
            self.entry("neighbor-project-bystander", project_ids=["beta"]),
        ])
        first = self.edge("seed-cross", "entry:seed", "entry:cross-project")
        self.put("edge", [first, self.edge("cross-distant", "entry:cross-project", "entry:second-hop")])
        result = self.recall("uniqueseedneedle", "--project", "alpha")
        self.assertEqual(self.ids(result), {"seed", "cross-project"})
        self.assertEqual(result["results"][0]["id"], "seed")
        self.assertTrue(next(card for card in result["results"] if card["id"] == "cross-project")["match_reasons"])
        selected = [edge for edge in result["edges"]
                    if edge["from_ref"] == "entry:seed" and edge["to_ref"] == "entry:cross-project"]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["sources"], first["sources"])
        self.assertEqual(selected[0]["knowledge_state"], "confirmed")
        refs = {card["ref"] for card in result["results"]}
        for edge in result["edges"]:
            self.assertIn(edge["from_ref"], refs)
            self.assertIn(edge["to_ref"], refs)

    def test_explicit_link_is_followed_in_both_directions(self):
        self.put("entry", [self.entry("left"), self.entry("right", aliases=["reversehopneedle"])])
        self.put("edge", self.edge("left-right", "entry:left", "entry:right"))
        self.assertEqual(self.ids(self.recall("reversehopneedle")), {"left", "right"})

    def test_entities_are_searchable_and_keep_native_id_and_qualified_ref(self):
        entity = self.entity("concept:ablation", title="Контрольное отключение", aliases=["абляционныйанализ"])
        self.put("entity", entity)
        result = self.recall("абляционныйанализ")
        self.assertIn(entity["id"], self.ids(result))
        card = next(item for item in result["results"] if item["id"] == entity["id"])
        self.assertEqual(card["ref"], "entity:concept:ablation")
        self.assertEqual(card["knowledge_state"], "confirmed")
        self.assertEqual(card["sources"], entity["sources"])
        self.assertEqual(card["verified_at"], entity["verified_at"])
        self.assertTrue(card["match_reasons"])

    def test_entities_can_be_recalled_through_an_explicit_entry_link(self):
        self.put("entry", self.entry("entity-seed", aliases=["entitylinkneedle"]))
        self.put("entity", self.entity("concept:neighbor"))
        self.put("edge", self.edge("to-concept", "entry:entity-seed", "entity:concept:neighbor"))
        self.assertEqual(self.ids(self.recall("entitylinkneedle")), {"entity-seed", "concept:neighbor"})

    def test_knowledge_states_require_separate_opt_in(self):
        self.put("entry", [
            self.entry("current", aliases=["knowledgestateneedle"]),
            self.entry("guess", aliases=["knowledgestateneedle"], knowledge_state="hypothesis"),
            self.entry("old", aliases=["knowledgestateneedle"], knowledge_state="superseded"),
        ])
        cases = [
            ((), {"current"}),
            (("--include-hypotheses",), {"current", "guess"}),
            (("--include-superseded",), {"current", "old"}),
            (("--include-hypotheses", "--include-superseded"), {"current", "guess", "old"}),
        ]
        for flags, expected in cases:
            with self.subTest(flags=flags):
                self.assertEqual(self.ids(self.recall("knowledgestateneedle", *flags)), expected)

    def test_hypothesis_edge_is_not_used_by_default(self):
        self.put("entry", [self.entry("edge-seed", aliases=["edgehypothesisneedle"]), self.entry("edge-neighbor")])
        self.put("edge", self.edge("guessed-link", "entry:edge-seed", "entry:edge-neighbor", knowledge_state="hypothesis"))
        self.assertEqual(self.ids(self.recall("edgehypothesisneedle")), {"edge-seed"})
        opted_in = self.recall("edgehypothesisneedle", "--include-hypotheses")
        self.assertEqual(self.ids(opted_in), {"edge-seed", "edge-neighbor"})
        self.assertEqual(opted_in["edges"][0]["knowledge_state"], "hypothesis")

    def test_query_punctuation_cannot_execute_sql_or_fts_operators(self):
        original = self.entry("safe-record", aliases=["integrityneedle"])
        self.put("entry", original)
        for query in ('" OR *', "'); DROP TABLE entries; --", "redis NEAR(foo", "a:b+(c)", "* : \" ( )"):
            with self.subTest(query=query):
                self.recall(query)
        stored = self.cli("entry", "get", original["id"])
        self.assertEqual(stored["summary"], original["summary"])
        self.assertEqual(self.cli("check")["status"], "ok")

    def test_no_match_is_distinct_from_a_truncated_result(self):
        self.put("entry", self.entry("ordinary-record"))
        result = self.recall("zzzxunfindablewordxzzz")
        self.assertEqual(result["results"], [])
        self.assertEqual(result["edges"], [])
        self.assertTrue(result["no_match"])
        self.assertEqual(result["candidate_count"], 0)

    def test_unknown_explicit_jira_key_does_not_fall_back_to_generic_words(self):
        self.put("entry", self.entry(
            "known-ticket", kind="task", jira_key="DEMO-42",
            title="Что сделали в задаче DEMO-42",
            summary="В этой задаче мы сделали повторное использование соединений Redis.",
        ))
        result = self.recall("Что сделали в этой задаче DEMO-999999?")
        self.assertTrue(result["no_match"])
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["results"], [])
        self.assertEqual(result["edges"], [])

    def test_unmatched_technical_subject_does_not_match_only_generic_verbs(self):
        self.put("entry", self.entry(
            "redis-pool-attempt", kind="experiment", title="Пул соединений Redis",
            summary="Мы уже пробовали повторно использовать соединения Redis. Эксперимент завершён.",
        ))
        result = self.recall("Мы уже пробовали QUIC?")
        self.assertTrue(result["no_match"])
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["results"], [])
        self.assertEqual(result["edges"], [])

    def test_default_result_limit_is_eight_and_explicit_limit_is_respected(self):
        self.put("entry", [self.entry("limit-%02d" % i, aliases=["resultlimitneedle"]) for i in range(12)])
        default = self.recall("resultlimitneedle")
        self.assertEqual(len(default["results"]), 8)
        self.assertGreaterEqual(default["candidate_count"], 12)
        self.assertEqual(len(self.recall("resultlimitneedle", "--limit", "3")["results"]), 3)

    def test_max_chars_bounds_the_entire_unicode_json_stdout(self):
        self.put("entry", [
            self.entry("budget-%02d" % i, aliases=["serializationbudgetneedle"],
                       summary=("Подробный русский текст измерения, включая отклонённые варианты.\n" * 160)
                       + "Оговорка: выкладка в production не подтверждена.")
            for i in range(10)
        ])
        for maximum in (2000, 2001, 4096, 16000):
            with self.subTest(maximum=maximum):
                proc = self.process("recall", "serializationbudgetneedle", "--max-chars", str(maximum))
                self.assertLessEqual(len(proc.stdout), maximum, "Budget must include indentation and trailing newline")
                self.assertTrue(proc.stdout.endswith("\n"))
                value = json.loads(proc.stdout)
                self.assertFalse(value["no_match"])
                self.assertGreater(value["candidate_count"], 0)
                self.assertIn("budget", value)
                self.assertTrue(value["truncated"])
        self.cli("recall", "serializationbudgetneedle", "--max-chars", "1999", ok=False)

    def test_giant_summary_is_not_presented_as_complete_when_last_caveat_is_cut(self):
        summary = "Краткий положительный результат.\n" + ("Промежуточный замер и подробности.\n" * 800)
        summary += "КРИТИЧЕСКАЯ ОГОВОРКА: изменение откатили; считать эксперимент успешным нельзя."
        record = self.entry("critical-caveat", aliases=["lastcaveatneedle"], summary=summary)
        self.put("entry", record)
        response = self.recall("lastcaveatneedle", "--max-chars", "2000")
        self.assertTrue(response["results"], "A normal-source card should fit in the minimum budget")
        card = response["results"][0]
        self.assertNotEqual(card["summary_excerpt"], summary)
        self.assertTrue(card["is_excerpt"])
        self.assertTrue(card["excerpt_note"])
        self.assertEqual(card["read_more"], {"command": "entry get", "id": record["id"]})
        self.assertEqual(self.cli("entry", "get", record["id"])["summary"], summary)

    def test_sources_are_whole_objects_with_explicit_omission_metadata(self):
        sources = [
            {"reference": "https://example.test/reports/%d" % i,
             "label": "Отчёт %d" % i, "observed_at": "2026-09-30", "type": "measurement"}
            for i in range(4)
        ]
        self.put("entry", self.entry("many-sources", aliases=["provenanceneedle"], sources=sources))
        card = self.recall("provenanceneedle")["results"][0]
        self.assertGreater(len(card["sources"]), 0)
        self.assertLessEqual(len(card["sources"]), 2)
        for source in card["sources"]:
            self.assertIn(source, sources)
        self.assertEqual(card["source_count"], 4)
        self.assertTrue(card["sources_omitted"])

    def test_unfit_provenance_keeps_candidate_count_and_does_not_claim_no_match(self):
        source = {"reference": "https://example.test/" + ("long-source-segment/" * 400)}
        self.put("entry", self.entry("oversize-provenance", aliases=["unfitprovenanceneedle"], sources=[source]))
        proc = self.process("recall", "unfitprovenanceneedle", "--max-chars", "2000")
        self.assertLessEqual(len(proc.stdout), 2000)
        value = json.loads(proc.stdout)
        self.assertFalse(value["no_match"])
        self.assertGreater(value["candidate_count"], 0)
        self.assertTrue(value["truncated"])
        self.assertEqual(value["results"], [], "Do not silently cut the sole provenance URL to fit a card")

    def test_current_release_question_requires_rechecking_live_status(self):
        self.put("entry", self.entry("release-question", kind="task", jira_key="DEMO-42",
                                     title="DEMO-42: реализация нового ранжирования",
                                     summary="Реализация завершена. Текущий статус выкладки надо проверить отдельно.",
                                     work_state="implemented", verified_at="2020-01-01"))
        result = self.recall("DEMO-42 уже выкатили в прод сейчас?")
        card = next(item for item in result["results"] if item["id"] == "release-question")
        self.assertTrue(card["needs_recheck"])
        self.assertTrue(card["recheck_reasons"])
        self.assertEqual(card["work_state"], "implemented")
        self.assertEqual(card["verified_at"], "2020-01-01")

    def test_entry_get_omits_history_by_default_but_explicit_history_preserves_it(self):
        first = self.entry("history-record", summary="Первоначальный вывод.")
        self.put("entry", first)
        self.put("entry", dict(first, summary="Пересмотренный вывод после новой проверки."))
        plain = self.cli("entry", "get", first["id"])
        self.assertNotIn("history", plain)
        self.assertEqual(plain["summary"], "Пересмотренный вывод после новой проверки.")
        with_history = self.cli("entry", "get", first["id"], "--history")
        self.assertEqual(with_history["summary"], plain["summary"])
        self.assertEqual(len(with_history["history"]), 2)
        self.assertEqual(with_history["history"][0]["after"]["summary"], first["summary"])
        self.assertEqual(with_history["history"][1]["before"]["summary"], first["summary"])

    def test_compact_entry_read_has_explicit_excerpt_and_bounded_stdout(self):
        summary = "Сведения о проверке.\n" * 800 + "Финальная оговорка: результат нельзя обобщать на весь трафик."
        record = self.entry("compact-read", summary=summary)
        self.put("entry", record)
        proc = self.process("entry", "read", record["id"], "--max-chars", "2000")
        self.assertLessEqual(len(proc.stdout), 2000)
        value = json.loads(proc.stdout)
        self.assertTrue(value["truncated"])
        self.assertIn("budget", value)
        card = value["entry"]
        self.assertEqual(card["id"], record["id"])
        self.assertEqual(card["ref"], "entry:" + record["id"])
        self.assertTrue(card["is_excerpt"])
        self.assertTrue(card["excerpt_note"])
        self.assertNotEqual(card["summary_excerpt"], summary)
        self.assertEqual(card["read_more"], {"command": "entry get", "id": record["id"]})
        self.assertEqual(card["sources"], record["sources"])
        self.assertNotIn("history", card)
        self.assertNotIn("summary", card)

    def test_compact_short_entry_is_complete_and_still_has_full_read_pointer(self):
        record = self.entry("short-read", summary="Короткий проверенный вывод.")
        self.put("entry", record)
        value = self.cli("entry", "read", record["id"], "--max-chars", "2000")
        card = value["entry"]
        self.assertEqual(card["summary_excerpt"], record["summary"])
        self.assertFalse(card["is_excerpt"])
        self.assertEqual(card["read_more"], {"command": "entry get", "id": record["id"]})
        self.assertEqual(card["knowledge_state"], "confirmed")


if __name__ == "__main__":
    unittest.main()
