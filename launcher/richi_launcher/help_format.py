"""Help presentation shared by the launcher's parsers and the selected runtime.

Only help and error text, and the reading of a command line that its errors
need, live here: no settings, storage or runtime selection. A parser carries
its page as data (HelpPage); the text is composed only when the page is
printed. The runtime uses this module only when the installed launcher
provides the API version it was written for and every name it needs
(richi.cli_help.NEEDS), and keeps plain argparse help otherwise.
"""
import argparse
from collections import Counter, namedtuple
import functools
import sys

# Increase when the runtime starts to rely on a changed behavior or
# signature here: a runtime and a launcher with different versions then
# keep plain argparse help instead of failing. A new name needs no increase
# once it is listed in richi.cli_help.NEEDS.
API = 1
REPOSITORY = "https://github.com/Hidanio/Richi/"
REFERENCES = "skills/project-memory/references/"
TEXT_WIDTH = 78

HelpPage = namedtuple("HelpPage", "summary about examples see guide")


class HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Keep authored paragraphs and examples; list subcommands like options.

    The width is fixed and colors are off, so a page reads the same in every
    terminal, in agent output and in tests.
    """

    def __init__(self, prog, indent_increment=2, max_help_position=30, width=TEXT_WIDTH):
        super().__init__(prog, indent_increment, max_help_position, width)

    def _set_color(self, color, *args, **kwargs):
        # Python 3.14 colors help printed to a terminal.
        super()._set_color(False, *args, **kwargs)

    def _split_lines(self, text, width):
        # Never cut an option name such as --include-superseded at a hyphen.
        import textwrap
        return textwrap.wrap(" ".join(text.split()), width, break_on_hyphens=False)

    def _format_action(self, action):
        # Argparse prints a "{a,b,c}" header row above subcommands; the
        # commands section already names them, so list only the rows.
        if isinstance(action, argparse._SubParsersAction):
            return "".join(self._format_action(choice) for choice in action._get_subactions())
        return super()._format_action(action)

    def _iter_indented_subactions(self, action):
        # The rows are not indented under a header (_format_action), so the
        # help column is measured without that indentation, as before 3.12.
        if isinstance(action, argparse._SubParsersAction):
            yield from action._get_subactions()
        else:
            yield from super()._iter_indented_subactions(action)


def paragraphs(*texts):
    """Wrap authored prose. Each text is dedented as a whole; a block with an
    indented line (an example command, a list, a table) keeps its layout."""
    import textwrap
    blocks = []
    for text in texts:
        for block in textwrap.dedent(text or "").split("\n\n"):
            block = block.strip("\n").rstrip()
            if not block.strip():
                continue
            if any(line[:1].isspace() for line in block.splitlines()):
                blocks.append(block)
            else:
                blocks.append(textwrap.fill(" ".join(block.split()), TEXT_WIDTH, break_on_hyphens=False))
    return "\n\n".join(blocks)


def sentence(text):
    """Text as a sentence: stripped and ending with a full stop."""
    text = (text or "").strip()
    return text if not text or text.endswith((".", "?", "!")) else text + "."


def _active_runtime():
    """The runtime the launcher selected for this process, or None."""
    import json
    import os
    try:
        runtime = json.loads(os.environ.get("RICHI_ACTIVE_RUNTIME") or "null")
    except ValueError:
        return None
    return runtime if isinstance(runtime, dict) else None


@functools.lru_cache(maxsize=None)
def documentation():
    """Base URL of the documentation for the running code: the release tag
    of an installed release, the main branch for development sources."""
    from pathlib import Path
    import re
    runtime = _active_runtime()
    if runtime is not None:
        # The selected runtime: its mode and version come from the launcher.
        version = runtime.get("version") if runtime.get("mode") == "release" else None
    else:
        # A launcher is installed next to the runtime package of its release.
        try:
            text = (Path(__file__).resolve().parent.parent / "richi" / "__init__.py").read_text(encoding="utf-8")
            match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
            version = match.group(1) if match else None
        except (OSError, UnicodeError):
            version = None
    release = isinstance(version, str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
    return REPOSITORY + "blob/" + ("v" + version if release else "main") + "/"


def guide_path(guide):
    """Repository path of a guide, without its #anchor: NAME.md is a skill
    reference, DIR/NAME.md a repository path and a link into this repository
    its path. A link elsewhere has no path."""
    if "://" in guide:
        prefix = REPOSITORY + "blob/"
        if not guide.startswith(prefix):
            return None
        guide = guide[len(prefix):].partition("/")[2]
    guide = guide.partition("#")[0]
    return guide if "/" in guide else REFERENCES + guide


def guide_url(guide):
    """Link to a guide in the documentation of the running code."""
    if "://" in guide:
        return guide
    path, mark, anchor = guide.partition("#")
    return documentation() + guide_path(path) + mark + anchor


def local_guide(guide):
    """This machine's copy of a guide for the running code, or None: the file
    in a development checkout, or the skill reference bundled with a
    release, so a page can be read offline."""
    from pathlib import Path
    path = guide_path(guide)
    if path is None:
        return None
    runtime = _active_runtime() or {}
    here = Path(__file__).resolve()
    candidates = []
    if runtime.get("mode") == "dev" and runtime.get("source"):
        candidates.append(Path(runtime["source"]) / path)
    if path.startswith(REFERENCES):
        candidates.append(here.parent / "agent_skill" / "references" / path[len(REFERENCES):])
    if not runtime:
        # The launcher's own pages, or sources run from a checkout.
        candidates.append(here.parents[2] / path)
    try:
        return next((candidate for candidate in candidates if candidate.is_file()), None)
    except OSError:
        return None


def epilog(examples=(), see=(), guide=None):
    """Render examples, related help pages and the detailed guide.

    Each example is (comment, command) or (comment, (command, ...)) for a
    short sequence; an empty comment continues the previous block.
    """
    parts = []
    if examples:
        lines = ["examples:"]
        for comment, commands in examples:
            if comment:
                if len(lines) > 1:
                    lines.append("")
                lines.append("  # " + comment)
            for command in ((commands,) if isinstance(commands, str) else commands):
                lines.append("  " + command)
        parts.append("\n".join(lines))
    footer = []
    if see:
        # Wrapped between pages, never inside a `richi help NAME`.
        lines = ["see also:"]
        for index, item in enumerate(see):
            piece = "richi help " + item + ("," if index < len(see) - 1 else "")
            if len(lines[-1]) + 1 + len(piece) > TEXT_WIDTH and lines[-1].strip() != "see also:":
                lines.append(" " * len("see also:"))
            lines[-1] += " " + piece
        footer.append("\n".join(lines))
    if guide:
        footer.append("guide: " + guide_url(guide))
        local = local_guide(guide)
        if local is not None:
            footer.append("local copy: " + str(local))
    if footer:
        parts.append("\n".join(footer))
    return "\n\n".join(parts) or None


def subcommands(parser):
    """The subcommands action of a parser, or None."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def summaries(parser):
    """{name: list summary} of a parser's subcommands in declaration order."""
    commands = subcommands(parser)
    if commands is None:
        return {}
    helps = {choice.dest: choice.help or "" for choice in commands._get_subactions()}
    return {name: helps.get(name, "") for name in commands.choices}


def walk(parser, path=(), summary=""):
    """(path, parser, list summary) of a parser and every subcommand below it."""
    yield path, parser, summary
    for name, text in summaries(parser).items():
        yield from walk(subcommands(parser).choices[name], path + (name,), text)


def defined(parser):
    """{option string: action} of a parser and every subcommand below it."""
    return {name: action for _, command, _ in walk(parser) for action in command._actions
            for name in action.option_strings}


def tidy(parser):
    """Name sections and metavars consistently on every Python version."""
    parser.formatter_class = HelpFormatter
    parser._positionals.title = "arguments"
    parser._optionals.title = "options"
    for action in parser._actions:
        if isinstance(action, argparse._HelpAction):
            action.help = "Show this help and exit"
        elif isinstance(action, argparse._SubParsersAction):
            action.metavar = "COMMAND"
            if action in parser._positionals._group_actions:
                parser._positionals._group_actions.remove(action)
                group = parser.add_argument_group("commands")
                group._group_actions.append(action)
                # Commands first: they are what a group page is read for.
                parser._action_groups.remove(group)
                parser._action_groups.insert(0, group)
        elif not action.option_strings and action.metavar is None and action.choices is None:
            action.metavar = action.dest.upper()
    return parser


def document(parser, summary=None, about=None, examples=(), see=(), guide=None):
    """Attach a page in the house style: summary, details, examples, links.

    A subcommand may leave out its summary: complete() takes it from the
    parent's help=. The text is composed by finish() when the page is printed.
    """
    parser._help_page = HelpPage(summary, about, tuple(examples), tuple(see), guide)
    if summary:
        parser.description = summary
    return tidy(parser)


def page_parts(parser):
    """The HelpPage of a documented parser, or None."""
    return parser.__dict__.get("_help_page")


def complete(parser, guide=None):
    """Fill what subcommand pages inherit: a missing summary from the
    parent's help= and a missing guide from the nearest page above."""
    page = page_parts(parser)
    if page is not None:
        guide = page.guide or guide
        parser._help_page = page._replace(guide=guide)
    commands = subcommands(parser)
    if commands is not None:
        for name, text in summaries(parser).items():
            child = commands.choices[name]
            page = page_parts(child)
            if page is not None and not page.summary and text:
                child._help_page = page._replace(summary=sentence(text))
                child.description = child._help_page.summary
            complete(child, guide)
    return parser


def finish(parser):
    """Compose the page of a parser; its format_help() calls this. It can run
    any number of times: the page data stays on the parser."""
    page = page_parts(parser)
    if page is not None:
        parser.description = paragraphs(page.summary, page.about)
        parser.epilog = epilog(page.examples, page.see, page.guide)


def syntax(parser):
    """Usage, summary, arguments and options of a page, without its
    description, examples and links."""
    page = page_parts(parser)
    saved = parser.description, parser.epilog
    parser.description = paragraphs(page.summary) if page is not None else saved[0]
    parser.epilog = None
    try:
        return argparse.ArgumentParser.format_help(parser)
    finally:
        parser.description, parser.epilog = saved


def suggestions(value, choices):
    """Prefix matches first, then close spellings; at most three."""
    import difflib
    value = str(value)
    choices = [str(choice) for choice in choices]
    found = [choice for choice in choices if value and choice.startswith(value)]
    found += difflib.get_close_matches(value, choices, n=3, cutoff=0.6)
    unique = []
    for choice in found:
        if choice not in unique:
            unique.append(choice)
    return unique[:3]


def did_you_mean(close):
    """' Did you mean 'a' or 'b'?' for the suggestions, or ''."""
    if not close:
        return ""
    return " Did you mean " + " or ".join("'" + choice + "'" for choice in close) + "?"


def choice_error(parser, action, value):
    """Argparse error for an unknown choice with suggestions, or None if valid."""
    if action.choices is None or value in action.choices:
        return None
    close = did_you_mean(suggestions(value, action.choices))
    if isinstance(action, argparse._SubParsersAction):
        return argparse.ArgumentError(None, "'%s' is not a %s command.%s" % (value, parser.prog, close))
    listed = ", ".join(str(choice) for choice in action.choices)
    return argparse.ArgumentError(action, "invalid choice: '%s' (choose from %s).%s" % (value, listed, close))


def selected(parser, namespace):
    """The innermost subcommand parser that a parsed namespace selected."""
    commands = subcommands(parser)
    while commands is not None:
        child = commands.choices.get(getattr(namespace, commands.dest, None))
        if child is None:
            break
        parser, commands = child, subcommands(child)
    return parser


def unrecognized(parser, extras, global_options=(), accepted=None, tree=None):
    """Error for arguments the innermost parser rejected, with a correction
    for the first unknown option. global_options go before the command and
    map each option string to whether it takes a value; accepted are those
    this command takes (default: all of them). An option that TREE, the
    parser of the whole command (default: PARSER), defines anywhere is not a
    global option in it, even where one has its name: update check defines
    --version, so update rollback has no global --version either."""
    message = "unrecognized arguments: " + " ".join(extras)
    index = next((index for index, token in enumerate(extras) if token.startswith("-") and token != "-"), None)
    if index is None:
        return message
    token = extras[index]
    option = token.split("=", 1)[0]
    if option not in global_options and not option.startswith("--") and option[:2] in global_options:
        option = option[:2]  # -wNAME
    below = defined(tree or parser)
    accepted = [name for name in (global_options if accepted is None else accepted) if name not in below]
    if option in global_options and option not in below:
        if option not in accepted:
            return message + " (%s does not take the global option %s)" % (parser.prog, option)
        # Before the command it takes no value either, so moving it there
        # loses the value: richi --version 1.2.0 status only prints the version.
        valued = token != option or (index + 1 < len(extras) and parser._parse_optional(extras[index + 1]) is None)
        if valued and not global_options[option]:
            return message + " (%s is a global option and takes no value)" % option
        return message + " (a global option: put it before the command)"
    # Compare names without their dashes, which every option shares. The
    # command's own options win a tie: they are what is typed after it.
    import difflib
    typed = option.lstrip("-")

    def closest(names):
        names = {name.lstrip("-"): name for name in names}
        close = suggestions(typed, names)
        return [names[name] for name in close], max(
            (difflib.SequenceMatcher(None, typed, name).ratio() for name in close), default=0)
    own, own_score = closest([name for action in parser._actions for name in action.option_strings])
    other, other_score = closest(accepted)
    if other and other_score > own_score:
        return sentence(message) + did_you_mean(other) + " Global options go before the command."
    return sentence(message) + did_you_mean(own) if own else message


def known_args(parser, args, namespace, parse, global_options, unrecognized):
    """parse_known_args of PARSER, a command written after the global
    options; parse(args, namespace) is argparse's own. The separate value of
    a global option written after the command, which argparse would give to
    a positional, stays with its option among the leftovers, so an error
    names them together. An option PARSER defines itself stays its own.

    global_options maps each global option string to whether it takes a
    value. A word after an unknown option that the first positional rejects
    as a choice is reported as that option's value, with the tree's error
    for leftovers: unrecognized(parser, extras)."""
    args = list(sys.argv[1:] if args is None else args)
    aside = _misread(parser, args, global_options, unrecognized)
    namespace, extras = parse([word for index, word in enumerate(args) if index not in aside], namespace)
    # Argparse keeps the options a command does not define among the
    # leftovers, in order: each value follows the same occurrence of its
    # option again, or comes last if a positional that takes the rest took
    # the option.
    values, count = {}, Counter()
    for index, word in enumerate(args):
        if index + 1 in aside:
            values[word, count[word]] = args[index + 1]
        count[word] += 1
    restored, count = [], Counter()
    for word in extras:
        value = values.pop((word, count[word]), None)
        restored += [word] if value is None else [word, value]
        count[word] += 1
    return namespace, restored + list(values.values())


def _option(options, word):
    """(name, whether WORD carries its value) of an option word among
    OPTIONS: --name, --name=VALUE, -n or -nVALUE; (None, False) for any
    other word."""
    name = word.split("=", 1)[0]
    if name not in options and not word.startswith("--"):
        name = word[:2]  # -nVALUE
    return (name, name != word) if name in options else (None, False)


def _misread(parser, argv, global_options, unrecognized):
    """Indexes of ARGV words that argparse would give PARSER's positionals
    although they are separate values of global options PARSER does not
    define. After an option of either kind, a word that the first
    positional takes as a choice is that choice; one it rejects as a choice
    after an unknown option is reported, after the leftovers before it, as
    that option's value."""
    own = parser._option_string_actions
    first = next(iter(parser._get_positional_actions()), None)
    aside, extras, unknown, index = set(), [], None, 0
    while index < len(argv) and argv[index] != "--":
        word = argv[index]
        index += 1
        # Positional as argparse reads it, like -, -1 or a word with a space.
        if parser._parse_optional(word) is None:
            if first is not None:
                if unknown and first.choices is not None and word not in first.choices:
                    parser.error(unrecognized(parser, extras + [word]))
                if first.nargs in (argparse.PARSER, argparse.REMAINDER):
                    break  # a subcommand, or the positional, takes the rest
                first = None
            unknown = None
            continue
        name, attached = _option(own, word)
        if name is None:
            extras.append(word)
            name, attached = _option(global_options, word)
            if name is None:
                # An unknown option without an attached value may take the next word.
                unknown = word if "=" not in word and (word.startswith("--") or len(word) == 2) else None
                continue
            # Its separate value, unless the next word is an option or a
            # choice of the first positional: then the value is missing.
            value = argv[index] if global_options[name] and not attached and index < len(argv) else None
            if value is not None and parser._parse_optional(value) is None and not (
                    first is not None and first.choices is not None and value in first.choices):
                aside.add(index)
                extras.append(value)
                index += 1
        elif isinstance(own[name], argparse._HelpAction):
            break  # argparse prints the page before it reads further
        elif own[name].nargs != 0 and not attached:
            index += 1  # the command's own option takes its value
        unknown = None
    return aside


def help_target(parser):
    """The `richi help ...` page that explains this parser."""
    return " ".join(["richi", "help"] + parser.prog.split()[1:])


def with_help(message, target):
    """MESSAGE as a sentence that ends with the help page to read."""
    return sentence(message) + " See '" + target + "'."


def usage_message(parser, message):
    """Return (message, help command) for an argument error of this parser."""
    target = help_target(parser)
    commands = subcommands(parser)
    if commands is not None and message == ("the following arguments are required: "
                                            + (commands.metavar or commands.dest)):
        message = "'%s' needs a command: %s" % (parser.prog, ", ".join(commands.choices))
    return with_help(message, target), target


def find(parser, words, error):
    """The page that `richi help` with subcommand WORDS below PARSER shows.
    error(message, help command) raises for a word that names no page; the
    caller ends the message with the help command. -h and --help are ignored."""
    for word in words:
        if word in ("-h", "--help"):
            continue
        commands = subcommands(parser)
        target = help_target(parser)
        if word.startswith("-"):
            error("'richi help' takes command and topic names, not options such as %s" % word, target)
        if commands is None:
            error("'%s' has no subcommands" % parser.prog, target)
        if word not in commands.choices:
            error("'%s' is not a %s command.%s" % (word, parser.prog, did_you_mean(suggestions(word, commands.choices))),
                  target)
        parser = commands.choices[word]
    return parser
