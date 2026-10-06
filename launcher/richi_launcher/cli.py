"""Stable entry point; config and workspace recovery never import dev code."""
import argparse
from dataclasses import replace
import json
import os
import sys

from .config import ConfigError, resolve_settings, set_config_value
from .chats import (ChatError, bind_chat, current_chat, detect_chat, pin_chat,
                    reject_storage_overrides)
from .runtime import ACTIVE_RUNTIME, installed_runtime, resolve_runtime, runtime_command
from .workspaces import create_workspace, list_workspaces, use_workspace


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)

    def error(self, message):
        raise ConfigError(message)


def _selectors(argv):
    # Consume only global selectors and map's supported local selectors. Never
    # interpret option-looking recall text or another command's payload.
    options = {}
    position = 0
    names = {"--config": "config_file", "--db": "db",
             "--workspace": "workspace", "-w": "workspace",
             "--chat": "chat_id", "--project-path": "project_path"}
    while position < len(argv):
        token = argv[position]
        key, sep, value = token.partition("=")
        if key.startswith("-w") and key != "-w":
            # argparse accepts -wNAME as well as -w NAME and -w=NAME.
            # Resolve it here before pinning storage for the selected runtime.
            key, sep, value = "-w", "=", token[2:]
        if key not in names:
            break
        if not sep:
            position += 1
            if position >= len(argv):
                raise ConfigError(key + " requires a value")
            value = argv[position]
        options[names[key]] = value
        position += 1
    command = argv[position] if position < len(argv) else None
    tail = argv[position + 1:]
    if command == "map":
        normalized = []
        index = 0
        while index < len(tail):
            token = tail[index]
            key, sep, value = token.partition("=")
            if key in {"--config", "--db", "--port"}:
                consumed = [token]
                if not sep:
                    index += 1
                    if index >= len(tail):
                        raise ConfigError(key + " requires a value")
                    value = tail[index]
                    consumed.append(value)
                options[{"--config": "config_file", "--db": "db", "--port": "port"}[key]] = value
                if key == "--port":
                    normalized.extend(consumed)
            else:
                normalized.append(token)
            index += 1
        tail = normalized
    return options, command, tail


def _wants_help(arguments):
    # A literal --help after -- is payload, not permission to skip binding.
    options = arguments[:arguments.index("--")] if "--" in arguments else arguments
    return "--help" in options or "-h" in options


def _emit(result):
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def configuration(argv, selectors):
    parser = Parser(prog="richi config", description="Inspect settings or select installed/development code")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("show")
    change = commands.add_parser("set")
    change.add_argument("key", choices=("dev", "development.source"))
    change.add_argument("value")
    args = parser.parse_args(argv)
    # Resolve once, so another process changing the default workspace cannot
    # redirect a config update or its result to a different workspace.
    custom_config = selectors.get("config_file") is not None or (
        selectors.get("workspace") is None and "RICHI_CONFIG" in os.environ)
    settings = resolve_settings(**selectors, config_required=(False if args.action == "set" and custom_config else None))
    if args.action == "set":
        value = args.value
        if args.key == "dev":
            if value not in {"true", "false"}:
                raise ConfigError("dev must be true or false")
            value = value == "true"
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
    return _emit(result)


def _choose_workspace():
    if not sys.stdin.isatty():
        raise ConfigError("A workspace name is required without an interactive terminal; use richi workspace list, then richi use NAME")
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


def workspace_command(argv, selectors, alias=False):
    parser = Parser(prog="richi use" if alias else "richi workspace",
                    description="Create isolated workspaces and select the default for future commands")
    if alias:
        parser.set_defaults(action="use")
        parser.add_argument("name", nargs="?")
    else:
        actions = parser.add_subparsers(dest="action", required=True)
        actions.add_parser("list", help="List registered workspaces")
        actions.add_parser("current", help="Show the workspace selected for this invocation")
        create = actions.add_parser("create", help="Create an empty workspace configuration")
        create.add_argument("name")
        select = actions.add_parser("use", help="Select a default workspace, or choose interactively")
        select.add_argument("name", nargs="?")
    args = parser.parse_args(argv)
    if args.action == "current":
        settings = resolve_settings(**selectors)
        return _emit({"workspace": settings.workspace, "config_file": str(settings.config_file),
                      "database": str(settings.database), "data_dir": str(settings.data_dir),
                      "initialized": settings.database.is_file()})
    if selectors:
        raise ConfigError("Storage selectors apply to workspace current; use workspace list/create/use without --workspace, --config or --db")
    if args.action == "list":
        return _emit(list_workspaces())
    if args.action == "create":
        result = create_workspace(args.name)
        result["next_command"] = "richi --workspace " + args.name + " init"
        return _emit(result)
    name = args.name if args.name is not None else _choose_workspace()
    result = use_workspace(name)
    result["scope"] = "Default for future commands; explicit --workspace/--config and RICHI_* selectors take precedence"
    return _emit(result)


def chat_command(argv, selectors, chat_id):
    parser = Parser(prog="richi chat", description="Choose a workspace for this chat without changing the CLI default")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("current", help="Inspect this chat binding and the current CLI workspace")
    bind = actions.add_parser("bind", help="Save an explicit workspace choice for this chat")
    selection = bind.add_mutually_exclusive_group(required=True)
    selection.add_argument("name", nargs="?")
    selection.add_argument("--current", action="store_true", help="Snapshot the current CLI workspace")
    bind.add_argument("--replace", action="store_true", help="Deliberately replace this chat's existing choice")
    args = parser.parse_args(argv)
    if selectors:
        raise ConfigError("Use chat bind NAME or chat bind --current without storage selectors")
    if args.action == "current":
        return _emit(current_chat(chat_id))
    reject_storage_overrides(selectors)
    return _emit(bind_chat(chat_id, workspace=args.name, current=args.current, replace=args.replace))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        selectors, command, tail = _selectors(argv)
        if command == "update":
            from .update import command as update_command
            # Package updates are installation-wide and never select/read a
            # workspace, chat registry, configuration, or knowledge store.
            selectors.pop("chat_id", None)
            if selectors:
                raise ConfigError("Updates affect this installation, not a workspace; omit --workspace, --config, --db and --project-path")
            return _emit(update_command(tail))
        chat_id = detect_chat(selectors.pop("chat_id", None))
        project_path = selectors.pop("project_path", None)
        if project_path is not None and command in {"chat", "config", "workspace", "use", "map"}:
            raise ConfigError("--project-path applies to knowledge commands, not " + command)
        if command == "chat":
            return chat_command(tail, selectors, chat_id)
        if command == "status":
            from .status import command as status_command
            return _emit(status_command(tail, selectors, chat_id, project_path))
        if command == "config":
            selectors = pin_chat(chat_id, selectors, metadata=tail == ["show"] or _wants_help(tail))
            return configuration(tail, selectors)
        if command in {"workspace", "use"}:
            state = current_chat(chat_id) if chat_id is not None else None
            if state and (command == "use" or tail[:1] == ["use"]):
                raise ChatError("A chat cannot change the global CLI default; use chat bind NAME --replace to change this chat",
                                "chat_global_use_forbidden", chat_id=chat_id, workspace=state["workspace"])
            if command == "workspace" and tail[:1] == ["current"]:
                selectors = pin_chat(chat_id, selectors, metadata=True)
            return workspace_command(tail, selectors, alias=command == "use")
        if chat_id is not None and (command in {None, "--help", "-h", "--version"}
                                    or _wants_help(tail)):
            # Help does not require a choice or working development sources.
            # Argparse exits before opening the store in the fixed runtime.
            arguments = ([command] if command is not None else ["--help"]) + tail
            execution = runtime_command(installed_runtime(), arguments)
            environment = dict(os.environ)
            environment.pop(ACTIVE_RUNTIME, None)
            os.execve(execution[0], execution, environment)
            return 0
        selectors = pin_chat(chat_id, selectors)
        settings = resolve_settings(**selectors)
        selected = resolve_runtime(settings)
        # Pin the resolution before entering mutable code. In particular, strip
        # map-local storage options: they must not override this selected store.
        arguments = ["--db", str(settings.database)]
        required = settings.workspace not in {None, "default"} or selectors.get("config_file") is not None or (
            selectors.get("workspace") is None and "RICHI_CONFIG" in os.environ)
        pin_config = required or settings.config_file.is_file()
        if pin_config:
            arguments += ["--config", str(settings.config_file)]
        if project_path is not None:
            arguments += ["--project-path", project_path]
        if command is not None:
            arguments.append(command)
        arguments.extend(tail)
        execution = runtime_command(selected, arguments)
        environment = dict(os.environ)
        environment.pop(ACTIVE_RUNTIME, None)
        environment.pop("RICHI_DB", None)
        if chat_id is not None:
            environment["RICHI_CHAT_ID"] = chat_id
        environment["RICHI_DATA_DIR"] = str(settings.data_dir)
        environment["RICHI_ACTIVE_CONFIG"] = str(settings.config_file)
        if settings.workspace is not None:
            environment["RICHI_ACTIVE_WORKSPACE"] = settings.workspace
        else:
            environment.pop("RICHI_ACTIVE_WORKSPACE", None)
        # An absent implicit default config remains optional. Pin its workspace
        # so creating/selecting another workspace during this process cannot
        # redirect its future child calls to the registry's new default.
        if not pin_config:
            environment.pop("RICHI_CONFIG", None)
            environment["RICHI_WORKSPACE"] = "default"
        else:
            environment["RICHI_CONFIG"] = str(settings.config_file)
        os.execve(execution[0], execution, environment)
    except (ConfigError, OSError) as exc:
        result = exc.details if isinstance(exc, ChatError) else {"error": str(exc)}
        print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
        return 1
