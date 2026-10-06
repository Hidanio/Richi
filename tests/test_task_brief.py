"""Brief composition contracts on disposable Git repositories and SQLite only."""
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_git_impact as fixtures

from richi import compact
from richi import git_evidence
from richi import git_impact
from richi import graph
from richi import memory
from richi import recall
from richi import task_brief

from cli_environment import cli_environment


class TaskBriefTests(unittest.TestCase):
    # Reuse fixture construction, not its unrelated impact test methods.
    setUp = fixtures.GitImpactTests.setUp
    git = fixtures.GitImpactTests.git
    commit = fixtures.GitImpactTests.commit
    anchor = fixtures.GitImpactTests.anchor
    entry = fixtures.GitImpactTests.entry
    change = fixtures.GitImpactTests.change

    def args(self, **changes):
        values = dict(query="needleword", project=None, base=None, target=None,
                      worktree=False, repo=None, limit=8, max_chars=16000,
                      include_hypotheses=False, include_superseded=False)
        values.update(changes)
        return SimpleNamespace(**values)

    def brief(self, **changes):
        conn = memory.connect(self.db, readonly=True)
        try:
            with memory.transaction(conn, write=False):
                return task_brief.brief(conn, self.args(**changes), memory, self.db)
        finally:
            conn.close()

    def cli(self, *args, ok=True):
        result = subprocess.run([sys.executable, "-B", "-m", "richi",
                                 "--db", str(self.db), "brief", *args],
                                capture_output=True, text=True, timeout=30, env=cli_environment(self.db.parent))
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(result.stderr, "")
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def edge(self, identity, source, target, **changes):
        value = dict(id=identity, from_ref=source, to_ref=target, kind="informs",
                     description="Historical observation informs this task.",
                     sources=[{"reference": "https://example.invalid/evidence/" + identity}],
                     knowledge_state="confirmed", verified_at="2026-10-01")
        value.update(changes)
        return graph.put(self.conn, value, "edge", memory)

    def entity(self, identity, **changes):
        value = dict(id=identity, kind="concept", title="Meaning " + identity,
                     summary="A scoped definition with a separate contract.",
                     sources=[{"reference": "https://example.invalid/definition/" + identity}],
                     knowledge_state="confirmed", aliases=["Y"], verified_at="2026-10-01")
        value.update(changes)
        return graph.put(self.conn, value, "entity", memory)

    @staticmethod
    def by_ref(value):
        return {card["ref"]: card for card in value["results"]}

    def assert_budget(self, value, maximum):
        size = len(compact.rendered(value))
        self.assertLessEqual(size, maximum)
        self.assertEqual(value["budget"]["max_chars"], maximum)
        self.assertEqual(value["budget"]["output_chars"], size)

    def assert_connection_integrity(self, value):
        cards = self.by_ref(value)
        for card in cards.values():
            if "explicit_relation" in card["selected_by"]:
                self.assertTrue(card["connections"], card["ref"])
            for connection in card["connections"]:
                anchor = connection["anchor_ref"]
                self.assertIn(anchor, cards)
                self.assertEqual({connection["from_ref"], connection["to_ref"]},
                                 {card["ref"], anchor})
                self.assertTrue(connection["sources"])
                self.assertEqual(connection["origin"], "explicit")
                self.assertEqual(connection["read_more"],
                                 {"command": "edge get", "id": connection["ref"][5:]})
                family, identity = anchor.split(":", 1)
                self.assertEqual(connection["anchor_read_more"],
                                 {"command": family + " get", "id": identity})

    def test_query_only_preserves_recall_selection_and_never_calls_git(self):
        self.entry("seed", sources=[], aliases=["needleword"])
        self.entry("second", sources=[], title="needleword historical result")
        self.entry("neighbor", sources=[], title="Different topic")
        self.edge("connection", "entry:neighbor", "entry:seed")
        expected = recall.recall(self.conn, self.args(), memory)
        with patch.object(git_impact, "impact", side_effect=AssertionError("unexpected impact")), \
             patch.object(git_evidence, "_git", side_effect=AssertionError("unexpected Git")):
            actual = self.brief()
        self.assertEqual([c["ref"] for c in actual["results"]],
                         [c["ref"] for c in expected["results"]])
        for card in actual["results"]:
            original = next(c for c in expected["results"] if c["ref"] == card["ref"])
            self.assertEqual(card["query_match_reasons"], original["match_reasons"])
        self.assertFalse(actual["coverage"]["changes"]["requested"])
        self.assert_connection_integrity(actual)

    def test_exact_jira_query_does_not_loosen_guard_or_expand_membership(self):
        self.entry("task:DEMO-42", sources=[], jira_key="DEMO-42")
        self.entry("neighbor", sources=[])
        self.edge("related", "entry:neighbor", "entry:task:DEMO-42")
        actual = self.brief(query="DEMO-42", project="repo")
        self.assertEqual(set(self.by_ref(actual)), {"entry:task:DEMO-42"})
        unknown = self.brief(query="DEMO-424242", project="repo")
        self.assertTrue(unknown["no_match"])
        self.assertEqual(unknown["results"], [])

    def test_combined_deduplicates_and_preserves_exact_endpoint_source_index(self):
        source = self.anchor()
        self.entry("seed", sources=[{"reference": "https://example.invalid/report"}, source],
                   aliases=["needleword"], work_state="merged")
        self.change()
        actual = self.brief(project="repo", base=self.base, target="HEAD")
        self.assertEqual(len(actual["results"]), 1)
        card = actual["results"][0]
        self.assertEqual(card["selected_by"], ["query", "code_change"])
        self.assertEqual(card["work_state"], "merged")
        self.assertEqual(card["sources"][1], source)
        match = card["matches"][0]
        self.assertEqual(match["source_index"], 2)
        self.assertEqual(match["reference"], source["reference"])
        self.assertEqual(match["captured_commit"], self.base)
        self.assertEqual(match["changed_paths"], ["code.txt"])
        self.assertEqual(actual["comparison"]["base_commit"], self.base)
        self.assertEqual(actual["comparison"]["target_commit"], self.git("rev-parse", "HEAD").strip())
        self.assert_budget(actual, 16000)

    def test_code_seed_adds_sourced_one_hop_but_no_second_hop_or_membership(self):
        self.entry("seed")
        for identity in ("reverse", "forward", "second-hop", "member-only", "unbacked"):
            self.entry(identity, sources=[])
        self.edge("reverse-proof", "entry:reverse", "entry:seed")
        self.edge("forward-proof", "entry:seed", "entry:forward")
        self.edge("second-proof", "entry:forward", "entry:second-hop")
        self.edge("unbacked-proof", "entry:seed", "entry:unbacked", sources=[], knowledge_state="hypothesis")
        self.change()
        actual = self.brief(query="absentneedle", project="repo", base=self.base, include_hypotheses=True)
        self.assertEqual(set(self.by_ref(actual)), {"entry:seed", "entry:reverse", "entry:forward"})
        reverse = self.by_ref(actual)["entry:reverse"]
        self.assertEqual(reverse["selected_by"], ["explicit_relation"])
        self.assertEqual(reverse["connections"][0]["from_ref"], "entry:reverse")
        self.assertEqual(reverse["connections"][0]["to_ref"], "entry:seed")
        self.assertEqual(actual["coverage"]["relations"]["depth"], 1)
        self.assertEqual(actual["coverage"]["relations"]["code_seed_count"], 1)
        self.assert_connection_integrity(actual)

    def test_knowledge_state_flags_apply_to_query_and_code_channels(self):
        source = self.anchor()
        for state in ("confirmed", "hypothesis", "superseded"):
            self.entry(state, sources=[source], state=state, aliases=["needleword"])
        self.change()
        default = self.brief(project="repo", base=self.base)
        self.assertEqual(set(self.by_ref(default)), {"entry:confirmed"})
        expanded = self.brief(project="repo", base=self.base,
                              include_hypotheses=True, include_superseded=True)
        self.assertEqual(set(self.by_ref(expanded)), {"entry:confirmed", "entry:hypothesis", "entry:superseded"})
        for card in expanded["results"]:
            self.assertEqual(card["selected_by"], ["query", "code_change"])
            self.assertEqual(card["knowledge_state"], card["id"])
            if card["knowledge_state"] != "confirmed":
                self.assertTrue(card["state_note"])

    def test_edge_and_neighbor_states_both_control_relation_expansion(self):
        self.entry("seed")
        self.entry("hyp-node", sources=[], state="hypothesis")
        self.entry("hyp-edge", sources=[])
        self.edge("confirmed-link", "entry:seed", "entry:hyp-node")
        self.edge("hyp-link", "entry:seed", "entry:hyp-edge", knowledge_state="hypothesis")
        self.change()
        default = self.brief(query="absentneedle", project="repo", base=self.base)
        self.assertEqual(set(self.by_ref(default)), {"entry:seed"})
        expanded = self.brief(query="absentneedle", project="repo", base=self.base, include_hypotheses=True)
        self.assertEqual(set(self.by_ref(expanded)), {"entry:seed", "entry:hyp-node", "entry:hyp-edge"})
        self.assert_connection_integrity(expanded)

    def test_ambiguous_concepts_keep_unreturned_scopes_under_limit(self):
        memory.project_put(self.conn, {"id": "other", "name": "Other"})
        self.entity("first")
        self.entity("second")
        self.edge("first-scope", "entity:first", "project:repo", kind="used_in")
        self.edge("second-scope", "entity:second", "project:other", kind="used_in")
        actual = self.brief(query="Что такое Y?", project="repo", limit=1)
        self.assertEqual(actual["results"][0]["ref"], "entity:first")
        self.assertEqual(actual["results"][0]["project_ids"], ["repo"])
        self.assertTrue(actual["ambiguity"]["detected"])
        self.assertEqual(actual["ambiguity"]["concept_count"], 2)
        alternatives = {c["ref"]: c["project_ids"] for c in actual["ambiguity"]["candidates"]}
        self.assertEqual(alternatives, {"entity:first": ["repo"], "entity:second": ["other"]})

    def test_budget_omission_retains_ambiguity_and_is_not_no_match(self):
        huge = {"reference": "https://example.invalid/" + "long-evidence/" * 600}
        self.entity("first", sources=[huge])
        self.entity("second", sources=[huge])
        process = self.cli("Что такое Y?", "--max-chars", "4000")
        actual = json.loads(process.stdout)
        self.assert_budget(actual, 4000)
        self.assertEqual(len(process.stdout), actual["budget"]["output_chars"])
        self.assertFalse(actual["no_match"])
        self.assertTrue(actual["ambiguity"]["detected"])
        self.assertEqual(actual["ambiguity"]["concept_count"], 2)
        self.assertEqual(actual["results"], [])
        self.assertEqual(actual["budget"]["omitted_results"], 2)

    def test_budget_keeps_connections_atomic_or_omits_card_honestly(self):
        self.entry("seed", aliases=["needleword"])
        self.entry("context", sources=[], summary="Историческое наблюдение. Ограничение: не каузальный AB-тест.")
        self.edge("proof", "entry:context", "entry:seed", sources=[
            {"reference": "https://example.invalid/evidence/" + "segment/" * 150},
            {"reference": "https://example.invalid/another"}])
        self.change()
        for maximum in (4000, 8000, 16000):
            with self.subTest(max_chars=maximum):
                actual = self.brief(project="repo", base=self.base, max_chars=maximum)
                self.assert_budget(actual, maximum)
                self.assertFalse(actual["no_match"])
                self.assert_connection_integrity(actual)
                if "entry:context" not in self.by_ref(actual):
                    self.assertTrue(actual["truncated"])
                    self.assertGreater(actual["budget"]["omitted_results"] + actual["selection_omitted"], 0)

    def test_mixed_relation_and_code_origins_require_witness_for_each_retained_origin(self):
        self.entry("query-seed", sources=[], aliases=["needleword"])
        self.entry("code-and-related")
        self.edge("mixed-proof", "entry:query-seed", "entry:code-and-related")
        self.change()
        full = self.brief(project="repo", base=self.base, max_chars=100000)
        self.assertEqual(set(self.by_ref(full)["entry:code-and-related"]["selected_by"]),
                         {"explicit_relation", "code_change"})
        self.assert_connection_integrity(full)
        for maximum in (4000, 5500, 7000):
            with self.subTest(max_chars=maximum):
                actual = self.brief(project="repo", base=self.base, max_chars=maximum)
                self.assert_budget(actual, maximum)
                self.assertFalse(actual["no_match"])
                self.assert_connection_integrity(actual)
                card = self.by_ref(actual).get("entry:code-and-related")
                if card and "code_change" in card["selected_by"]:
                    self.assertTrue(card["matches"])
                if card and not card["connections"]:
                    self.assertNotIn("explicit_relation", card["selected_by"])
                    self.assertGreater(card["connections_omitted"], 0)

    def test_freed_budget_restores_related_qualification_before_leaving_unused_space(self):
        self.entry("seed", aliases=["needleword"])
        self.entry("incidental", title="needleword incidental mention", sources=[
            {"reference": "https://example.invalid/" + "large-source/" * 700}])
        summary = ("Historical before/after reported lower tail latency. " +
                   "The measured interval was a production window with observations captured before and after rollout; " * 3 +
                   "Limitation: these service-wide panels had no AB cohort split, so this is correlation, not a controlled causal estimate.")
        self.entry("context", sources=[], summary=summary)
        self.edge("qualification-proof", "entry:context", "entry:seed")
        self.change()
        actual = self.brief(project="repo", base=self.base, max_chars=8000)
        cards = self.by_ref(actual)
        self.assertEqual(set(cards), {"entry:seed", "entry:context"})
        self.assertEqual(cards["entry:context"]["summary_excerpt"], summary)
        self.assertFalse(cards["entry:context"]["is_excerpt"])
        self.assertIn("not a controlled causal estimate", cards["entry:context"]["summary_excerpt"])
        self.assert_connection_integrity(actual)
        self.assert_budget(actual, 8000)

    def test_unknown_code_coverage_is_not_false_no_match(self):
        self.entry("legacy", sources=[{"reference": "git:" + self.base}], projects=[])
        self.change()
        actual = self.brief(query="absentneedle", project="repo", base=self.base)
        self.assertEqual(actual["results"], [])
        self.assertFalse(actual["coverage_complete"])
        self.assertFalse(actual["coverage"]["changes"]["complete"])
        self.assertEqual(actual["coverage"]["changes"]["unscoped_legacy_sources"], 1)
        self.assertFalse(actual["no_match"])
        self.assertTrue(actual["truncated"])

    def test_current_status_warning_also_applies_to_code_and_related_cards(self):
        self.entry("seed", work_state="merged")
        self.entry("context", sources=[])
        self.edge("current-proof", "entry:context", "entry:seed")
        self.change()
        actual = self.brief(query="Что известно сейчас?", project="repo", base=self.base)
        self.assertEqual(set(self.by_ref(actual)), {"entry:seed", "entry:context"})
        for card in actual["results"]:
            self.assertTrue(card["needs_recheck"])
            self.assertTrue(card["recheck_reasons"])
        self.assertEqual(self.by_ref(actual)["entry:seed"]["work_state"], "merged")

    def test_nested_connection_description_omission_sets_top_level_truncated(self):
        self.entry("seed")
        self.entry("context", sources=[])
        self.edge("long-proof", "entry:context", "entry:seed", description="Historical evidence. " * 50)
        self.change()
        actual = self.brief(query="absentneedle", project="repo", base=self.base, max_chars=100000)
        connection = self.by_ref(actual)["entry:context"]["connections"][0]
        self.assertTrue(connection["description_is_excerpt"])
        self.assertTrue(actual["truncated"])
        self.assertEqual(actual["budget"]["omitted_results"], 0)
        self.assertEqual(actual["selection_omitted"], 0)

    def test_nested_connection_source_omission_sets_top_level_truncated(self):
        self.entry("seed")
        self.entry("context", sources=[])
        self.edge("many-proof", "entry:context", "entry:seed", sources=[
            {"reference": "https://example.invalid/proof/" + str(i)} for i in range(3)])
        self.change()
        actual = self.brief(query="absentneedle", project="repo", base=self.base, max_chars=100000)
        connection = self.by_ref(actual)["entry:context"]["connections"][0]
        self.assertEqual(connection["sources_omitted"], 1)
        self.assertTrue(actual["truncated"])
        self.assertEqual(actual["budget"]["omitted_results"], 0)
        self.assertEqual(actual["selection_omitted"], 0)

    def test_nested_code_path_omission_sets_top_level_truncated(self):
        self.entry("seed", sources=[{"reference": "git:" + self.base}])
        for name in ("code.txt", "delete.txt", "old.txt"):
            (self.repo / name).write_text("Changed historical input\n")
        self.commit("Change all historical files")
        with patch.object(git_impact, "MAX_MATCH_PATH_CHARS", 1):
            actual = self.brief(query="absentneedle", project="repo", base=self.base, max_chars=100000)
        match = self.by_ref(actual)["entry:seed"]["matches"][0]
        self.assertEqual(match["changed_paths_omitted"], 2)
        self.assertTrue(actual["truncated"])
        self.assertEqual(actual["budget"]["omitted_results"], 0)
        self.assertEqual(actual["selection_omitted"], 0)

    def test_invalid_flag_combinations_fail_before_git(self):
        for changes in (dict(target="HEAD"), dict(worktree=True), dict(repo=str(self.repo)), dict(base=self.base)):
            with self.subTest(changes=changes), \
                 patch.object(git_evidence, "_git", side_effect=AssertionError("unexpected Git")):
                with self.assertRaises(memory.MemoryError):
                    self.brief(**changes)
        process = self.cli("needleword", "--project", "repo", "--base", self.base,
                           "--target", "HEAD", "--worktree", ok=False)
        self.assertIn("not allowed", process.stderr)
        for flags in (("--max-chars", "3999"), ("--limit", "51")):
            self.cli("needleword", *flags, ok=False)

    def test_cli_and_api_do_not_change_database_git_index_or_files(self):
        self.entry("seed", aliases=["needleword"])
        self.change()
        before = list(self.conn.iterdump())
        index_before = (self.repo / ".git" / "index").read_bytes()
        files_before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        result = self.cli("needleword", "--project", "repo", "--base", self.base,
                          "--target", "HEAD", "--max-chars", "16000")
        self.assert_budget(json.loads(result.stdout), 16000)
        self.brief(project="repo", base=self.base)
        self.assertEqual(list(self.conn.iterdump()), before)
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), index_before)
        self.assertEqual(sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*")), files_before)


if __name__ == "__main__":
    unittest.main()
