"""Independent checks for the synthetic growth measurement harness."""

import copy
from argparse import Namespace
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from cli_environment import cli_environment
from richi import compact, graph, memory, recall


EVALS = Path(__file__).resolve().parents[1] / "evals"
RUNNER = EVALS / "evaluate_memory_growth.py"
CASES = EVALS / "memory_growth_cases.json"
ORACLE_PROBE = "intentional-missing-oracle"


def load_runner():
    spec = importlib.util.spec_from_file_location("memory_growth_eval_under_test", RUNNER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class MemoryGrowthEvalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="richi-growth-eval-test-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.runner = load_runner()
        cls.fixture = json.loads(CASES.read_text(encoding="utf-8"))

        # Add an independently wrong expectation for a real CLI query. This
        # checks that misses remain visible without freezing today's organic
        # retrieval misses into an expectation that they must never improve.
        cls.custom_fixture = copy.deepcopy(cls.fixture)
        target = next(entry for entry in cls.fixture["entry"]
                      if entry["knowledge_state"] == "confirmed")
        cls.custom_fixture["cases"].append(dict(cls.fixture["cases"][0], **{
            "id": ORACLE_PROBE,
            "query": "quartzscopeunmatchedsentinel",
            "expected_refs": ["entry:" + target["id"]],
            "expect_ref": "entry:" + target["id"],
            "expected_no_match": False,
            "limit": 8,
        }))
        cls.custom_path = cls.root / "independent-oracle.json"
        cls.custom_path.write_text(json.dumps(cls.custom_fixture, ensure_ascii=False), encoding="utf-8")

        cls.poisoned_database = cls.root / "ambient-memory.sqlite3"
        cls.poisoned_database.write_bytes(b"Synthetic sentinel: this is deliberately not a SQLite database.")
        cls.poisoned_config = cls.root / "ambient-config.json"
        cls.poisoned_config.write_text("deliberately invalid synthetic configuration", encoding="utf-8")
        cls.environment = cli_environment(
            cls.root, RICHI_DB=str(cls.poisoned_database), RICHI_CONFIG=str(cls.poisoned_config),
            RICHI_DATA_DIR=str(cls.root / "ambient-data"), RICHI_PORT="invalid",
            RICHI_WORKSPACE="missing-synthetic-workspace", RICHI_ACTIVE_WORKSPACE="missing-synthetic-workspace",
            RICHI_ACTIVE_CONFIG=str(cls.poisoned_config), RICHI_CHAT_ID="synthetic-unbound-growth-chat",
            CODEX_THREAD_ID="synthetic-different-growth-chat")
        cls.ambient_home = Path(cls.environment["HOME"])
        registry = Path(cls.environment["XDG_CONFIG_HOME"]) / "richi"
        registry.mkdir(parents=True)
        for name in ("config.json", "workspaces.json", "chat-workspaces.json"):
            (registry / name).write_text("invalid synthetic ambient registry", encoding="utf-8")
        cls.ambient_before = cls.ambient_snapshot()
        cls.report_path = cls.root / "measurement.json"
        cls.process = cls.run_cli("--quick", "--repeats", "1", "--cases", str(cls.custom_path),
                                  "--output", str(cls.report_path), timeout=240)
        if cls.process.returncode != 0:
            raise AssertionError("Quick growth measurement failed: " + cls.process.stderr + cls.process.stdout[:3000])
        cls.report = json.loads(cls.process.stdout)

    @classmethod
    def run_cli(cls, *arguments, timeout=30):
        return subprocess.run([sys.executable, "-B", str(RUNNER), *arguments],
                              capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                              env=cls.environment)

    @classmethod
    def ambient_snapshot(cls):
        paths = [cls.poisoned_database, cls.poisoned_config]
        paths.extend(path for path in cls.ambient_home.rglob("*") if path.is_file())
        return {str(path): path.read_bytes() for path in paths}

    def test_quick_cli_ignores_ambient_store_configuration_and_chat(self):
        self.assertEqual(self.process.stderr, "")
        self.assertEqual(self.ambient_snapshot(), self.ambient_before)
        self.assertFalse((self.root / "ambient-data").exists())
        self.assertEqual(json.loads(self.report_path.read_text(encoding="utf-8")), self.report)
        self.assertEqual(self.report["schema_version"], 1)
        self.assertEqual(self.report["mode"], "quick")
        self.assertEqual(self.report["summary"]["datasets"], 6)
        self.assertTrue(self.report["summary"]["execution_completed"])
        self.assertEqual(self.report["summary"]["invariant_failures"], 0)

    def test_dataset_axes_are_independent_and_baseline_is_not_repeated(self):
        expected = {
            True: {(8, 0, 0), (24, 0, 0), (8, 3, 0), (8, 12, 0), (8, 0, 2), (8, 0, 5)},
            False: {(100, 0, 0), (1000, 0, 0), (10000, 0, 0),
                    (100, 10, 0), (100, 100, 0), (100, 0, 10), (100, 0, 100)},
        }
        for quick, tuples in expected.items():
            with self.subTest(quick=quick):
                specs = self.runner.dataset_specs(quick=quick)
                self.assertEqual({(row["background"], row["details"], row["history"]) for row in specs}, tuples)
                self.assertEqual(len(specs), len(tuples))
                self.assertEqual(len({row["id"] for row in specs}), len(specs))

    def test_generated_corpora_are_deterministic_nested_and_do_not_mutate_fixture(self):
        original = copy.deepcopy(self.fixture)
        small = self.runner.generate(self.fixture, background=8, details=3)
        larger = self.runner.generate(self.fixture, background=24, details=12)
        self.assertEqual(self.runner.generate(self.fixture, background=8, details=3), small)
        self.assertEqual(self.fixture, original)
        for family in ("project", "entry", "entity", "edge"):
            with self.subTest(family=family):
                first = {record["id"]: record for record in small[family]}
                second = {record["id"]: record for record in larger[family]}
                self.assertEqual(len(first), len(small[family]))
                self.assertEqual(len(second), len(larger[family]))
                self.assertTrue(first.keys() <= second.keys())
                self.assertEqual(first, {identity: second[identity] for identity in first})
        self.assertGreater(len(larger["entry"]), len(small["entry"]))

    def test_quality_misses_are_reported_separately_from_invariants_and_exit_policy(self):
        # The intentionally absent-token query runs through the installed CLI;
        # an empty answer must not quietly turn our positive oracle into a pass.
        self.assertGreater(self.report["summary"]["quality_misses"], 0)
        self.assertFalse(self.report["summary"]["quality_expectations_met"])
        for dataset in self.report["datasets"]:
            probe = next(case for case in dataset["cases"] if case["id"] == ORACLE_PROBE)
            self.assertTrue(probe["quality_misses"])
            self.assertEqual(probe["invariant_failures"], [])
            self.assertEqual(probe["normal"]["returned_refs"], [])
            self.assertFalse(probe["normal"]["hit_at_limit"])
            self.assertEqual(probe["normal"]["recall_at_limit"], 0)
            self.assertTrue(probe["normal"]["no_match"])
        self.assertEqual(self.runner.exit_status(self.report), 0)
        self.assertNotEqual(self.runner.exit_status(self.report, fail_on_miss=True), 0)
        unsafe = copy.deepcopy(self.report)
        unsafe["summary"]["invariant_failures"] = 1
        self.assertNotEqual(self.runner.exit_status(unsafe), 0)
        incomplete = copy.deepcopy(self.report)
        incomplete["summary"]["execution_completed"] = False
        self.assertNotEqual(self.runner.exit_status(incomplete), 0)

    def test_report_preserves_provenance_read_only_checks_and_separate_measurements(self):
        provenance = self.report["provenance"]
        self.assertEqual(provenance["fixture_sha256"], hashlib.sha256(self.custom_path.read_bytes()).hexdigest())
        self.assertEqual(provenance["generator_sha256"], hashlib.sha256(RUNNER.read_bytes()).hexdigest())
        for name in ("memory.py", "recall.py", "compact.py", "punisher.py", "graph.py", "schema.sql", "graph_schema.sql"):
            self.assertRegex(provenance["code_sha256"][name], r"^[0-9a-f]{64}$")
        self.assertEqual(self.report["scenarios"], self.custom_fixture["cases"])
        self.assertEqual(self.report["summary"]["cases"], 6 * len(self.custom_fixture["cases"]))
        self.assertEqual(self.report["configuration"]["repeats_after_first_call"], 1)
        for dataset in self.report["datasets"]:
            with self.subTest(dataset=dataset["id"]):
                self.assertTrue(dataset["read_only"]["unchanged"])
                self.assertEqual(dataset["read_only"]["before"], dataset["read_only"]["after"])
                self.assertGreater(dataset["storage"]["files_bytes"]["database"], 0)
                self.assertEqual(dataset["storage"]["counts"]["entries"],
                                 len(self.fixture["entry"]) + dataset["background"] + dataset["details"])
                self.assertTrue(all(item["content_matches_fixture"] for item in dataset["full_reads"]))
                audit = dataset["punisher"]
                self.assertEqual(audit["report"]["budget"]["output_chars"], audit["output_chars"])
                self.assertLessEqual(audit["output_chars"], 16000)
                self.assertEqual(audit["report"]["health"]["status"], "ok")
                for case in dataset["cases"]:
                    normal, diagnostic = case["normal"], case["diagnostic"]
                    self.assertEqual(normal["budget"]["max_chars"], 16000)
                    self.assertEqual(diagnostic["max_chars"], 100000)
                    self.assertLessEqual(normal["output_chars"], 16000)
                    self.assertLessEqual(diagnostic["output_chars"], 100000)
                    self.assertEqual(diagnostic["explain"]["expected"]["ref"], case["expect_ref"])
                    self.assertEqual(len(normal["repeated"]["samples_ms"]), 1)
                    for elapsed in [normal["first_call_ms"], diagnostic["elapsed_ms"],
                                    *normal["repeated"]["samples_ms"]]:
                        self.assertTrue(math.isfinite(elapsed))
                        self.assertGreaterEqual(elapsed, 0)

    def test_history_axis_keeps_current_size_and_adds_exact_revision_counts(self):
        history = [dataset for dataset in self.report["datasets"]
                   if dataset["background"] == 8 and dataset["details"] == 0]
        self.assertEqual({dataset["history"] for dataset in history}, {0, 2, 5})
        baseline = next(dataset for dataset in history if dataset["history"] == 0)
        for dataset in history:
            self.assertEqual(dataset["current_corpus_sha256"], baseline["current_corpus_sha256"])
            for field in ("current_summary_chars", "current_summary_utf8_bytes"):
                self.assertEqual(dataset["storage"][field], baseline["storage"][field])
            self.assertEqual(dataset["storage"]["counts"]["entry_history"],
                             baseline["storage"]["counts"]["entry_history"] + dataset["history"])
            if dataset["history"]:
                self.assertGreater(dataset["storage"]["entry_history_before_json_bytes"],
                                   baseline["storage"]["entry_history_before_json_bytes"])

    def test_history_seeding_keeps_actual_current_payloads_equal(self):
        # Compare stored full records, not two hashes of the same generator
        # input; a broken final history write must fail this test.
        corpus = self.runner.generate(self.fixture, background=1, details=0)
        snapshots = []
        for revisions in (0, 2):
            database = self.root / ("history-proof-" + str(revisions)) / "synthetic.sqlite3"
            self.runner.seed(database, corpus, revisions, history_target=self.fixture["generator"]["history_target"])
            with memory.connect(database, readonly=True) as connection:
                records = {}
                for family in ("entry", "entity", "edge"):
                    for expected in corpus[family]:
                        actual = (memory.entry_get(connection, expected["id"]) if family == "entry" else
                                  graph.get(connection, family, expected["id"], memory, history=False))
                        records[family + ":" + expected["id"]] = {
                            key: value for key, value in actual.items() if key not in {"created_at", "updated_at"}}
                target = memory.entry_get(connection, self.fixture["generator"]["history_target"], history=True)
                self.assertEqual(len(target["history"]), 1 + revisions)
                snapshots.append(records)
        self.assertEqual(snapshots[0], snapshots[1])

    @staticmethod
    def render_response(response):
        for _ in range(8):
            raw = compact.rendered(response)
            if response["budget"]["output_chars"] == len(raw):
                return raw
            response["budget"]["output_chars"] = len(raw)
        raise AssertionError("Serialized response size did not stabilize")

    def test_classifier_detects_false_oracle_and_corrupted_evidence_independently(self):
        database = self.root / "classifier.sqlite3"
        connection = memory.connect(database, create=True)
        self.addCleanup(connection.close)
        memory.initialize(connection, database)
        corpus = self.runner.generate(self.fixture, background=0, details=0)
        for record in corpus["project"]:
            memory.project_put(connection, record)
        for record in corpus["entry"]:
            memory.entry_put(connection, record)
        for family in ("entity", "edge"):
            for record in corpus[family]:
                graph.put(connection, record, family, memory)
        catalog = {family + ":" + record["id"]: record
                   for family in ("entry", "entity") for record in corpus[family]}
        case = self.fixture["cases"][0]
        response = recall.recall(connection, Namespace(query=case["query"], project=case.get("project"),
                                                       limit=case["limit"], max_chars=16000), memory)
        raw = self.render_response(response)
        checked = self.runner.classify_result(raw, response, case, catalog, 16000)
        self.assertEqual(checked["invariant_failures"], [])
        self.assertEqual(checked["quality_misses"], [])
        absent = next(ref for ref, record in catalog.items()
                      if ref not in checked["returned_refs"] and record["knowledge_state"] == "confirmed")
        wrong = dict(case, expected_refs=[absent], expect_ref=absent)
        measured = self.runner.classify_result(raw, response, wrong, catalog, 16000)
        self.assertTrue(measured["quality_misses"])
        self.assertEqual(measured["invariant_failures"], [])
        for field, invalid in (("sources", [{"reference": "https://example.invalid/invented-evidence"}]),
                               ("work_state", "released"), ("knowledge_state", "hypothesis"),
                               ("read_more", {"command": "entry get", "id": "wrong"})):
            with self.subTest(field=field):
                changed = copy.deepcopy(response)
                changed["results"][0][field] = invalid
                measured = self.runner.classify_result(self.render_response(changed), changed, case, catalog, 16000)
                self.assertTrue(measured["invariant_failures"])

    def test_existing_report_is_never_overwritten(self):
        before = self.report_path.read_bytes()
        process = self.run_cli("--quick", "--output", str(self.report_path))
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("overwrite", process.stderr.lower())
        self.assertEqual(self.report_path.read_bytes(), before)

    def test_invalid_numeric_arguments_fail_before_measurement(self):
        for flag, value in (("--repeats", "0"), ("--repeats", "21"), ("--repeats", "1.5"),
                            ("--timeout", "0"), ("--timeout", "-1"), ("--timeout", "601")):
            with self.subTest(flag=flag, value=value):
                process = self.run_cli("--quick", flag, value)
                self.assertNotEqual(process.returncode, 0)
                self.assertIn(flag[2:], process.stderr)


if __name__ == "__main__":
    unittest.main()
