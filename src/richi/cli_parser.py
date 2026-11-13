"""CLI declarations, separate from storage and command execution.

The launcher owns recovery-command parsing independently of the selected runtime.
Command builders receive only argument types, never the storage module.
Every command and option carries a help string; cli_help adds the longer
pages, examples and the grouped overview when help is printed.
"""
import argparse
import os
from pathlib import Path
from types import SimpleNamespace

from . import compact, graph

# Set by a running map that checks this runtime before handing itself off to
# it (richi_launcher.maps.PROBE_ENV). --version then also names what this
# runtime's map server takes over, and fails if that server cannot be imported.
MAP_PROBE_ENV = "RICHI_MAP_PROBE"


class _Version(argparse._VersionAction):
    """--version; a version action still, so help pages and the documentation
    validator treat it as one."""

    def __call__(self, parser, namespace, values, option_string=None):
        if os.environ.get(MAP_PROBE_ENV) == "1":
            from .serve import answer_probe
            answer_probe()
        print(self.version, flush=True)
        parser.exit()


BOOST = "Boost this project's records in ranking (not a filter; see --where)"
QUESTION = "Question or keywords; try both RU and EN terms"
TAGLINE = "local engineering memory for projects and AI agents"
# Commands the installed launcher parses and runs itself, with the summaries
# that `richi help` lists. The runtime declares them only so the list is
# complete and a direct runtime call is refused; their pages and options live
# in the launcher (scripts/check_cli_docs.py keeps both sides equal).
LAUNCHER_COMMANDS = {
    "agents": "Find local AI agents and connect one",
    "setup": "Connect this installation to an AI agent",
    "update": "Check, install or roll back stable Richi releases",
    "status": "Diagnose the chat binding, workspace, runtime and project paths",
    "workspace": "List, create, inspect or select isolated workspaces",
    "use": "Select the default workspace for future terminal commands",
    "chat": "Show or set the workspace bound to an agent chat",
    "config": "Inspect effective settings and choose installed or development code",
}


def build_parser(parser_class, *, limit, positive_int, scan_depth, bounded):
    from . import __version__
    argument_types = SimpleNamespace(limit=limit, compact=compact)

    class Tree(parser_class):
        """The parsers of one command tree; printing any page documents the
        whole tree once (cli_help.decorate)."""

    class Root(Tree):
        def format_help(self):
            from . import cli_help
            return cli_help.root_help(self)

    cli = Root(prog="richi", add_help=False, description="Richi " + __version__ + " - " + TAGLINE + ".")
    Tree.root = cli
    cli.add_argument("-w", "--workspace", metavar="NAME", help="Use this workspace for one command (see richi help workspace)")
    cli.add_argument("--project-path", type=Path, metavar="PATH",
                     help="First verify that this repository belongs to the workspace")
    cli.add_argument("--compact-json", action="store_true",
                     help="Print JSON without indentation (--max-chars measures it)")
    cli.add_argument("--chat", metavar="ID", help="Chat identity for workspace binding (agents set it automatically)")
    cli.add_argument("--db", metavar="PATH", help="Use this SQLite file (overrides environment and config)")
    cli.add_argument("--config", metavar="FILE", help="Use this JSON configuration file instead of a workspace")
    cli.add_argument("--version", action=_Version, version="Richi " + __version__, help="Print the Richi version and exit")
    cli.add_argument("-h", "--help", action="help", help="Show this help and exit")
    # Without a command the runtime prints a short overview instead of an error.
    commands = cli.add_subparsers(dest="command", parser_class=Tree)
    for name, summary in LAUNCHER_COMMANDS.items():
        commands.add_parser(name, help=summary).add_argument(
            "arguments", nargs=argparse.REMAINDER, help="Parsed by the installed launcher")
    map_cmd = commands.add_parser("map", help="Open the interactive knowledge map in a browser")
    map_cmd.add_argument("action", nargs="?", choices=("serve", "stop", "restart"),
                         help="serve: run the server in this terminal instead of the background; "
                              "stop or restart: manage the map that is running")
    map_cmd.add_argument("--db", default=argparse.SUPPRESS, metavar="PATH", help="SQLite path")
    map_cmd.add_argument("--config", default=argparse.SUPPRESS, metavar="FILE", help="JSON configuration file")
    map_cmd.add_argument("--port", help="Loopback HTTP port (default: from configuration, usually 8765)")
    map_cmd.add_argument("--no-open", action="store_true", help="Launch or restart without opening a browser")
    map_cmd.add_argument("--open", action="store_true", help="Open the browser (serve and restart)")
    commands.add_parser("init", help="Create the selected workspace's knowledge store (safe to repeat)")
    for name, action, summary, write in (
            ("project", "upsert", "Register, list and check projects in the workspace", "Create or replace projects from JSON"),
            ("entry", "put", "Read or write knowledge entries (tasks, decisions, facts...)", "Create or replace entries from JSON"),
            ("relation", "put", "Write a dependency between two projects", "Create or replace project relations from JSON")):
        group = commands.add_parser(name, help=summary).add_subparsers(dest="action", required=True)
        group.add_parser(action, help=write).add_argument("--json", required=True, metavar="FILE")
        if name == "project":
            group.add_parser("list", help="List registered projects")
            add = group.add_parser("add", help="Register an existing project folder in the selected workspace")
            add.add_argument("path", type=Path, help="Project folder, usually a repository root")
            add.add_argument("--id", dest="project_id", help="Project ID (default: the folder name)")
            add.add_argument("--name", help="Display name (default: the folder name)")
            add.add_argument("--description", default="", help="Short description of the project")
            scan = group.add_parser("scan", help="Preview or atomically register Git repositories under a directory")
            scan.add_argument("path", type=Path, help="Folder to search for Git repositories")
            scan.add_argument("--apply", action="store_true", help="Apply the complete collision-free discovery plan")
            scan.add_argument("--max-depth", type=scan_depth, default=3,
                              help="Folder levels to descend, 0-20 (default: %(default)s)")
            check = group.add_parser("check", help="Resolve a path against projects in this workspace only")
            check.add_argument("path", type=Path, help="A path inside the checkout, for example .")
            check.add_argument("--id", dest="project_id", help="Expected project ID; report a mismatch otherwise")
        elif name == "entry":
            read = group.add_parser("get", help="Read one whole entry by ID")
            read.add_argument("id", help="Entry ID, for example task:ABC-123")
            read.add_argument("--history", action="store_true")
            short = group.add_parser("read", help="Read a size-limited card of one entry")
            short.add_argument("id", help="Entry ID, for example task:ABC-123")
            short.add_argument("--max-chars", type=compact.max_chars, default=6000)
    query = commands.add_parser("search", help="Lexical search over entries, with strict filters")
    query.add_argument("query", help="Words to match in IDs, titles, summaries, Jira keys, tags and aliases")
    query.add_argument("--project", help="Only entries of this project")
    query.add_argument("--where", help="Strict composed filter; AND/OR/NOT, comparisons, IN, LIKE, HAS")
    ctx = commands.add_parser("context", help="Show a project with its direct relations and recent entries")
    ctx.add_argument("--project", required=True, help="Project ID")
    for command in (query, ctx):
        command.add_argument("--limit", type=limit, default=20)
        command.add_argument("--include-superseded", action="store_true")
    commands.add_parser("backup", help="Write a verified copy of the knowledge database").add_argument(
        "--output", metavar="FILE", help="New file for the copy (default: backups/ next to the database)")
    commands.add_parser("check", help="Verify database integrity, schema and foreign keys")
    from . import retrieval_tools, storage, query_tools, capabilities
    capabilities.add_commands(commands, argument_types)
    query_tools.add_commands(commands, argument_types)
    retrieval_tools.add_commands(commands, argument_types)
    storage.add_commands(commands, argument_types)
    recall_cmd = commands.add_parser("recall", help="Answer a question with ranked excerpts and linked evidence")
    recall_cmd.add_argument("query", help=QUESTION)
    recall_cmd.add_argument("--project", help=BOOST)
    recall_cmd.add_argument("--where", help="Strict composed filter applied before shortlist and to graph results")
    recall_cmd.add_argument("--limit", type=limit, default=8)
    recall_cmd.add_argument("--max-chars", type=compact.max_chars, default=16000)
    recall_cmd.add_argument("--include-hypotheses", action="store_true")
    recall_cmd.add_argument("--include-superseded", action="store_true")
    recall_cmd.add_argument("--explain", action="store_true", help="Explain the actual retrieval stages within the output budget")
    recall_cmd.add_argument("--expect", metavar="REF", help="Diagnose one expected entry:<id> or entity:<id>; implies --explain")
    index_cmd = commands.add_parser("search-index", help="Build or inspect the optional derived lookup index")
    index_actions = index_cmd.add_subparsers(dest="action", required=True)
    index_actions.add_parser("build", help="Build or rebuild lookup keys and filter indexes atomically")
    index_actions.add_parser("status", help="Report index availability and fallback records without writes")
    brief_cmd = commands.add_parser("brief", help="Collect task context: knowledge, changed files, source checks")
    brief_cmd.add_argument("query", help="The task or question to collect context for")
    brief_cmd.add_argument("--project", help="Recall project boost; scopes optional Git source checks and identifies a diff repository")
    brief_cmd.add_argument("--base", help="Explicit endpoint diff base; without base or --check-sources no Git operations run")
    brief_mode = brief_cmd.add_mutually_exclusive_group()
    brief_mode.add_argument("--target", help="Local target ref with --base or --check-sources (default: HEAD)")
    brief_mode.add_argument("--worktree", action="store_true", help="Compare --base with the working tree instead of --target")
    brief_cmd.add_argument("--repo", metavar="PATH", help="Linked worktree; requires --project and --base or --check-sources")
    brief_cmd.add_argument("--check-sources", action="store_true", help="Compare selected cards' Git evidence without verifying claims or updating memory")
    brief_cmd.add_argument("--check-limit", type=bounded(1, 50, "check-limit"),
                           help="Maximum source check attempts, 1-50 (default: 12); requires --check-sources")
    brief_cmd.add_argument("--limit", type=bounded(1, 50, "limit"), default=8)
    brief_cmd.add_argument("--max-chars", type=bounded(4000, 100000, "max-chars"), default=16000)
    brief_cmd.add_argument("--include-hypotheses", action="store_true")
    brief_cmd.add_argument("--include-superseded", action="store_true")
    punisher_cmd = commands.add_parser("punisher", help="Find stale, conflicting or oversized knowledge to review")
    punisher_cmd.add_argument("--project", help="Inspect strict membership/scope, not the connected component")
    punisher_cmd.add_argument("--limit", type=limit, default=20)
    punisher_cmd.add_argument("--max-chars", type=compact.max_chars, default=16000)
    punisher_cmd.add_argument("--stale-days", type=positive_int, default=180,
                              help="Report verification dates older than this many days (default: %(default)s)")
    punisher_cmd.add_argument("--summary-warn-chars", type=bounded(1, 1000000, "summary-warn-chars"), default=6000,
                              help="Review summaries longer than this many Unicode characters (1-1000000; default: 6000); a reading heuristic, not a quality limit")
    punisher_cmd.add_argument("--as-of", metavar="DATE", help="ISO date for reproducible age checks (default: today in UTC)")
    source_cmd = commands.add_parser("sources", help="Capture Git evidence, read code history, check for drift")
    source_actions = source_cmd.add_subparsers(dest="action", required=True)
    source_snapshot = source_actions.add_parser("snapshot", help="Save a new source/revision manifest without changing knowledge")
    source_snapshot.add_argument("--ref", dest="refs", action="append", help="Exact project:<id>, entry:<id> or entity:<id>; repeatable")
    source_snapshot.add_argument("--project", help="Snapshot the project's strict scope")
    source_snapshot.add_argument("--output", required=True, metavar="FILE", help="NEW manifest JSON file; existing files are never overwritten")
    source_check = source_actions.add_parser("check", help="Compare a manifest with current records, edges and local source files")
    source_check.add_argument("--manifest", required=True, metavar="FILE", help="Manifest written by sources snapshot")
    source_check.add_argument("--max-chars", type=compact.max_chars, default=16000)
    capture = source_actions.add_parser("capture", help="Capture a Git-backed source; default: observed working-copy bytes")
    capture.add_argument("--project", required=True, help="Registered repository project ID")
    capture.add_argument("--path", required=True, help="Repository-relative file path")
    capture_mode = capture.add_mutually_exclusive_group()
    capture_mode.add_argument("--rev", help="Capture committed bytes at this revision instead of the working copy")
    capture_mode.add_argument("--worktree", action="store_true", help="Capture working-copy bytes (default)")
    capture.add_argument("--repo", metavar="PATH", help="Explicit checkout of the registered repository; otherwise use --project-path, then registered repo_path")
    capture.add_argument("--output", metavar="FILE", help="NEW source JSON file for attach/show; never overwritten")
    capture.add_argument("--max-chars", type=compact.max_chars, default=16000)
    attach = source_actions.add_parser("attach", help="Append a captured source using optimistic concurrency and existing history")
    attach.add_argument("--ref", required=True, help="entry:<id>, entity:<id>, or edge:<id>")
    attach.add_argument("--json", dest="json_file", required=True, metavar="FILE", help="Captured source JSON file")
    attach.add_argument("--expected-updated-at", required=True, metavar="TIMESTAMP",
                        help="updated_at from a fresh read of the record")
    attach.add_argument("--max-chars", type=compact.max_chars, default=16000)
    git_check = source_actions.add_parser("git-check", help="Compare attached Git evidence with a chosen local revision or working copy")
    git_scope = git_check.add_mutually_exclusive_group(required=True)
    git_scope.add_argument("--ref", dest="refs", action="append", help="Record reference; repeatable")
    git_scope.add_argument("--project", help="Repository project ID referenced by source.git.repo_id")
    git_check_mode = git_check.add_mutually_exclusive_group()
    git_check_mode.add_argument("--target", default="HEAD", help="Local target ref (default: HEAD); never fetches")
    git_check_mode.add_argument("--worktree", action="store_true", help="Compare with current working-copy bytes")
    git_check.add_argument("--repo", metavar="PATH", help="Optional linked worktree of the registered repository")
    git_check.add_argument("--source", type=positive_int, metavar="N", help="Check one source in a single --ref, including legacy Git references")
    git_check.add_argument("--path", help="Changed file to check from a bare legacy commit; requires --source and one --ref")
    git_check.add_argument("--max-chars", type=compact.max_chars, default=16000)
    for action, text in (("show", "Read the recorded file content at its source revision"),
                         ("history", "Read the bounded commit history of a recorded file"),
                         ("diff", "Compare recorded bytes with a revision or the working copy"),
                         ("commit", "Read commit metadata and recorded changes")):
        operation = source_actions.add_parser(action, help=text + " without checkout")
        selection = operation.add_mutually_exclusive_group(required=True)
        selection.add_argument("--ref", help="Entry/entity/edge reference containing the source")
        if action != "commit":
            selection.add_argument("--json", dest="json_file", metavar="FILE", help="Captured source JSON file")
        operation.add_argument("--source", type=positive_int, default=1, metavar="N", help="1-based index in the full record's sources array")
        operation.add_argument("--repo", metavar="PATH", help="Optional linked worktree of the registered repository")
        operation.add_argument("--max-chars", type=compact.max_chars, default=16000)
        if action != "commit":
            operation.add_argument("--path", help="Select an available changed file from a bare legacy git:FULL_SHA source")
        if action == "history":
            operation.add_argument("--limit", type=bounded(1, 100, "limit"), default=20)
            operation.add_argument("--target", help="History ending at this local ref (default: captured commit)")
        elif action == "diff":
            comparison = operation.add_mutually_exclusive_group()
            comparison.add_argument("--target", default="HEAD", help="Local target ref (default: HEAD)")
            comparison.add_argument("--worktree", action="store_true", help="Compare with the working copy instead of --target")
    related = source_actions.add_parser("related", help="Find knowledge attached to a repository path and optional commit")
    related.add_argument("--project", required=True, help="Repository project ID")
    related.add_argument("--path", required=True, help="Exact repository-relative path")
    related.add_argument("--commit", help="Exact full commit object ID")
    related.add_argument("--limit", type=limit, default=20)
    related.add_argument("--max-chars", type=compact.max_chars, default=16000)
    impact = source_actions.add_parser("impact", help="Find recorded knowledge for changed files; direct source associations, not semantic impact")
    impact.add_argument("--project", required=True, help="Registered repository project ID")
    impact.add_argument("--repo", metavar="PATH", help="Optional linked worktree of the registered repository")
    impact.add_argument("--base", required=True, help="Local base ref; compares exact endpoints, not an inferred merge base")
    impact_mode = impact.add_mutually_exclusive_group()
    impact_mode.add_argument("--target", default="HEAD", help="Local target ref (default: HEAD)")
    impact_mode.add_argument("--worktree", action="store_true", help="Compare base with working-copy files, including non-ignored untracked files")
    impact.add_argument("--limit", type=limit, default=12)
    impact.add_argument("--max-chars", type=compact.max_chars, default=16000)
    graph.add_commands(commands, limit)
    from . import tool_catalog
    tool_catalog.add_commands(commands, limit)
    # Words after the first one stay words, so `richi help recall --help`
    # shows recall's page and an option there is reported by cli_help.page,
    # as the launcher's pages do; a leading -h shows this page.
    help_cmd = commands.add_parser("help", help="Show help for a command or topic",
                                   usage="%(prog)s [-h] [COMMAND|TOPIC ...]")
    help_cmd.add_argument("topic", nargs=argparse.REMAINDER, metavar="COMMAND|TOPIC",
                          help="A command path such as sources capture, or a topic such as filters")
    return cli
