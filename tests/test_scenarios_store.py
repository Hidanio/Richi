"""Scenario definitions store: scopes, optimistic concurrency, templates, dry-run plan and log."""
import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from richi import memory, rules, scenario_format, scenarios


class ScenariosStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="richi-scenarios-store-")
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "memory.sqlite3"
        self.conn = memory.connect(self.db, create=True)
        self.addCleanup(self.conn.close)
        memory.initialize(self.conn, self.db)
        with memory.transaction(self.conn):
            memory.project_put(self.conn, {"id": "alpha", "name": "Alpha"})
            memory.project_put(self.conn, {"id": "beta", "name": "Beta"})
        self.author = rules.Author("codex:thread-1", "codex")

    def test_builtin_review_template_loads_and_validates(self):
        templates = scenarios.builtin_templates()
        self.assertIn("review", templates)
        review = templates["review"]["definition"]
        self.assertEqual(scenario_format.normalize(review), review)
        self.assertEqual([step["id"] for step in review["steps"]], ["context", "angle", "merge", "approve"])
        listing = scenarios.list_templates()
        self.assertEqual([item["id"] for item in listing["templates"]], ["review"])

    def test_copy_and_scope_resolution_order(self):
        # Built-in resolves out of the box.
        self.assertEqual(scenarios.resolve(self.conn, "review", "alpha")["scope"], "builtin")
        # Copy into global scope shadows built-in.
        with memory.transaction(self.conn):
            glob = scenarios.copy_template(self.conn, memory, "review", scope="global", author=self.author)["scenario"]
        self.assertEqual(glob["scope"], "global")
        self.assertEqual(glob["version"], 1)
        self.assertEqual(scenarios.resolve(self.conn, "review", "alpha")["scope"], "global")
        # Copy into project alpha shadows global for alpha only.
        with memory.transaction(self.conn):
            proj = scenarios.copy_template(self.conn, memory, "review", scope="project", project_id="alpha",
                                           checked_project="alpha", author=self.author)["scenario"]
        self.assertEqual(proj["scope"], "project")
        self.assertEqual(scenarios.resolve(self.conn, "review", "alpha")["scope"], "project")
        self.assertEqual(scenarios.resolve(self.conn, "review", "beta")["scope"], "global")
        self.assertEqual(scenarios.resolve(self.conn, "review", scope="builtin")["scope"], "builtin")
        # Listing filters by scope.
        all_list = scenarios.listing(self.conn, memory, workspace="demo")
        self.assertEqual(len(all_list["scenarios"]), 2)
        self.assertTrue(all_list["builtin_templates"][0]["shadowed"])
        self.assertEqual(len(scenarios.listing(self.conn, memory, global_scope=True)["scenarios"]), 1)
        self.assertEqual(len(scenarios.listing(self.conn, memory, project="alpha")["scenarios"]), 1)

    def test_apply_update_cas_and_remove_with_log(self):
        with memory.transaction(self.conn):
            created = scenarios.copy_template(self.conn, memory, "review", new_id="custom-review",
                                              scope="global", author=self.author)["scenario"]
        # Unchanged payload returns unchanged without bumping version.
        with memory.transaction(self.conn):
            same = scenarios.change(self.conn, {
                "action": "update", "id": "custom-review", "scope": "global",
                "expected_updated_at": created["updated_at"],
                "definition": created["definition"],
            }, memory, author=self.author)
        self.assertEqual(same["status"], "unchanged")
        self.assertEqual(same["scenario"]["version"], 1)
        # Modified definition bumps version.
        edited = copy.deepcopy(created["definition"])
        edited["title"] = "Custom review v2"
        with memory.transaction(self.conn):
            updated = scenarios.change(self.conn, {
                "action": "update", "id": "custom-review", "scope": "global",
                "expected_updated_at": created["updated_at"],
                "definition": edited,
            }, memory, author=self.author)
        self.assertEqual(updated["status"], "updated")
        self.assertEqual(updated["scenario"]["version"], 2)
        # Stale expected_updated_at fails with conflict.
        with self.assertRaises(memory.MemoryError) as caught:
            with memory.transaction(self.conn):
                scenarios.change(self.conn, {
                    "action": "update", "id": "custom-review", "scope": "global",
                    "expected_updated_at": created["updated_at"],
                    "definition": edited,
                }, memory, author=self.author)
        self.assertEqual(caught.exception.code, "conflict")
        # Remove works with current updated_at.
        with memory.transaction(self.conn):
            removed = scenarios.change(self.conn, {
                "action": "remove", "id": "custom-review", "scope": "global",
                "expected_updated_at": updated["scenario"]["updated_at"],
            }, memory, author=self.author)
        self.assertEqual(removed["status"], "removed")
        self.assertIsNone(scenarios.resolve(self.conn, "custom-review", include_builtin=False))
        # Log recorded created, updated, removed newest first.
        journal = scenarios.log(self.conn, scenario_id="custom-review", entries=10)
        self.assertEqual([item["action"] for item in journal["entries"]], ["removed", "updated", "created"])

    def test_unsupported_and_corrupt_rows_do_not_hide_healthy_scenarios(self):
        with memory.transaction(self.conn):
            scenarios.copy_template(self.conn, memory, "review", scope="global", author=self.author)
            self.conn.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                ("scenario:global:future", json.dumps({
                    "id": "future", "scope": "global", "project_id": None, "version": 1,
                    "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
                    "definition": {"format": "richi-scenario", "format_version": 2, "on_failure": {}},
                })),
            )
            self.conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?)", ("scenario:global:broken", "{not-json"))
        result = scenarios.listing(self.conn, memory)
        self.assertEqual([item["id"] for item in result["scenarios"]], ["review"])
        self.assertEqual(len(result["unsupported_scenarios"]), 1)
        self.assertEqual(result["unsupported_scenarios"][0]["requires"], 2)
        self.assertEqual(result["unsupported_scenarios"][0]["next"], "richi update")
        self.assertEqual(len(result["invalid_scenarios"]), 1)

    def test_plan_resolves_inputs_matrix_and_missing_skills(self):
        with mock.patch("richi.scenarios._installed_skills", return_value={"other-skill"}):
            missing = scenarios.plan(self.conn, memory, "review", agent="codex")
        self.assertFalse(missing["ready_to_run"])
        self.assertEqual(missing["missing_required_inputs"], ["branch"])
        self.assertTrue(any("code-review" in warning for warning in missing["warnings"]))

        with mock.patch("richi.scenarios._installed_skills", return_value={"code-review"}):
            ready = scenarios.plan(self.conn, memory, "review", agent="codex",
                                   raw_inputs=["branch=feature_x", "angles=security,api"])
        self.assertTrue(ready["ready_to_run"])
        self.assertEqual(ready["run_title"], "Review: feature_x")
        self.assertEqual(ready["warnings"], [])
        angle_step = next(step for step in ready["steps"] if step["id"] == "angle")
        self.assertEqual(angle_step["matrix_instances"], [{"angle": "security"}, {"angle": "api"}])
        self.assertEqual(angle_step["skill"], "code-review")

    def test_scenarios_never_appear_in_recall_or_search(self):
        with memory.transaction(self.conn):
            scenarios.copy_template(self.conn, memory, "review", new_id="uniqueneedle-scn",
                                    scope="global", author=self.author)
        args = SimpleNamespace(query="uniqueneedle", project=None, where=None, limit=20, include_superseded=False)
        self.assertEqual(memory.search(self.conn, args, memory.backend(self.conn))["entries"], [])

    def test_project_mismatch_rejected(self):
        with self.assertRaises(memory.MemoryError) as caught:
            with memory.transaction(self.conn):
                scenarios.copy_template(self.conn, memory, "review", scope="project", project_id="beta",
                                        checked_project="alpha", author=self.author)
        self.assertEqual(caught.exception.code, "project_mismatch")


if __name__ == "__main__":
    unittest.main()
