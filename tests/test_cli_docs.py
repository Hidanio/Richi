"""The CLI documentation validator: this checkout passes, and every check catches its violation.

Each test changes a copy of the checkout, mostly with synthetic commands, and
looks only at the problems its change adds: a problem in the live
documentation fails test_this_checkout_is_documented alone.
"""
from contextlib import redirect_stdout
import dataclasses
import importlib.util
import io
from pathlib import Path
import shutil
import tempfile
import textwrap
import unittest
from unittest import mock

from richi import cli_help
from richi_launcher import help_format

ROOT = Path(__file__).resolve().parents[1]
REFERENCES = help_format.REFERENCES
_spec = importlib.util.spec_from_file_location("richi_check_cli_docs", ROOT / "scripts" / "check_cli_docs.py")
validator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validator)
_BASELINE = []


def baseline():
    """Problems of the unchanged checkout."""
    if not _BASELINE:
        _BASELINE.append(frozenset(validator.check(validator.current())))
    return _BASELINE[0]


def setUpModule():
    # Before any test patches a module table, so a test run alone compares
    # with the same baseline.
    baseline()


class Checkout:
    """Fresh parsers and tables of the packages and a copy of the Markdown files."""

    def __init__(self, test):
        directory = tempfile.TemporaryDirectory(prefix="richi-docs-")
        test.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        sources = validator.current()
        for source in validator.markdown_files(ROOT, sources):
            target = self.root / source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        self.sources = dataclasses.replace(sources, root=self.root)

    def problems(self):
        """The problems this test's changes add."""
        known = baseline()
        return [problem for problem in validator.check(self.sources) if problem not in known]

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def append(self, relative, text):
        path = self.root / relative
        path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")

    def page(self, *path):
        return help_format.find(self.sources.runtime, path, None)

    def replace(self, parser, **fields):
        parser._help_page = help_format.page_parts(parser)._replace(**fields)

    def add_command(self, name, guide=None, uncataloged=True):
        """A fully documented synthetic runtime command, grouped and, unless
        asked otherwise, excluded from the catalog."""
        guide = guide or name.upper() + ".md"
        commands = help_format.subcommands(self.sources.runtime)
        added = commands.add_parser(name, help="Frobnicate the store")
        added.add_argument("--level", help="How much")
        help_format.document(added, "Frobnicate the store.", examples=(("", "richi %s --level 2" % name),),
                             guide=guide)
        self.write(help_format.guide_path(guide), "```sh\nrichi %s --level 2\n```\n" % name)
        self.write(ru(help_format.guide_path(guide)), "Запуск: `richi %s`.\n" % name)
        self.sources.groups += (("other", (name,)),)
        if uncataloged:
            self.sources.uncataloged[name] = "Example command for this test."
        return added

    def add_group(self, name, children=("one", "two"), guide=None):
        """A documented synthetic group of leaf subcommands, grouped but not cataloged."""
        guide = guide or name.upper() + ".md"
        group = help_format.subcommands(self.sources.runtime).add_parser(name, help="Group the store")
        actions = group.add_subparsers(dest="action", required=True)
        help_format.document(group, "Group the store.", examples=(("", "richi %s %s" % (name, children[0])),),
                             guide=guide)
        lines = ["richi %s %s" % (name, child) for child in children]
        for child, line in zip(children, lines):
            help_format.document(actions.add_parser(child, help="Run " + child), examples=(("", line),))
        help_format.complete(group)
        self.write(help_format.guide_path(guide), "```sh\n" + "\n".join(lines) + "\n```\n")
        self.write(ru(help_format.guide_path(guide)), "".join("Запуск: `%s`.\n" % line for line in lines))
        self.sources.groups += (("other", (name,)),)
        return group

    def guide(self, *path):
        """Repository path of the guide that a page links."""
        return help_format.guide_path(help_format.page_parts(self.page(*path)).guide)


def about(problems, subject):
    return {(problem.rule, problem.message) for problem in problems if problem.subject == subject}


def ru(path):
    return path[:-len(".md")] + ".ru.md"


class CheckoutTests(unittest.TestCase):
    def test_this_checkout_is_documented(self):
        output = io.StringIO()
        with redirect_stdout(output):
            status = validator.main([])
        self.assertEqual(status, 0, output.getvalue())
        self.assertIn("; OK", output.getvalue())

    def test_problems_are_printed_with_how_to_fix_them(self):
        checkout = Checkout(self)
        checkout.add_command("frobnicate")
        checkout.write(REFERENCES + "FROBNICATE.ru.md", "")
        output = io.StringIO()
        with mock.patch.object(validator, "current", return_value=checkout.sources), redirect_stdout(output):
            self.assertEqual(validator.main([]), 1)
        text = output.getvalue()
        self.assertIn("[mention] richi frobnicate: " + REFERENCES + "FROBNICATE.ru.md never shows it in code", text)
        self.assertIn("How to fix:", text)


class NewCommandTests(unittest.TestCase):
    def test_an_undocumented_command_fails_every_applicable_check(self):
        checkout = Checkout(self)
        added = help_format.subcommands(checkout.sources.runtime).add_parser("frobnicate")
        added.add_argument("--level")
        self.assertEqual(about(checkout.problems(), "richi frobnicate"), {
            ("help", "has no help= summary for command lists"), ("help", "has no description"),
            ("help", "--level has no help text"), ("examples", "no example runs this command"),
            ("guide", "page links no guide"), ("overview", "appears in 0 groups of richi help"),
            ("catalog", "is neither in capabilities.COMMANDS nor in capabilities.UNCATALOGED")})

    def test_a_fully_documented_command_passes(self):
        checkout = Checkout(self)
        checkout.add_command("frobnicate")
        checkout.add_group("frobs")
        checkout.sources.uncataloged["frobs"] = "Example group for this test."
        self.assertEqual([], checkout.problems())

    def test_a_new_option_needs_help(self):
        checkout = Checkout(self)
        checkout.add_command("frobnicate").add_argument("--fuzzy", action="store_true")
        self.assertEqual(about(checkout.problems(), "richi frobnicate"), {("help", "--fuzzy has no help text")})

    def test_global_options_need_help(self):
        checkout = Checkout(self)
        checkout.sources.runtime.add_argument("--frob-global", metavar="X")
        self.assertIn(("help", "global option --frob-global has no help text"), about(checkout.problems(), "richi"))

    def test_a_new_launcher_subcommand_needs_only_its_documentation(self):
        checkout = Checkout(self)
        name = next(name for name, parser in sorted(checkout.sources.launcher.items())
                    if help_format.subcommands(parser) is not None)
        group = checkout.sources.launcher[name]
        added = help_format.subcommands(group).add_parser("frob", help="Frobnicate")
        help_format.document(added, examples=(("", "richi %s frob" % name),), guide="FROB.md")
        help_format.complete(group)
        checkout.sources.uncataloged[name + " frob"] = "Example subcommand for this test."
        guide = help_format.guide_path("FROB.md")
        checkout.write(guide, "Nothing yet.\n")
        checkout.write(ru(guide), "Пока пусто.\n")
        self.assertEqual(about(checkout.problems(), "richi %s frob" % name), {
            ("mention", guide + " never shows it in code"), ("mention", ru(guide) + " never shows it in code")})
        checkout.append(guide, "Run `richi %s frob`.\n" % name)
        checkout.append(ru(guide), "Запуск: `richi %s frob`.\n" % name)
        self.assertEqual(about(checkout.problems(), "richi %s frob" % name), set())

    def test_guides_outside_the_documentation_folders_are_checked(self):
        for guide in ("docs/guides/FROB.md", "manual/FROB.md"):
            for page in (guide, ru(guide)):
                with self.subTest(page=page):
                    checkout = Checkout(self)
                    checkout.add_command("frobnicate", guide=guide)
                    self.assertEqual(checkout.problems(), [])
                    checkout.append(page, "```sh\nrichi recal cache\n```\n")
                    messages = about(checkout.problems(), page)
                    self.assertEqual(len(messages), 1, messages)
                    self.assertIn("`richi recal cache`: 'recal' is not a", next(iter(messages))[1])

    def test_every_page_under_the_documentation_folders_is_checked(self):
        for page in ("docs/notes/OPS.md", "evals/notes/OPS.md", "skills/frob/NOTES.md"):
            with self.subTest(page=page):
                checkout = Checkout(self)
                checkout.write(page, "```sh\nrichi recal cache\n```\n")
                checkout.write(ru(page), "Пусто.\n")
                self.assertEqual({rule for rule, _ in about(checkout.problems(), page)}, {"docs"})


class PageTests(unittest.TestCase):
    def test_examples_must_parse_and_run_the_command(self):
        checkout = Checkout(self)
        added = checkout.add_command("frobnicate")
        checkout.replace(added, examples=(("", "richi frobnicate --everything"), ("", "richi help")))
        problems = about(checkout.problems(), "richi frobnicate")
        self.assertIn(("examples", "no example runs this command"), problems)
        self.assertTrue(any(rule == "examples" and "--everything" in message for rule, message in problems))

    def test_a_help_request_does_not_run_the_command_and_leaves_pages_intact(self):
        checkout = Checkout(self)
        checkout.replace(checkout.add_command("frobnicate"), examples=(("", "richi frobnicate --help"),))
        problems = checkout.problems()
        self.assertEqual(about(problems, "richi frobnicate"), {("examples", "no example runs this command")})
        self.assertEqual(problems, checkout.problems())

    def test_every_page_must_render(self):
        checkout = Checkout(self)
        # Python 3.14 already rejects such help in add_argument.
        checkout.add_command("frobnicate").add_argument("--share", help="Share").help = "Up to 100% of the results"
        problems = about(checkout.problems(), "richi frobnicate")
        self.assertEqual({rule for rule, _ in problems}, {"help"})
        self.assertTrue(all(message.startswith("page fails to render: ") for _, message in problems), problems)

    def test_runtime_stubs_root_pages_and_topics_must_render(self):
        checkout = Checkout(self)
        stub = next(iter(checkout.sources.stubs))
        broken = help_format.subcommands(checkout.sources.runtime).choices[stub]
        checkout.replace(broken, about=1)
        checkout.sources.topics["broken"] = ("Broken", None)
        with mock.patch.object(cli_help, "EXAMPLES", ((None, None),)), \
                mock.patch.object(cli_help, "OVERVIEW_COMMANDS", (None,)):
            problems = checkout.problems()
        for subject in ("richi %s (runtime)" % stub, "richi help", "richi", "richi help broken"):
            with self.subTest(subject=subject):
                self.assertTrue(any(rule == "help" and message.startswith("page fails to render: ")
                                    for rule, message in about(problems, subject)), about(problems, subject))

    def test_help_reaches_every_page_and_topic(self):
        # richi help reads `topics` and the topic names before command names.
        checkout = Checkout(self)
        entry = next(iter(checkout.sources.topics.values()))
        checkout.sources.topics.update(frobnicate=entry, topics=entry)
        checkout.add_command("frobnicate")
        checkout.add_command("topics")
        problems = checkout.problems()
        self.assertIn(("help", "richi help frobnicate shows a help topic instead of this page"),
                      about(problems, "richi frobnicate"))
        self.assertIn(("help", "richi help topics shows the list of help topics instead of this page"),
                      about(problems, "richi topics"))
        self.assertIn(("help", "the list of help topics hides this topic"), about(problems, "richi help topics"))

    def test_topics_are_pairs_of_a_summary_and_a_text(self):
        checkout = Checkout(self)
        summary, text = next(iter(checkout.sources.topics.values()))
        checkout.sources.topics.update(listed=[summary, text], tripled=(summary, text, ""), joined=summary + text,
                                       encoded=(summary.encode(), text))
        problems = checkout.problems()
        for name in ("listed", "tripled", "joined", "encoded"):
            with self.subTest(name=name):
                self.assertIn(("help", "the TOPICS entry is not a (summary, text) pair of strings"),
                              about(problems, "richi help " + name))

    def test_page_checks_name_each_violation(self):
        checkout = Checkout(self)
        moved, undescribed = checkout.add_command("frob-a"), checkout.add_command("frob-b")
        moved.prog = "richi other"
        checkout.replace(undescribed, summary=None)
        undescribed.description = None
        workspace = checkout.sources.launcher["workspace"]
        listing = help_format.subcommands(workspace)
        help_format.document(listing.add_parser("prune"), "Remove empty workspaces.",
                             examples=(("", "richi workspace prune"),))
        tidy = listing.add_parser("tidy", help="Tidy workspaces")
        help_format.document(tidy, "Tidy workspaces.", examples=(("", "richi workspace tidy"),))
        tidy.prog = "richi other"
        help_format.document(listing.add_parser("bare"), examples=(("", "richi workspace bare"),))
        problems = checkout.problems()
        self.assertIn(("help", "prints usage as 'richi other'"), about(problems, "richi frob-a"))
        self.assertIn(("help", "has no description"), about(problems, "richi frob-b"))
        self.assertIn(("help", "has no help= summary for command lists"), about(problems, "richi workspace prune"))
        self.assertIn(("help", "prints usage as 'richi other'"), about(problems, "richi workspace tidy"))
        self.assertIn(("help", "has no description"), about(problems, "richi workspace bare"))

    def test_a_page_needs_a_guide_in_this_repository(self):
        checkout = Checkout(self)
        parser = checkout.add_command("frobnicate")
        checkout.replace(parser, guide="https://example.com/frob")
        self.assertIn(("guide", "guide https://example.com/frob is not a file in this repository"),
                      about(checkout.problems(), "richi frobnicate"))
        checkout.replace(parser, guide=None)
        self.assertIn(("guide", "page links no guide"), about(checkout.problems(), "richi frobnicate"))

    def test_details_see_also_and_help_references_name_real_pages(self):
        checkout = Checkout(self)
        checkout.replace(checkout.add_command("frobnicate"), see=("help", "toolz"),
                         about="Compare with richi help toolz. People prefer richi help to browse.")
        details = checkout.sources.details
        key = next(iter(details))
        details["frob frob"] = {}
        details["config show"] = {}
        details[key] = dict(details[key], exampels=())
        problems = checkout.problems()
        self.assertEqual(about(problems, "richi frobnicate"), {
            ("links", "see also names unknown page 'toolz'"),
            ("links", "help text refers to richi help toolz: 'richi help toolz' names no command or help topic")})
        self.assertEqual(about(problems, "DETAILS frob frob"), {("links", "is not a runtime command page")})
        self.assertEqual(about(problems, "DETAILS config show"), {("links", "is not a runtime command page")})
        self.assertEqual(about(problems, "DETAILS " + key),
                         {("links", "has unknown field 'exampels' (expected about, examples, guide, see)")})

    def test_help_references_in_topics_global_options_and_arguments_name_real_pages(self):
        checkout = Checkout(self)
        name = next(iter(checkout.sources.topics))
        summary, body = checkout.sources.topics[name]
        checkout.sources.topics[name] = (summary, body + "\nAlso read richi help recrds first.\n")
        option = next(action for action in checkout.sources.runtime._actions if action.option_strings)
        option.help = "See richi help dbs"
        checkout.add_command("frobnicate").add_argument("--mode", help="See richi help modez")
        problems = checkout.problems()
        for subject, word in (("richi help " + name, "recrds"), ("richi", "dbs"), ("richi frobnicate", "modez")):
            with self.subTest(subject=subject):
                self.assertIn(("links", "help text refers to richi help %s: 'richi help %s' names no command or "
                                        "help topic" % (word, word)), about(problems, subject))

    def test_shared_option_tables_name_existing_options(self):
        checkout = Checkout(self)
        checkout.sources.metavars["--ghost"] = "X"
        checkout.sources.common_help["--phantom"] = "Phantom"
        problems = checkout.problems()
        self.assertEqual(about(problems, "cli_help.METAVARS"),
                         {("help", "names --ghost, which no runtime command has")})
        self.assertEqual(about(problems, "cli_help.COMMON_HELP"),
                         {("help", "names --phantom, which no runtime command has")})

    def test_overview_groups_list_each_command_once(self):
        checkout = Checkout(self)
        checkout.add_command("frob-a")
        checkout.add_command("frob-b")
        groups = [(title, tuple(name for name in names if name != "frob-a")) for title, names in checkout.sources.groups]
        groups[0] = (groups[0][0], groups[0][1] + ("frob-b", "ghost"))
        checkout.sources.groups = tuple(groups)
        problems = checkout.problems()
        self.assertEqual(about(problems, "richi frob-a"), {("overview", "appears in 0 groups of richi help")})
        self.assertEqual(about(problems, "richi frob-b"), {("overview", "appears in 2 groups of richi help")})
        self.assertEqual(about(problems, "richi ghost"), {("overview", "is grouped but is not a command")})

    def test_root_examples_overview_and_topic_lines_must_parse(self):
        checkout = Checkout(self)
        checkout.sources.examples += (("Stale", "richi recall q --wehre x"),)
        checkout.sources.overview += ("richi recal", "richi recal --help")
        name = next(iter(checkout.sources.topics))
        summary, text = checkout.sources.topics[name]
        checkout.sources.topics[name] = (summary, textwrap.dedent(text) + "\n\n  richi recall q --wehre x\n")
        problems = checkout.problems()
        for subject, count in (("richi help", 1), ("richi", 2), ("richi help " + name, 1)):
            with self.subTest(subject=subject):
                self.assertEqual([rule for rule, _ in about(problems, subject)], ["examples"] * count)

    def test_the_overview_help_example_must_parse(self):
        with mock.patch.object(cli_help, "HELP_EXAMPLE", "richi recal --help"):
            checkout = Checkout(self)
        self.assertTrue(any(rule == "examples" and "`richi recal --help` fails" in message
                            for rule, message in about(checkout.problems(), "richi")))

    def test_the_help_guide_shows_every_topic_as_a_page(self):
        guide = "docs/HELP_TEST.md"

        def checkout(english, russian, topic=True):
            """A checkout whose `richi help` links GUIDE: it shows the topics
            there are, then ENGLISH or RUSSIAN, and topic adds `frobbing`."""
            checkout = Checkout(self)
            shown = "```sh\nrichi help\n" + "".join("richi help %s\n" % name for name in checkout.sources.topics)
            if topic:
                checkout.sources.topics["frobbing"] = ("Frobbing", "Frobbing\n")
            checkout.replace(checkout.page("help"), guide=guide)
            checkout.write(guide, shown + "```\n" + english)
            checkout.write(ru(guide), shown + "```\n" + russian)
            return checkout.problems()

        self.assertEqual(about(checkout("", ""), "richi help frobbing"), {
            ("mention", guide + " never shows it in code"), ("mention", ru(guide) + " never shows it in code")})
        self.assertEqual(about(checkout("Read `richi help\nfrobbing` first.\n", "Сначала `richi help frobbing`.\n"),
                               "richi help frobbing"), set())
        # A bare topic name is not a page.
        self.assertEqual(about(checkout("Read `frobbing` first.\n", "Сначала `richi help frobbing`.\n"),
                               "richi help frobbing"), {("mention", guide + " never shows it in code")})
        # A removed topic leaves a stale page name.
        self.assertTrue(any("'richi help frobbing' names no command or help topic" in message
                            for _, message in about(checkout("Read `richi help frobbing` first.\n", "", topic=False),
                                                    guide)))


class CatalogTests(unittest.TestCase):
    def test_every_command_needs_one_catalog_decision(self):
        checkout = Checkout(self)
        checkout.add_command("frobnicate", uncataloged=False)
        checkout.add_command("frob-a")
        checkout.sources.catalog += (("frob-a", "maintenance", "read", "Frob.", "FROB-A.md"),)
        checkout.add_command("frob-b")
        excluded = checkout.sources.uncataloged
        excluded.update({"ghost": "Stale.", "frob-b": " "})
        problems = checkout.problems()
        self.assertEqual(about(problems, "richi frobnicate"),
                         {("catalog", "is neither in capabilities.COMMANDS nor in capabilities.UNCATALOGED")})
        self.assertEqual(about(problems, "richi frob-a"), {("catalog", "is both cataloged and excluded as 'frob-a'")})
        self.assertEqual(about(problems, "UNCATALOGED ghost"), {("catalog", "names no command")})
        self.assertEqual(about(problems, "UNCATALOGED frob-b"), {("catalog", "gives no reason")})

    def test_a_group_exclusion_covers_its_subcommands(self):
        checkout = Checkout(self)
        checkout.add_group("grp", guide="GRP.md")
        checkout.sources.catalog += (("grp one", "maintenance", "read", "One.", "GRP.md"),)
        checkout.sources.uncataloged.update({"grp two": "Excluded.", "grp": "Whole group."})
        problems = checkout.problems()
        self.assertEqual(about(problems, "richi grp one"), {("catalog", "is both cataloged and excluded as 'grp'")})
        self.assertNotIn("richi grp two", {problem.subject for problem in problems})

    def test_catalog_entries_name_commands_and_their_page_guides(self):
        checkout = Checkout(self)
        for name in ("frob-empty", "frob-path", "frob-other", "frob-missing"):
            checkout.add_command(name, uncataloged=False)
        checkout.write(REFERENCES + "FROB-EMPTY.md", "Nothing here.\n")
        checkout.write(REFERENCES + "FROB-EMPTY.ru.md", "Здесь пусто.\n")
        checkout.write(REFERENCES + "OTHER.md", "Run `richi frob-other`.\n")
        checkout.write(REFERENCES + "OTHER.ru.md", "Запуск: `richi frob-other`.\n")
        checkout.add_group("grp")
        checkout.sources.uncataloged["grp"] = "Example group for this test."
        checkout.sources.catalog += tuple((name, "maintenance", "read", "Frob.", guide) for name, guide in (
            ("grp sum", "GRP.md"), ("grp", "GRP.md"), ("frob-empty", "FROB-EMPTY.md"),
            ("frob-path", "docs/CLI.md"), ("frob-other", "OTHER.md"), ("frob-missing", "MISSING.md")))
        problems = checkout.problems()
        self.assertEqual(about(problems, "capabilities grp sum"), {("catalog", "names no command")})
        self.assertEqual(about(problems, "capabilities grp"), {("catalog", "names no command")})
        self.assertEqual(about(problems, "capabilities frob-empty"), {
            ("mention", REFERENCES + "FROB-EMPTY.md never shows it in code"),
            ("mention", REFERENCES + "FROB-EMPTY.ru.md never shows it in code")})
        self.assertEqual(about(problems, "capabilities frob-path"), {
            ("catalog", "guide docs/CLI.md must be NAME.md under %s, as capabilities describe prints "
                        "references/NAME" % REFERENCES)})
        self.assertEqual(about(problems, "capabilities frob-other"), {
            ("catalog", "the catalog names guide OTHER.md, but its help page links FROB-OTHER.md")})
        self.assertEqual(about(problems, "capabilities frob-missing"), {
            ("catalog", "the catalog names guide MISSING.md, but its help page links FROB-MISSING.md"),
            ("guide", "guide " + REFERENCES + "MISSING.md does not exist"),
            ("guide", "guide " + REFERENCES + "MISSING.ru.md does not exist")})


class MirrorTests(unittest.TestCase):
    def test_the_runtime_lists_the_launcher_commands_with_their_summaries(self):
        checkout = Checkout(self)
        stubs = checkout.sources.stubs
        changed, removed = sorted(stubs)[:2]
        stubs[changed] = "Report something"
        del stubs[removed]
        stubs["ghost"] = "Haunt the store"
        pin = checkout.add_command("pin")
        checkout.sources.launcher["pin"] = pin
        checkout.sources.launcher_commands |= {"pin"}
        stubs["pin"] = "Frobnicate the store"
        help_format.subcommands(checkout.sources.runtime).choices.pop("pin")
        problems = checkout.problems()
        page = help_format.page_parts(checkout.sources.launcher[changed]).summary
        self.assertEqual(about(problems, "richi " + changed), {
            ("mirror", "the runtime lists it as 'Report something.', its page says '%s'" % page)})
        self.assertIn(("mirror", "is missing from richi.cli_parser.LAUNCHER_COMMANDS"), about(problems, "richi " + removed))
        self.assertIn(("mirror", "is listed by the runtime but is not a launcher command"), about(problems, "richi ghost"))
        self.assertIn(("mirror", "is not declared by the runtime parser"), about(problems, "richi pin"))

    def test_launcher_and_runtime_have_the_same_global_options(self):
        checkout = Checkout(self)
        options = checkout.sources.global_options
        removed = next(option for option in sorted(options) if option not in checkout.sources.global_flags)
        del options[removed]
        options["--quiet"] = "quiet"
        flag = sorted(checkout.sources.global_flags)[0]
        value = next(option for option in sorted(options) if option.startswith("--") and option != "--quiet"
                     and option not in checkout.sources.global_flags)
        checkout.sources.global_flags = (checkout.sources.global_flags - {flag}) | {value, "--verbose"}
        self.assertEqual(about(checkout.problems(), "richi"), {
            ("mirror", "the launcher does not consume global option %s" % removed),
            ("mirror", "the runtime does not declare global option --quiet"),
            ("mirror", "global option %s takes no value in the runtime but a value in the launcher" % flag),
            ("mirror", "global option %s takes a value in the runtime but none in the launcher" % value),
            ("mirror", "GLOBAL_FLAGS names --verbose, which GLOBAL_OPTIONS lacks")})

    def test_no_runtime_command_looks_like_a_mistyped_launcher_command(self):
        checkout = Checkout(self)
        name = sorted(checkout.sources.launcher_commands)[0] + "s"
        checkout.add_command(name)
        self.assertEqual(about(checkout.problems(), "richi " + name), {
            ("mirror", "looks like launcher command '%s'; an unbound chat would report it as a mistyped one"
             % name[:-1])})

    def test_launcher_commands_and_parsers_agree(self):
        checkout = Checkout(self)
        checkout.sources.launcher_commands = checkout.sources.launcher_commands | {"pin"}
        self.assertEqual(about(checkout.problems(), "LAUNCHER_COMMANDS"),
                         {("mirror", "differs from launcher_parsers(): pin")})


class MarkdownTests(unittest.TestCase):
    def setUp(self):
        self.sources = validator.current()

    def synthetic(self):
        """Sources with `frobs one REF [--json FILE] [--view {full,compact}]`,
        `frobs two` and `frobs deep on|off`."""
        checkout = Checkout(self)
        actions = help_format.subcommands(checkout.add_group("frobs"))
        one = actions.choices["one"]
        one.add_argument("ref", help="Record")
        one.add_argument("--json", metavar="FILE", help="Input")
        one.add_argument("--view", choices=("full", "compact"), help="Layout")
        modes = actions.add_parser("deep", help="Go deeper").add_subparsers(dest="mode", required=True)
        for name in ("on", "off"):
            modes.add_parser(name, help="Turn it " + name)
        return checkout.sources

    def test_guides_must_show_their_commands_in_both_languages(self):
        checkout = Checkout(self)
        checkout.add_group("grp", guide="GRP.md")
        checkout.sources.uncataloged["grp"] = "Example group for this test."
        checkout.write(REFERENCES + "GRP.ru.md", "Группа команд: grp one.\n")
        problems = checkout.problems()
        for command in ("richi grp", "richi grp one", "richi grp two"):
            self.assertIn(("mention", REFERENCES + "GRP.ru.md never shows it in code"), about(problems, command))
            self.assertNotIn(("mention", REFERENCES + "GRP.md never shows it in code"), about(problems, command))

    def test_subcommands_may_be_named_without_richi_but_top_commands_may_not(self):
        shown = validator.shown("Use `tools get ID`, then `check`.\n```sh\nrichi -w work status\n```\n", self.sources)
        self.assertIn(("tools", "get"), shown)
        self.assertIn(("status",), shown)
        self.assertNotIn(("check",), shown)
        shown = validator.shown("Read with `sources show/history --ref REF` or `richi sources diff/commit`.",
                                self.sources)
        for path in (("sources", "show"), ("sources", "history"), ("sources", "diff"), ("sources", "commit")):
            self.assertIn(path, shown)

    def test_code_is_read_the_way_markdown_reads_it(self):
        text = ("Run `richi recall\n--nope q` and `check`.\n\n"
                "~~~sh\n```\nrichi status\n~~~\n\n"
                "```text\nrichi output only\n```\n\n"
                "````markdown\nAsk with `richi chat current`.\n```sh\nrichi chat bind --current\n```\n````\n")
        self.assertEqual(validator.code(text), [
            ("inline", "richi recall --nope q", 1), ("inline", "check", 2), ("block", "```", 5),
            ("block", "richi status", 6), ("inline", "richi chat current", 14), ("block", "richi chat bind --current", 16)])

    def test_indented_code_and_list_items_are_read_as_code(self):
        text = ("- Step:\n\n    ```sh\n    richi recal cache\n    ```\n\n"
                "1. Run:\n\n       richi status --limt 5\n\n"
                "Text that\n    richi continues the paragraph\n\n"
                "    richi indented code\n\n"
                "- An item with `a\nlazy` span\n"
                "  - nested\n\n      ~~~bash\n      richi nested --x\n      ~~~\n")
        self.assertEqual(validator.code(text), [
            ("block", "richi recal cache", 4), ("block", "richi status --limt 5", 9), ("block", "", 10),
            ("block", "richi indented code", 14), ("block", "", 15), ("inline", "a lazy", 16),
            ("block", "richi nested --x", 21)])

    def test_quotes_breaks_and_paragraph_rules_are_read_the_way_markdown_reads_them(self):
        for text, expected in (
                # A lazy line continues the paragraph of its list item.
                ("- Item\nlazy\n\n    richi recal cache\n", []),
                # A heading ends a paragraph, and code may follow it at once.
                ("# Commands\n    richi recal cache\n", [("block", "richi recal cache", 2)]),
                ("Some `text\n# H\nmore` text\n", []),
                ("Some `text\n===\nmore` text\n", []),
                # A line outside a list item ends the code block it holds.
                ("- Step:\n  ```sh\n  richi status\n- Next\n\nRun `richi recal`.\n",
                 [("block", "richi status", 3), ("inline", "richi recal", 6)]),
                # A backtick fence has no backtick in its info string.
                ("```richi recall --nope q``` is wrong.\n\n```sh\nrichi recal x\n```\n",
                 [("inline", "richi recall --nope q", 1), ("block", "richi recal x", 4)]),
                # Block quotes hold code blocks, GitHub alerts among them.
                ("> [!NOTE]\n> ```sh\n> richi recall --nope z\n> ```\n", [("block", "richi recall --nope z", 3)]),
                ("> [!TIP]\n>\n>     richi recal cache\n", [("block", "richi recal cache", 3)]),
                ("> Run `richi recall\n> --nope z` to see.\n", [("inline", "richi recall --nope z", 1)]),
                ("> Run `richi recall\n--nope z` lazily.\n", [("inline", "richi recall --nope z", 1)]),
                ("> ~~~text\n> Write `richi recal cache` in a guide.\n> ~~~\n", []),
                ("> A quote.\n\n    richi indented\n", [("block", "richi indented", 3)]),
                ("- item\n  > ```sh\n  > richi quoted\n  > ```\n", [("block", "richi quoted", 3)]),
                ("- item\n> ```sh\n> richi quoted\n> ```\n", [("block", "richi quoted", 3)]),
                # Thematic breaks are not list items.
                ("* * *\n\n    richi recal cache\n", [("block", "richi recal cache", 3)]),
                ("- - -\n    richi recal cache\n", [("block", "richi recal cache", 2)]),
                # Only a bullet with text or an ordered item from 1 interrupts a paragraph.
                ("Then step\n2. Check:\n\n    richi recal cache\n", [("block", "richi recal cache", 4)]),
                ("Use `richi\n2. p1` here\n", [("inline", "richi 2. p1", 1)]),
                ("- foo\n2. bar `richi x`\n", [("inline", "richi x", 2)]),
                ("- a `richi\n2. b` c\n", []),
                # A fence's own indentation is not part of its content.
                ("- item\n\n    ```markdown\n      Use `richi x9`\n    ```\n", [("inline", "richi x9", 4)]),
                ("  ```markdown\n    richi is a small tool\n  ```\n", []),
                # An escaped backtick opens no span.
                ("Escaped \\`richi n4\\` not a span, `richi n5` is.\n", [("inline", "richi n5", 1)]),
                # A span ends at a backtick run of its own length only.
                ("`` richi a ``` b ``\n", [("inline", "richi a ``` b", 1)]),
                # A '>' alone opens a quote without a paragraph.
                (">\n>     richi recal q1\n", [("block", "richi recal q1", 2)]),
                (">\n    richi recal q2\n", [("block", "richi recal q2", 2)]),
                (">\n    `richi recal q3`\n", [("block", "`richi recal q3`", 2)]),
                ("- step\n\n  >\n  >     richi recal q4\n", [("block", "richi recal q4", 4)]),
                # A quote marker is indented at most three spaces.
                (">     richi a\n    >     richi recal b\n", [("block", "richi a", 1), ("block", ">     richi recal b", 2)]),
                # A list item can begin with one blank line; a second one ends it empty.
                ("-\n\n    richi recal e1\n", [("block", "richi recal e1", 3)]),
                ("1.\n\n     richi recal e2\n", [("block", "richi recal e2", 3)]),
                ("2.\n\n    ``2``\n", [("block", "``2``", 3)]),
                ("> -\n>\n>     richi recal e3\n", [("block", "richi recal e3", 3)]),
                ("-\n    richi is the text of the item\n", []),
                (">  \n>     richi recal q5\n", [("block", "richi recal q5", 2)]),
                ("-     \n\n    richi recal e5\n", [("block", "richi recal e5", 3)]),
                ("-\n\n- a\n\n    richi x8\n", []),
                ("1.\n\n1. Step\n\n    richi x9\n", []),
                ("-\n  `richi x1`\n\n    richi x2\n", [("inline", "richi x1", 2)]),
                ("-\n- `a`\n\n    richi c\n", [("inline", "a", 2)]),
                ("-\n\n      richi recal e4\n", [("block", "richi recal e4", 3)]),
                # After a marker, five spaces or more start code one space in; tabs count to columns of four.
                ("-     richi recal w\n", [("block", "richi recal w", 1)]),
                ("-\t\trichi recal t\n", [("block", "richi recal t", 1)]),
                # Breaks, headings and fences interrupt a lazy paragraph, unless indented four spaces.
                ("> para\n***\n    richi recal t\n", [("block", "richi recal t", 3)]),
                ("> para\n# H\n    richi recal h\n", [("block", "richi recal h", 3)]),
                ("- item\n```sh\nrichi recal f\n```\n", [("block", "richi recal f", 3)]),
                ("1.   a `richi\n    - b` c\n", [("inline", "richi - b", 1)]),
                # An empty bullet does not interrupt a paragraph.
                ("Para `richi\n*\nx` y\n", [("inline", "richi * x", 1)]),
                # A setext underline ends a paragraph of several lines.
                ("Some `text\nmore\n===\nx` y\n", []),
                # A fence closes only with a run at least as long as its own.
                ("````sh\nrichi a\n```\nrichi recal b\n````\n",
                 [("block", "richi a", 2), ("block", "```", 3), ("block", "richi recal b", 4)])):
            with self.subTest(text=text):
                self.assertEqual(validator.code(text), expected)

    def test_stale_command_lines_fail_with_their_line(self):
        checkout = Checkout(self)
        text = ("Intro.\n\n```sh\nrichi recal cache\nrichi tools list \\\n  --limt 5\n```\n"
                "Run `richi recall --nope q` or `richi entry fetch`.\n\n- Then:\n\n      richi recal again\n")
        checkout.write("docs/EXTRA.md", text)
        checkout.write("docs/EXTRA.ru.md", "Пусто.\n")
        messages = sorted(message for _, message in about(checkout.problems(), "docs/EXTRA.md"))
        self.assertEqual(len(messages), 5, messages)
        self.assertTrue(any(message.startswith("line 4: `richi recal cache`: 'recal' is not a")
                            for message in messages), messages)
        self.assertTrue(any(message.startswith("line 5: ") and "--limt" in message for message in messages), messages)
        self.assertTrue(any("richi recall has no option --nope" in message for message in messages), messages)
        self.assertTrue(any("'fetch' is not a richi entry command" in message for message in messages), messages)
        self.assertTrue(any(message.startswith("line 12: `richi recal again`") for message in messages), messages)

    def test_dated_reports_keep_their_old_commands_but_need_translations(self):
        checkout = Checkout(self)
        checkout.write("docs/AUDIT-2030-01-01.md", "Ran `richi recall --old-flag q`.\n")
        checkout.write("docs/SAMPLE_EVAL.md", "Ran `richi recall --old-flag q`.\n")
        checkout.write("docs/SAMPLE_EVAL.ru.md", "Запуск `richi recall --old-flag q`.\n")
        problems = checkout.problems()
        self.assertEqual(about(problems, "docs/AUDIT-2030-01-01.md"), {("translation", "has no Russian page")})
        self.assertEqual(about(problems, "docs/SAMPLE_EVAL.md") | about(problems, "docs/SAMPLE_EVAL.ru.md"), set())

    def test_inline_spans_may_use_placeholders_and_omit_arguments(self):
        sources = self.synthetic()
        for text in ("richi frobs", "richi frobs one", "richi -w NAME ...", "richi --compact-json …",
                     "richi --chat", "richi --project-path", "richi -w",
                     "richi frobs one REF --json FILE", "richi frobs one -- -odd-ref", "richi help COMMAND",
                     "richi [GLOBAL OPTIONS] COMMAND [ARGS...]", 'richi capabilities describe "COMMAND ID"',
                     "richi frobs one REF --view VIEW", "richi frobs deep MODE"):
            with self.subTest(text=text):
                self.assertIsNone(validator.check_mention(text, sources))
        self.assertEqual(validator.check_mention("richi frbos one", sources), "'frbos' is not a richi command")

    def test_spans_without_richi_are_checked_when_they_name_a_command(self):
        sources = self.synthetic()
        for text in ("frobs one REF --view compact", "frobs one/two", "frobs deep on", "frobs one --json FILE",
                     "frobs = 'example'", "frobs", "status: ok", "kind IN ('decision')"):
            with self.subTest(text=text):
                self.assertIsNone(validator.check_mention(text, sources))
        for text, error in (("frobs one --nope", "richi frobs one has no option --nope"),
                            ("frobs one/bogus", "'one/bogus' is not a richi frobs command"),
                            ("frobs one/two --json FILE", "richi frobs two has no option --json"),
                            ("frobs deep onn", "'onn' is not a richi frobs deep command"),
                            ("frobs --json", "richi frobs has no option --json")):
            with self.subTest(text=text):
                self.assertEqual(validator.check_mention(text, sources), error)

    def test_inline_spans_need_global_options_first_and_valid_values(self):
        checkout = Checkout(self)
        checkout.add_command("frobnicate").add_argument("--view", choices=("full", "compact"), help="Layout")
        name = next(name for name, parser in sorted(checkout.sources.launcher.items())
                    if help_format.subcommands(parser) is not None)
        frob = help_format.subcommands(checkout.sources.launcher[name]).add_parser("frob", help="Frobnicate")
        frob.add_argument("mode", choices=("on", "off"), help="How")
        for text, error in (("richi --compact frobnicate", "richi has no global option --compact"),
                            ("richi frobnicate --level 2 --compact-json",
                             "global option --compact-json must come before the command"),
                            ("richi frobnicate --view compat",
                             "argument --view: invalid choice: 'compat' (choose from full, compact). Did you mean 'compact'?"),
                            ("richi %s frob onn" % name,
                             "argument mode: invalid choice: 'onn' (choose from on, off). Did you mean 'on'?")):
            with self.subTest(text=text):
                self.assertEqual(validator.check_mention(text, checkout.sources), error)

    def test_help_pages_and_catalog_entries_must_exist(self):
        pages = validator.pages(self.sources)
        topic = next(iter(self.sources.topics))
        group, child = next(path for path, page in pages.items() if len(path) == 2 and not page.launcher)
        leaf = next(path[0] for path, page in pages.items() if len(path) == 1 and validator._leaf(page, self.sources))
        outer, inner = next(path for path, page in pages.items() if len(path) == 2 and page.launcher)
        for line in ("richi help", "richi help topics", "richi help " + topic, "richi help %s %s" % (outer, inner),
                     "richi help %s %s" % (group, child), 'richi capabilities describe "%s"' % self.sources.catalog[0][0],
                     "richi help %s -h" % leaf, "richi help %s --help" % topic, "richi help -h %s" % leaf,
                     "richi help COMMAND", "richi help %s SUBCOMMAND" % group):
            with self.subTest(line=line):
                self.assertIsNone(validator.check_line(line, self.sources))
        # -h and --help are ignored, as by the CLI, and other options refused.
        option = "'richi help' takes command and topic names, not options such as %s"
        for line, error in (("richi help %sx" % topic, "'richi help %sx' names no command or help topic" % topic),
                            ("richi help %s --json" % leaf, option % "--json"),
                            ("richi help -- %s" % leaf, option % "--"),
                            ("richi help %s --bogus" % outer, option % "--bogus"),
                            ("richi help %s %s --x" % (group, child), option % "--x"),
                            ("richi help %s --json" % topic, "'richi help %s' takes no further words" % topic),
                            ("richi help %s FIELD" % topic, "'richi help %s' takes no further words" % topic),
                            ("richi help %s -hx" % leaf, option % "-hx"),
                            # A placeholder stands for any name; no name makes an option valid.
                            ("richi help COMMAND --json", option % "--json"),
                            ("richi help %s SUBCOMMAND --x" % group, option % "--x"),
                            ("richi help %s QUERY" % leaf, "richi %s has no subcommands" % leaf),
                            ("richi help %s %sx" % (group, child), "'%sx' is not a richi %s command" % (child, group)),
                            ("richi help %s extra" % leaf, "richi %s has no subcommands" % leaf),
                            ("richi help %s where" % topic, "'richi help %s' takes no further words" % topic),
                            ('richi capabilities describe "frob nothing"',
                             "capabilities describe 'frob nothing' names no catalog entry")):
            with self.subTest(line=line):
                self.assertEqual(validator.check_line(line, self.sources), error)
                self.assertEqual(validator.check_mention(line, self.sources), error)

    def test_a_comment_starts_only_where_the_shell_starts_one(self):
        for line in ("richi recall q # --nope", "richi recall q\t#--nope", "richi recall q #"):
            with self.subTest(line=line):
                self.assertIsNone(validator.check_line(line, self.sources))
                self.assertIsNone(validator.check_mention(line, self.sources))
        # Inside a word, quotes or after a backslash, # is part of an argument.
        for line in ("richi recall task#1 --nope", "richi recall 'q # x' --nope", 'richi recall "q #x" --nope',
                     "richi recall a\\ #b --nope", "richi recall \\#b --nope", 'richi recall "a\\"#" --nope'):
            with self.subTest(line=line):
                self.assertIn("--nope", validator.check_line(line, self.sources) or "")
                self.assertIn("--nope", validator.check_mention(line, self.sources) or "")

    def test_documented_global_options_must_be_accepted_by_the_command(self):
        from richi import memory
        from richi_launcher import cli
        # A help request runs nothing, so any global option may precede it.
        for line in ("richi -w work status", "richi --compact-json recall q", "richi -w work update --help",
                     "richi --project-path . recall q", "richi -w work workspace current",
                     "richi --project-path . project upsert --help", "richi --compact-json map -h",
                     "richi --chat task-1 use --help", "richi -w NAME recall q", "richi map --port PORT",
                     "richi --chat ID recall q", "richi --chat [ID] recall q", "richi --chat bad/id update auto status",
                     # Help needs no valid selection.
                     "richi -w Work recall --help", "richi -w Work help recall", "richi --chat bad/id help recall",
                     "richi --compact-json --help map",
                     # Settings and diagnostics accept storage options in a chat without a workspace.
                     "richi --chat ID --db PATH status", "richi --chat ID --config FILE config show",
                     "richi --chat ID --db PATH workspace current", "richi --chat ID --db PATH --version",
                     "richi --chat ID -w work --db PATH status",
                     "richi -w default --db PATH recall q", "richi -w NAME --db PATH recall q"):
            with self.subTest(line=line):
                self.assertIsNone(validator.check_line(line, self.sources))
                self.assertIsNone(validator.check_mention(line, self.sources))
        # The launcher's refusals and the runtime's rules, whatever they say.
        rules = memory.global_option_rules
        for line, expected in (("richi -w work update check", None), ("richi --project-path . chat current", None),
                               ("richi --db /x.sqlite3 setup --dry-run", None), ("richi -w work workspace list", None),
                               ("richi --chat task-1 use work", None), ("richi --chat task-1 workspace use work", None),
                               ("richi --compact-json=1 status", None), ("richi -w Work init", None),
                               ("richi -wmy.work recall q", None), ("richi -w Work --version", None),
                               ("richi --chat bad/id chat current", None), ("richi --chat= recall q", None),
                               ("richi --chat bad/id --version", None), ("richi --chat bad/id use work", None),
                               ("richi -w NAME --config FILE recall q", None), ("richi -w work map --config FILE", None),
                               ("richi --compact-json=1 recall --help", None), ("richi --compact-json=1 --help", None),
                               ("richi -w Work ...", None), ("richi --chat bad/id ...", None),
                               ("richi -w NAME --config FILE ...", None),
                               # No registry or chat state accepts these.
                               ("richi --chat task-1 --db /x.sqlite3 recall q", None),
                               ("richi --chat ID map --config FILE", None), ("richi -w work --db /x.sqlite3 recall q", None),
                               ("richi --project-path . init", rules(("init",))["--project-path"]),
                               ("richi --compact-json map", rules(("map",))["--compact-json"]),
                               ("richi --project-path . project upsert --json projects.json",
                                rules(("project", "upsert"))["--project-path"])):
            expected = expected or cli.global_option_error(line.split()[1:], validator._placeholder)
            with self.subTest(line=line):
                self.assertIsNotNone(expected)
                self.assertIn(expected, validator.check_line(line, self.sources) or "")
                self.assertIn(expected, validator.check_mention(line, self.sources) or "")
        # Python 3.12+ runs `config -- show` as `config show` (3.9 refuses it while parsing).
        self.assertIsNone(cli.global_option_error(["--chat", "ID", "--db", "PATH", "config", "--", "show"],
                                                  validator._placeholder))
        # The chat ID is checked before a chat is refused the default.
        self.assertIn("Chat ID must be", validator.check_line("richi --chat bad/id use work", self.sources))
        # A span that names several subcommands is checked for each of them.
        for span, argv, expected in (
                ("richi --project-path PATH project add/scan", None, rules(("project", "add"))["--project-path"]),
                ("richi --project-path PATH project check/add", None, rules(("project", "add"))["--project-path"]),
                ("richi -w NAME workspace list/create", ["-w", "NAME", "workspace", "list"], None),
                ("richi --config FILE update check/apply", ["--config", "FILE", "update", "check"], None)):
            expected = expected or cli.global_option_error(argv, validator._placeholder)
            with self.subTest(span=span):
                self.assertIsNotNone(expected)
                self.assertIn(expected, validator.check_mention(span, self.sources) or "")

    def test_every_page_needs_its_translation(self):
        checkout = Checkout(self)
        checkout.add_command("frobnicate")
        (checkout.root / REFERENCES / "FROBNICATE.ru.md").unlink()
        checkout.write("docs/EXTRA.ru.md", "Только русский.\n")
        problems = checkout.problems()
        self.assertEqual(about(problems, REFERENCES + "FROBNICATE.md"), {("translation", "has no Russian page")})
        self.assertEqual(about(problems, "docs/EXTRA.ru.md"), {("translation", "has no English page")})
        self.assertEqual(about(problems, "richi frobnicate"),
                         {("guide", "guide " + REFERENCES + "FROBNICATE.ru.md does not exist")})


if __name__ == "__main__":
    unittest.main()
