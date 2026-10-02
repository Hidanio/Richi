"""Scoped concept recall contracts; every write is to a disposable database."""
import json
import unittest

import test_recall as fixtures


class ConceptTests(unittest.TestCase):
    setUp = fixtures.RecallTests.setUp
    process = fixtures.RecallTests.process
    cli = fixtures.RecallTests.cli
    put = fixtures.RecallTests.put
    entry = fixtures.RecallTests.entry
    entity = fixtures.RecallTests.entity
    edge = fixtures.RecallTests.edge
    recall = fixtures.RecallTests.recall
    ids = staticmethod(fixtures.RecallTests.ids)

    def concepts(self):
        self.put("entity", [
            self.entity("concept:y-feed", title="Y — feed marker in alpha", aliases=["Y"],
                        summary="A marker for continuation of the main feed, not a unique key."),
            self.entity("concept:y-label", title="Y — training label in beta", aliases=["Y"],
                        summary="The supervised model target in this separate project."),
            self.entity("concept:yandex", title="Yandex integration", aliases=["Yandex"]),
        ])
        self.put("edge", [
            self.edge("feed-scope", "entity:concept:y-feed", "project:alpha", kind="used_in"),
            self.edge("label-scope", "entity:concept:y-label", "project:beta", kind="used_in"),
        ])

    def test_short_alias_and_definition_questions_keep_distinct_meanings(self):
        self.concepts()
        self.put("entry", self.entry("noise", title="Yandex and yesterday", summary="An ordinary y occurs here."))
        for query in ("Y", "y", "Что такое Y?", "WHAT is Y?"):
            with self.subTest(query=query):
                value = self.recall(query)
                self.assertEqual(self.ids(value), {"concept:y-feed", "concept:y-label"})
                self.assertTrue(value["ambiguity"]["detected"])
                self.assertEqual(value["ambiguity"]["concept_count"], 2)
                self.assertEqual(value["ambiguity"]["matched_aliases"], ["y"])
                for card in value["results"]:
                    self.assertEqual(card["knowledge_state"], "confirmed")
                    self.assertTrue(card["sources"])

    def test_unknown_symbol_and_unknown_technical_qualifier_abstain(self):
        self.concepts()
        for query in ("Z", "Что такое Z?", "Что такое Y QUIC?", "Y в UNKNOWN_COMPONENT"):
            with self.subTest(query=query):
                value = self.recall(query)
                self.assertTrue(value["no_match"])
                self.assertEqual(value["results"], [])

    def test_symbol_in_anchored_question_recalls_legacy_facts_without_alias(self):
        self.put("entry", [
            self.entry("legacy-y", title="LPOP main feed semantics", aliases=[],
                       summary="Y marks the delivered page. After timeout, repeating LPOP may pop another page."),
            self.entry("prefix-noise", title="LPOP main feed semantics", aliases=[],
                       summary="Yandex is a separate integration. LPOP after timeout needs review."),
        ])
        value = self.recall("Можно ли безопасно повторять LPOP страницы main feed после timeout и что означает Y?")
        self.assertIn("legacy-y", self.ids(value))
        self.assertNotIn("prefix-noise", self.ids(value))
        for query in ("Y", "Что такое Y?"):
            value = self.recall(query)
            self.assertTrue(value["no_match"])
            self.assertEqual(value["results"], [])

    def test_qualified_question_and_project_boost_do_not_merge_meanings(self):
        self.concepts()
        value = self.recall("Что такое Y для alpha?")
        self.assertEqual(value["results"][0]["id"], "concept:y-feed")
        self.assertEqual(self.ids(value), {"concept:y-feed", "concept:y-label"})
        for project, expected in (("alpha", "concept:y-feed"), ("beta", "concept:y-label")):
            value = self.recall("Что такое Y?", "--project", project)
            self.assertEqual(value["results"][0]["id"], expected)
            self.assertIn("confirmed_concept_scope", value["results"][0]["match_reasons"])
            self.assertEqual(value["results"][0]["project_ids"], [project])
            self.assertTrue(value["ambiguity"]["detected"])

    def test_only_direct_confirmed_outgoing_used_in_edges_define_scope(self):
        self.concepts()
        self.put("entity", self.entity("concept:a-wrong-scope", aliases=["Y"]))
        self.put("edge", [
            self.edge("wrong-kind", "entity:concept:a-wrong-scope", "project:alpha", kind="related_to"),
            self.edge("wrong-direction", "project:beta", "entity:concept:a-wrong-scope", kind="used_in"),
            self.edge("uncertain-scope", "entity:concept:a-wrong-scope", "project:gamma",
                      kind="used_in", knowledge_state="hypothesis"),
            self.edge("neighbor", "entity:concept:y-feed", "entity:concept:a-wrong-scope", kind="related_to"),
        ])
        value = self.recall("Y", "--project", "alpha", "--include-hypotheses")
        self.assertEqual(value["results"][0]["id"], "concept:y-feed")
        wrong = next(card for card in value["results"] if card["id"] == "concept:a-wrong-scope")
        self.assertEqual(wrong["project_ids"], [])
        self.assertNotIn("confirmed_concept_scope", wrong["match_reasons"])
        self.assertNotIn("explicit_project_link", wrong["match_reasons"])

    def test_concept_can_have_multiple_explicit_project_scopes(self):
        self.concepts()
        self.put("edge", self.edge("also-used", "entity:concept:y-feed", "project:gamma", kind="used_in"))
        value = self.recall("Y", "--project", "gamma")
        self.assertEqual(value["results"][0]["id"], "concept:y-feed")
        self.assertEqual(value["results"][0]["project_ids"], ["alpha", "gamma"])
        candidate = next(c for c in value["ambiguity"]["candidates"] if c["ref"] == "entity:concept:y-feed")
        self.assertEqual(candidate["project_ids"], ["alpha", "gamma"])

    def test_graph_recalled_concept_keeps_its_direct_scope(self):
        self.concepts()
        self.put("entry", self.entry("seed", aliases=["uniquescopeseed"]))
        self.put("edge", self.edge("seed-concept", "entry:seed", "entity:concept:y-label", kind="explains"))
        value = self.recall("uniquescopeseed", "--project", "alpha")
        card = next(c for c in value["results"] if c["id"] == "concept:y-label")
        self.assertEqual(card["project_ids"], ["beta"])

    def test_numeric_query_parameter_is_not_treated_as_a_short_alias(self):
        self.put("entry", self.entry("parameter", title="ANN batch size measurements", aliases=["ANN"]))
        value = self.recall("ANN batch size 1")
        self.assertIn("parameter", self.ids(value))

    def test_hypotheses_and_historical_variants_require_opt_in(self):
        self.concepts()
        self.put("entity", [
            self.entity("concept:y-old", aliases=["Y"], knowledge_state="superseded"),
            self.entity("concept:y-idea", aliases=["Y"], knowledge_state="hypothesis"),
        ])
        self.put("edge", self.edge("old-meaning", "entity:concept:y-feed", "entity:concept:y-old", kind="supersedes"))
        default = self.recall("Y")
        self.assertEqual(default["ambiguity"]["concept_count"], 2)
        self.assertNotIn("concept:y-old", self.ids(default))
        expanded = self.recall("Y", "--include-hypotheses", "--include-superseded")
        self.assertEqual(expanded["ambiguity"]["concept_count"], 4)
        states = {card["id"]: card["knowledge_state"] for card in expanded["results"]}
        self.assertEqual(states["concept:y-old"], "superseded")
        self.assertEqual(states["concept:y-idea"], "hypothesis")
        states = {card["ref"]: card["knowledge_state"] for card in expanded["ambiguity"]["candidates"]}
        self.assertEqual(states["entity:concept:y-old"], "superseded")

    def test_limit_one_still_reports_unreturned_alternative(self):
        self.concepts()
        value = self.recall("Y", "--project", "alpha", "--limit", "1")
        self.assertEqual(len(value["results"]), 1)
        self.assertTrue(value["ambiguity"]["detected"])
        self.assertEqual(value["ambiguity"]["concept_count"], 2)
        self.assertEqual({c["ref"] for c in value["ambiguity"]["candidates"]},
                         {"entity:concept:y-feed", "entity:concept:y-label"})
        self.assertEqual(value["budget"]["omitted_results"], 1)

    def test_ambiguity_survives_tiny_budget_and_unfit_evidence(self):
        long_source = {"reference": "https://example.test/" + "source-segment/" * 600}
        self.put("entity", [self.entity("concept:y-" + str(i), aliases=["Y"], sources=[long_source])
                            for i in range(3)])
        process = self.process("recall", "Что такое Y?", "--max-chars", "2000")
        value = json.loads(process.stdout)
        self.assertLessEqual(len(process.stdout), 2000)
        self.assertEqual(value["budget"]["output_chars"], len(process.stdout))
        self.assertFalse(value["no_match"])
        self.assertTrue(value["ambiguity"]["detected"])
        self.assertEqual(value["ambiguity"]["concept_count"], 3)
        self.assertEqual(value["ambiguity"]["omitted_candidates"], 3)
        self.assertEqual(value["results"], [])

    def test_many_meanings_keep_bounded_honest_ambiguity_counts(self):
        self.put("entity", [self.entity("concept:y-" + str(i), aliases=["Y"],
                            knowledge_state="hypothesis" if i % 2 else "confirmed") for i in range(20)])
        for budget in (2000, 4000, 16000):
            process = self.process("recall", "Y", "--include-hypotheses", "--max-chars", str(budget))
            value = json.loads(process.stdout)
            self.assertLessEqual(len(process.stdout), budget)
            self.assertEqual(value["budget"]["output_chars"], len(process.stdout))
            ambiguity = value["ambiguity"]
            self.assertEqual(ambiguity["concept_count"], 20)
            self.assertEqual(ambiguity["omitted_candidates"], 20 - len(ambiguity["candidates"]))
            self.assertTrue(value["truncated"])
            for candidate in ambiguity["candidates"]:
                number = int(candidate["ref"].rsplit("-", 1)[1])
                self.assertEqual(candidate["knowledge_state"], "hypothesis" if number % 2 else "confirmed")

    def test_long_alias_uses_whole_tokens_and_native_ids_still_resolve(self):
        self.put("entity", [
            self.entity("concept:bit-vector", title="BIT-vector for alpha", aliases=["BIT-vector"]),
            self.entity("concept:bitset", title="BITSET for beta", aliases=["BITSET"]),
        ])
        value = self.recall("Что такое BIT-vector?")
        self.assertEqual(self.ids(value), {"concept:bit-vector"})
        self.assertNotIn("ambiguity", value)
        for query in ("concept:bit-vector", "entity:concept:bit-vector"):
            self.assertEqual(self.recall(query)["results"][0]["id"], "concept:bit-vector")


if __name__ == "__main__":
    unittest.main()
