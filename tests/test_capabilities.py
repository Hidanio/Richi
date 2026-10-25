"""Capability IDs resolve to the real parser without opening personal storage."""
import argparse
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from richi import capabilities, memory


class CapabilitiesTests(unittest.TestCase):
    def test_small_group_listing_matches_unique_full_catalog(self):
        groups = capabilities.run(SimpleNamespace(action="list", group=None), memory)
        catalog = capabilities.run(SimpleNamespace(action="list", group="all"), memory)
        operations = catalog["operations"]
        self.assertEqual(len({row["id"] for row in operations}), len(operations))
        self.assertEqual(sum(row["operations"] for row in groups["groups"]), len(operations))
        self.assertNotIn("operations", groups)
        grouped = []
        for group in groups["groups"]:
            listing = capabilities.run(SimpleNamespace(action="list", group=group["id"]), memory)
            self.assertEqual(len(listing["operations"]), group["operations"])
            grouped.extend(row["id"] for row in listing["operations"])
        self.assertCountEqual(grouped, [row["id"] for row in operations])
        self.assertTrue(all(row["effect"] in {"read", "write", "conditional"} for row in operations))

    def test_every_catalog_id_resolves_and_describes_actual_parser_syntax(self):
        root = memory.parser()
        listing = capabilities.run(SimpleNamespace(action="list", group="all"), memory)
        for operation in listing["operations"]:
            with self.subTest(identity=operation["id"]):
                current = root
                for token in operation["id"].split():
                    actions = [action for action in current._actions if isinstance(action, argparse._SubParsersAction)]
                    self.assertEqual(len(actions), 1)
                    self.assertIn(token, actions[0].choices)
                    current = actions[0].choices[token]
                result = capabilities.run(SimpleNamespace(action="describe", id=operation["id"]), memory)
                self.assertEqual(result["syntax"], current.format_help())
                self.assertEqual(result["effect"], operation["effect"])
                self.assertTrue(result["guide"].startswith("references/"))
                with redirect_stdout(StringIO()), self.assertRaises(SystemExit) as exit_context:
                    root.parse_args(operation["id"].split() + ["--help"])
                self.assertEqual(exit_context.exception.code, 0)

    def test_describe_exposes_real_filters_and_pagination_options(self):
        for identity, options in (("discover", ["--where", "--max-chars"]),
                                  ("query select", ["--after", "--fields", "--family"]),
                                  ("query facets", ["--by", "--limit"]),
                                  ("fetch", ["--ref", "--offset"])):
            result = capabilities.run(SimpleNamespace(action="describe", id=identity), memory)
            for option in options:
                self.assertIn(option, result["syntax"])

    def test_dispatch_does_not_resolve_settings_or_open_database(self):
        with tempfile.TemporaryDirectory(prefix="richi-capabilities-") as directory:
            missing = Path(directory) / "must-not-exist.sqlite3"
            for command in (["capabilities", "list"], ["capabilities", "list", "--group", "all"],
                            ["capabilities", "describe", "query count"]):
                args = memory.parser().parse_args(["--db", str(missing), "--config", str(missing) + ".json", *command])
                with patch.object(memory, "connect", side_effect=AssertionError("Must not open SQLite")), \
                        patch("richi_launcher.config.resolve_settings", side_effect=AssertionError("Must not resolve workspace")):
                    result = memory.run(args)
                self.assertIn(result["view"], {"capabilities", "capability"})
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_unknown_group_and_command_are_explicit_errors(self):
        for args in (SimpleNamespace(action="list", group="missing"),
                     SimpleNamespace(action="describe", id="query count; DROP TABLE entries"),
                     SimpleNamespace(action="describe", id="query"),
                     SimpleNamespace(action="missing")):
            with self.subTest(args=args), self.assertRaises(memory.MemoryError):
                capabilities.run(args, memory)


if __name__ == "__main__":
    unittest.main()
