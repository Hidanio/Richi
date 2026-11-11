"""Stable entry point; config and workspace recovery never import dev code."""
import argparse
import json
import os
import sys

from .config import ConfigError, resolve_settings
from .chats import ChatError, current_chat, detect_chat, pin_chat, storage_override_error
from .runtime import ACTIVE_RUNTIME, installed_runtime, resolve_runtime, runtime_command

# Commands this launcher parses itself; `richi help NAME` shows their pages.
LAUNCHER_COMMANDS = frozenset({"agents", "setup", "update", "status", "workspace", "use", "chat", "config"})
HELP_COMMANDS = frozenset({None, "help", "--help", "-h", "--version"})
# Global options consumed before COMMAND and the setting each one selects.
# The runtime declares the same options (scripts/check_cli_docs.py).
GLOBAL_OPTIONS = {"--config": "config_file", "--db": "db", "--workspace": "workspace", "-w": "workspace",
                  "--chat": "chat_id", "--project-path": "project_path", "--compact-json": "compact_json"}
# The global options that take no value; every other one takes exactly one.
GLOBAL_FLAGS = frozenset({"--compact-json"})
_VALUE_OPTIONS = {option: name for option, name in GLOBAL_OPTIONS.items() if option not in GLOBAL_FLAGS}
_STORAGE = ("config_file", "db", "workspace")
_INSTALLATION_WIDE = {
    "agents": "Agent setup is installation-wide; omit storage selectors.",
    "setup": "Setup connects this installation to the local agent; omit workspace and storage selectors.",
    "update": "Updates affect this installation, not a workspace; omit --workspace, --config, --db and --project-path.",
}


def launcher_parser(name):
    """The parser of one launcher command; its module is imported on request,
    so ordinary commands do not pay for it."""
    if name in {"agents", "setup"}:
        from . import agent_setup
        return agent_setup.agents_parser() if name == "agents" else agent_setup.setup_parser()
    if name == "update":
        from . import update
        return update.parser()
    if name == "status":
        from . import status
        return status.parser()
    from . import workspace_commands
    if name == "use":
        return workspace_commands.workspace_parser(alias=True)
    return {"workspace": workspace_commands.workspace_parser, "chat": workspace_commands.chat_parser,
            "config": workspace_commands.config_parser}[name]()


def launcher_parsers():
    """The parser of every launcher command."""
    return {name: launcher_parser(name) for name in sorted(LAUNCHER_COMMANDS)}


class UsageError(ConfigError):
    """An argument error that names the help page explaining the command.

    details is the whole JSON error payload, as for ChatError.
    """

    def __init__(self, message, target):
        super().__init__(message)
        self.details = {"error": message, "code": "usage_error", "help": target}


def usage(message, target):
    """A UsageError whose message ends with the help page to read."""
    from .help_format import with_help
    return UsageError(with_help(message, target), target)


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        from . import help_format
        kwargs["allow_abbrev"] = False
        kwargs.setdefault("formatter_class", help_format.HelpFormatter)
        super().__init__(*args, **kwargs)
        # Python 3.14 colors help in a terminal; a page reads the same everywhere.
        self.color = False

    def parse_args(self, args=None, namespace=None):
        from . import help_format
        namespace, extras = self.parse_known_args(args, namespace)
        if extras:
            # Report leftovers on the command they were given to; a global
            # option is suggested only where that command accepts it.
            parser = help_format.selected(self, namespace)
            path = parser.prog.split()[1:]
            rejected = _rejections(path[0], path[1:]) if path else {}
            accepted = [option for option, name in GLOBAL_OPTIONS.items() if name not in rejected]
            parser.error(help_format.unrecognized(parser, extras, GLOBAL_OPTIONS, accepted))
        return namespace

    def _check_value(self, action, value):
        from . import help_format
        error = help_format.choice_error(self, action, value)
        if error is not None:
            raise error

    def format_help(self):
        from . import help_format
        help_format.finish(self)
        return super().format_help()

    def error(self, message):
        from . import help_format
        raise UsageError(*help_format.usage_message(self, message))


def _selectors(argv):
    # Consume only global options and map's supported local selectors. Never
    # interpret option-looking recall text or another command's payload.
    options = {}
    position = 0
    while position < len(argv):
        token = argv[position]
        if token in GLOBAL_FLAGS:
            options[GLOBAL_OPTIONS[token]] = True
            position += 1
            continue
        key, sep, value = token.partition("=")
        if key.startswith("-w") and key != "-w":
            # argparse accepts -wNAME as well as -w NAME and -w=NAME.
            # Resolve it here before pinning storage for the selected runtime.
            key, sep, value = "-w", "=", token[2:]
        if key not in _VALUE_OPTIONS:
            break
        if not sep:
            position += 1
            if position >= len(argv):
                raise usage(key + " requires a value.", "richi help")
            value = argv[position]
        options[_VALUE_OPTIONS[key]] = value
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
                        raise usage(key + " requires a value.", "richi help map")
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


def _interactive(chat_id):
    """A person at a terminal, not an agent, script or scheduler."""
    try:
        return chat_id is None and sys.stdin is not None and sys.stdin.isatty()
    except (OSError, ValueError):
        return False


def _hand_off_maps(port, interactive):
    """Once per installation generation and port range; never prevents the requested command."""
    try:
        from .maps import first_invocation
        first_invocation(port, interactive=interactive)
    except KeyboardInterrupt:
        # The person cancelled the command; the next one hands the maps off.
        raise SystemExit(130)
    except Exception:
        pass


def asks_for_help(command, tail):
    """Whether an invocation only prints help, which needs no selection."""
    return command in HELP_COMMANDS or _wants_help(tail)


def _checks_arguments(command, tail):
    """Whether the selectors of `richi COMMAND TAIL` are checked before it
    runs: help needs none, but --version names the selected runtime."""
    return command == "--version" or not asks_for_help(command, tail)


def _after_dash(tail):
    """TAIL as argparse 3.12+ reads it: a '--' before the subcommand is
    dropped (earlier versions reject it), so a check of the subcommand sees
    the word that runs."""
    return tail[1:] if tail[:1] == ["--"] else tail


def _changes_default(command, tail):
    """Whether `richi COMMAND TAIL` changes the global default workspace."""
    return command == "use" or (command == "workspace" and _after_dash(tail)[:1] == ["use"])


def _check_command_word(command):
    """Reject a command word that starts with "-" and asks for no help
    (UsageError): the launcher consumes every global option, so the command
    never starts with one."""
    if command is None or not command.startswith("-") or command in HELP_COMMANDS | {"-", "--"}:
        return
    from . import help_format
    option = command.split("=", 1)[0]
    if option in GLOBAL_FLAGS:
        raise usage(option + " takes no value.", "richi help")
    names = {name.lstrip("-"): name for name in GLOBAL_OPTIONS}
    close = [names[name] for name in help_format.suggestions(option.lstrip("-"), names)]
    raise usage("unrecognized arguments: " + command + "." + help_format.did_you_mean(close), "richi help")


def _rejections(command, tail):
    """{setting: why COMMAND rejects the global option that selects it};
    tail is what follows COMMAND."""
    if command in _INSTALLATION_WIDE:
        return dict.fromkeys(_STORAGE + ("project_path",), _INSTALLATION_WIDE[command])
    rules = {}
    if command in {"chat", "config", "workspace", "use", "map"}:
        rules["project_path"] = "--project-path applies to knowledge commands, not " + command + "."
    if command == "chat":
        rules.update(dict.fromkeys(_STORAGE, "Use chat bind NAME or chat bind --current without storage selectors."))
    elif command == "use" or (command == "workspace" and _after_dash(tail)[:1] != ["current"]):
        rules.update(dict.fromkeys(_STORAGE, "Storage selectors apply to workspace current; use workspace "
                                             "list/create/use without --workspace, --config or --db."))
    return rules


def check_selectors(command, tail, selectors):
    """Reject global options that COMMAND does not accept (UsageError);
    selectors as parsed by _selectors()."""
    for name, message in _rejections(command, tail).items():
        if selectors.get(name) is not None:
            if command == "workspace":
                # The rule depends on the subcommand, so a mistyped or
                # missing one is reported instead of a misplaced option.
                launcher_parser(command).parse_args(tail)
            raise usage(message, "richi help " + command if command in LAUNCHER_COMMANDS | {"map"} else "richi help")


def _check_values(command, selectors, placeholder=None):
    """Reject a malformed selector value, or a workspace together with a
    config (UsageError), before the settings that would reject them;
    selectors as parsed by _selectors(). A path is checked only for its
    form: what it names is for the settings to find. A value for which
    placeholder(value) is true stands for any valid one."""
    from .config import _path_text, _port
    from .workspaces import WITH_CONFIG, _name
    page = "richi help map" if command == "map" else "richi help"
    checks = (("workspace", _name, "richi help workspace"),
              ("db", lambda value: _path_text(value, "--db"), page),
              ("config_file", lambda value: _path_text(value, "--config"), page),
              ("port", lambda value: _port(value, "--port"), "richi help map"))
    for name, check, target in checks:
        value = selectors.get(name)
        if value is not None and not (placeholder and isinstance(value, str) and placeholder(value)):
            try:
                check(value)
            except ConfigError as exc:
                raise usage(str(exc), target) from exc
    if selectors.get("workspace") is not None and selectors.get("config_file") is not None:
        raise usage(WITH_CONFIG, page)


_GLOBAL_USE = "A chat cannot change the global CLI default"


def _reads_metadata(command, tail):
    """Whether `richi COMMAND TAIL` may run in a chat that has no workspace:
    it reads settings or diagnostics, not knowledge."""
    return command in {"status", "--version"} or (command, tuple(_after_dash(tail)[:1])) in {
        ("config", ("show",)), ("workspace", ("current",))}


def global_option_error(argv, placeholder=None):
    """Why the launcher rejects the global options of `richi ARGV`, or None:
    the checks main() makes before it selects anything, in its order, then
    the selections that no registry or chat state accepts. A documented line
    names a chat only with --chat, and a value for which placeholder(value)
    is true stands for any valid one. scripts/check_cli_docs.py checks
    documented command lines with it."""
    def given(value):
        return value is not None and not (placeholder and placeholder(value))
    try:
        selectors, command, tail = _selectors(argv)
        _check_command_word(command)
        if not _checks_arguments(command, tail):
            return None
        chat = selectors.get("chat_id")
        if chat is not None and _changes_default(command, tail):
            if given(chat):
                detect_chat(chat)
            return _GLOBAL_USE + "; use chat bind NAME to choose this chat's workspace."
        check_selectors(command, tail, selectors)
        _check_values(command, selectors, placeholder)
        if command in _INSTALLATION_WIDE:
            return None  # These finish before the chat is read.
        if given(chat):
            detect_chat(chat)
        overrides = [flag for name, flag in (("config_file", "--config"), ("db", "--db"))
                     if selectors.get(name) is not None]
        if chat is not None and overrides and not _reads_metadata(command, tail):
            # Unbound, the chat may not read knowledge; bound, it keeps its storage.
            raise storage_override_error(overrides)
        workspace = selectors.get("workspace")
        if chat is None and selectors.get("db") is not None and given(workspace) and workspace != "default":
            # A named workspace other than default keeps its own database.
            from .config import isolated_storage
            raise isolated_storage(workspace, ["--db"])
    except ConfigError as exc:
        return str(exc)
    return None


def _refuse(message, target):
    raise usage(message, target)


def _refuse_global_use(chat_id):
    """A chat may never change the global default, whatever else it passes."""
    state = current_chat(chat_id)
    hint = ("use chat bind NAME --replace to change this chat" if state.get("workspace") is not None
            else "use chat bind NAME to choose this chat's workspace")
    raise ChatError(_GLOBAL_USE + "; " + hint,
                    "chat_global_use_forbidden", chat_id=chat_id, workspace=state["workspace"])


def _launcher_help(command, tail):
    """Print the page of a launcher command for `richi help NAME ...` or
    `richi NAME ... --help`, before any selection, binding or recovery.
    Returns False when the request is not for a launcher page."""
    if command == "help" and tail and tail[0] in LAUNCHER_COMMANDS:
        from . import help_format
        sys.stdout.write(help_format.find(launcher_parser(tail[0]), tail[1:], _refuse).format_help())
        return True
    if command in LAUNCHER_COMMANDS and _wants_help(tail):
        # Argparse prints the page and exits, or raises a usage error.
        launcher_parser(command).parse_args(tail)
    return False


def _runtime_help(in_chat, selectors, command, tail):
    """Overview, command list, topics and runtime command pages. A chat gets
    the installed release without a binding. A terminal gets the selected
    runtime, or the installed one when the selection itself is broken;
    --version names the selected runtime there, so a broken selection stays
    an error."""
    runtime = None
    if not in_chat:
        try:
            settings = resolve_settings(**selectors)
            runtime = resolve_runtime(settings)
        except ConfigError:
            if command == "--version":
                raise
        else:
            if command != "map" and "RICHI_INSTALLATION_GENERATION" in os.environ:
                # As for any terminal command: the first one of a new
                # installation generation, help included, hands off the maps.
                _hand_off_maps(settings.port, _interactive(None))
    arguments = ([command] if command is not None else []) + tail
    execution = runtime_command(runtime or installed_runtime(), arguments)
    environment = dict(os.environ)
    environment.pop(ACTIVE_RUNTIME, None)
    os.execve(execution[0], execution, environment)
    return 0


def _typo(command):
    """A launcher command name mistyped in an unbound chat gets a correction
    instead of a request to bind the chat (no runtime command looks like one;
    scripts/check_cli_docs.py keeps it so)."""
    from . import help_format
    close = help_format.suggestions(command, sorted(LAUNCHER_COMMANDS))
    if close:
        raise usage("'%s' is not a richi command.%s" % (command, help_format.did_you_mean(close)), "richi help")


def _emit(result, *, compact_json=False):
    print(json.dumps(result, ensure_ascii=False, indent=None if compact_json else 2,
                     separators=(",", ":") if compact_json else None))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        selectors, command, tail = _selectors(argv)
        # Presentation is not a storage selector and must never affect binding
        # checks, recovery commands, or the selected runtime's configuration.
        compact_json = selectors.pop("compact_json", False)
        _check_command_word(command)
        # Help changes nothing, so it needs no selection, chat choice or
        # recovery; launcher pages are printed before any of them.
        if _launcher_help(command, tail):
            return 0
        asking = asks_for_help(command, tail)
        root = os.environ.get("RICHI_INSTALLATION_ROOT")
        readonly_setup = asking or (command == "setup" and ("--status" in tail or "--dry-run" in tail)) or (command == "agents" and "--status" in tail)
        if root and os.path.isfile(os.path.join(root, "setup-pending.json")) and not readonly_setup:
            from .agent_setup import recover_pending
            recover_pending()
        if _checks_arguments(command, tail):
            if _changes_default(command, tail):
                chat_id = detect_chat(selectors.get("chat_id"))
                if chat_id is not None:
                    _refuse_global_use(chat_id)
            check_selectors(command, tail, selectors)
            _check_values(command, selectors)
        if command == "agents":
            from .agent_setup import agents_command
            return _emit(agents_command(tail), compact_json=compact_json)
        if command == "setup":
            from .agent_setup import command as setup_command
            return _emit(setup_command(tail), compact_json=compact_json)
        if command == "update":
            # Package updates are installation-wide and never select/read a
            # workspace, chat registry, configuration, or knowledge store.
            from .update import command as update_command
            return _emit(update_command(tail), compact_json=compact_json)
        try:
            chat_id = detect_chat(selectors.pop("chat_id", None))
        except ChatError:
            # Help reads no knowledge, so a malformed chat ID does not hide
            # the pages that explain it; they come from the installed
            # release, as in any chat. --version still reports the error.
            if not asking or command == "--version":
                raise
            return _runtime_help(True, selectors, command, tail)
        project_path = selectors.pop("project_path", None)
        if command == "status":
            from .status import command as status_command
            return _emit(status_command(tail, selectors, chat_id, project_path), compact_json=compact_json)
        if command in {"chat", "config", "workspace", "use"}:
            from . import workspace_commands
            if command == "chat":
                return workspace_commands.chat_command(tail, selectors, chat_id, compact_json=compact_json)
            if command == "config":
                # Its arguments are checked first: an unbound chat may still
                # see config show, and every chat sees its argument mistakes.
                args = workspace_commands.config_arguments(tail)
                selectors = pin_chat(chat_id, selectors, metadata=args.action == "show")
                return workspace_commands.configuration(args, selectors, compact_json=compact_json)
            if command == "workspace" and _after_dash(tail)[:1] == ["current"]:
                selectors = pin_chat(chat_id, selectors, metadata=True)
            return workspace_commands.workspace_command(tail, selectors, alias=command == "use",
                                                        compact_json=compact_json)
        if asking:
            return _runtime_help(chat_id is not None, selectors, command, tail)
        try:
            selectors = pin_chat(chat_id, selectors)
        except ChatError as exc:
            if exc.details.get("code") == "chat_workspace_required":
                _typo(command)
            raise
        settings = resolve_settings(**selectors)
        selected = resolve_runtime(settings)
        if command != "map" and "RICHI_INSTALLATION_GENERATION" in os.environ:
            # A frozen updater cannot hand off maps; the first command of the
            # new generation does, without waiting for them. richi map replaces
            # its own map itself. Only a terminal command starts a server here:
            # agents may be sandboxed and scripts may run unattended.
            _hand_off_maps(settings.port, _interactive(chat_id))
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
        if compact_json:
            arguments.append("--compact-json")
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
        result = getattr(exc, "details", None) or {"error": str(exc)}
        print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
        return 1
