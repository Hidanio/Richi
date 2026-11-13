"""The launcher's workspace, chat and configuration commands.

Imported only when one of them runs or its page is printed, so other
commands never compile these parsers and pages.
"""
import argparse
from dataclasses import replace
import os
import sys

from .config import ConfigError, _path_text, resolve_settings, set_config_value
from .chats import bind_chat, current_chat, reject_storage_overrides
from .cli import Parser, _emit, _interactive, usage
from .help_format import complete, document
from .runtime import installed_runtime, resolve_runtime
from .workspaces import _name, create_workspace, list_workspaces, use_workspace


def workspace_name(value):
    """Argument type of a workspace NAME: argparse reports a malformed one."""
    try:
        return _name(value)
    except ConfigError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def config_parser():
    parser = Parser(prog="richi config")
    commands = parser.add_subparsers(dest="action", required=True)
    show = commands.add_parser("show", help="Print effective settings and the selected runtime as JSON")
    change = commands.add_parser("set", help="Change one setting in the selected workspace's configuration")
    change.add_argument("key", choices=("dev", "development.source"), help="Setting to change: dev or development.source")
    change.add_argument("value", help="true or false for dev; a Richi checkout path for development.source")
    document(parser, "Inspect effective settings and choose installed or development code.", """
        Reads the selected workspace's configuration without opening its knowledge
        database: database and data paths, map port, dev mode, and both the
        selected and the installed runtime. Changes write the selected
        workspace's configuration file and never touch knowledge; in a registered
        workspace, config set dev (and development.source while dev is on) also
        re-points the managed agent skill links, which are user-wide, and reports
        them in agent_skills.""",
             examples=(("Where is the database and which code runs?", "richi config show"),
                       ("Run the checkout's sources for CLI, agents and map",
                        ("richi config set development.source /path/to/Richi", "richi config set dev true")),
                       ("Return to the installed release", "richi config set dev false")),
             see=("workspace", "environment"), guide="docs/INSTALLATION.md")
    document(show, about="""
        Never opens the knowledge database, so it also works while the development
        checkout is broken. Use it to locate config_file, database and data_dir
        instead of guessing paths. agent_skills tells where the managed agent
        skill links point; when they do not follow this configuration,
        matches_config is false and next_command is the command that re-points
        them.""",
             examples=(("", "richi config show"), ("Settings of another workspace", "richi -w work config show")))
    document(change, about="""
        dev true selects the sources under development.source for CLI, agents and
        the map; edits take effect on the next command and reload a running map,
        but in an agent chat, help pages and --version still come from the
        installed release. dev false restores the installed package and skills.
        In a registered workspace, a change of dev, or of development.source while
        dev is on, also re-points the managed agent skill links, which are
        user-wide; agent_skills reports them. Knowledge data is shared by both
        modes, and SQLite is never migrated automatically.""",
             examples=(("", ("richi config set development.source /path/to/Richi", "richi config set dev true")),
                       ("Back to the installed release", "richi config set dev false")))
    return complete(parser)


def config_arguments(argv):
    """The parsed `richi config ARGV`. A malformed value is an argument error
    (UsageError), checked before any lookup and before the chat is pinned."""
    args = config_parser().parse_args(argv)
    if args.action == "set":
        if args.key == "dev":
            if args.value not in {"true", "false"}:
                from .help_format import did_you_mean, suggestions
                raise usage("dev must be true or false." + did_you_mean(suggestions(args.value, ("true", "false"))),
                            "richi help config set")
        else:
            # Only the form: a path that cannot be resolved stays a plain error.
            try:
                _path_text(args.value, "development.source")
            except ConfigError as exc:
                raise usage(str(exc), "richi help config set") from exc
    return args


def configuration(args, selectors, *, compact_json=False):
    """Run `richi config` with ARGS from config_arguments()."""
    value = args.value if args.action == "set" else None
    if args.action == "set" and args.key == "dev":
        value = value == "true"
    # Resolve once, so another process changing the default workspace cannot
    # redirect a config update or its result to a different workspace.
    custom_config = selectors.get("config_file") is not None or (
        selectors.get("workspace") is None and "RICHI_CONFIG" in os.environ)
    settings = resolve_settings(**selectors, config_required=(False if args.action == "set" and custom_config else None))
    if args.action == "set":
        if settings.workspace is not None and not custom_config:
            # A named selection keeps requiredness and managed-storage checks
            # through the locked update, even if its config disappears meanwhile.
            set_config_value(args.key, value, workspace=settings.workspace)
            settings = resolve_settings(workspace=settings.workspace, port=settings.port,
                                        db=settings.database if settings.workspace == "default" else None)
        else:
            set_config_value(args.key, value, config_file=settings.config_file)
            settings = replace(resolve_settings(db=settings.database, config_file=settings.config_file,
                                                port=settings.port), workspace=settings.workspace)
    result = settings.as_dict()
    try:
        result["runtime"] = dict(resolve_runtime(settings).as_dict(), available=True)
    except ConfigError as exc:
        result["runtime"] = {"mode": "dev" if settings.dev else "release", "available": False, "error": str(exc)}
    try:
        result["installed_runtime"] = installed_runtime().as_dict()
    except ConfigError as exc:
        result["installed_runtime"] = {"available": False, "error": str(exc)}
    # Managed agent skills are user-wide and follow the latest dev switch of a
    # registered workspace: `config set dev` re-points them, as does a new
    # source while dev is on. An ad hoc --config/RICHI_CONFIG file changes only
    # itself. The installed launcher does this, so broken sources never block it.
    from . import agent_setup
    from .workspaces import registered_config
    registered = not custom_config or registered_config(settings.config_file)
    if registered and args.action == "set" and (args.key == "dev" or settings.dev):
        result["agent_skills"] = agent_setup.switch_skills(settings.dev, settings.development_source)
    else:
        result["agent_skills"] = agent_setup.skill_links(settings.dev, settings.development_source,
                                                         registered=registered)
        if not registered and args.action == "set" and result["agent_skills"]["status"] == "configured":
            result["agent_skills"]["status"] = "skipped"
    return _emit(result, compact_json=compact_json)


def _choose_workspace(target):
    # No chat gets here: a chat may never change the default.
    if not _interactive(None):
        raise usage("A workspace name is required without an interactive terminal; "
                    "use richi workspace list, then richi use NAME.", target)
    listing = list_workspaces()
    choices = listing["workspaces"]
    print("Workspaces:", file=sys.stderr)
    for number, record in enumerate(choices, 1):
        suffix = " (current)" if record["name"] == listing["current"] else ""
        print("  %d. %s%s" % (number, record["name"], suffix), file=sys.stderr)
    print("Select workspace [1-%d]: " % len(choices), end="", file=sys.stderr, flush=True)
    try:
        response = sys.stdin.readline().strip()
    except KeyboardInterrupt as exc:
        raise ConfigError("Workspace selection canceled; no workspace was changed") from exc
    try:
        selected = int(response)
    except ValueError as exc:
        raise ConfigError("Select a workspace by its number; no workspace was changed") from exc
    if not 1 <= selected <= len(choices):
        raise ConfigError("Workspace number is out of range; no workspace was changed")
    return choices[selected - 1]["name"]


USE_ABOUT = """
    The default applies to future terminal commands without -w. Without NAME, an
    interactive terminal shows a numbered menu. An agent chat keeps its own
    binding and can never change the default; see richi help chat."""
USE_EXAMPLES = (("Pick from a numbered menu", "richi use"),
                ("Make work the default", "richi use work"))
NAME_HELP = "Workspace name: a lowercase letter, then up to 47 of a-z, 0-9, _ or -"
USE_NAME_HELP = "Workspace to make the default; omit for a menu"


def workspace_parser(alias=False):
    if alias:
        parser = Parser(prog="richi use")
        parser.set_defaults(action="use")
        parser.add_argument("name", nargs="?", type=workspace_name, help=USE_NAME_HELP)
        return document(parser, "Select the default workspace for future terminal commands.", USE_ABOUT,
                        examples=USE_EXAMPLES, see=("workspace", "chat"), guide="docs/WORKSPACES.md")
    parser = Parser(prog="richi workspace")
    actions = parser.add_subparsers(dest="action", required=True)
    listing = actions.add_parser("list", help="List workspaces, their storage paths and the current default")
    current = actions.add_parser("current", help="Show the workspace, configuration and database this invocation would use")
    create = actions.add_parser("create", help="Create an empty workspace with its own isolated storage")
    create.add_argument("name", type=workspace_name, help=NAME_HELP)
    select = actions.add_parser("use", help="Select the default workspace (same as richi use)")
    select.add_argument("name", nargs="?", type=workspace_name, help=USE_NAME_HELP)
    document(parser, "List, create, inspect or select isolated workspaces.", """
        A workspace groups related projects and keeps its own SQLite database,
        history, graph, Git artifacts, configuration and logs. Knowledge is never
        copied between workspaces automatically; the same repository may belong
        to several of them.

        Which workspace a command uses, highest priority first:
          1. -w/--workspace NAME or --config FILE (not both; --config names a
             configuration outside the workspace registry)
          2. RICHI_CONFIG, then RICHI_WORKSPACE from the environment
          3. the default chosen with richi use

        --db PATH (or RICHI_DB) selects another database file and is rejected for
        named workspaces; that database's backups, captured Git artifacts and
        runtime files are read and written next to it. In an agent chat the
        chat's binding decides instead and storage overrides are rejected; see
        richi help chat.""",
             examples=(("", "richi workspace list"),
                       ("Start a separate workspace and its knowledge store",
                        ("richi workspace create work", "richi -w work init")),
                       ("Run one command in another workspace", "richi -w work recall \"deploy steps\""),
                       ("Change the terminal default", "richi use work")),
             see=("chat", "project", "environment"), guide="docs/WORKSPACES.md")
    document(listing, examples=(("", "richi workspace list"),))
    document(current, about="""
        Honors -w, --config and an agent chat's binding, so it answers exactly which
        store the next command with the same options would open.""",
             examples=(("", "richi workspace current"), ("", "richi -w work workspace current")))
    document(create, about="""
        Creates only the configuration; run init in the new workspace to create its
        knowledge database. The new workspace copies the caller's dev settings.""",
             examples=(("", ("richi workspace create work", "richi -w work init")),))
    document(select, about=USE_ABOUT, examples=(("", "richi workspace use work"),) + USE_EXAMPLES)
    return complete(parser)


def workspace_command(argv, selectors, alias=False, *, compact_json=False):
    args = workspace_parser(alias).parse_args(argv)
    if args.action == "current":
        settings = resolve_settings(**selectors)
        return _emit({"workspace": settings.workspace, "config_file": str(settings.config_file),
                      "database": str(settings.database), "data_dir": str(settings.data_dir),
                      "initialized": settings.database.is_file()}, compact_json=compact_json)
    if args.action == "list":
        return _emit(list_workspaces(), compact_json=compact_json)
    if args.action == "create":
        result = create_workspace(args.name)
        result["next_command"] = "richi --workspace " + args.name + " init"
        return _emit(result, compact_json=compact_json)
    name = args.name if args.name is not None else _choose_workspace("richi help use" if alias else "richi help workspace use")
    result = use_workspace(name)
    result["scope"] = "Default for future commands; explicit --workspace/--config and RICHI_* selectors take precedence"
    return _emit(result, compact_json=compact_json)


def chat_parser():
    parser = Parser(prog="richi chat")
    actions = parser.add_subparsers(dest="action", required=True)
    current = actions.add_parser("current", help="Show this chat's binding, the CLI default and the available workspaces")
    bind = actions.add_parser("bind", help="Save the workspace chosen for this chat")
    selection = bind.add_mutually_exclusive_group(required=True)
    selection.add_argument("name", nargs="?", type=workspace_name, help="Workspace to bind this chat to")
    selection.add_argument("--current", action="store_true", help="Bind the workspace that is the CLI default now")
    bind.add_argument("--replace", action="store_true", help="Replace an existing binding; only on the user's explicit request")
    document(parser, "Show or set the workspace bound to an agent chat.", """
        A chat is identified by RICHI_CHAT_ID, CODEX_THREAD_ID or --chat ID; agent
        setup provides it. The binding overrides the terminal default only for
        that chat. Until a chat is bound it cannot read or write knowledge. No
        agent chat may run richi use, bound or not, and a bound chat also rejects
        storage overrides.""",
             examples=(("At the start of a chat", "richi chat current"),
                       ("After the user chooses", ("richi chat bind --current", "richi chat bind work")),
                       ("Explicitly move this chat to another workspace", "richi chat bind other --replace")),
             see=("workspace", "environment"), guide="WORKSPACES.md")
    document(current, about="""
        Reads only metadata and never opens a knowledge database. When a release
        check has recorded an available update, the result also carries an update
        notice; nothing is downloaded or installed.""",
             examples=(("", "richi chat current"), ("Another client passes its chat ID", "richi --chat my-client:42 chat current")))
    document(bind, about="""
        Pass either NAME or --current. An existing different binding is kept unless
        --replace is given, so a chat never moves between workspaces silently.""",
             examples=(("", ("richi chat bind --current", "richi chat bind work")),
                       ("Change an existing binding", "richi chat bind other --replace")))
    return complete(parser)


def chat_command(argv, selectors, chat_id, *, compact_json=False):
    args = chat_parser().parse_args(argv)
    if args.action == "current":
        result = current_chat(chat_id)
        # Agents run this at every chat start, so it carries a stored update
        # notice; it never checks the network or installs anything.
        from . import auto_update
        pending = auto_update.notice()
        if pending is not None:
            result["update"] = pending
        return _emit(result, compact_json=compact_json)
    reject_storage_overrides(selectors)
    return _emit(bind_chat(chat_id, workspace=args.name, current=args.current, replace=args.replace),
                 compact_json=compact_json)
