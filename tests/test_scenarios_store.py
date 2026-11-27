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
        self.assertEqual([item["action"] for item in journal["entries"]],
                         ["scenario_removed", "scenario_updated", "scenario_created"])

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

    def test_colon_project_ids_and_scenario_ids_do_not_corrupt_or_collide_r2_j06(self):
        with memory.transaction(self.conn):
            memory.project_put(self.conn, {"id": "org:svc", "name": "Org Svc"})
            memory.project_put(self.conn, {"id": "a", "name": "A"})
            memory.project_put(self.conn, {"id": "a:b", "name": "AB"})
            s1 = scenarios.copy_template(self.conn, memory, "review", new_id="ops:deploy",
                                         scope="project", project_id="org:svc",
                                         checked_project="org:svc", author=self.author)["scenario"]
            s2 = scenarios.copy_template(self.conn, memory, "review", new_id="b:c",
                                         scope="project", project_id="a",
                                         checked_project="a", author=self.author)["scenario"]
            s3 = scenarios.copy_template(self.conn, memory, "review", new_id="c",
                                         scope="project", project_id="a:b",
                                         checked_project="a:b", author=self.author)["scenario"]
        self.assertEqual((s1["project_id"], s1["id"]), ("org:svc", "ops:deploy"))
        self.assertEqual((s2["project_id"], s2["id"]), ("a", "b:c"))
        self.assertEqual((s3["project_id"], s3["id"]), ("a:b", "c"))
        listed = scenarios.listing(self.conn, memory)
        self.assertEqual(listed.get("invalid_scenarios", []), [])
        self.assertEqual({(s["project_id"], s["id"]) for s in listed["scenarios"]},
                         {("org:svc", "ops:deploy"), ("a", "b:c"), ("a:b", "c")})
        self.assertEqual(scenarios.resolve(self.conn, "ops:deploy", "org:svc")["id"], "ops:deploy")
        self.assertEqual(scenarios.resolve(self.conn, "b:c", "a")["project_id"], "a")
        self.assertEqual(scenarios.resolve(self.conn, "c", "a:b")["project_id"], "a:b")

    def test_read_json_input_bounds_and_large_report_r2_j07(self):
        big_file = Path(self.temp.name) / "big-report.json"
        big_file.write_text(json.dumps({"status": "done", "summary": "x" * 300_000}), encoding="utf-8")
        parsed = scenarios.read_json_input(str(big_file), memory, limit=1024 * 1024)
        self.assertEqual(len(parsed["summary"]), 300_000)
        with self.assertRaises(memory.MemoryError) as caught:
            scenarios.read_json_input(str(big_file), memory, limit=256 * 1024)
        self.assertEqual(caught.exception.code, "limit")
        bad_utf8 = Path(self.temp.name) / "bad-utf8.json"
        bad_utf8.write_bytes(b"\xff\xfe")
        with self.assertRaises(memory.MemoryError) as caught_utf8:
            scenarios.read_json_input(str(bad_utf8), memory, limit=1024)
        self.assertEqual(caught_utf8.exception.code, "invalid_json")

    def test_v4_log_has_before_after_without_metadata_dual_write_r2_j15(self):
        with memory.transaction(self.conn):
            created = scenarios.copy_template(self.conn, memory, "review", new_id="log-check",
                                              scope="global", author=self.author)["scenario"]
        meta_logs = self.conn.execute(
            "SELECT key FROM metadata WHERE key >= 'scenario-log:' AND key < 'scenario-log;'"
        ).fetchall()
        self.assertEqual(meta_logs, [])
        journal = scenarios.log(self.conn, scenario_id="log-check", entries=10)
        self.assertEqual(len(journal["entries"]), 1)
        entry = journal["entries"][0]
        self.assertEqual(entry["action"], "scenario_created")
        self.assertEqual(entry["detail"]["after"]["id"], "log-check")
        self.assertIsNone(entry["detail"]["before"])
        self.assertEqual(entry["detail"]["scope"], "global")

    def test_corrupt_nan_surrogate_quarantined_and_copy_preserves_error_codes_r2_j24_j26(self):
        with memory.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                ("scenario:global:nan-row",
                 '{"id": "nan-row", "scope": "global", "project_id": null, "version": 1, '
                 '"created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z", '
                 '"definition": {"format": "richi-scenario", "format_version": 1, "id": "nan-row", '
                 '"title": "T", "steps": [{"id": "s", "type": "instruction", "params": {"text": "x"}}], '
                 '"layout": {"x": NaN}}}'),
            )
            self.conn.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                ("scenario:global:future-copy", json.dumps({
                    "id": "future-copy", "scope": "global", "project_id": None, "version": 1,
                    "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
                    "definition": {"format": "richi-scenario", "format_version": 99},
                })),
            )
        listed = scenarios.listing(self.conn, memory)
        invalid_keys = {item["key"] for item in listed["invalid_scenarios"]}
        self.assertIn("scenario:global:nan-row", invalid_keys)
        with self.assertRaises(memory.MemoryError) as caught_future:
            with memory.transaction(self.conn):
                scenarios.copy_template(self.conn, memory, "future-copy", scope="global", author=self.author)
        self.assertEqual(caught_future.exception.code, "unsupported_scenario")
        with self.assertRaises(memory.MemoryError) as caught_bad:
            with memory.transaction(self.conn):
                scenarios.copy_template(self.conn, memory, "nan-row", scope="global", author=self.author)
        self.assertEqual(caught_bad.exception.code, "invalid_scenario")

    def test_show_and_plan_reject_unknown_project_and_empty_secret_stays_empty_r2_j19_j27(self):
        with self.assertRaisesRegex(memory.MemoryError, "Unknown project"):
            scenarios.show(self.conn, memory, "review", project_id="does-not-exist")
        with self.assertRaisesRegex(memory.MemoryError, "Unknown project"):
            scenarios.plan(self.conn, memory, "review", project_id="does-not-exist")
        defn = {
            "format": "richi-scenario", "format_version": 1, "id": "sec-empty", "title": "Sec Empty",
            "inputs": {"tok": {"type": "secret"}},
            "steps": [{"id": "s1", "type": "instruction", "params": {"text": "hi"}}],
        }
        _, redacted_empty, _, _ = scenarios.resolve_inputs(defn, {"tok": ""})
        self.assertEqual(redacted_empty["tok"], "")
        _, redacted_set, _, _ = scenarios.resolve_inputs(defn, {"tok": "abc"})
        self.assertEqual(redacted_set["tok"], "<secret>")

    def test_unmocked_installed_skills_pinned_agent_utf8_stdin_and_huge_int_r3(self):
        # R3-I1: unmocked installed_skills works with real synthetic HOME + skill folder.
        fake_home = Path(self.temp.name) / "fake_home"
        skill_dir = fake_home / ".codex" / "skills" / "code-review"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: code-review\ndescription: Review\n---\nBody\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"HOME": str(fake_home)}, clear=False):
            shown_ok = scenarios.show(self.conn, memory, "review", agent="codex")
            self.assertNotIn("missing_skills", shown_ok)
            shown_miss = scenarios.show(self.conn, memory, "review", agent="claude")
            self.assertEqual(shown_miss["missing_skills"][0]["skill"], "code-review")
            # R3-C4: pinned executor.agent is used instead of caller's agent!
            pinned_def = {
                "format": "richi-scenario", "format_version": 1, "id": "pinned-scn", "title": "Pinned",
                "steps": [{
                    "id": "s1", "type": "skill", "executor": {"agent": "codex"},
                    "params": {"skill": {"per_agent": {"codex": "code-review", "claude": "missing-cl"}}},
                    "checks": [{"kind": "evidence_git"}],
                }],
            }
            with memory.transaction(self.conn):
                scenarios.change(self.conn, {"action": "add", "scope": "global", "definition": pinned_def},
                                 memory, author=self.author)
            shown_p = scenarios.show(self.conn, memory, "pinned-scn", agent="claude")
            self.assertEqual(shown_p["resolved_skills"]["s1"], "code-review")
            self.assertNotIn("missing_skills", shown_p)
            planned_p = scenarios.plan(self.conn, memory, "pinned-scn", agent="claude")
            self.assertEqual(planned_p["steps"][0]["skill"], "code-review")
            # R3-X1: plan warns when project_id is None and scenario uses evidence_git.
            self.assertTrue(any("project-bound checks" in w for w in planned_p["warnings"]))
        # R3-I12: invalid UTF-8 on stdin buffer maps to invalid_json (not UnicodeDecodeError).
        import io
        fake_stdin = SimpleNamespace(buffer=io.BytesIO(b"\xff\xfe"))
        with mock.patch("sys.stdin", fake_stdin):
            with self.assertRaises(memory.MemoryError) as caught_u:
                scenarios.read_json_input("-", memory, limit=1024)
        self.assertEqual(caught_u.exception.code, "invalid_json")
        # R3-X2: huge integer CLI input fails with FormatError cleanly.
        with self.assertRaises(scenario_format.FormatError):
            scenarios._coerce_cli_input("n", {"type": "number"}, "9" * 400)


if __name__ == "__main__":
    unittest.main()
