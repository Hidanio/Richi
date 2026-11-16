"""`richi skill-sources`: the agents Richi knows on this computer and where each finds skills.

A built-in agent stays as Richi defines it; here the user may only switch its
skill sources off or on and add skill folders, and `skill-sources remove` drops those
adjustments again. An agent Richi does not have is described with `skill-sources add`.
The settings belong to the user and are shared by every workspace
(agents.settings_path); the map's Agents tab calls the same functions.
"""
import contextlib
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets

from . import agents, skill_catalog


# What each change may carry; `skill-sources` options and the map send exactly these.
OPTIONS = {"add": {"name", "folders", "variable", "manual_skills"},
           "update": {"expected_updated_at", "name", "variable", "manual_skills", "add_folders", "remove_folders",
                      "enable", "disable"},
           "remove": {"expected_updated_at"}}


class AgentError(ValueError):
    """A refused change with a stable code: invalid, exists, not_found, conflict, limit, builtin, settings."""

    def __init__(self, message, code="invalid"):
        super().__init__(message)
        self.code = code


def _argument_id(value):
    import argparse
    if not agents.valid_id(value):
        raise argparse.ArgumentTypeError(agents.ID_HELP)
    return value


def add_commands(commands, api):
    parser = commands.add_parser("skill-sources", help="List the AI agents Richi knows and where each finds skills; add your own")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="Show every agent with its skill sources and how many skills each gives")
    folder = "Folder with one skill per subfolder, absolute or under ~"
    variable = ("Environment variable that marks this agent's shells, so that rules show knows which agent asks: "
                "NAME when being set is enough, NAME=VALUE for an exact value")
    manual = ("How a skill says that only the user may invoke it, so that it is left out of this agent's skills: "
              "front-matter is disable-model-invocation: true in SKILL.md (default: none, every skill is listed)")
    add = actions.add_parser("add", help="Describe an agent Richi does not have built in, so rules can name skills for it")
    add.add_argument("id", type=_argument_id, help=agents.ID_HELP)
    add.add_argument("--name", required=True, help="Name shown to people, at most %d characters" % agents.MAX_NAME)
    add.add_argument("--skills", metavar="FOLDER", action="append", required=True, help=folder + "; repeat for several")
    add.add_argument("--variable", metavar="NAME[=VALUE]", help=variable)
    add.add_argument("--manual-skills", choices=agents.MANUAL_SKILLS, help=manual)
    update = actions.add_parser("update", help="Switch an agent's skill sources, add or remove skill folders, "
                                               "or change an own agent's name and detection")
    update.add_argument("id", type=_argument_id, help="ID of the agent to change")
    update.add_argument("--expected-updated-at", metavar="TIMESTAMP",
                        help="The agent's updated_at as last read; omit it for an agent whose updated_at is null")
    update.add_argument("--add-skills", metavar="FOLDER", action="append", help=folder + " to add")
    update.add_argument("--remove-skills", metavar="FOLDER", action="append", help="Added skill folder to remove")
    update.add_argument("--disable", metavar="SOURCE", action="append",
                        help="Skill source to stop using: its key from skill-sources list, or an added folder")
    update.add_argument("--enable", metavar="SOURCE", action="append", help="Skill source to use again")
    update.add_argument("--name", help="New name of an own agent")
    update.add_argument("--variable", metavar="NAME[=VALUE]", help="New detection variable of an own agent; "
                                                                   "an empty value removes it")
    update.add_argument("--manual-skills", choices=agents.MANUAL_SKILLS, help=manual)
    remove = actions.add_parser("remove", help="Delete an own agent, or drop the adjustments of a built-in one")
    remove.add_argument("id", type=_argument_id, help="ID of the agent")
    remove.add_argument("--expected-updated-at", required=True, metavar="TIMESTAMP",
                        help="The agent's updated_at as last read; a newer version is a conflict")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _display(path, home):
    """A path as people write it: under the home directory it starts with ~."""
    try:
        relative = Path(path).relative_to(home).as_posix()
    except ValueError:
        return str(path)
    return "~" if relative == "." else "~/" + relative


def _launcher_agents():
    """({agent ID: (detected, connection status)}, problem) as the launcher's `richi agents --status` reports them.

    The launcher owns the integrations, so it alone knows whether one is set
    up. One that cannot tell (an older launcher, a damaged setup record) is
    named in problem instead of being hidden.
    """
    try:
        from richi_launcher import agent_setup
        items = agent_setup.agents_command(["--status"])["agents"]
        return {item["agent"]: (bool(item["detected"]), item["status"]) for item in items}, None
    except (ImportError, AttributeError, KeyError, TypeError, ValueError, OSError) as exc:
        return {}, "Agent connections are unknown: " + (str(exc) or type(exc).__name__)


def _looks_installed(roots, home):
    """Whether an agent the launcher does not connect looks installed for this user."""
    for root in roots:
        folder = Path(root["path"])
        # An agent whose skills folder is not created yet still has its own directory.
        if root["exists"] or (folder.parent != home and skill_catalog.folder_state(folder.parent) == "ok"):
            return True
    return False


def listing(environ=None, home=None, catalog=False):
    """Every agent with its skill sources, plus agents that could be added.

    catalog=True also returns the skill catalog this listing was counted
    from, so that a caller that needs both scans the skill folders once.
    """
    real = environ is None and home is None
    environ = os.environ if environ is None else environ
    home = Path(home) if home is not None else Path.home()
    _entries, problem = agents.load_settings(environ, home)
    skills = skill_catalog.catalog(environ, home)
    totals, roots = {}, {}
    for skill in skills["skills"]:
        for identifier in skill["agents"]:
            totals[identifier] = totals.get(identifier, 0) + 1
    for root in skills["roots"]:
        roots.setdefault(root["agent"], []).append(root)
    launcher, connections_problem = _launcher_agents() if real else ({}, None)
    items = []
    for agent in agents.registry(environ, home):
        own = roots.get(agent.id, [])
        detected, connection = launcher.get(agent.id, (_looks_installed(own, home), None))
        items.append({
            "id": agent.id, "name": agent.name, "builtin": agent.builtin, "updated_at": agent.updated_at,
            "detected": detected,
            # configured, not_configured, repair_required...; None when Richi installs no integration for it.
            "connection": connection, "variables": agents.variables(agent),
            # Only an own agent has this setting; a built-in one keeps its own rule.
            "manual_skills": None if agent.builtin else agent.invocation,
            "skills": totals.get(agent.id, 0),
            "sources": [{"key": root["key"], "kind": root["kind"], "layout": root["layout"],
                         "path": _display(root["path"], home), "exists": root["exists"], "denied": root["denied"],
                         "enabled": root["enabled"], "added": root["added"], "skills": root["skills"]}
                        for root in own]})
    known = {item["id"] for item in items}
    # A folder that a known agent already reads says nothing about another agent being installed.
    used = {root["path"] for root in skills["roots"]}
    presets = [{"id": identifier, "name": name, "folder": folder,
                "exists": (str(agents.folder_path(folder, home)) not in used
                           and skill_catalog.folder_state(agents.folder_path(folder, home)) == "ok")}
               for identifier, name, folder in agents.PRESETS if identifier not in known]
    result = {"view": "agents", "agents": items, "presets": presets,
              "settings_file": str(agents.settings_path(environ, home)), "problem": problem,
              "connections_problem": connections_problem}
    if catalog:
        result["catalog"] = skills
    return result


@contextlib.contextmanager
def _locked(path):
    """One writer of the settings at a time, across processes."""
    try:
        import fcntl
    except ImportError:
        fcntl = None
    with open(str(path) + ".lock", "a") as handle:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _read(path):
    try:
        with open(path, "rb") as handle:
            data = handle.read(agents.MAX_SETTINGS + 1)
    except FileNotFoundError:
        return {}
    try:
        if len(data) > agents.MAX_SETTINGS:
            raise ValueError("the file is larger than %d bytes" % agents.MAX_SETTINGS)
        return agents.check_settings(json.loads(data.decode("utf-8")))
    except ValueError as exc:
        raise AgentError("Cannot change agent settings: " + str(path) + ": " + str(exc)
                         + "; fix or remove the file", "settings") from exc


def _write(path, entries):
    text = json.dumps({"version": agents.SETTINGS_VERSION, "agents": entries}, ensure_ascii=False, indent=2,
                      sort_keys=True) + "\n"
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(6) + ".tmp")
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(str(temporary), str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(str(temporary))
        raise
    agents._cache.clear()


def _folder(value, home):
    """A folder as it is stored: absolute, written with ~ when it is under the home directory."""
    text = value.strip() if isinstance(value, str) else ""
    text = (text.rstrip("/") or "/") if text not in {"", "~"} else text
    if not agents.valid_folder(text):
        raise AgentError(agents.FOLDER_HELP)
    text = _display(agents.folder_path(text, home), home)
    if not agents.valid_folder(text):
        raise AgentError(agents.FOLDER_HELP)
    return text


def _variable(value):
    if value is None or value == "":
        return None
    if not agents.valid_variable(value):
        raise AgentError(agents.VARIABLE_HELP)
    return value


def _name(value):
    if not agents.valid_name(value):
        raise AgentError(agents.NAME_HELP)
    return value


def _manual(value):
    if value not in agents.MANUAL_SKILLS:
        raise AgentError("manual_skills must be one of " + ", ".join(agents.MANUAL_SKILLS))
    return value


def _strings(options, key):
    values = options.get(key) or []
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise AgentError(key + " must be a list of strings")
    return values


def _check_version(identifier, current, expected):
    stored = current.get("updated_at") if current else None
    if expected != stored:
        raise AgentError("Agent update conflict for {}: expected {} but the current version is {}; read it again "
                         "with skill-sources list".format(identifier, expected or "no settings yet",
                                                  stored or "no settings yet"), "conflict")


def _added(identifier, options, builtin, current, entries, home):
    if builtin:
        raise AgentError(identifier + " is built into Richi; adjust its skill sources with skill-sources update", "exists")
    if current is not None:
        raise AgentError("Agent " + identifier + " already exists; change it with skill-sources update", "exists")
    if sum(1 for known in entries if known not in agents.builtin_ids()) >= agents.MAX_CUSTOM:
        raise AgentError("At most %d own agents; remove one first" % agents.MAX_CUSTOM, "limit")
    folders = []
    for value in _strings(options, "folders"):
        text = _folder(value, home)
        if text in folders:
            raise AgentError("Skill folder given twice: " + text)
        folders.append(text)
    if not folders:
        raise AgentError("An agent needs at least one skill folder")
    if len(folders) > agents.MAX_FOLDERS:
        raise AgentError("An agent may have at most %d skill folders" % agents.MAX_FOLDERS, "limit")
    timestamp = _now()
    entry = {"name": _name(options.get("name")), "folders": [{"path": text, "enabled": True} for text in folders],
             "manual_skills": _manual(options.get("manual_skills") or "none"),
             "created_at": timestamp, "updated_at": timestamp}
    variable = _variable(options.get("variable"))
    if variable:
        entry["variable"] = variable
    return entry


def _updated(identifier, options, builtin, current, environ, home):
    """(status, entry or None): None drops the stored entry of a built-in agent left without adjustments."""
    if not builtin and (current is None or "name" not in current):
        raise AgentError("Unknown agent: " + identifier + "; see skill-sources list", "not_found")
    _check_version(identifier, current, options.get("expected_updated_at"))
    entry = copy.deepcopy(current) if current else {}
    base_keys, base_paths = set(), set()
    if builtin:
        if any(options.get(key) is not None for key in ("name", "variable", "manual_skills")):
            raise AgentError(identifier + " is built into Richi: only its skill sources can be switched or added",
                             "builtin")
        base = next(agent for agent in agents.AGENTS if agent.id == identifier).sources(environ, home)
        base_keys, base_paths = {source.key for source in base}, {source.path for source in base}
    else:
        if options.get("name") is not None:
            entry["name"] = _name(options["name"])
        if options.get("variable") is not None:
            entry.pop("variable", None)
            if _variable(options["variable"]):
                entry["variable"] = options["variable"]
        if options.get("manual_skills") is not None:
            entry["manual_skills"] = _manual(options["manual_skills"])
    folders, disabled = entry.get("folders", []), set(entry.get("disabled", []))
    for value in _strings(options, "remove_folders"):
        text = _folder(value, home)
        if not any(folder["path"] == text for folder in folders):
            raise AgentError("Not an added skill folder of " + identifier + ": " + text + "; see skill-sources list",
                             "not_found")
        folders = [folder for folder in folders if folder["path"] != text]
    for value in _strings(options, "add_folders"):
        text = _folder(value, home)
        if any(folder["path"] == text for folder in folders) or agents.folder_path(text, home) in base_paths:
            raise AgentError("Already a skill source of " + identifier + ": " + text, "exists")
        folders.append({"path": text, "enabled": True})
    if len(folders) > agents.MAX_FOLDERS:
        raise AgentError("An agent may have at most %d skill folders" % agents.MAX_FOLDERS, "limit")
    for key, enabled in ([(key, True) for key in _strings(options, "enable")]
                         + [(key, False) for key in _strings(options, "disable")]):
        stored = _display(agents.folder_path(key, home), home) if agents.valid_folder(key.rstrip("/") or "/") else key
        folder = next((folder for folder in folders if folder["path"] in {key, stored}), None)
        if folder is not None:
            folder["enabled"] = enabled
        elif key in base_keys:
            (disabled.discard if enabled else disabled.add)(key)
        else:
            raise AgentError("Unknown skill source of " + identifier + ": " + key + "; see skill-sources list", "not_found")
    if not builtin and not folders:
        raise AgentError("An own agent needs at least one skill folder; remove the agent instead")
    for key, value in (("folders", folders), ("disabled", sorted(disabled))):
        entry.pop(key, None)
        if value:
            entry[key] = value
    given = ("name", "variable", "manual_skills", "add_folders", "remove_folders", "enable", "disable")
    if not any(options.get(key) not in (None, []) for key in given):
        raise AgentError("Nothing to change; give a source to switch, a folder to add or remove, or a new name")
    if entry == (current or {}):
        return "unchanged", current
    # A built-in agent left without adjustments needs no stored entry, unless the entry holds
    # something else: a field a newer Richi wrote is not this version's to drop.
    if builtin and not set(entry) - {"created_at", "updated_at"}:
        return "updated", None
    entry["updated_at"] = _now()
    return "updated", entry


def _apply(action, identifier, options, entries, environ, home):
    """(status, entry or None) of one change against the stored entries; nothing is written."""
    builtin, current = identifier in agents.builtin_ids(), entries.get(identifier)
    if action == "add":
        return "created", _added(identifier, options, builtin, current, entries, home)
    if action == "update":
        return _updated(identifier, options, builtin, current, environ, home)
    if current is None:
        raise AgentError((identifier + " has no adjustments to remove" if builtin
                          else "Unknown agent: " + identifier + "; see skill-sources list"), "not_found")
    _check_version(identifier, current, options.get("expected_updated_at"))
    return ("reset" if builtin else "removed"), None


def change(action, identifier, options=None, environ=None, home=None, full=False):
    """Apply one change to the user's agent settings and describe the agent as it is afterwards.

    full=True also returns the whole listing with its skill catalog, read once
    after the change, for a caller that shows all of it.
    """
    explicit = (environ, home)
    environ = os.environ if environ is None else environ
    home = Path(home) if home is not None else Path.home()
    options = {} if options is None else options
    if action not in OPTIONS:
        raise AgentError("Unknown action; use add, update or remove")
    if not isinstance(options, dict) or set(options) - OPTIONS[action]:
        raise AgentError("A change to " + action + " takes only: " + ", ".join(sorted(OPTIONS[action])))
    if not agents.valid_id(identifier):
        raise AgentError(agents.ID_HELP)
    expected = options.get("expected_updated_at")
    if expected is not None and not isinstance(expected, str):
        raise AgentError("expected_updated_at must be a string")
    path = agents.settings_path(environ, home)
    # A refused change leaves nothing behind: the directory and the lock appear with the first write.
    if not path.parent.is_dir():
        status, entry = _apply(action, identifier, options, {}, environ, home)
        if status != "unchanged":
            path.parent.mkdir(parents=True, exist_ok=True)
    with _locked(path) if path.parent.is_dir() else contextlib.nullcontext():
        entries = _read(path)
        status, entry = _apply(action, identifier, options, entries, environ, home)
        if status != "unchanged":
            if entry is None:
                entries.pop(identifier, None)
            else:
                try:
                    agents.check_entry(identifier, entry)
                except ValueError as exc:
                    raise AgentError(str(exc)) from exc
                entries[identifier] = entry
            _write(path, entries)
    current = listing(*explicit, catalog=full)
    described = next((item for item in current["agents"] if item["id"] == identifier), None)
    result = {"results": [{"id": identifier, "status": status}], "agent": described}
    if full:
        result["listing"] = current
    return result


def run(args, api):
    """Run an agent command; the settings are the user's, so no workspace store is opened."""
    try:
        if args.action == "list":
            return listing()
        if args.action == "add":
            options = {"name": args.name, "folders": args.skills, "variable": args.variable,
                       "manual_skills": args.manual_skills}
        elif args.action == "update":
            options = {"expected_updated_at": args.expected_updated_at, "name": args.name,
                       "variable": args.variable, "manual_skills": args.manual_skills,
                       "add_folders": args.add_skills, "remove_folders": args.remove_skills,
                       "enable": args.enable, "disable": args.disable}
        else:
            options = {"expected_updated_at": args.expected_updated_at}
        return change(args.action, args.id, options)
    except AgentError as exc:
        if exc.code == "not_found":
            raise api.NotFoundError(str(exc)) from exc
        error = api.MemoryError(str(exc))
        error.code = None if exc.code == "invalid" else exc.code
        raise error from exc
