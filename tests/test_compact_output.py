"""Presentation and byte-budget contracts against synthetic knowledge only."""
from contextlib import redirect_stderr, redirect_stdout
from contextvars import Context
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_recall as fixtures
import test_git_cli as git_fixtures
from richi import git_sources, memory, output


class OutputContextTests(unittest.TestCase):
    def test_nested_contexts_and_exceptions_restore_the_callers_format(self):
        value = {"text": "Условие: только после проверки.\nException: cold restore.", "items": [1, None]}
        pretty = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        small = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        self.assertEqual(output.rendered(value), pretty)
        with output.json_mode(compact=True):
            self.assertEqual(output.rendered(value), small)
            # An independent request does not inherit another request's choice.
            self.assertEqual(Context().run(output.rendered, value), pretty)
            with self.assertRaises(ValueError), output.json_mode(compact=False):
                self.assertEqual(output.rendered(value), pretty)
                raise ValueError("nested failure")
            self.assertEqual(output.rendered(value), small)
        self.assertEqual(output.rendered(value), pretty)

    def test_in_process_cli_success_and_failure_do_not_leak_format(self):
        for arguments, failure in ((["--compact-json", "check"], False),
                                   (["--compact-json", "check"], True),
                                   (["check"], False)):
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(memory, "run", side_effect=memory.MemoryError("failure") if failure else None,
                              return_value={"ok": True}), redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(memory.main(arguments), int(failure))
            self.assertEqual(output.rendered({"ok": True}), '{\n  "ok": true\n}\n')
            if not failure:
                self.assertEqual(stdout.getvalue(), output.rendered({"ok": True}, compact="--compact-json" in arguments))

    def test_source_artifact_bytes_do_not_follow_stdout_format(self):
        source = {"reference": "fixture://source", "label": "Свидетельство", "revision": "v1"}
        with tempfile.TemporaryDirectory() as directory:
            files = [Path(directory) / name for name in ("pretty.json", "compact.json")]
            for enabled, path in zip((False, True), files):
                with output.json_mode(compact=enabled):
                    git_sources._write_source(path, source)
            self.assertEqual(files[0].read_bytes(), files[1].read_bytes())
            self.assertEqual(files[0].read_text(), output.rendered(source, compact=False))


class CompactOutputCliTests(unittest.TestCase):
    process = fixtures.RecallTests.process
    cli = fixtures.RecallTests.cli
    put = fixtures.RecallTests.put
    entry = fixtures.RecallTests.entry
    entity = fixtures.RecallTests.entity
    edge = fixtures.RecallTests.edge

    def setUp(self):
        fixtures.RecallTests.setUp(self)
        self.put("entry", [self.entry("needle", aliases=["formatneedle"],
                                      summary="Только после проверки. Исключение: cold restore.",
                                      sources=[{"reference": "test://source", "label": "Русский текст",
                                                "revision": "v1", "observed_at": None}]),
                           self.entry("neighbor", aliases=["formatneedle"], project_ids=["beta"]),
                           self.entry("guess", aliases=["formatneedle"], knowledge_state="hypothesis")])
        self.put("edge", self.edge("detail", "entry:neighbor", "entry:needle", kind="informs"))
        self.manifest = self.db.parent / "sources.json"
        self.cli("sources", "snapshot", "--ref", "entry:needle", "--output", str(self.manifest))

    def parsed(self, *arguments, enabled=False):
        process = self.process(*(["--compact-json"] if enabled else []), *arguments)
        value = json.loads(process.stdout)
        self.assertEqual(process.stdout, output.rendered(value, compact=enabled))
        if "budget" in value:
            self.assertEqual(value["budget"]["output_chars"], len(process.stdout))
            self.assertLessEqual(len(process.stdout), value["budget"]["max_chars"])
        return value, process.stdout

    @staticmethod
    def without_wire_size(value):
        value = copy.deepcopy(value)
        if "budget" in value:
            del value["budget"]["output_chars"]
        return value

    def commands(self):
        return [("entry", "read", "needle"),
                ("recall", "formatneedle", "--project", "alpha", "--include-hypotheses"),
                ("recall", "formatneedle", "--expect", "entry:needle"),
                ("brief", "formatneedle"),
                ("punisher", "--as-of", "2026-10-01"),
                ("sources", "check", "--manifest", str(self.manifest))]

    def test_ample_budget_preserves_all_fields_and_read_only_database(self):
        before = self.db.read_bytes()
        for command in self.commands():
            with self.subTest(command=command):
                plain, pretty = self.parsed(*command, "--max-chars", "100000")
                small, raw = self.parsed(*command, "--max-chars", "100000", enabled=True)
                self.assertEqual(self.without_wire_size(plain), self.without_wire_size(small))
                self.assertLess(len(raw), len(pretty))
        self.assertEqual(self.db.read_bytes(), before)

    def test_tight_budgets_use_actual_format_for_each_fitter(self):
        for maximum in (2000, 4000):
            for command in self.commands():
                if command[0] == "brief" and maximum < 4000:
                    continue  # Brief has always required at least 4000 characters.
                with self.subTest(command=command, maximum=maximum):
                    self.parsed(*command, "--max-chars", str(maximum), enabled=True)

    def test_full_get_is_lossless_and_history_stays_opt_in(self):
        for extra in ([], ["--history"]):
            plain, pretty = self.parsed("entry", "get", "needle", *extra)
            small, raw = self.parsed("entry", "get", "needle", *extra, enabled=True)
            self.assertEqual(plain, small)
            self.assertEqual("history" in small, bool(extra))
            self.assertIn("Исключение: cold restore.", small["summary"])
            self.assertLess(len(raw), len(pretty))

    def test_compact_budget_can_retain_evidence_lost_to_pretty_format(self):
        # The card has more than 2000 pretty characters but fits compactly.
        self.put("entry", self.entry("budget", sources=[
            {"reference": "test://" + "x" * 500, "label": "Evidence " + str(i),
             "revision": "v1", "locator": "section", "observed_at": "2026-10-01"}
            for i in range(2)]))
        plain, _ = self.parsed("entry", "read", "budget", "--max-chars", "2000")
        small, _ = self.parsed("entry", "read", "budget", "--max-chars", "2000", enabled=True)
        self.assertIsNotNone(small.get("entry"))
        self.assertGreater(len(small["entry"]["sources"]), len((plain.get("entry") or {}).get("sources", [])))

    def test_ambiguity_and_no_match_survive_small_output(self):
        self.put("entity", [self.entity("one", aliases=["meaningword"]),
                            self.entity("two", aliases=["meaningword"])])
        for enabled in (False, True):
            value, _ = self.parsed("recall", "meaningword", "--limit", "1", "--max-chars", "2000", enabled=enabled)
            self.assertTrue(value["ambiguity"]["detected"])
            self.assertFalse(value["no_match"])
            missing, _ = self.parsed("recall", "absentunfindable", "--max-chars", "2000", enabled=enabled)
            self.assertTrue(missing["no_match"])

    def test_flag_position_errors_and_text_modes(self):
        value, _ = self.parsed("recall", "--", "--compact-json", enabled=True)
        self.assertEqual(value["query"], "--compact-json")
        self.process("recall", "formatneedle", "--compact-json", ok=False)
        plain = self.process("entry", "get", "missing", ok=False)
        small = self.process("--compact-json", "entry", "get", "missing", ok=False)
        self.assertEqual(plain.stderr, small.stderr)
        rejected = self.process("--compact-json", "map", ok=False)
        self.assertIn("not map", json.loads(rejected.stderr)["error"])
        self.assertEqual(self.process("--help").stdout, self.process("--compact-json", "--help").stdout)


class CompactGitCliTests(unittest.TestCase):
    setUp = git_fixtures.GitCliTests.setUp
    git = git_fixtures.GitCliTests.git
    cli = git_fixtures.GitCliTests.cli
    put = git_fixtures.GitCliTests.put
    capture_attach = git_fixtures.GitCliTests.capture_attach
    process = fixtures.RecallTests.process
    parsed = CompactOutputCliTests.parsed

    def test_git_commands_measure_compact_stdout_and_keep_source_files_stable(self):
        _, _, _, path = self.capture_attach("--rev", "HEAD")
        second_path = self.root / "compact-source.json"
        self.cli("--compact-json", "sources", "capture", "--project", "fixture", "--path", "contract.py",
                 "--rev", "HEAD", "--output", str(second_path))
        sources = [json.loads(p.read_text()) for p in (path, second_path)]
        for p, source in zip((path, second_path), sources):
            self.assertEqual(p.read_text(), output.rendered(source, compact=False))
            source.pop("observed_at")  # Independent captures record their actual time.
        self.assertEqual(*sources)
        (self.repo / "contract.py").write_text("version = 2\n")
        self.git("add", "contract.py")
        self.git("commit", "-qm", "Second version")
        for command in [("sources", "show", "--ref", "entry:task:contract", "--source", "2"),
                        ("sources", "git-check", "--project", "fixture"),
                        ("sources", "related", "--project", "fixture", "--path", "contract.py"),
                        ("sources", "impact", "--project", "fixture", "--base", self.commit, "--target", "HEAD")]:
            with self.subTest(command=command):
                self.parsed(*command, "--max-chars", "4000", enabled=True)


if __name__ == "__main__":
    unittest.main()
