#!/usr/bin/env python3
"""Fail when a CLI command is not documented or the documentation is stale.

Commands come from the real parsers of the runtime and the launcher, so a new
command, subcommand or option is checked without being registered here.

Rules (the name in brackets prefixes each reported problem):

  [help]        every command has a list summary and a description; every
                argument and option, global options included, has help text;
                every page, the command list and the overview render; every
                help topic is a (summary, text) pair, and `richi help` reaches
                every page and topic (no command or topic is named `topics`,
                no command like a topic); shared option texts and
                placeholders (COMMON_HELP, METAVARS) name options that exist
  [examples]    every command without subcommands has an example that runs
                it; every example, every command line of a help topic and of
                `richi help` and the bare `richi` overview parses
  [guide]       every command page links a guide in this repository, and the
                guide exists in English and Russian (NAME.ru.md)
  [mention]     both languages of that guide show the command in code:
                `richi [global options] COMMAND ...` or, for a subcommand,
                a code span that starts with its full path; the guide of
                `richi help` shows `help TOPIC` for every help topic
  [catalog]     every command is in the agent catalog (richi capabilities) or
                in capabilities.UNCATALOGED with a reason; a catalog guide is
                a skill reference that exists, shows its command and is also
                the guide of the command's page
  [overview]    every top-level command is in exactly one group of `richi help`
  [links]       DETAILS entries name runtime pages and use only known fields;
                "see also" lines and `richi help ...` in help text name real
                pages or topics
  [mirror]      the runtime lists exactly the launcher's commands, with the
                summaries of their pages, and declares exactly the global
                options the launcher consumes, each taking a value on both
                sides or on neither; no runtime command looks like a mistyped
                launcher command
  [docs]        every `richi ...` line of a shell code block (fenced or
                indented, also inside a list item or block quote) parses and
                is accepted with its global options; inline code naming a
                command (`richi recall ...`, `sources show --ref`) uses real
                commands, options and values, and global options the command
                accepts; `richi help ...` and `capabilities describe ...` name
                real pages and catalog entries. Dated reports (file names that
                end in -YYYY-MM-DD or _EVAL) record the CLI as it was and are
                not checked.
  [translation] every Markdown page has its .ru.md counterpart and back

The documentation is the Markdown of the repository root, docs/, evals/ and
skills/, and every guide that a page or catalog entry links.

Usage: python scripts/check_cli_docs.py
The script checks this checkout's sources; imported by tests, it checks the
packages of the running environment. Exit status 0 when everything is
documented; 1 with one line per problem.
"""
import argparse
from collections import namedtuple
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
import io
from pathlib import Path
import re
import shlex
import sys
import textwrap

ROOT = Path(__file__).resolve().parents[1]

Problem = namedtuple("Problem", "rule subject message")
Page = namedtuple("Page", "path parser summary parts launcher")

FIXES = {
    "help": "add help= to the declaration (src/richi/*.py or the launcher's parser builder), "
            "make the page render, drop COMMON_HELP and METAVARS entries of removed options, "
            "and name help topics unlike commands and `topics`",
    "examples": "add or correct examples: DETAILS, EXAMPLES, OVERVIEW_COMMANDS, HELP_EXAMPLE or TOPICS in "
                "src/richi/cli_help.py, document() in launcher/richi_launcher/",
    "guide": "set guide= on the page or its group; NAME.md is a skill reference, docs/NAME.md a repository file",
    "mention": "show the command, or `richi help TOPIC` for a topic, in a code block or span of its guide, "
               "in English and Russian",
    "catalog": "add the command to capabilities.COMMANDS or, with a reason, to capabilities.UNCATALOGED; "
               "a catalog guide is a skill reference and the page's guide",
    "overview": "list each top-level command in exactly one of cli_help.GROUPS",
    "links": "point DETAILS keys, see= and richi help references at existing pages or topics; "
             "DETAILS entries take only about, examples, see and guide",
    "mirror": "keep richi.cli_parser.LAUNCHER_COMMANDS and the runtime's global options equal to the launcher's "
              "(GLOBAL_OPTIONS, GLOBAL_FLAGS)",
    "docs": "update the command line to the current syntax (richi help COMMAND)",
    "translation": "add the missing English or Russian page",
}
SHELL_OPERATORS = frozenset({"<", ">", ">>", "2>", "|", "||", "&&", ";", "&"})
PLACEHOLDER = re.compile(r"^\[?(\.\.\.|…|[A-Z][A-Z0-9_|.-]*)\]?(\.\.\.)?$")
FENCE = re.compile(r"(`{3,}|~{3,})\s*([^\s]*)")
CLOSING = re.compile(r" {0,3}(`{3,}|~{3,})\s*$")
LIST_ITEM = re.compile(r"([-+*]|[0-9]{1,9}[.)])( +|$)")
HEADING = re.compile(r"#{1,6}(\s|$)")
THEMATIC = re.compile(r"([-*_])(?: *\1){2,} *$")
SETEXT = re.compile(r"(=+|-+) *$")
QUOTE = re.compile(r" {0,3}> ?")
PUNCTUATION = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")  # what a backslash escapes
HELP_REFERENCE = re.compile(r"\brichi help((?: [a-z][a-z0-9-]*)+)")
HELP_OPTION = "'richi help' takes command and topic names, not options such as %s"
# Words that may follow "richi help" in prose without naming a page.
PROSE = frozenset({"to", "for", "and", "or", "with", "as", "is", "in", "on", "lists", "shows", "prints",
                   "explains", "describes"})
# Code blocks whose lines are commands; Markdown blocks are read as Markdown.
SHELL = frozenset({"", "sh", "bash", "shell", "console", "zsh"})
MARKDOWN = frozenset({"markdown", "md"})
DETAIL_FIELDS = frozenset({"about", "examples", "see", "guide"})
# Markdown files that are intentionally English only.
UNTRANSLATED = frozenset({"AGENTS.md", "LICENSE.md"})


@dataclass
class Sources:
    """What the rules compare. current() copies every table, so a test can
    change one without touching the packages."""
    root: Path
    runtime: argparse.ArgumentParser  # the documented runtime parser tree
    launcher: dict                    # {command: parser} of the launcher
    launcher_commands: frozenset      # richi_launcher.cli.LAUNCHER_COMMANDS
    stubs: dict                       # the runtime's {launcher command: list summary}
    global_options: dict              # richi_launcher.cli.GLOBAL_OPTIONS
    catalog: tuple
    uncataloged: dict
    groups: tuple
    details: dict
    topics: dict
    examples: tuple                   # examples of `richi help`
    overview: tuple                   # command lines of the bare `richi`
    style: object                     # richi_launcher.help_format
    help: object                      # richi.cli_help, which renders the root pages
    global_flags: frozenset           # richi_launcher.cli.GLOBAL_FLAGS: global options without a value
    metavars: dict                    # cli_help.METAVARS
    common_help: dict                 # cli_help.COMMON_HELP
    launcher_policy: object           # (argv, placeholder) -> why the launcher rejects its global options
    runtime_policy: object            # parsed namespace -> raises for rejected global options
    runtime_rules: object             # (command path, apply) -> {global option: why it is rejected}


def current(root=ROOT):
    """The parsers, catalog and help tables of the imported packages, and the
    documentation of this checkout."""
    from richi import capabilities, cli_help, cli_parser, memory
    from richi_launcher import cli, help_format
    return Sources(
        root=Path(root), runtime=cli_help.decorate(memory.parser()), launcher=cli.launcher_parsers(),
        launcher_commands=frozenset(cli.LAUNCHER_COMMANDS), stubs=dict(cli_parser.LAUNCHER_COMMANDS),
        global_options=dict(cli.GLOBAL_OPTIONS), catalog=tuple(capabilities.COMMANDS),
        uncataloged=dict(capabilities.UNCATALOGED), groups=tuple(cli_help.GROUPS), details=dict(cli_help.DETAILS),
        topics=dict(cli_help.TOPICS), examples=tuple(cli_help.EXAMPLES),
        overview=tuple(cli_help.OVERVIEW_COMMANDS) + (cli_help.HELP_EXAMPLE,), style=help_format, help=cli_help,
        global_flags=frozenset(cli.GLOBAL_FLAGS), metavars=dict(cli_help.METAVARS),
        common_help=dict(cli_help.COMMON_HELP), launcher_policy=cli.global_option_error,
        runtime_policy=memory.check_global_options, runtime_rules=memory.global_option_rules)


def pages(sources):
    """Every command page: the runtime's own commands, then the launcher's."""
    found = {}
    for path, parser, summary in sources.style.walk(sources.runtime):
        if path and path[0] not in sources.stubs:
            found[path] = Page(path, parser, summary, sources.style.page_parts(parser), False)
    summaries = sources.style.summaries(sources.runtime)
    for name, parser in sources.launcher.items():
        for path, child, summary in sources.style.walk(parser, (name,), summaries.get(name, "")):
            found[path] = Page(path, child, summary, sources.style.page_parts(child), True)
    return found


def _leaf(page, sources):
    return sources.style.subcommands(page.parser) is None


def _label(path):
    return " ".join(("richi",) + tuple(path))


# Command lines -------------------------------------------------------------

def _comment(line):
    """Where a shell comment starts: at an unquoted # that begins a word, as
    in sh (shlex would also start one inside a word, as in entry:task#1)."""
    quote, escaped, boundary = None, False, True
    for index, char in enumerate(line):
        if escaped:
            escaped = boundary = False
        elif quote:
            if char == quote:
                quote = None
            elif char == "\\" and quote == '"':
                escaped = True
        elif char == "#" and boundary:
            return index
        elif char == "\\":
            escaped, boundary = True, False
        elif char in "'\"":
            quote, boundary = char, False
        else:
            boundary = char.isspace()
    return len(line)


def _tokens(line):
    line = line[:_comment(line)]
    try:
        tokens = shlex.split(line)
    except ValueError:
        tokens = line.split()
    for index, token in enumerate(tokens):
        if token in SHELL_OPERATORS or token.startswith("<<"):
            return tokens[:index]
    return tokens


def _placeholder(token):
    """NAME, [ARGS...], "COMMAND ID" or ... stand for anything."""
    return bool(token.split()) and all(PLACEHOLDER.match(part) for part in token.split())


def _global_options(sources):
    """{option string: takes a value} of the runtime's top-level options."""
    return {option: action.nargs != 0 for action in sources.runtime._actions for option in action.option_strings}


def _after_globals(tokens, globals_):
    """Index of the first token after leading global options."""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        key = token.split("=", 1)[0]
        if key in globals_:
            index += 2 if globals_[key] and "=" not in token else 1
        elif token.startswith("-w") and len(token) > 2 and "-w" in globals_:
            index += 1
        else:
            break
    return index


def _top(name, sources):
    """Parser of a top-level command; the launcher owns its own commands."""
    if name in sources.launcher:
        return sources.launcher[name]
    return sources.style.subcommands(sources.runtime).choices.get(name)


def _resolve(words, sources):
    """(path, parser, rest) following command words from the top level."""
    parser = _top(words[0], sources) if words else None
    if parser is None:
        return (), None, list(words)
    path, rest = (words[0],), list(words[1:])
    while rest:
        commands = sources.style.subcommands(parser)
        if commands is None or rest[0] not in commands.choices:
            break
        parser = commands.choices[rest[0]]
        path, rest = path + (rest[0],), rest[1:]
    return path, parser, rest


def _asks_help(tokens):
    options = tokens[:tokens.index("--")] if "--" in tokens else tokens
    return "-h" in options or "--help" in options


def _parse_error(argv, sources):
    """Why `richi ARGV` would be rejected before it runs, or None."""
    words = argv[_after_globals(argv, _global_options(sources)):]
    output = io.StringIO()
    try:
        with redirect_stdout(output), redirect_stderr(output):
            error = sources.launcher_policy(argv, _placeholder)
            if error:
                return error
            if words and words[0] in sources.launcher:
                # The launcher strips global options before its own parser.
                sources.launcher[words[0]].parse_args(words[1:])
            else:
                sources.runtime_policy(sources.runtime.parse_args(argv))
    except SystemExit as exc:
        if exc.code:
            lines = [line for line in output.getvalue().splitlines() if line.strip()]
            return lines[-1] if lines else "exit status %s" % exc.code
    except Exception as exc:  # The CLI's own usage errors.
        return str(exc)
    return None


def _later_option(words):
    """The refusal of the first option among WORDS, which follow a placeholder:
    a placeholder stands for any name, but no name makes an option valid."""
    option = next((word for word in words if word.startswith("-")), None)
    return HELP_OPTION % option if option else None


def _help_target(words, sources, strict=True):
    """Why the words after `richi help` name no page, or None, read as the CLI
    reads them: -h and --help are ignored, other options are refused. Prose
    may go on after a valid page name unless strict."""
    words = [word for word in words if word not in ("-h", "--help")]
    if not words:
        return None
    name = words[0]
    if _placeholder(name):
        return _later_option(words[1:])
    if name.startswith("-"):
        return HELP_OPTION % name
    if name == "topics" or name in sources.topics:
        if strict and len(words) > 1:
            return "'richi help %s' takes no further words" % name
        return None
    parser = _top(name, sources)
    if parser is None:
        return "'richi help %s' names no command or help topic" % name
    path = (name,)
    for index, word in enumerate(words[1:], 1):
        if word.startswith("-"):
            return HELP_OPTION % word
        commands = sources.style.subcommands(parser)
        if _placeholder(word):
            # It stands for any subcommand, so only a page without one refuses it.
            if strict and commands is None:
                return "%s has no subcommands" % _label(path)
            return _later_option(words[index + 1:])
        if commands is None or word not in commands.choices:
            if not strict:
                return None
            if commands is None:
                return "%s has no subcommands" % _label(path)
            return "'%s' is not a %s command" % (word, _label(path))
        parser, path = commands.choices[word], path + (word,)
    return None


def _target_error(path, rest, sources):
    """Help pages and catalog entries that a line names must exist."""
    if path == ("help",):
        return _help_target(rest, sources)
    if path == ("capabilities", "describe"):
        identity = next((word for word in rest if not word.startswith("-")), None)
        if identity and not _placeholder(identity) and identity not in {row[0] for row in sources.catalog}:
            return "capabilities describe '%s' names no catalog entry" % identity
    return None


def invoked(line, sources):
    """Command path that a complete `richi ...` line runs, or None; a help
    request runs nothing."""
    tokens = _tokens(line)
    if not tokens or tokens[0] != "richi" or _asks_help(tokens):
        return None
    argv = tokens[1:]
    return _resolve(argv[_after_globals(argv, _global_options(sources)):], sources)[0]


def check_line(line, sources):
    """Error of a complete command line, or None."""
    tokens = _tokens(line)
    if not tokens or tokens[0] != "richi":
        return "does not start with richi"
    argv = tokens[1:]
    error = _parse_error(argv, sources)
    if error:
        return error
    path, _, rest = _resolve(argv[_after_globals(argv, _global_options(sources)):], sources)
    return _target_error(path, rest, sources)


def _choice_error(parser, action, value, sources):
    if not action.choices or _placeholder(value):
        return None
    error = sources.style.choice_error(parser, action, value)
    return str(error) if error is not None else None


def _arguments_error(parser, path, rest, sources):
    """Unknown or misplaced options and invalid values in the arguments of an
    inline span; omitted arguments are fine."""
    globals_ = _global_options(sources)
    options = {name: action for action in parser._actions for name in action.option_strings}
    positionals = [action for action in parser._actions
                   if not action.option_strings and not isinstance(action, argparse._SubParsersAction)]
    index, position = 0, 0
    while index < len(rest):
        token = rest[index]
        if token == "--":
            break
        if token.startswith("-") and token != "-" and not _placeholder(token):
            option, sep, value = token.partition("=")
            action = options.get(option)
            if action is None:
                if option in globals_:
                    return "global option %s must come before the command" % option
                return "%s has no option %s" % (_label(path), option)
            if action.nargs != 0:
                if not sep:
                    if index + 1 >= len(rest):
                        break
                    index += 1
                    value = rest[index]
                error = _choice_error(parser, action, value, sources)
                if error:
                    return error
        elif position < len(positionals):
            action = positionals[position]
            error = _choice_error(parser, action, token, sources)
            if error:
                return error
            if action.nargs == argparse.REMAINDER:
                break  # The words that follow are its own, as `richi help` reads them.
            if action.nargs not in ("*", "+"):
                position += 1
        index += 1
    return None


def check_mention(text, sources):
    """Error of an inline code span that names a command, or None.

    `richi ...` must name real commands, options and values; arguments may be
    left out, placeholders (NAME, ...) stand for anything, and `a/b` names
    several subcommands. A span without richi counts as a command when it
    starts with a command followed by an argument, one of its subcommands or
    an option, like `sources show --ref REF`; a filter such as
    `project = 'x'` does not.
    """
    tokens = _tokens(text)
    prefixed = bool(tokens) and tokens[0] == "richi"
    words = tokens[1:] if prefixed else tokens
    given, start = [], 0
    if prefixed:
        start = _after_globals(words, _global_options(sources))
        if start < len(words) and words[start].startswith("-") and words[start] != "--" \
                and not _placeholder(words[start]):
            return "richi has no global option %s" % words[start].split("=", 1)[0]
        given, words = words[:start], words[start:]
    elif len(words) < 2:
        return None
    argv = tokens[1:]
    if not words or _placeholder(words[0]) or words[0].startswith("-"):
        # Global options alone, such as `richi --version`, meet the launcher's
        # checks; a value left out at the end, as in `richi --chat`, may be any.
        if start > len(argv):
            argv = argv + ["VALUE"]
        return sources.launcher_policy(argv, _placeholder) if given else None
    parser = _top(words[0], sources)
    if parser is None:
        return "'%s' is not a richi command" % words[0] if prefixed else None

    def error_of(parser, path, rest, argv):
        """Arguments, pages and catalog entries, then the global options."""
        return (_arguments_error(parser, path, rest, sources) or _target_error(path, rest, sources)
                or (_globals_error(argv, given, path, rest, sources, _asks_help(tokens)) if given else None))

    path, rest = (words[0],), words[1:]
    while rest and sources.style.subcommands(parser) is not None:
        if rest[0].startswith("-") or _placeholder(rest[0]):
            break
        choices = sources.style.subcommands(parser).choices
        names = rest[0].split("/")
        if not all(name in choices for name in names):
            if not prefixed and len(path) == 1 and not any(name in choices for name in names):
                return None
            return "'%s' is not a %s command" % (rest[0], _label(path))
        if len(names) > 1:
            # Each subcommand is checked as if the span named it alone.
            position = len(argv) - len(rest)
            return next((error for error in (error_of(choices[name], path + (name,), rest[1:],
                                                      argv[:position] + [name] + argv[position + 1:])
                                             for name in names) if error), None)
        parser, path, rest = choices[names[0]], path + (names[0],), rest[1:]
    return error_of(parser, path, rest, argv)


def _globals_error(argv, given, path, rest, sources, help_request=False):
    """Why the command of a span rejects the global options it shows, or None.
    The launcher checks every span; the rules of runtime commands do not apply
    to a help request, which runs nothing."""
    error = sources.launcher_policy(argv, _placeholder)
    if error or help_request or path[0] in sources.launcher:
        return error
    options = {token.split("=", 1)[0] for token in given}
    for option, message in sources.runtime_rules(path, apply="--apply" in rest).items():
        if option in options:
            return message
    return None


# Markdown ------------------------------------------------------------------

def _indent(line):
    return len(line) - len(line.lstrip(" "))


def _fence(content):
    """The opening fence CONTENT starts with, or None: the info string of a
    backtick fence has no backtick."""
    fence = FENCE.match(content)
    if fence and not (fence.group(1)[0] == "`" and "`" in content[len(fence.group(1)):]):
        return fence
    return None


def _starts_list(item, content):
    """Whether a list item may interrupt a paragraph: it has content and, when
    ordered, starts with 1."""
    marker = item.group(1)
    return bool(content[item.end():].strip()) and (not marker[0].isdigit() or int(marker[:-1]) == 1)


def _interrupts(rest):
    """Whether a line that leaves an open container starts a block instead
    of continuing the paragraph inside it."""
    if _indent(rest) >= 4:
        return False
    content = rest.strip()
    return bool(THEMATIC.match(content) or _fence(content) or QUOTE.match(rest) or LIST_ITEM.match(content)
                or HEADING.match(content))


def code(text, first_line=1):
    """(kind, text, line) of every command line of a shell code block
    ("block") and every inline code span ("inline"), read the way CommonMark
    reads it: fenced and indented code blocks, also inside block quotes and
    list items, and spans that wrap across lines. Markdown code blocks are
    read as Markdown; other code blocks are skipped. Known differences: HTML
    blocks and link reference definitions are read as text, Unicode
    whitespace counts as spaces, and a tab in a Markdown code block counts
    columns from the start of its line on the page."""
    found, paragraph = [], []
    containers = []          # open containers: "quote", or a list item's content width
    fresh = set()            # indices of list items that have had no content yet
    state = {"block": None}  # the open code block

    def flush():
        found.extend(_spans(paragraph))
        paragraph.clear()

    def close():
        block = state["block"]
        found.extend(_block(block["info"], block["start"], block["body"]))
        state["block"] = None

    def enter(line):
        """(how many open containers LINE continues, its text inside them)."""
        rest = line
        for depth, container in enumerate(containers):
            if container == "quote":
                marker = QUOTE.match(rest)
                if marker is None:
                    return depth, rest
                rest = rest[marker.end():]
            elif rest.strip() and _indent(rest) < container:
                return depth, rest
            else:
                rest = rest[container:]
        return len(containers), rest

    def begin(rest, number):
        """A line outside code blocks; REST is its text inside the open containers."""
        relative, content = _indent(rest), rest.strip()
        if relative >= 4:
            if paragraph:
                paragraph.append((number, content))
            else:
                state["block"] = dict(kind="indented", info="", depth=len(containers), start=number,
                                      body=[rest[4:]])
            return
        if (paragraph and SETEXT.match(content)) or THEMATIC.match(content):
            flush()  # A heading underline or a thematic break.
            return
        fence = _fence(content)
        if fence:
            flush()
            state["block"] = dict(kind="fence", info=fence.group(2).lower(), depth=len(containers),
                                  start=number + 1, body=[], marker=fence.group(1), indent=relative)
            return
        quote = QUOTE.match(rest)
        if quote:
            flush()
            containers.append("quote")
            if rest[quote.end():].strip():
                begin(rest[quote.end():], number)
            return
        item = LIST_ITEM.match(content)
        if item and (not paragraph or _starts_list(item, content)):
            flush()
            spaces = len(item.group(2))
            width = relative + len(item.group(1)) + (spaces if 1 <= spaces <= 4 else 1)
            containers.append(width)
            if rest[width:].strip():
                begin(rest[width:], number)
            else:
                fresh.add(len(containers) - 1)
            return
        if HEADING.match(content):
            flush()
            paragraph.append((number, content))
            flush()
            return
        paragraph.append((number, content))

    for number, line in enumerate(text.expandtabs(4).splitlines(), first_line):
        depth, rest = enter(line)
        blank = not rest.strip()
        if not blank:
            fresh.difference_update(range(depth))  # The items this line continues have content.
        block = state["block"]
        if block is not None:
            if depth < block["depth"]:
                close()  # A container that holds the block ended.
            elif block["kind"] == "fence":
                closing = CLOSING.match(rest)
                if closing and closing.group(1)[0] == block["marker"][0] \
                        and len(closing.group(1)) >= len(block["marker"]):
                    close()
                else:
                    # Up to the opening fence's indentation is not content.
                    block["body"].append(rest[min(block["indent"], _indent(rest)):])
                continue
            elif blank or _indent(rest) >= 4:
                block["body"].append(rest[4:])
                continue
            else:
                close()
        if depth < len(containers):
            if paragraph and not blank and not _interrupts(rest):
                paragraph.append((number, rest.strip()))  # A lazy continuation line.
                continue
            flush()
            del containers[depth:]
            fresh.intersection_update(range(depth))
        if blank:
            flush()
            if fresh:
                # A list item can begin with one blank line, not two: it ends empty.
                del containers[min(fresh):]
                fresh.clear()
            continue
        begin(rest, number)
    flush()
    if state["block"] is not None:  # An unclosed block runs to the end of the page.
        close()
    return found


def _spans(paragraph):
    """Code spans as CommonMark reads them: a backtick run opens a span that
    the next run of the same length closes; an escaped backtick opens none."""
    text = "\n".join(line for _, line in paragraph)
    index = 0
    while index < len(text):
        if text[index] == "\\" and text[index + 1:index + 2] in PUNCTUATION:
            index += 2
            continue
        if text[index] != "`":
            index += 1
            continue
        end = index
        while end < len(text) and text[end] == "`":
            end += 1
        closing = re.compile(r"(?<!`)`{%d}(?!`)" % (end - index)).search(text, end)
        if closing is None:
            index = end
            continue
        content = " ".join(text[end:closing.start()].split())
        if content:
            yield "inline", content, paragraph[0][0] + text.count("\n", 0, index)
        index = closing.end()


def _block(info, start, body):
    if info in MARKDOWN:
        yield from code("\n".join(body), start)
        return
    if info not in SHELL:
        return
    pending, first = "", start
    for number, line in enumerate(body, start):
        if not pending:
            first = number
        line = (pending + " " + line.strip()).strip() if pending else line.strip()
        if line.endswith("\\"):
            pending = line[:-1]
            continue
        pending = ""
        yield "block", line, first


def _variants(words):
    """Command words with `a/b` at the subcommand position as separate paths."""
    names = words[1].split("/") if len(words) > 1 else []
    if len(names) > 1 and all(names):
        return [[words[0], name] + words[2:] for name in names]
    return [words]


def shown(text, sources):
    """Command paths a Markdown page shows in code."""
    globals_ = _global_options(sources)
    found = set()
    for _, fragment, _ in code(text):
        words = fragment.split()
        # A subcommand may be named on its own: `project scan ROOT`.
        for variant in _variants(words):
            for size in range(2, min(len(variant), 4) + 1):
                found.add(tuple(variant[:size]))
        for index, word in enumerate(words):
            if word == "richi":
                rest = words[index + 1:]
                for variant in _variants(rest[_after_globals(rest, globals_):]):
                    for size in range(1, min(len(variant), 4) + 1):
                        found.add(tuple(variant[:size]))
    return found


def markdown_files(root, sources=None):
    """Current documentation and its translations: the Markdown files of the
    repository root, docs/, evals/ and skills/ and, given sources, the guides
    that pages and catalog entries link wherever they are."""
    files = set(root.glob("*.md"))
    for folder in ("docs", "evals", "skills"):
        files |= set((root / folder).rglob("*.md"))
    if sources is not None:
        guides = [page.parts.guide for page in pages(sources).values() if page.parts is not None and page.parts.guide]
        for guide in guides + [row[4] for row in sources.catalog]:
            relative = sources.style.guide_path(guide)
            if relative is not None:
                files |= {path for path in (root / relative, _ru(root / relative)) if path.is_file()}
    return sorted(files)


def historical(path):
    """Dated reports and evaluations record the CLI as it was."""
    stem = path.name[:-len(".ru.md")] if path.name.endswith(".ru.md") else path.stem
    return bool(re.search(r"-[0-9]{4}-[0-9]{2}-[0-9]{2}$", stem)) or stem.endswith("_EVAL")


def _ru(path):
    return path.with_name(path.name[:-len(".md")] + ".ru.md")


def _topic_texts(sources):
    """{topic: text} of the well-formed (summary, text) entries of TOPICS."""
    return {name: entry[1] for name, entry in sources.topics.items()
            if isinstance(entry, tuple) and len(entry) == 2 and isinstance(entry[1], str)}


def _texts(page):
    """Help text of a page: its summary, description and argument help."""
    parts = page.parts
    texts = [parts.summary, parts.about] if parts is not None else [page.parser.description]
    return [text for text in texts + [action.help for action in page.parser._actions] if text]


# Rules ---------------------------------------------------------------------

def check(sources):
    problems = []

    def report(rule, subject, message):
        problems.append(Problem(rule, subject, message))

    found = pages(sources)
    texts = {}

    def text(path):
        if path not in texts:
            texts[path] = path.read_text(encoding="utf-8") if path.is_file() else None
        return texts[path]

    mentions = {}

    def shows(path, command):
        if path not in mentions:
            mentions[path] = shown(text(path) or "", sources)
        return command in mentions[path]

    def guide_files(guide, subject):
        relative = sources.style.guide_path(guide)
        if relative is None:
            report("guide", subject, "guide %s is not a file in this repository" % guide)
            return []
        english = sources.root / relative
        files = []
        for path in (english, _ru(english)):
            if path.is_file():
                files.append(path)
            else:
                report("guide", subject, "guide %s does not exist" % path.relative_to(sources.root))
        return files

    def render(subject, function):
        try:
            function()
        except Exception as exc:  # A page that fails here fails for its reader.
            report("help", subject, "page fails to render: %s: %s" % (type(exc).__name__, exc))

    def examples_of(subject, lines):
        """Report failing lines; return the lines that parse."""
        passed = []
        for line in lines:
            error = check_line(line, sources)
            if error:
                report("examples", subject, "example `%s` fails: %s" % (line, error))
            else:
                passed.append(line)
        return passed

    # help, examples, guide, mention
    for action in sources.runtime._actions:
        if action.option_strings and not action.help:
            report("help", "richi", "global option %s has no help text" % action.option_strings[0])
    options = {option for path, parser, _ in sources.style.walk(sources.runtime) if path
               for action in parser._actions for option in action.option_strings}
    for table, entries in (("COMMON_HELP", sources.common_help), ("METAVARS", sources.metavars)):
        for option in sorted(set(entries) - options):
            report("help", "cli_help." + table, "names %s, which no runtime command has" % option)
    for path, page in found.items():
        label = _label(path)
        render(label, page.parser.format_help)
        if not page.summary:
            report("help", label, "has no help= summary for command lists")
        if not page.parser.description:
            report("help", label, "has no description")
        if page.parser.prog != label:
            report("help", label, "prints usage as '%s'" % page.parser.prog)
        for action in page.parser._actions:
            if isinstance(action, (argparse._HelpAction, argparse._VersionAction, argparse._SubParsersAction)):
                continue
            if not action.help:
                name = action.option_strings[0] if action.option_strings else action.dest.upper()
                report("help", label, "%s has no help text" % name)
        parts = page.parts
        examples = []
        for _, lines in (parts.examples if parts is not None else ()):
            examples.extend((lines,) if isinstance(lines, str) else lines)
        passed = examples_of(label, examples)
        if _leaf(page, sources) and not any(invoked(line, sources) == path for line in passed):
            report("examples", label, "no example runs this command")
        guide = parts.guide if parts is not None else None
        if guide is None:
            report("guide", label, "page links no guide")
            continue
        for file in guide_files(guide, label):
            if not shows(file, path):
                report("mention", label, "%s never shows it in code" % file.relative_to(sources.root))
    for name in sources.stubs:
        stub = sources.style.subcommands(sources.runtime).choices.get(name)
        if stub is not None:
            render(_label((name,)) + " (runtime)", stub.format_help)
    render("richi help", lambda: sources.help.root_help(sources.runtime))
    render("richi", lambda: sources.help.overview(sources.runtime))
    for name, entry in sources.topics.items():
        render("richi help " + name, lambda name=name: sources.help.topic(name, sources.topics))
        if not (isinstance(entry, tuple) and len(entry) == 2 and all(isinstance(part, str) for part in entry)):
            report("help", "richi help " + name, "the TOPICS entry is not a (summary, text) pair of strings")
    # richi help reads `topics` and the topic names before command names.
    commands = sources.style.subcommands(sources.runtime).choices
    for name in sorted(({"topics"} | set(sources.topics)) & set(commands)):
        report("help", _label((name,)), "richi help %s shows %s instead of this page"
               % (name, "the list of help topics" if name == "topics" else "a help topic"))
    if "topics" in sources.topics:
        report("help", "richi help topics", "the list of help topics hides this topic")
    examples_of("richi help", [line for _, line in sources.examples])
    examples_of("richi", sources.overview)
    # Topics: command lines indented as examples. Malformed entries are
    # reported above.
    for name, body in _topic_texts(sources).items():
        for line in textwrap.dedent(body).splitlines():
            if line.startswith(" ") and line.strip().startswith("richi "):
                error = check_line(line.strip(), sources)
                if error:
                    report("examples", "richi help " + name, "`%s` fails: %s" % (line.strip(), error))
    guide = found.get(("help",))
    if guide is not None and guide.parts is not None and guide.parts.guide:
        for file in guide_files(guide.parts.guide, "richi help"):
            fragments = [fragment for _, fragment, _ in code(text(file) or "")]
            for name in sources.topics:
                # As a page name, so a removed topic is a stale `richi help NAME`.
                if not any(re.search(r"\bhelp %s\b" % re.escape(name), fragment) for fragment in fragments):
                    report("mention", "richi help " + name,
                           "%s never shows it in code" % file.relative_to(sources.root))

    # catalog
    leaves = {path for path, page in found.items() if _leaf(page, sources)}
    catalog = {}
    for row in sources.catalog:
        catalog[tuple(row[0].split())] = row
    excluded = {tuple(key.split()): reason for key, reason in sources.uncataloged.items()}

    def excluded_by(path):
        return [path[:size] for size in range(1, len(path) + 1) if path[:size] in excluded]

    for path in sorted(leaves):
        listed, skipped = path in catalog, excluded_by(path)
        if listed and skipped:
            report("catalog", _label(path), "is both cataloged and excluded as '%s'" % " ".join(skipped[0]))
        elif not listed and not skipped:
            report("catalog", _label(path), "is neither in capabilities.COMMANDS nor in capabilities.UNCATALOGED")
    for path, row in catalog.items():
        subject = "capabilities " + row[0]
        if path not in leaves:
            report("catalog", subject, "names no command")
            continue
        if any(mark in row[4] for mark in ("/", "#", "://")):
            report("catalog", subject, "guide %s must be NAME.md under %s, as capabilities describe prints "
                   "references/NAME" % (row[4], sources.style.REFERENCES))
            continue
        parts = found[path].parts
        page_guide = parts.guide if parts is not None else None
        if page_guide is None or sources.style.guide_path(page_guide) != sources.style.guide_path(row[4]):
            report("catalog", subject, "the catalog names guide %s, but its help page links %s" % (row[4], page_guide))
        for file in guide_files(row[4], subject):
            if not shows(file, path):
                report("mention", subject, "%s never shows it in code" % file.relative_to(sources.root))
    for path, reason in excluded.items():
        if path not in found:
            report("catalog", "UNCATALOGED " + " ".join(path), "names no command")
        if not str(reason or "").strip():
            report("catalog", "UNCATALOGED " + " ".join(path), "gives no reason")

    # overview
    top = sources.style.subcommands(sources.runtime)
    grouped = [name for _, names in sources.groups for name in names]
    for name in top.choices:
        count = grouped.count(name)
        if count != 1:
            report("overview", _label((name,)), "appears in %d groups of richi help" % count)
    for name in sorted(set(grouped) - set(top.choices)):
        report("overview", _label((name,)), "is grouped but is not a command")

    # links
    names = {" ".join(path) for path in found}
    for key, detail in sources.details.items():
        if key not in names or found[tuple(key.split())].launcher:
            report("links", "DETAILS " + key, "is not a runtime command page")
        for field in sorted(set(detail) - DETAIL_FIELDS):
            report("links", "DETAILS " + key, "has unknown field '%s' (expected %s)"
                   % (field, ", ".join(sorted(DETAIL_FIELDS))))
    referring = [(_label(path), _texts(page)) for path, page in found.items()]
    referring += [("richi help " + name, [body]) for name, body in _topic_texts(sources).items()]
    referring.append(("richi", [action.help for action in sources.runtime._actions if action.help]))
    for subject, strings in referring:
        for string in strings:
            for match in HELP_REFERENCE.finditer(string):
                words = match.group(1).split()
                if words[0] in PROSE and _help_target(words[:1], sources):
                    continue  # Prose about richi help itself.
                error = _help_target(words, sources, strict=False)
                if error:
                    report("links", subject, "help text refers to richi help %s: %s" % (words[0], error))
    for path, page in found.items():
        for target in (page.parts.see if page.parts is not None else ()):
            if target not in names and target not in sources.topics:
                report("links", _label(path), "see also names unknown page '%s'" % target)

    # mirror
    launcher = set(sources.launcher)
    if launcher != set(sources.launcher_commands):
        report("mirror", "LAUNCHER_COMMANDS", "differs from launcher_parsers(): %s"
               % ", ".join(sorted(launcher ^ set(sources.launcher_commands))))
    for name in sorted(launcher - set(sources.stubs)):
        report("mirror", _label((name,)), "is missing from richi.cli_parser.LAUNCHER_COMMANDS")
    for name in sorted(set(sources.stubs) - launcher):
        report("mirror", _label((name,)), "is listed by the runtime but is not a launcher command")
    for name in sorted(set(sources.stubs) & launcher):
        if name not in top.choices:
            report("mirror", _label((name,)), "is not declared by the runtime parser")
        page = sources.style.page_parts(sources.launcher[name])
        expected = sources.style.sentence(sources.stubs[name])
        if page is None or page.summary != expected:
            report("mirror", _label((name,)), "the runtime lists it as '%s', its page says '%s'"
                   % (expected, page.summary if page is not None else None))
    declared = {option for action in sources.runtime._actions for option in action.option_strings
                if not isinstance(action, (argparse._HelpAction, argparse._VersionAction))}
    for option in sorted(declared - set(sources.global_options)):
        report("mirror", "richi", "the launcher does not consume global option %s" % option)
    for option in sorted(set(sources.global_options) - declared):
        report("mirror", "richi", "the runtime does not declare global option %s" % option)
    for option in sorted(set(sources.global_flags) - set(sources.global_options)):
        report("mirror", "richi", "GLOBAL_FLAGS names %s, which GLOBAL_OPTIONS lacks" % option)
    flags = {option for action in sources.runtime._actions for option in action.option_strings if action.nargs == 0}
    for option in sorted((declared & set(sources.global_options)) & (flags ^ set(sources.global_flags))):
        report("mirror", "richi", "global option %s takes %s in the runtime but %s in the launcher"
               % ((option,) + (("no value", "a value") if option in flags else ("a value", "none"))))
    for name in sorted(set(top.choices) - set(sources.stubs)):
        close = sources.style.suggestions(name, sorted(sources.launcher_commands))
        if close:
            report("mirror", _label((name,)), "looks like launcher command '%s'; an unbound chat would "
                   "report it as a mistyped one" % close[0])

    # docs, translation
    for file in markdown_files(sources.root, sources):
        relative = file.relative_to(sources.root)
        if file.name.endswith(".ru.md"):
            if not file.with_name(file.name[:-len(".ru.md")] + ".md").is_file():
                report("translation", str(relative), "has no English page")
        elif file.name not in UNTRANSLATED and not _ru(file).is_file():
            report("translation", str(relative), "has no Russian page")
        if historical(file):
            continue
        for kind, fragment, line in code(text(file)):
            if kind == "block" and fragment.startswith("richi "):
                error = check_line(fragment, sources)
            elif kind == "inline":
                error = check_mention(fragment, sources)
            else:
                continue
            if error:
                report("docs", str(relative), "line %d: `%s`: %s" % (line, fragment, error))
    return problems


def main(argv=None):
    argparse.ArgumentParser(description=__doc__.split("\n\n")[0]).parse_args(argv)
    sources = current()
    problems = check(sources)
    if not problems:
        lines = sum(1 for file in markdown_files(sources.root, sources) if not historical(file)
                    for _, fragment, _ in code(file.read_text(encoding="utf-8")) if fragment.startswith("richi "))
        print("CLI documentation: %d commands and %d documented command lines checked; OK"
              % (len(pages(sources)), lines))
        return 0
    print("CLI documentation: %d problem%s" % (len(problems), "" if len(problems) == 1 else "s"))
    for problem in problems:
        print("  [%s] %s: %s" % problem)
    print("\nHow to fix:")
    for rule in sorted({problem.rule for problem in problems}):
        print("  %-12s %s" % (rule, FIXES[rule]))
    return 1


if __name__ == "__main__":
    # Check the sources being edited, not a previously installed package.
    sys.path[:0] = [str(ROOT / "launcher"), str(ROOT / "src")]
    sys.exit(main())
