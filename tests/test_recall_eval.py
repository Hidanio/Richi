"""The public eval must be isolated and fail when its expectations regress."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


EVALS = Path(__file__).resolve().parents[1] / "evals"


class PublicRecallEvalTests(unittest.TestCase):
    def run_eval(self, *args, env=None):
        return subprocess.run([sys.executable, "-B", str(EVALS / "evaluate_recall.py"), *args],
                              capture_output=True, text=True, timeout=30, env=env)

    def test_public_cases_use_disposable_db_and_ignore_user_settings(self):
        with tempfile.TemporaryDirectory(prefix="richi-eval-isolation-") as temporary:
            root = Path(temporary)
            database = root / "user-memory.sqlite3"
            original = b"User memory must never be opened by a public eval."
            database.write_bytes(original)
            config = root / "user-config.json"
            config.write_text("deliberately invalid user config")
            env = dict(os.environ, RICHI_DB=str(database), RICHI_CONFIG=str(config), RICHI_PORT="invalid")
            result = self.run_eval(env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertGreaterEqual(report["summary"]["cases"], 7)
            self.assertEqual(report["summary"]["failed"], 0)
            self.assertTrue(report["database_unchanged_by_recall"])
            self.assertEqual(database.read_bytes(), original)

    def test_wrong_expected_result_makes_runner_fail(self):
        with tempfile.TemporaryDirectory(prefix="richi-eval-regression-") as temporary:
            fixture = json.loads((EVALS / "recall_cases.json").read_text())
            fixture["cases"][0]["expected_refs"] = ["entry:task:DEMO-170"]
            fixture["cases"][0]["expected_first"] = "entry:task:DEMO-170"
            path = Path(temporary) / "bad-expectation.json"
            path.write_text(json.dumps(fixture))
            result = self.run_eval("--cases", str(path))
            self.assertEqual(result.returncode, 1, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["summary"]["failed"], 1)
            self.assertIn("expected results in top five", report["cases"][0]["failures"])

    def test_existing_report_is_preserved(self):
        with tempfile.TemporaryDirectory(prefix="richi-eval-report-") as temporary:
            report = Path(temporary) / "report.json"
            report.write_text("existing report")
            result = self.run_eval("--output", str(report))
            self.assertEqual(result.returncode, 1)
            self.assertIn("Refusing to overwrite", json.loads(result.stderr)["error"])
            self.assertEqual(report.read_text(), "existing report")


if __name__ == "__main__":
    unittest.main()
