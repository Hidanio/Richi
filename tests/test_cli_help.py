"""CLI help: overview, grouped command list, pages, topics, corrections and help routing."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from richi import capabilities, cli_help, cli_parser, graph, memory, query_filters
from richi_launcher import agent_setup, chats, cli, config, help_format
from richi_launcher.config import ConfigError

from cli_environment import cli_environment


def run(argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            code = memory.main(argv)
        except SystemExit as exc:
            code = exc.code
    return code, stdout.getvalue(), stderr.getvalue()


def documented():
    """The runtime parser tree with its help pages attached."""
    return cli_help.decorate(memory.parser())


def runtime_pages():
    """(path, parser) of every runtime page; launcher commands have their own."""
    for path, parser, _ in help_format.walk(documented()):
        if path and path[0] not in cli_parser.LAUNCHER_COMMANDS:
            yield path, parser


def launcher_pages():
    for name, parser in cli.launcher_parsers().items():
        for path, child, _ in help_format.walk(parser, (name,)):
            yield path, child


class IsolatedCase(unittest.TestCase):
    """A synthetic home without RICHI_* or chat variables; help never opens a store."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="richi-help-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.database = self.root / "memory.sqlite3"
        for patch in (mock.patch.dict(os.environ, cli_environment(self.root), clear=True),
                      mock.patch.object(memory, "connect", side_effect=AssertionError("help must not open a store"))):
            patch.start()
            self.addCleanup(patch.stop)

    def help(self, argv):
        return run(["--db", str(self.database), *argv])

    def tearDown(self):
        self.assertFalse(self.database.exists())

    def assert_usage_error(self, argv, contains, target):
        code, stdout, stderr = self.help(argv)
        self.assertEqual((code, stdout), (1, ""), stderr)
        error = json.loads(stderr)
        self.assertEqual((error["code"], error["help"]), ("usage_error", target), error)
        self.assertIn(contains, error["error"])
        self.assertIn("See '" + target + "'", error["error"])


class HelpOutputTests(IsolatedCase):
    def test_bare_command_prints_a_short_overview_of_real_commands(self):
        root = memory.parser()
        for argv in ([], ["--compact-json"]):
            with self.subTest(argv=argv):
                code, stdout, stderr = self.help(argv)
                self.assertEqual((code, stderr), (0, ""))
                self.assertIn("richi help COMMAND", stdout)
                self.assertLess(len(stdout.splitlines()), 30)
                for line in cli_help.OVERVIEW_COMMANDS:
                    self.assertIn(line, stdout)
                for name in cli_help.TOPICS:
                    self.assertIn(name, stdout)
        # Each suggested command is described by its own summary.
        text = " ".join(stdout.split())
        for row in cli_help.OVERVIEW_COMMANDS:
            parser, summary = root, None
            for word in row.split()[1:]:
                commands = help_format.subcommands(parser)
                if commands is None or word not in commands.choices:
                    break
                summary, parser = help_format.summaries(parser)[word], commands.choices[word]
            with self.subTest(row=row):
                self.assertIsNotNone(summary, "the row names no command")
                self.assertIn(" ".join((row + " " + summary).split()), text)

    def test_help_and_dash_help_print_the_grouped_command_list(self):
        code, listing, _ = self.help(["help"])
        self.assertEqual(code, 0)
        self.assertEqual(self.help(["--help"])[1], listing)
        for title, _ in cli_help.GROUPS:
            self.assertIn(title + ":", listing)
        for name in cli_help.TOPICS:
            self.assertIn("\n  " + name, listing)
        for text in ("global options", "examples:", "richi help COMMAND",
                     "Documentation: " + help_format.documentation() + "docs/CLI.md"):
            self.assertIn(text, listing)

    def test_help_command_matches_each_command_page(self):
        for path, parser in runtime_pages():
            with self.subTest(command=" ".join(path)):
                code, stdout, _ = self.help(["help", *path])
                self.assertEqual(code, 0)
                self.assertEqual(stdout, parser.format_help())
                self.assertEqual(self.help([*path, "--help"])[1], stdout)

    def test_direct_runtime_pages_of_launcher_commands_point_to_the_launcher(self):
        for name in cli_parser.LAUNCHER_COMMANDS:
            with self.subTest(command=name):
                code, stdout, _ = self.help(["help", name])
                self.assertEqual(code, 0)
                self.assertIn("\n\n  richi help %s\n" % name, stdout)

    def test_pages_have_examples_links_and_readable_usage(self):
        code, stdout, _ = self.help(["help", "recall"])
        self.assertEqual(code, 0)
        self.assertTrue(stdout.startswith("usage: richi recall"))
        for text in ("QUERY", "--limit " + cli_help.METAVARS["--limit"], "examples:"):
            self.assertIn(text, stdout)
        see = " ".join(stdout.split("\nsee also: ", 1)[1].split("\nguide: ", 1)[0].split())
        for target in cli_help.DETAILS["recall"]["see"]:
            self.assertIn("richi help " + target, see)
        guide = help_format.page_parts(dict(runtime_pages())[("recall",)]).guide
        self.assertIn("guide: " + help_format.guide_url(guide), stdout)
        code, stdout, _ = self.help(["help", "entry"])
        commands = stdout.split("commands:\n", 1)[1].split("\n\n", 1)[0]
        for name in help_format.summaries(dict(runtime_pages())[("entry",)]):
            self.assertIn("  " + name, commands)
        self.assertNotIn("{", commands)

    def test_a_page_composes_again_on_every_print(self):
        parser = dict(runtime_pages())[("sources", "capture")]
        first = parser.format_help()
        self.assertEqual(parser.format_help(), first)
        self.assertIsNotNone(help_format.page_parts(parser))

    def test_topics_are_listed_and_name_the_values_the_code_accepts(self):
        listing = self.help(["help", "topics"])[1]
        for name in cli_help.TOPICS:
            with self.subTest(topic=name):
                self.assertIn(name, listing)
                code, stdout, _ = self.help(["help", name])
                self.assertEqual((code, stdout), (0, cli_help.topic(name)))
        records, filters = cli_help.topic("records"), cli_help.topic("filters")
        for value in memory.KINDS | memory.STATES | graph.ENTITY_KINDS | memory.KNOWLEDGE:
            self.assertRegex(records, r"\b%s\b" % value)
        for value in query_filters.FIELDS:
            self.assertRegex(filters, r"\b%s\b" % value)

    def test_write_pages_name_every_field_a_write_accepts(self):
        pages = dict(runtime_pages())
        for path, fields in ((("entry", "put"), memory.ENTRY_FIELDS), (("entity", "put"), graph.ENTITY_FIELDS),
                             (("edge", "put"), graph.EDGE_FIELDS), (("relation", "put"), memory.RELATION_FIELDS),
                             (("project", "upsert"), memory.PROJECT_FIELDS)):
            about = help_format.page_parts(pages[path]).about
            for field in set(fields) - {"created_at", "updated_at"}:
                with self.subTest(command=" ".join(path), field=field):
                    self.assertRegex(about, r"\b%s\b" % field)

    def test_documented_ranges_are_the_ranges_the_types_accept(self):
        for path, parser in list(runtime_pages()) + list(launcher_pages()):
            for action in parser._actions:
                bounds = getattr(action.type, "bounds", None)
                if bounds is None:
                    continue
                with self.subTest(command=" ".join(path), option=action.option_strings):
                    self.assertIn("%d-%d" % bounds, action.help or "")
        pages = dict(runtime_pages())
        for path, option, text in ((("brief",), "--limit", "1-50"), (("brief",), "--max-chars", "4000-100000"),
                                   (("sources", "history"), "--limit", "1-100")):
            action = next(action for action in pages[path]._actions if option in action.option_strings)
            self.assertIn(text, action.help)

    def test_wrapped_lines_never_split_an_option_name(self):
        for path, parser in list(runtime_pages()) + list(launcher_pages()):
            with self.subTest(command=" ".join(path)):
                for line in parser.format_help().splitlines():
                    self.assertNotRegex(line, r"(^|\s)--[a-z0-9-]*-$")

    def test_prose_is_wrapped_and_indented_blocks_keep_their_layout(self):
        text = help_format.paragraphs("Summary.", "\n    First block,\n    wrapped.\n\n    A list:\n      1. one\n"
                                      "      2. two\n\n      richi backup\n      richi migrate")
        self.assertEqual(text, "Summary.\n\nFirst block, wrapped.\n\nA list:\n  1. one\n  2. two\n\n"
                               "  richi backup\n  richi migrate")
        # A page whose text is a shared block and more paragraphs.
        lines = self.help(["help", "entry", "put"])[1].splitlines()
        for paragraph in textwrap.dedent(cli_help.DETAILS["entry put"]["about"]).strip().split("\n\n"):
            start = " ".join(paragraph.split()[:3])
            self.assertTrue(any(line.startswith(start) for line in lines), start)

    def test_pages_fit_the_text_width(self):
        root = documented()
        texts = {"richi help": cli_help.root_help(root), "richi": cli_help.overview(root)}
        texts.update((" ".join(path), parser.format_help()) for path, parser in list(runtime_pages()) + list(launcher_pages()))
        texts.update(("help " + name, cli_help.topic(name)) for name in cli_help.TOPICS)
        texts.update(("describe " + row[0], memory.run(root.parse_args(["capabilities", "describe", row[0]]), root)["syntax"])
                     for row in capabilities.COMMANDS)
        for subject, text in texts.items():
            with self.subTest(page=subject):
                # Commands and links are never cut.
                self.assertEqual([line for line in text.splitlines() if len(line) > help_format.TEXT_WIDTH
                                  and not line.lstrip().startswith(("richi ", "guide: ", "local copy: ",
                                                                    "Documentation: ", "Local copy: "))], [])

    def test_see_also_lines_wrap_between_pages(self):
        long = tuple("command%d subcommand" % index for index in range(8))
        texts = [(help_format.epilog(see=long), long)]
        for path, parser in list(runtime_pages()) + list(launcher_pages()):
            text = parser.format_help()
            if help_format.page_parts(parser).see:
                texts.append((text, help_format.page_parts(parser).see))
        for text, see in texts:
            with self.subTest(see=see):
                lines = text.split("\n")
                start = next(index for index, line in enumerate(lines) if line.startswith("see also: "))
                block = lines[start:start + 1]
                for line in lines[start + 1:]:
                    if not line.startswith(" " * len("see also: ") + "richi help "):
                        break
                    block.append(line)
                self.assertTrue(all(line.endswith(",") for line in block[:-1]), block)
                self.assertTrue(all(len(line) <= help_format.TEXT_WIDTH for line in block), block)
                pieces = [piece.strip() for line in block for piece in line[len("see also:"):].split(",")]
                self.assertEqual([piece for piece in pieces if piece], ["richi help " + item for item in see])

    def test_pages_have_no_colors_even_when_a_terminal_would(self):
        # On every Python, so a revert fails where CI runs: the parsers turn
        # colors off and the formatter refuses them.
        recall = help_format.subcommands(memory.parser()).choices["recall"]
        for parser in (cli.Parser(prog="richi demo"), memory.Parser(prog="richi demo"), memory.parser(), recall):
            self.assertIs(parser.color, False)
        self.assertIn("_set_color", vars(help_format.HelpFormatter))
        with mock.patch.dict(os.environ, FORCE_COLOR="1"):
            for argv in (["help"], ["help", "graph"], ["recall", "--help"]):
                self.assertNotIn("\x1b", self.help(argv)[1], argv)
            self.assertNotIn("\x1b", memory.run(memory.parser().parse_args(["capabilities", "describe", "recall"]))["syntax"])
            self.assertNotIn("\x1b", cli.launcher_parser("config").format_help())
            with mock.patch.object(cli_help, "style", None):
                self.assertNotIn("\x1b", self.help(["help", "recall"])[1])

    def test_subcommands_line_up_like_options_on_every_python(self):
        parser = cli.Parser(prog="richi demo")
        parser.add_subparsers(dest="action").add_parser("a-long-subcommand", help="Run it")
        help_format.document(parser, "Demo.")
        rows = parser.format_help().splitlines()
        self.assertIn("  a-long-subcommand  Run it", rows)
        self.assertIn("  -h, --help         Show this help and exit", rows)

    def test_mistakes_suggest_a_correction_and_a_help_page(self):
        entry = help_format.subcommands(dict(runtime_pages())[("entry",)]).choices
        for argv, contains, target in (
                (["stauts"], "Did you mean 'status'?", "richi help"),
                (["entry", "gte", "task:x"], "Did you mean 'get'?", "richi help entry"),
                (["graph", "neighbors", "entry:x", "--view", "compat"], "Did you mean 'compact'?",
                 "richi help graph neighbors"),
                (["entry"], "'richi entry' needs a command: " + ", ".join(entry), "richi help entry"),
                (["--bogus", "recall", "q"], "unrecognized arguments: --bogus", "richi help"),
                (["--compact-jsn", "chat", "current"], "Did you mean '--compact-json'?", "richi help"),
                (["map", "--compact-json"], "(richi map does not take the global option --compact-json)", "richi help map"),
                (["recall", "q", "-wx"], "-wx (a global option: put it before the command)", "richi help recall"),
                (["recall", "q", "--projct", "x"], "Did you mean '--project'? See", "richi help recall"),
                (["recall", "q", "--projct-path", "."],
                 "--projct-path . Did you mean '--project-path'? Global options go before the command.", "richi help recall"),
                (["--project-path", ".", "project", "upsert", "--json", "projects.json"], "without --project-path",
                 "richi help project upsert"),
                (["recall"], "required: QUERY", "richi help recall"),
                (["recall", "q", "--limt", "3"], "Did you mean '--limit'?", "richi help recall"),
                (["sources", "capture", "--project", "p", "--path", "x", "--outptu", "f"], "Did you mean '--output'?",
                 "richi help sources capture"),
                (["recall", "q", "--compact-json"], "a global option: put it before the command", "richi help recall"),
                (["brief", "q", "--limit", "60"], "limit must be between 1 and 50", "richi help brief"),
                (["help", "filtres"], "Did you mean 'filters'?", "richi help"),
                (["help", "sources", "captur"], "Did you mean 'capture'?", "richi help sources"),
                (["help", "recall", "extra"], "has no subcommands", "richi help recall"),
                (["help", "filters", "where"], "'richi help filters' takes no further words", "richi help filters")):
            with self.subTest(argv=argv):
                self.assert_usage_error(argv, contains, target)

    def test_values_from_fixed_lists_get_a_suggestion(self):
        from richi import query_tools
        code, _, stderr = self.help(["capabilities", "describe", "recal"])
        self.assertEqual(code, 1)
        self.assertIn("Did you mean 'recall'?", json.loads(stderr)["error"])
        with self.assertRaisesRegex(memory.MemoryError, r"Did you mean 'title'\?"):
            query_tools._fields("ref,titel", memory)

    def test_help_words_ignore_help_flags_and_reject_options(self):
        name = next(iter(cli_help.TOPICS))
        for style in (help_format, None):
            with mock.patch.object(cli_help, "style", style):
                recall = self.help(["help", "recall"])
                for argv, expected in ((["help", "recall", "-h"], recall), (["help", "recall", "--help"], recall),
                                       (["help", name, "-h"], (0, cli_help.topic(name), "")),
                                       (["help", name, "--help"], (0, cli_help.topic(name), ""))):
                    with self.subTest(style=style, argv=argv):
                        self.assertEqual(self.help(argv), expected)
        # Before any word -h shows the page of help itself.
        self.assertEqual(self.help(["help", "-h"]), self.help(["help", "help"]))
        self.assertTrue(self.help(["help", "-h"])[1].startswith("usage: richi help [-h] [COMMAND|TOPIC ...]"))
        # The whole message: the reason, a period and the page to read.
        error = json.loads(self.help(["help", name, "extra"])[2])["error"]
        self.assertEqual(error, "'richi help %s' takes no further words. See 'richi help %s'." % (name, name))
        for style in (help_format, None):
            with self.subTest(style=style), mock.patch.object(cli_help, "style", style):
                self.assertEqual(self.help(["help", "recall", "--help"])[0], 0)
                self.assert_usage_error(["help", "recall", "--projec"], "not options such as --projec",
                                        "richi help recall")
                self.assert_usage_error(["help", "--", "recall"], "not options such as --", "richi help")

    def test_direct_runtime_calls_of_launcher_commands_are_refused(self):
        for name in cli_parser.LAUNCHER_COMMANDS:
            with self.subTest(command=name):
                code, _, stderr = self.help([name, "--some-option", "value"])
                self.assertEqual(code, 1)
                self.assertIn("Use the installed richi " + name + " command", json.loads(stderr)["error"])
                # A mistyped global option before it is not the launcher's to parse.
                self.assert_usage_error(["--some-option", name], "unrecognized arguments: --some-option", "richi help")

    def test_suggestions_prefer_prefixes_then_close_spellings(self):
        self.assertEqual(help_format.suggestions("rec", ["recall", "check", "search"]), ["recall"])
        self.assertEqual(help_format.suggestions("serach", ["search", "sources"]), ["search"])
        self.assertEqual(help_format.suggestions("zzz", ["search"]), [])

    def test_runtime_keeps_plain_argparse_help_without_a_compatible_launcher(self):
        def plain_help():
            self.assertIsNone(cli_help.style)
            code, stdout, _ = self.help(["help", "recall"])
            self.assertEqual(code, 0)
            self.assertIn("usage: richi recall", stdout)
            self.assertIn("usage: richi", self.help([])[1])
            self.assertTrue(self.help(["help", "sources", "capture"])[1].startswith("usage: richi sources capture"))
            # Every argument error still names its page.
            for argv, target in ((["stauts"], "richi help"), (["recall"], "richi help recall"),
                                 (["recall", "q", "--limt", "3"], "richi help"), (["help", "recall", "extra"], "richi help recall"),
                                 (["help", "config", "show"], "richi help config")):
                with self.subTest(argv=argv):
                    self.assert_usage_error(argv, "", target)
            result = memory.run(memory.parser().parse_args(["capabilities", "describe", "recall"]))
            self.assertIn("usage: richi recall", result["syntax"])

        with mock.patch.object(cli_help, "style", None):
            plain_help()
        # A launcher whose help_format API differs from the runtime's, or
        # that lacks a name the runtime needs.
        @contextmanager
        def missing_name():
            with mock.patch.dict(help_format.__dict__):
                del help_format.__dict__[cli_help.NEEDS[-1]]
                yield
        for launcher in (lambda: mock.patch.object(help_format, "API", help_format.API + 1), missing_name):
            try:
                with launcher():
                    importlib.reload(cli_help)
                    plain_help()
            finally:
                importlib.reload(cli_help)
        self.assertIs(cli_help.style, help_format)

    def test_the_runtime_declares_every_help_format_name_it_uses(self):
        import ast
        used = set()
        for path in Path(memory.__file__).parent.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Attribute) and (
                        isinstance(node.value, ast.Name) and node.value.id == "style"
                        or isinstance(node.value, ast.Attribute) and node.value.attr == "style"):
                    used.add(node.attr)
        self.assertEqual(sorted(used - set(cli_help.NEEDS)), [])
        self.assertEqual([name for name in cli_help.NEEDS if not hasattr(help_format, name)], [])

    def test_knowledge_commands_never_load_help(self):
        paths = [str(Path(memory.__file__).resolve().parents[1]), str(Path(help_format.__file__).resolve().parents[1])]
        loaded = ("print(sorted(name for name in ('richi.cli_help', 'richi_launcher.help_format', "
                  "'richi_launcher.workspace_commands') if name in sys.modules))")
        runtime = ("import sys; sys.path[:0] = %r\n"
                   "from richi import memory\n"
                   "memory.parser().parse_args(['--compact-json', 'recall', 'cache', '--limit', '3'])\n"
                   % paths) + loaded
        # The launcher selects the runtime of an ordinary command up to exec.
        launcher = ("import os, sys; sys.path[:0] = %r\n"
                    "from unittest import mock\n"
                    "from richi_launcher import cli\n"
                    "def run(*arguments):\n"
                    "    %s\n"
                    "    sys.stdout.flush()\n"
                    "    os._exit(0)\n"
                    "with mock.patch.object(cli, 'resolve_runtime'), "
                    "mock.patch.object(cli, 'runtime_command', return_value=['python']), "
                    "mock.patch.object(cli.os, 'execve', side_effect=run):\n"
                    "    cli.main(['-w', 'default', '--compact-json', 'recall', 'cache'])\n" % (paths, loaded))
        for script in (runtime, launcher):
            with self.subTest(script=script.splitlines()[-1]):
                result = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True,
                                        env=cli_environment(self.root))
                self.assertEqual((result.returncode, result.stdout.strip()), (0, "[]"), result.stderr)

    def test_map_errors_are_not_usage_errors(self):
        with self.assertRaises(memory.MemoryError) as raised:
            memory.Parser(prog="-c").error("Port must be 1-65535")
        self.assertNotIsInstance(raised.exception, memory.UsageError)
        self.assertEqual(str(raised.exception), "Port must be 1-65535")

    def test_usage_errors_keep_the_whole_payload_in_both_packages(self):
        expected = {"error": "x", "code": "usage_error", "help": "richi help"}
        self.assertEqual(memory.UsageError("x", "richi help").details, expected)
        self.assertEqual(cli.UsageError("x", "richi help").details, expected)


class GuideTests(IsolatedCase):
    def test_subcommand_pages_print_their_group_guide(self):
        cataloged = {row[0] for row in capabilities.COMMANDS}
        group, child = next(path for path, _ in runtime_pages() if len(path) == 2
                            and "guide" not in cli_help.DETAILS.get(" ".join(path), {})
                            and " ".join(path) not in cataloged and cli_help.DETAILS.get(path[0], {}).get("guide"))
        code, stdout, _ = self.help(["help", group, child])
        self.assertEqual(code, 0)
        self.assertIn("guide: " + help_format.guide_url(cli_help.DETAILS[group]["guide"]), stdout)
        update = cli.launcher_parser("update")
        page = help_format.find(update, ["auto", "enable"], None)
        self.assertIn("guide: " + help_format.guide_url(help_format.page_parts(update).guide), page.format_help())

    def test_guides_name_repository_files(self):
        references = help_format.REFERENCES
        for guide, path in (("RECALL.md", references + "RECALL.md"), ("RECALL.md#recall", references + "RECALL.md"),
                            ("docs/SETUP.md", "docs/SETUP.md"), ("docs/CLI.md#help", "docs/CLI.md"),
                            (help_format.REPOSITORY + "blob/v1.2.3/docs/CLI.md#help", "docs/CLI.md"),
                            ("https://example.com/guide", None)):
            with self.subTest(guide=guide):
                self.assertEqual(help_format.guide_path(guide), path)

    def test_pages_name_a_local_copy_of_their_guide(self):
        checkout = self.root / "checkout"
        guide = help_format.page_parts(dict(runtime_pages())[("recall",)]).guide
        copy = checkout / help_format.guide_path(guide)
        for path in (checkout / "docs/CLI.md", copy):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# Guide\n", encoding="utf-8")
        runtime = json.dumps({"mode": "dev", "version": "1.2.3", "source": str(checkout)})
        with mock.patch.dict(os.environ, {"RICHI_ACTIVE_RUNTIME": runtime}):
            self.assertEqual(help_format.local_guide("docs/CLI.md#help"), checkout / "docs/CLI.md")
            self.assertEqual(help_format.local_guide(guide), copy)
            self.assertIsNone(help_format.local_guide("docs/MISSING.md"))
            self.assertIsNone(help_format.local_guide("https://example.com/guide"))
            self.assertIn("\nlocal copy: %s\n" % copy, self.help(["help", "recall"])[1])
            self.assertIn("\nLocal copy: %s\n" % (checkout / "docs/CLI.md"), self.help(["help"])[1])

    def test_a_release_names_the_skill_reference_it_bundles(self):
        guide = help_format.page_parts(dict(runtime_pages())[("recall",)]).guide
        path = help_format.guide_path(guide)
        self.assertTrue(path.startswith(help_format.REFERENCES), path)
        package = self.root / "site-packages" / "richi_launcher"
        bundled = package / "agent_skill" / "references" / path[len(help_format.REFERENCES):]
        bundled.parent.mkdir(parents=True)
        bundled.write_text("# Guide\n", encoding="utf-8")
        release = json.dumps({"mode": "release", "version": "1.2.3"})
        for environment in ({"RICHI_ACTIVE_RUNTIME": release}, {}):
            with self.subTest(runtime=environment), mock.patch.dict(os.environ, environment), \
                    mock.patch.object(help_format, "__file__", str(package / "help_format.py")):
                self.assertEqual(help_format.local_guide(guide), bundled)
                self.assertIn("\nlocal copy: %s\n" % bundled, self.help(["help", "recall"])[1])

    def test_links_point_to_the_documentation_of_the_running_code(self):
        help_format.documentation.cache_clear()
        self.addCleanup(help_format.documentation.cache_clear)
        for runtime, ref in (({"mode": "release", "version": "1.2.3"}, "v1.2.3"),
                             ({"mode": "dev", "version": "1.2.3"}, "main"),
                             ({"mode": "release", "version": "1.2.3.dev1"}, "main")):
            with self.subTest(runtime=runtime), mock.patch.dict(os.environ, {"RICHI_ACTIVE_RUNTIME": json.dumps(runtime)}):
                help_format.documentation.cache_clear()
                self.assertEqual(help_format.documentation(), help_format.REPOSITORY + "blob/" + ref + "/")
                self.assertEqual(help_format.guide_url("docs/CLI.md#help"),
                                 help_format.REPOSITORY + "blob/" + ref + "/docs/CLI.md#help")


class LauncherHelpTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-launcher-help-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for patch in (mock.patch.dict(os.environ, cli_environment(self.root), clear=True),
                      mock.patch.object(config.sys, "platform", "linux")):
            patch.start()
            self.addCleanup(patch.stop)

    def launch(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = cli.main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, stdout.getvalue(), stderr.getvalue()

    def assert_page(self, argv, heading):
        code, stdout, stderr = self.launch(argv)
        self.assertEqual((code, stderr), (0, ""), argv)
        self.assertTrue(stdout.startswith(heading), stdout)
        self.assertIn("examples:", stdout)

    def assert_error(self, argv, contains, code_name="usage_error", target=None):
        code, stdout, stderr = self.launch(argv)
        self.assertEqual((code, stdout), (1, ""), argv)
        error = json.loads(stderr)
        self.assertEqual(error["code"], code_name, error)
        if target is not None:
            self.assertEqual(error["help"], target)
        self.assertIn(contains, error["error"])
        return error

    def test_launcher_pages_print_before_selection_binding_and_recovery(self):
        refuse = AssertionError("help must not read selection or chat state")
        with mock.patch.object(cli, "detect_chat", side_effect=refuse), \
                mock.patch.object(cli, "pin_chat", side_effect=refuse), \
                mock.patch.object(cli, "current_chat", side_effect=refuse), \
                mock.patch.object(cli, "resolve_settings", side_effect=refuse), \
                mock.patch.object(agent_setup, "recover_pending", side_effect=refuse):
            for argv, heading in ((["help", "config"], "usage: richi config"),
                                  (["help", "config", "set"], "usage: richi config set"),
                                  (["-w", "work", "help", "update"], "usage: richi update"),
                                  (["--db", "/synthetic.sqlite3", "help", "setup"], "usage: richi setup"),
                                  (["--project-path", ".", "help", "workspace", "current"],
                                   "usage: richi workspace current"),
                                  (["--chat", "synthetic", "help", "chat", "bind"], "usage: richi chat bind"),
                                  (["--chat", "synthetic", "use", "--help"], "usage: richi use"),
                                  (["-w", "work", "config", "show", "--help"], "usage: richi config show"),
                                  (["help", "status", "-h"], "usage: richi status"),
                                  (["help", "update", "auto", "enable"], "usage: richi update auto enable")):
                with self.subTest(argv=argv):
                    self.assert_page(argv, heading)

    def test_launcher_help_survives_a_broken_chat_state(self):
        registry = chats._registry_path()
        registry.parent.mkdir(parents=True)
        registry.write_text("{not json", encoding="utf-8")
        os.environ.update(RICHI_CHAT_ID="synthetic", RICHI_WORKSPACE="work", RICHI_DB="/synthetic.sqlite3")
        self.assert_error(["chat", "current"], "Invalid chat binding registry", code_name="chat_registry_invalid")
        for argv, heading in ((["help", "config"], "usage: richi config"),
                              (["help", "workspace"], "usage: richi workspace"),
                              (["workspace", "current", "--help"], "usage: richi workspace current")):
            with self.subTest(argv=argv):
                self.assert_page(argv, heading)

    def test_launcher_argument_errors_are_json_with_a_help_page(self):
        config_commands = help_format.subcommands(cli.launcher_parser("config")).choices
        for argv, contains, target in (
                (["config", "sho"], "Did you mean 'show'?", "richi help config"),
                (["config"], "'richi config' needs a command: " + ", ".join(config_commands), "richi help config"),
                (["update", "auto", "enable", "--interval", "90s"], "whole minutes, hours or days",
                 "richi help update auto enable"),
                (["update", "auto", "enable", "--interval", "30d"], "between 15m and 7d", "richi help update auto enable"),
                (["update", "check", "--version", "1.x"], "stable X.Y.Z", "richi help update check"),
                (["workspace", "create", "Bad"], "Workspace name must match", "richi help workspace create"),
                (["workspace", "use", "Bad"], "Workspace name must match", "richi help workspace use"),
                (["--chat", "synthetic", "chat", "bind", "Bad"], "Workspace name must match", "richi help chat bind"),
                (["-w", "missing", "config", "set", "dev", "maybe"], "dev must be true or false",
                 "richi help config set"),
                (["status", "--project", ""], "nonempty ID", "richi help status"),
                (["use", "Bad"], "Workspace name must match", "richi help use"),
                (["use"], "without an interactive terminal", "richi help use"),
                (["config", "set", "development.source", ""], "nonempty path", "richi help config set"),
                (["-w", "Bad", "recall", "q"], "Workspace name must match", "richi help workspace"),
                (["-w", "work", "workspace", "curent"], "Did you mean 'current'", "richi help workspace"),
                (["-w", "work", "workspace"], "needs a command", "richi help workspace"),
                (["--compact-jsn", "chat", "current"], "Did you mean '--compact-json'?", "richi help"),
                (["--compact-json=false", "check"], "--compact-json takes no value", "richi help"),
                (["help", "status", "--projects"], "not options such as --projects", "richi help status"),
                (["status", "--limit", "0"], "limit must be between 1 and 200", "richi help status"),
                (["config", "show", "--project-path", "."],
                 "(richi config show does not take the global option --project-path)", "richi help config show"),
                (["update", "chek"], "Did you mean 'check'?", "richi help update"),
                (["update", "auto", "enable", "--intervall", "6h"], "Did you mean '--interval'",
                 "richi help update auto enable"),
                (["config", "show", "--compact-json"], "a global option: put it before the command",
                 "richi help config show"),
                (["config", "set", "dev", "maybe"], "dev must be true or false", "richi help config set"),
                (["config", "set", "dev", "ture"], "dev must be true or false. Did you mean 'true'?",
                 "richi help config set"),
                (["-w", "work", "update", "check"], "omit --workspace", "richi help update"),
                (["--project-path", ".", "config", "show"], "--project-path applies to knowledge commands",
                 "richi help config"),
                (["-w", "work", "workspace", "list"], "Storage selectors apply to workspace current",
                 "richi help workspace"),
                (["--db"], "--db requires a value", "richi help"),
                (["--db", "", "recall", "q"], "--db must be a nonempty path", "richi help"),
                (["--config", "", "recall", "q"], "--config must be a nonempty path", "richi help"),
                (["map", "--port", "abc"], "--port must be an integer between 1 and 65535", "richi help map"),
                (["map", "--db", ""], "--db must be a nonempty path", "richi help map"),
                # --version names the selected runtime, so its selectors are checked too.
                (["-w", "Bad", "--version"], "Workspace name must match", "richi help workspace"),
                (["--db=", "--version"], "--db must be a nonempty path", "richi help"),
                (["--config=", "--version"], "--config must be a nonempty path", "richi help"),
                (["-w", "work", "--config", "/synthetic.json", "recall", "q"],
                 "--workspace and --config cannot be combined", "richi help"),
                (["-w", "work", "map", "--config", "/synthetic.json"], "--workspace and --config cannot be combined",
                 "richi help map"),
                (["-w", "work", "--config", "/synthetic.json", "--version"], "--workspace and --config cannot be combined",
                 "richi help"),
                # The option a command rejects is reported before its malformed value.
                (["-w", "Bad", "workspace", "list"], "Storage selectors apply to workspace current",
                 "richi help workspace"),
                (["help", "workspace", "list", "extra"], "'richi workspace list' has no subcommands",
                 "richi help workspace list"),
                (["help", "config", "sho"], "Did you mean 'show'?", "richi help config")):
            with self.subTest(argv=argv), mock.patch("sys.stdin", io.StringIO()):
                self.assert_error(argv, contains, target=target)

    def test_selector_paths_are_checked_only_for_their_form(self):
        # A well-formed path that cannot be resolved is not a usage error.
        for argv in (["--db", "~nosuch-richi-user/x.sqlite3", "workspace", "current"],
                     ["--config", "~nosuch-richi-user/x.json", "workspace", "current"],
                     ["map", "--config", "~nosuch-richi-user/x.json"],
                     ["config", "set", "development.source", "~nosuch-richi-user/src"]):
            with self.subTest(argv=argv):
                error = json.loads(self.launch(argv)[2])
                self.assertNotIn("code", error)
                self.assertIn("Invalid", error["error"])
        here = os.getcwd()
        gone = self.root / "gone"
        gone.mkdir()
        os.chdir(gone)
        try:
            gone.rmdir()
            code, _, stderr = self.launch(["--db", "x.sqlite3", "workspace", "current"])
        finally:
            os.chdir(here)
        self.assertEqual(code, 1)
        self.assertNotIn("code", json.loads(stderr))

    def test_config_mistakes_are_usage_errors_in_an_unbound_chat(self):
        os.environ["RICHI_CHAT_ID"] = "synthetic"
        for argv, contains, target in ((["config", "shwo"], "Did you mean 'show'?", "richi help config"),
                                       (["config", "set", "dev", "maybe"], "dev must be true or false",
                                        "richi help config set"),
                                       (["config", "set", "development.source", ""], "nonempty path",
                                        "richi help config set")):
            with self.subTest(argv=argv):
                self.assert_error(argv, contains, target=target)
        # A valid change still needs the chat's workspace; config show reads only settings.
        self.assert_error(["config", "set", "dev", "false"], "Choose a workspace", code_name="chat_workspace_required")
        code, _, stderr = self.launch(["config", "show"])
        self.assertEqual(code, 0, stderr)

    def test_help_prints_whatever_the_selectors_say(self):
        installed = object()
        for argv, arguments in ((["-w", "Bad", "help", "recall"], ["help", "recall"]),
                                (["--db", "", "recall", "--help"], ["recall", "--help"]),
                                (["-w", "work", "--config", "/synthetic.json", "help", "environment"],
                                 ["help", "environment"])):
            with self.subTest(argv=argv), mock.patch.object(cli, "installed_runtime", return_value=installed), \
                    mock.patch.object(cli, "runtime_command", return_value=["/synthetic/python"]) as command, \
                    mock.patch.object(cli.os, "execve", side_effect=SystemExit(0)):
                with self.assertRaises(SystemExit):
                    cli.main(argv)
                command.assert_called_once_with(installed, arguments)

    def test_terminal_help_hands_off_the_maps_of_a_new_generation(self):
        os.environ["RICHI_INSTALLATION_GENERATION"] = "synthetic-generation"
        # As any terminal command; richi map replaces its own map, and a chat
        # gets the installed release's help without a selection.
        for argv, chat, hands_off in ((["--version"], None, True), (["help", "recall"], None, True),
                                      (["map", "--help"], None, False), (["--version"], "synthetic", False)):
            with self.subTest(argv=argv, chat=chat), \
                    mock.patch.dict(os.environ, {"RICHI_CHAT_ID": chat} if chat else {}), \
                    mock.patch.object(cli, "resolve_settings", return_value=mock.Mock(port=8799)), \
                    mock.patch.object(cli, "resolve_runtime", return_value=object()), \
                    mock.patch.object(cli, "installed_runtime", return_value=object()), \
                    mock.patch.object(cli, "runtime_command", return_value=["/synthetic/python"]), \
                    mock.patch.object(cli, "_hand_off_maps") as hand_off, \
                    mock.patch.object(cli.os, "execve", side_effect=SystemExit(0)):
                with self.assertRaises(SystemExit):
                    cli.main(argv)
                self.assertEqual([call.args[0] for call in hand_off.call_args_list], [8799] if hands_off else [])

    def test_a_chat_never_changes_the_default_whatever_it_selects(self):
        os.environ["RICHI_CHAT_ID"] = "synthetic"
        # Python 3.12+ drops a '--' before the subcommand and runs it.
        for argv in (["use", "work"], ["-w", "work", "use", "work"], ["--db", "/synthetic.sqlite3", "workspace", "use", "work"],
                     ["--config", "/synthetic.json", "use"], ["--project-path", ".", "use", "work"],
                     ["workspace", "--", "use", "work"]):
            with self.subTest(argv=argv):
                self.assert_error(argv, "cannot change the global CLI default", code_name="chat_global_use_forbidden")
        self.assertEqual(cli._rejections("workspace", ["--", "current"]), cli._rejections("workspace", ["current"]))

    @unittest.skipUnless(sys.version_info >= (3, 12), "argparse drops a '--' before a subcommand since 3.12")
    def test_a_dash_before_a_subcommand_selects_like_the_subcommand_alone(self):
        self.assertEqual(self.launch(["workspace", "create", "work"])[0], 0)
        os.environ["RICHI_CHAT_ID"] = "synthetic"
        self.assertEqual(self.launch(["chat", "bind", "work"])[0], 0)
        # A bound chat and one that has not chosen a workspace yet.
        for chat, argv in (("synthetic", ["workspace", "current"]), ("synthetic", ["config", "show"]),
                           ("synthetic", ["-w", "work", "workspace", "current"]),
                           ("unbound", ["workspace", "current"]), ("unbound", ["config", "show"])):
            with self.subTest(chat=chat, argv=argv), mock.patch.dict(os.environ, RICHI_CHAT_ID=chat):
                position = argv.index(argv[-1])
                plain, dashed = self.launch(argv), self.launch(argv[:position] + ["--"] + argv[position:])
                self.assertEqual(plain[0], 0, plain)
                self.assertEqual(dashed, plain)

    def test_help_survives_a_malformed_chat_id(self):
        installed = object()
        for variables in ({"RICHI_CHAT_ID": ""}, {"CODEX_THREAD_ID": "bad id"}):
            with self.subTest(variables=variables), mock.patch.dict(os.environ, variables), \
                    mock.patch.object(cli, "installed_runtime", return_value=installed), \
                    mock.patch.object(cli, "runtime_command", return_value=["/synthetic/python"]) as command, \
                    mock.patch.object(cli.os, "execve", side_effect=SystemExit(0)):
                with self.assertRaises(SystemExit):
                    cli.main(["help", "environment"])
                command.assert_called_once_with(installed, ["help", "environment"])
                # Everything else, --version included, reports the ID.
                for argv in (["recall", "q"], ["--version"]):
                    self.assert_error(argv, "Chat ID must be", code_name="invalid_chat_id")

    def test_setup_and_agents_without_a_terminal_need_their_flags(self):
        opened = open

        def no_terminal(file, *args, **kwargs):
            if file == "/dev/tty":
                raise OSError(6, "Device not configured")
            return opened(file, *args, **kwargs)
        record = {"skill": "skill", "skill_target": "/synthetic/skill", "instructions": "instructions",
                  "command": "richi", "profiles": []}
        change = {"path": "/synthetic/AGENTS.md", "before": None, "after": "text"}
        with mock.patch("builtins.open", no_terminal), \
                mock.patch("richi_launcher.installation._context", return_value={"root": str(self.root)}), \
                mock.patch.object(agent_setup, "plan", return_value=(record, [change])), \
                mock.patch.object(agent_setup, "_records", return_value={}):
            for argv, target in ((["setup"], "richi help setup"), (["agents"], "richi help agents")):
                with self.subTest(argv=argv):
                    # stderr is one JSON object; only a terminal is shown the plan.
                    self.assert_error(argv, "terminal", target=target)

    def test_an_unbound_chat_still_gets_corrections_and_accurate_hints(self):
        os.environ["RICHI_CHAT_ID"] = "synthetic"
        self.assert_error(["workspce", "list"], "Did you mean 'workspace'?", target="richi help")
        # A knowledge command cannot be told from a runtime command before binding.
        self.assert_error(["recal", "cache"], "Choose a workspace", code_name="chat_workspace_required")
        error = self.assert_error(["use", "work"], "use chat bind NAME to choose", code_name="chat_global_use_forbidden")
        self.assertIsNone(error["workspace"])

    def test_terminal_help_uses_the_installed_release_when_selection_is_broken(self):
        installed, selected = object(), object()
        for settings, runtime in ((ConfigError("Unknown workspace"), installed), (None, selected)):
            with self.subTest(selection="broken" if runtime is installed else "valid"), \
                    mock.patch.object(cli, "resolve_settings", side_effect=settings) as resolve, \
                    mock.patch.object(cli, "resolve_runtime", return_value=selected), \
                    mock.patch.object(cli, "installed_runtime", return_value=installed), \
                    mock.patch.object(cli, "runtime_command", return_value=["/synthetic/python"]) as command, \
                    mock.patch.object(cli.os, "execve", side_effect=SystemExit(0)):
                with self.assertRaises(SystemExit):
                    cli.main(["-w", "work", "help", "environment"])
                resolve.assert_called_once_with(workspace="work")
                command.assert_called_once_with(runtime, ["help", "environment"])
        # The version names the selected runtime, so a broken selection is reported.
        with mock.patch.object(cli, "resolve_settings", side_effect=ConfigError("Unknown workspace")), \
                mock.patch.object(cli, "runtime_command", side_effect=AssertionError("must not run")):
            self.assertEqual(self.assert_error_text(["--version"]), "Unknown workspace")

    def assert_error_text(self, argv):
        code, stdout, stderr = self.launch(argv)
        self.assertEqual((code, stdout), (1, ""), argv)
        return json.loads(stderr)["error"]

    def test_pending_setup_is_recovered_for_ordinary_commands_only(self):
        root = self.root / "installation"
        root.mkdir()
        (root / "setup-pending.json").write_text("{}", encoding="utf-8")
        os.environ["RICHI_INSTALLATION_ROOT"] = str(root)
        for argv, recovers in ((["config", "show"], True), ([], False), (["help"], False),
                               (["recall", "--help"], False), (["--version"], False), (["help", "setup"], False),
                               (["setup", "--dry-run"], False), (["setup", "--status"], False),
                               (["agents", "--status"], False), (["setup", "--yes"], True), (["agents"], True)):
            with self.subTest(argv=argv), \
                    mock.patch.object(agent_setup, "recover_pending") as recover, \
                    mock.patch.object(agent_setup, "command", return_value={}), \
                    mock.patch.object(agent_setup, "agents_command", return_value={}), \
                    mock.patch.object(cli, "runtime_command", return_value=["/synthetic/python"]), \
                    mock.patch.object(cli.os, "execve", side_effect=SystemExit(0)):
                self.launch(argv)
                self.assertEqual(recover.call_count, 1 if recovers else 0)


if __name__ == "__main__":
    unittest.main()
