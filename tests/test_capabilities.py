"""Capability IDs resolve to the real parser without opening personal storage."""
import argparse
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from richi import capabilities, cli_help, memory
from richi_launcher import help_format


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

    def test_describe_returns_the_contract_of_the_live_parser(self):
        # scripts/check_cli_docs.py checks that every ID names a command.
        root = cli_help.decorate(memory.parser())
        for identity, _, effect, _, guide in capabilities.COMMANDS:
            with self.subTest(identity=identity):
                result = capabilities.run(SimpleNamespace(action="describe", id=identity), memory)
                parser = help_format.find(root, identity.split(), None)
                self.assertEqual(result["syntax"], help_format.syntax(parser))
                self.assertNotIn("examples:", result["syntax"])
                self.assertEqual((result["effect"], result["guide"]), (effect, "references/" + guide))
                # The contract, read from the parser itself: usage, summary, arguments and options.
                text = " ".join(result["syntax"].split())
                self.assertTrue(text.startswith("usage: richi " + identity + " "), text)
                page = help_format.page_parts(parser)
                self.assertIn(" ".join(page.summary.split()), text)
                # The longer description stays on the help page.
                about = " ".join((page.about or "").split()[:8])
                if about:
                    self.assertNotIn(about, text)
                for action in parser._get_positional_actions():
                    if action.help != argparse.SUPPRESS:
                        self.assertIn(" ".join([action.metavar or action.dest, *(action.help or "").split()]), text)
                self.assertIn("\noptions:\n", result["syntax"])

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
