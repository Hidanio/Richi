# -*- coding: utf-8 -*-
"""A manual, resumable card-maintenance example using the existing CLI only.

Run ``python tests/test_card_maintenance.py --report`` for a disposable-store
measurement. This is fixture-specific test orchestration, not a split command.
No personal store, workspace, network, source-file or Git reads are involved.
"""
import copy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from cli_environment import cli_environment


FIXTURE = Path(__file__).parent / "fixtures" / "card_maintenance.json"


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def original_parent(fixture):
    """Reproduce accumulated notes without inventing additional observations."""
    parent = copy.deepcopy(fixture["parent_after"])
    recipe = fixture["before_recipe"]
    text = recipe["preamble"] + parent["summary"]
    text += "\n\n" + "\n\n".join(child["summary"] for child in fixture["children"])
    for number in range(1, recipe["editorial_copy_count"] + 1):
        for block, child in enumerate(fixture["children"], 1):
            text += recipe["note_prefix"].format(copy=number, block=block)
            text += child["summary"]
    parent["summary"] = text
    return parent


def child_edge(child, parent_id):
    return {
        "id": "part-of:" + child["id"], "from_ref": "entry:" + child["id"],
        "to_ref": "entry:" + parent_id, "kind": "part_of",
        "description": "Source-backed detail belongs to DEMO-84; membership does not confirm its hypothesis.",
        "knowledge_state": "confirmed", "sources": copy.deepcopy(child["sources"]),
        "verified_at": child["sources"][0]["observed_at"],
    }


class CardMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="richi-card-maintenance-")
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "synthetic.sqlite3"
        self.fixture = load_fixture()
        self.before = original_parent(self.fixture)
        self.parent_id = self.before["id"]
        self.cli("init")
        self.put("project", self.fixture["projects"], action="upsert")
        self.put("entry", dict(self.before, expected_updated_at=None))
        self.version = self.get(self.parent_id)["updated_at"]

    def process(self, *args, payload=None, ok=True):
        proc = subprocess.run(
            [sys.executable, "-m", "richi", "--db", str(self.db), *args],
            input=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
            capture_output=True, text=True, encoding="utf-8", timeout=20,
            env=cli_environment(self.db.parent, PYTHONDONTWRITEBYTECODE="1"),
        )
        if ok is True:
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertEqual(proc.stderr, "")
        elif ok is False:
            self.assertNotEqual(proc.returncode, 0, proc.stdout)
        return proc

    def cli(self, *args, payload=None, ok=True):
        proc = self.process(*args, payload=payload, ok=ok)
        return json.loads(proc.stdout if proc.returncode == 0 else proc.stderr)

    def put(self, family, payload, action="put", ok=True):
        return self.cli(family, action, "--json", "-", payload=payload, ok=ok)

    def get(self, identity, history=False):
        return self.cli("entry", "get", identity, *(("--history",) if history else ()))

    def ensure_created(self, family, payload):
        """Read known IDs on resume; never overwrite an independently changed child."""
        proc = self.process(family, "get", payload["id"], ok=None)
        if proc.returncode:
            self.assertIn("Unknown " + family, proc.stderr)
            return self.put(family, dict(payload, expected_updated_at=None))
        current = json.loads(proc.stdout)
        for field, expected in payload.items():
            if current[field] != expected:
                raise RuntimeError("Existing %s %s changed; reconcile before resuming" %
                                   (family, payload["id"]))
        return {"status": "reused"}

    def split(self, expected_version, stop_after_children=None, stop_after_edges=False,
              parent_ok=True):
        """Ordered independent writes; injection points demonstrate partial failure."""
        for index, child in enumerate(self.fixture["children"], 1):
            self.ensure_created("entry", child)
            if index == stop_after_children:
                raise RuntimeError("Injected interruption after child creation")
        for child in self.fixture["children"]:
            self.ensure_created("edge", child_edge(child, self.parent_id))
        # Check new records and links while the original parent is still intact.
        self.assert_after_queries()
        self.assert_links()
        if stop_after_edges:
            raise RuntimeError("Injected interruption before parent replacement")
        return self.put("entry", dict(self.fixture["parent_after"],
                                      expected_updated_at=expected_version), ok=parent_ok)

    @staticmethod
    def ids(value):
        return {item["id"] for item in value["results"]}

    def recall_query(self, query):
        flags = ["--include-hypotheses"] if query.get("include_hypotheses") else []
        return self.cli("recall", query["query"], "--project", "relay", "--limit",
                        str(query["limit"]), "--max-chars", "32000", *flags)

    def assert_after_queries(self):
        for query in self.fixture["queries"]:
            with self.subTest(query=query["name"]):
                self.assertIn(query["after_id"], self.ids(self.recall_query(query)))
                record = self.get(query["after_id"])
                self.assertEqual(record["jira_key"], self.before["jira_key"])
        # The exact-ID path is independent of ranking, Jira limit and graph expansion.
        self.assertEqual(self.get(self.parent_id)["id"], self.parent_id)
        hypothesis = self.fixture["children"][-1]
        hidden = self.cli("recall", "epoch-fence", "--project", "relay", "--limit", "8")
        self.assertNotIn(hypothesis["id"], self.ids(hidden))
        self.assertEqual(self.get(hypothesis["id"])["knowledge_state"], "hypothesis")

    def assert_links(self):
        data = self.cli("graph", "export", "--include-hypotheses")
        explicit = [edge for edge in data["edges"] if edge["origin"] == "explicit"]
        self.assertEqual(len(explicit), len(self.fixture["children"]))
        for child in self.fixture["children"]:
            edge = child_edge(child, self.parent_id)
            actual = self.cli("edge", "get", edge["id"])
            for field, expected in edge.items():
                self.assertEqual(actual[field], expected)
            memberships = [edge for edge in data["edges"]
                           if edge["from_ref"] == "entry:" + child["id"]
                           and edge["origin"] == "entry_project"]
            self.assertEqual({edge["to_ref"] for edge in memberships},
                             {"project:" + p for p in child["project_ids"]})
        # Only one direction is stored; export exposes enough to navigate either end.
        self.assertFalse(any(edge["from_ref"] == "entry:" + self.parent_id for edge in explicit))

    def database_snapshot(self):
        with sqlite3.connect(str(self.db)) as connection:
            return list(connection.iterdump())

    def test_preserves_predeclared_search_oracle_provenance_states_and_history(self):
        self.assertGreaterEqual(len(self.before["summary"]), 27000)
        self.assertLessEqual(len(self.before["summary"]), 29000)
        self.assertEqual(self.fixture["queries"][0]["limit"], 8)
        for query in self.fixture["queries"]:
            with self.subTest(before=query["name"]):
                self.assertIn(self.parent_id, self.ids(self.recall_query(query)))
        # Validate the oracle against the full old payload before transformation.
        for fact in self.fixture["preservation_map"]:
            self.assertIn(fact["text"], self.before["summary"], fact["fact_id"])
            self.assertIn(fact["source"], {s["reference"] for s in self.before["sources"]})
        self.split(self.version)
        parent = self.get(self.parent_id, history=True)
        self.assertGreaterEqual(len(parent["summary"]), 1000)
        self.assertLessEqual(len(parent["summary"]), 3000)
        for field in ("id", "jira_key", "project_ids", "aliases", "sources", "verified_at",
                      "knowledge_state", "work_state"):
            self.assertEqual(parent[field], self.before[field])
        self.assertEqual(len(parent["history"]), 2)
        self.assertEqual(parent["history"][0]["after"]["summary"], self.before["summary"])
        self.assertEqual(parent["history"][1]["before"]["summary"], self.before["summary"])
        for expected in self.fixture["children"]:
            record = self.get(expected["id"])
            for field, value in expected.items():
                self.assertEqual(record[field], value, (expected["id"], field))
        for fact in self.fixture["preservation_map"]:
            record = self.get(fact["destination"])
            self.assertIn(fact["text"], record["summary"], fact["fact_id"])
            self.assertIn(fact["source"], {s["reference"] for s in record["sources"]})
        # Old unique investigation detail is searchable in current children, not just audit history.
        self.assertNotIn("receipt-window-17", parent["summary"])
        results = self.cli("search", "receipt-window-17", "--project", "relay")
        self.assertIn(self.fixture["children"][0]["id"], {e["id"] for e in results["entries"]})
        self.assert_after_queries()
        self.assert_links()

    def test_resume_reuses_stable_ids_and_adds_no_extra_revisions(self):
        with self.assertRaisesRegex(RuntimeError, "after child creation"):
            self.split(self.version, stop_after_children=2)
        self.assertEqual(self.get(self.parent_id)["summary"], self.before["summary"])
        first = {child["id"]: self.get(child["id"], history=True)
                 for child in self.fixture["children"][:2]}
        with self.assertRaisesRegex(RuntimeError, "before parent replacement"):
            self.split(self.version, stop_after_edges=True)
        self.assertEqual(self.get(self.parent_id)["summary"], self.before["summary"])
        self.split(self.version)
        for identity, original in first.items():
            self.assertEqual(self.get(identity, history=True), original)
        snapshot = self.database_snapshot()
        self.split(self.version)  # Identical payload retry is accepted without a revision.
        self.assertEqual(self.database_snapshot(), snapshot)

    def test_stale_parent_cas_preserves_concurrent_work_and_created_details(self):
        with self.assertRaisesRegex(RuntimeError, "before parent replacement"):
            self.split(self.version, stop_after_edges=True)
        concurrent = dict(self.before, summary=self.before["summary"] +
                          "\nПараллельный автор: merge всё ещё требует отдельной проверки.",
                          expected_updated_at=self.version)
        self.put("entry", concurrent)
        snapshot = self.database_snapshot()
        error = self.split(self.version, parent_ok=False)
        self.assertIn("conflict", json.dumps(error).lower())
        self.assertEqual(self.database_snapshot(), snapshot)
        self.assertEqual(self.get(self.parent_id)["summary"], concurrent["summary"])
        self.assert_after_queries()

    def test_resume_refuses_changed_child_and_create_only_prevents_collision(self):
        child = self.fixture["children"][0]
        self.put("entry", dict(child, expected_updated_at=None))
        changed = dict(child, summary=child["summary"] + " Параллельное новое свидетельство.",
                       expected_updated_at=self.get(child["id"])["updated_at"])
        self.put("entry", changed)
        error = self.put("entry", dict(child, expected_updated_at=None), ok=False)
        self.assertIn("conflict", json.dumps(error).lower())
        with self.assertRaisesRegex(RuntimeError, "reconcile before resuming"):
            self.split(self.version)
        self.assertEqual(self.get(child["id"])["summary"], changed["summary"])
        self.assertEqual(self.get(self.parent_id)["summary"], self.before["summary"])

    def test_bounded_views_advertise_omission_and_require_full_get_for_conditions(self):
        self.split(self.version)
        full = self.get(self.parent_id)
        views = [
            ("entry", "read", self.parent_id, "--max-chars", "2000"),
            ("recall", self.parent_id, "--limit", "1", "--max-chars", "2000"),
            ("brief", self.parent_id, "--limit", "1", "--max-chars", "4000"),
        ]
        missed_facts = []
        for command in views:
            with self.subTest(command=command[0]):
                proc = self.process(*command)
                value = json.loads(proc.stdout)
                self.assertLessEqual(len(proc.stdout), int(command[-1]))
                self.assertEqual(value["budget"]["output_chars"], len(proc.stdout))
                self.assertTrue(value["truncated"])
                cards = ([value["entry"]] if value.get("entry") else []) if command[0] == "entry" else value["results"]
                if not cards:
                    # Whole-card omission is a valid, honest budget outcome too.
                    self.assertGreater(value["candidate_count"], 0)
                    self.assertFalse(value["no_match"])
                    self.assertGreater(value["budget"]["omitted_results"], 0)
                    self.assertIn({"command": "entry get", "id": self.parent_id}, value["read_more"])
                    missed_facts.extend(fact["fact_id"] for fact in self.fixture["preservation_map"]
                                        if fact["destination"] == self.parent_id)
                    continue
                card = cards[0]
                self.assertTrue(card["is_excerpt"])
                self.assertTrue(card["excerpt_note"])
                self.assertGreater(card["sources_omitted"], 0)
                self.assertEqual(card["read_more"], {"command": "entry get", "id": self.parent_id})
                self.assertNotEqual(card["summary_excerpt"], full["summary"])
                missed_facts.extend(fact["fact_id"] for fact in self.fixture["preservation_map"]
                                    if fact["destination"] == self.parent_id
                                    and fact["text"] not in card["summary_excerpt"])
        # A concrete incomplete view exists, without asserting that any heuristic
        # must always retain (or always omit) a particular caveat sentence.
        self.assertTrue(missed_facts)
        for fact in self.fixture["preservation_map"]:
            if fact["destination"] == self.parent_id:
                self.assertIn(fact["text"], self.get(self.parent_id)["summary"])

    def measurement_report(self):
        before_proc = self.process("entry", "get", self.parent_id)
        self.split(self.version)
        after_reads = {identity: self.process("entry", "get", identity).stdout
                       for identity in self.fixture["read_scenarios"]["all_preserved_facts"]}
        records = [self.fixture["parent_after"]] + self.fixture["children"]
        bounded = {}
        for name, command in (
            ("entry_read", ("entry", "read", self.parent_id, "--max-chars", "2000")),
            ("recall", ("recall", self.parent_id, "--limit", "1", "--max-chars", "2000")),
            ("brief", ("brief", self.parent_id, "--limit", "1", "--max-chars", "4000")),
        ):
            proc = self.process(*command)
            value = json.loads(proc.stdout)
            cards = ([value["entry"]] if value.get("entry") else []) if name == "entry_read" else value["results"]
            excerpt = cards[0]["summary_excerpt"] if cards else ""
            bounded[name] = {
                "stdout_chars": len(proc.stdout), "max_chars": int(command[-1]),
                "truncated": value["truncated"], "card_omitted": not bool(cards),
                "is_excerpt": cards[0]["is_excerpt"] if cards else None,
                "missing_parent_fact_ids": [fact["fact_id"] for fact in self.fixture["preservation_map"]
                                            if fact["destination"] == self.parent_id
                                            and fact["text"] not in excerpt],
                "read_more": cards[0]["read_more"] if cards else value["read_more"],
            }
        return {
            "fixture": str(FIXTURE.relative_to(FIXTURE.parents[2])),
            "parent_summary_chars_before": len(self.before["summary"]),
            "parent_summary_chars_after": len(self.fixture["parent_after"]["summary"]),
            "child_summary_chars": {child["id"]: len(child["summary"])
                                    for child in self.fixture["children"]},
            "all_current_summary_chars_after": sum(len(record["summary"]) for record in records),
            "bounded_views_after": bounded,
            "reading_volume_unit": "Unicode characters in full entry get JSON stdout, including formatting and final newline; retrieval, edges and history excluded; each distinct entry read once per scenario",
            "scenarios": {name: {
                "before": {"entry_ids": [self.parent_id], "stdout_chars": len(before_proc.stdout)},
                "after": {"entry_ids": identities,
                          "stdout_chars": sum(len(after_reads[identity]) for identity in identities)},
            } for name, identities in self.fixture["read_scenarios"].items()},
            "limits": "Synthetic preservation and reading-volume demonstration only; no time, token or LLM-quality claim. Excerpts may omit conditions; use full get before a condition-dependent action.",
        }


if __name__ == "__main__":
    if sys.argv[1:] == ["--report"]:
        case = CardMaintenanceTests()
        try:
            case.setUp()
            print(json.dumps(case.measurement_report(), ensure_ascii=False, indent=2))
        finally:
            case.doCleanups()
    else:
        unittest.main()
