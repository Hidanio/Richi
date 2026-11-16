"""Standing rules: instructions the user set for a workspace or one of its projects.

Rules are not knowledge. Recall, search, discovery and the graph never return
them; an agent loads the applicable set explicitly with `rules show` after it
selects the workspace, and follows it as the user's own instructions. A global
rule applies to every chat bound to the workspace; a project rule applies to
work in one registered project of that workspace.

Each rule is one `rule:<id>` row of the store's metadata table. Every supported
schema has that table, so rules need no migration, and backups and workspace
copies carry them. A row keeps the fields this version does not know, so a
store that a newer Richi wrote stays readable and editable; a row that cannot
be read is reported and skipped, never allowed to hide the other rules. A
field that changes what a rule means must not be ignored that way: a rule that
has one says so in `requires`, the format a reader has to know, and a version
that does not know it leaves the rule out and reports it instead of serving it.

Rules are instructions agents obey, so every change is also kept in a short
log (`rule-log:` rows): the rule before and after, when, and the chat that
made the change, as far as Richi can tell.
"""
import json
import os
import re
import shlex
import sys
from typing import NamedTuple, Optional

from . import agents


PREFIX = "rule:"
LOG_PREFIX = "rule-log:"
SCOPES = ("global", "project")
# IDs of rules and names of skills (as an agent lists them, e.g. plugin:skill) share one shape.
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}\Z")
ID_HELP = "Stable rule ID: 1-100 letters, digits, '.', '_', ':' or '-', starting with a letter or digit"
SKILL_HELP = agents.SKILL_HELP
valid_skill = agents.valid_skill
MAX_TEXT = 2000
# Agents load every applicable rule in full, so the whole set stays small.
MAX_TOTAL = 20000
MAX_SOURCE = 500
MAX_CHANGE = 100000
MAX_LOG = 200
# One stored rule with everything it carries. The limits above count the text; this one keeps a
# field this version does not know, or a hand-edited row, from growing without bound.
MAX_STORED = 50000
# What `rules show` gives an agent: what it needs to follow a rule and to change it. The stored
# record has more (where the rule came from, who changed it, fields of a newer Richi); rules list has it all.
SHOWN = ("id", "scope", "project_id", "text", "skill", "skills", "updated_at")
# The rule format this version reads. Raise it, and write it as a rule's `requires`, with the
# first field that changes when or whether a rule applies: older versions then skip that rule.
FORMAT = 1
# Fields this version understands, in the order commands print them. A stored
# rule must have the REQUIRED ones; any other field it has is kept as it is.
KNOWN = ("id", "scope", "project_id", "text", "skill", "skills", "source", "changed_by", "created_at", "updated_at")
REQUIRED = ("id", "scope", "project_id", "text", "created_at", "updated_at")
# What one change may carry. The command options and `rules apply` both become
# such an object, so a rule field is validated and stored in one place: change().
CHANGE_FIELDS = {"add": {"action", "id", "scope", "project", "text", "skill", "skills", "source"},
                 "update": {"action", "id", "expected_updated_at", "text", "skill", "skills", "source"},
                 "remove": {"action", "id", "expected_updated_at"}}
NOTE = ("Standing rules the user set for this workspace{project}. Follow them as the user's "
        "instructions in this chat; a newer explicit request from the user takes precedence. "
        "{skills}Run this command again after context compaction and when switching projects.")
SKILL_NOTE_AGENT = "A rule with use_skill asks you to use that skill whenever the rule applies. "
SKILL_NOTE_ANY = ("A rule that names a skill asks you to use it whenever the rule applies; skills maps an agent ID "
                  "({agents}) to a skill of its own that replaces skill for that agent. ")


def _known_agents():
    return ", ".join(agent.id + ": " + agent.name for agent in agents.registry())


def rule_id(value):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError(ID_HELP)
    return value


class Unsupported(ValueError):
    """A stored rule that only a newer Richi may serve."""

    def __init__(self, level):
        super().__init__("needs a Richi that reads rule format %d; this one reads format %d" % (level, FORMAT))
        self.level = level


def _failure(api, message, code):
    """A command error with a stable code, so the map can tell a conflict from bad input."""
    error = api.MemoryError(message)
    error.code = code
    return error


def _argument_id(value):
    import argparse
    try:
        return rule_id(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _argument_agent(value):
    import argparse
    if not agents.valid_id(value):
        raise argparse.ArgumentTypeError(agents.ID_HELP)
    return value


def _argument_entries(value):
    import argparse
    if not value.isdigit() or not 1 <= int(value) <= MAX_LOG:
        raise argparse.ArgumentTypeError("a number from 1 to %d" % MAX_LOG)
    return int(value)


def add_commands(commands, api):
    # Help names the built-in agents only, so that it does not depend on this computer's settings.
    known = ", ".join(agents.builtin_ids()) + " or one from skill-sources list"
    skill_help = ("Skill to use whenever the rule applies, as the agent names it (e.g. plugin:skill). NAME is for "
                  "every agent; AGENT=NAME is for that agent only (" + known + "), so repeat it when agents use "
                  "different skills")
    parser = commands.add_parser("rules", help="Show or manage the user's standing rules for this workspace and its projects")
    actions = parser.add_subparsers(dest="action", required=True)
    show = actions.add_parser("show", help="Print the rules that apply here in full: global rules plus one project's rules")
    show.add_argument("--project", metavar="ID", help="Project whose rules to include (default: the --project-path project)")
    show.add_argument("--agent", metavar="ID", type=_argument_agent,
                      help="Agent whose skill each rule gives as use_skill, such as " + known
                      + " (default: detected from the agent's shell)")
    listing = actions.add_parser("list", help="List every stored rule of this workspace, optionally of one scope")
    scope = listing.add_mutually_exclusive_group()
    scope.add_argument("--global", dest="global_scope", action="store_true", help="Only global rules")
    scope.add_argument("--project", metavar="ID", help="Only the rules of this project")
    add = actions.add_parser("add", help="Create a rule the user explicitly asked to keep")
    add.add_argument("id", type=_argument_id, help=ID_HELP)
    target = add.add_mutually_exclusive_group(required=True)
    target.add_argument("--global", dest="global_scope", action="store_true",
                        help="Apply the rule in every chat bound to this workspace")
    target.add_argument("--project", metavar="ID", help="Apply the rule only to work in this registered project")
    add.add_argument("--text", required=True,
                     help="The rule as the user stated it, at most %d characters; - reads it from stdin" % MAX_TEXT)
    add.add_argument("--skill", metavar="[AGENT=]NAME", action="append", help=skill_help)
    add.add_argument("--source", help="Where the user stated the rule, such as the chat identity (at most %d characters)" % MAX_SOURCE)
    update = actions.add_parser("update", help="Change a rule's text, skills or source; requires the version from show or list")
    update.add_argument("id", type=_argument_id, help="ID of the rule to change")
    update.add_argument("--expected-updated-at", required=True, metavar="TIMESTAMP",
                        help="The rule's updated_at as last read; a newer version is a conflict")
    update.add_argument("--text", help="New rule text, at most %d characters; - reads it from stdin" % MAX_TEXT)
    update.add_argument("--skill", metavar="[AGENT=]NAME", action="append",
                        help="New skills, replacing all the rule had: NAME for every agent, AGENT=NAME for one; "
                             "an empty value removes them")
    update.add_argument("--source", help="New source; an empty value removes it")
    remove = actions.add_parser("remove", help="Delete a rule; requires the version from show or list")
    # Not checked as an ID: a row that cannot be read is removed by whatever its key holds.
    remove.add_argument("id", help="ID of the rule to delete")
    remove.add_argument("--expected-updated-at", required=True, metavar="TIMESTAMP",
                        help="The rule's updated_at as last read; a newer version is a conflict")
    apply = actions.add_parser("apply", help="Add, update or remove a rule described by a JSON object")
    apply.add_argument("--json", required=True, metavar="FILE",
                       help="File with one change object: action (add, update or remove), id and that action's "
                            "fields; - reads it from stdin")
    log = actions.add_parser("log", help="Show recent rule changes: who changed what, with the rule before and after")
    log.add_argument("--id", help="Only the changes of this rule")
    log.add_argument("--entries", metavar="N", type=_argument_entries, default=20,
                     help="How many of the latest changes to show, 1-%d (default: %%(default)s)" % MAX_LOG)
    clear = actions.add_parser("clear-log", help="Delete the recorded rule changes, all or those of one rule")
    clear.add_argument("--id", help="Only the changes of this rule")


def writes(args):
    return args.command == "rules" and args.action in {"add", "update", "remove", "apply", "clear-log"}


def _storable(value):
    """Whether the text can be written as UTF-8: JSON may carry half of a surrogate pair."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _text(value, api):
    value = api.string(value, "text")
    if not _storable(value):
        raise api.MemoryError("Rule text must not contain an unpaired surrogate character")
    if len(value) > MAX_TEXT:
        raise _failure(api, "Rule text must be at most %d characters; keep one rule per instruction" % MAX_TEXT, "limit")
    return value


def _skill(value, api):
    if value is None or value == "":
        return None
    if not valid_skill(value):
        raise api.MemoryError(SKILL_HELP)
    return value


def _skill_options(values, api):
    """(skill for every agent, {agent: skill}) as --skill NAME and --skill AGENT=NAME give them."""
    shared, own, plain = None, {}, False
    for value in values or ():
        agent, separator, name = value.partition("=") if "=" in value else ("", "", value)
        if separator:
            if agent in own:
                raise api.MemoryError("Give one --skill " + agent + "=NAME")
            own[agent] = name
        else:
            if plain:
                raise api.MemoryError("Give one --skill NAME for every agent; use AGENT=NAME for one agent")
            plain, shared = True, name
    return shared, own


def _skill_setting(shared, own, api, kept=()):
    """(skill for every agent, skills per agent or None) as a rule stores them.

    An agent may be a known one or one the rule already names (kept). Agents
    that would get the same skill share it: when every known agent names the
    same one, it is stored as the skill for every agent.
    """
    if own is not None and not isinstance(own, dict):
        raise api.MemoryError("skills maps an agent ID to its skill")
    allowed = set(agents.ids()) | set(kept)
    unknown = sorted(str(agent) for agent in (own or {}) if agent not in allowed)
    if unknown:
        raise api.MemoryError("Unknown agent in skills: " + ", ".join(unknown) + "; known agents: "
                              + ", ".join(agents.ids()))
    shared, own = _skill(shared, api), {agent: _skill(name, api) for agent, name in (own or {}).items()}
    if own and set(agents.ids()) <= set(own) and len(set(own.values())) == 1:
        shared, own = next(iter(own.values())), {}
    own = {agent: name for agent, name in own.items() if name != shared}
    return shared, (dict(sorted(own.items())) or None)


def skill_for(rule, agent):
    """The skill a rule asks this agent to use, or None."""
    skills = rule.get("skills") or {}
    return skills[agent] if agent in skills else rule.get("skill")


def _source(value, api):
    if value is None or value == "":
        return None
    value = api.string(value, "source")
    if len(value) > MAX_SOURCE or any(ord(ch) < 32 for ch in value) or not _storable(value):
        raise api.MemoryError("Rule source must be one line of at most %d characters" % MAX_SOURCE)
    return value


class Author(NamedTuple):
    """Who makes a change: the chat identity or the map (by), and the agent whose shell it is."""
    by: Optional[str] = None
    agent: Optional[str] = None


def current_author(environ=None):
    """Who makes this change, as far as Richi can tell: the map, or the chat whose shell runs the command.

    It is a record for the user's review, not a proof: anything that can run
    the command can also choose its environment. A command reads it once,
    here, and hands it to change().
    """
    environ = os.environ if environ is None else environ
    marked = environ.get("RICHI_RULES_ACTOR")
    value = "".join(ch for ch in (marked or environ.get("RICHI_CHAT_ID") or "") if ord(ch) >= 32)[:200]
    # The map's server may have been started from an agent's shell; a change made in the page is not that agent's.
    return Author(value or None, None if marked else agents.detect(environ))


def _decode(key, value):
    """The stored rule, or ValueError. Fields this version does not know are kept."""
    if len(value) > MAX_STORED:
        raise ValueError("the stored record is larger than %d characters" % MAX_STORED)
    record = json.loads(value)
    if not isinstance(record, dict):
        raise ValueError("not an object")
    # First of all: a newer format may also change the fields checked below.
    level = record.get("requires", FORMAT)
    if type(level) is not int or level < 1:
        raise ValueError("invalid requires")
    if level > FORMAT:
        raise Unsupported(level)
    if not set(REQUIRED) <= set(record) or not isinstance(record["id"], str) or PREFIX + record["id"] != key:
        raise ValueError("a required field is missing or the ID does not match")
    rule_id(record["id"])
    if not isinstance(record["text"], str) or not isinstance(record["updated_at"], str):
        raise ValueError("invalid text or version")
    if record.get("skill") is not None and not valid_skill(record["skill"]):
        raise ValueError("invalid skill")
    skills = record.get("skills")
    # Any well-formed agent ID: a newer Richi may know agents this one does not.
    if skills is not None and (not isinstance(skills, dict) or not skills
                               or not all(agents.valid_id(agent) for agent in skills)
                               or any(name is not None and not valid_skill(name) for name in skills.values())):
        raise ValueError("invalid skills")
    scope, project = record["scope"], record["project_id"]
    if not ((scope == "global" and project is None) or (scope == "project" and isinstance(project, str))):
        raise ValueError("invalid scope")
    rule = {field: record.get(field) for field in KNOWN}
    rule.update((field, content) for field, content in record.items() if field not in KNOWN)
    return rule


def _stored_version(value):
    """What `rules remove --expected-updated-at` takes for a row that cannot be read."""
    try:
        version = json.loads(value)["updated_at"]
    except (ValueError, TypeError, KeyError, RecursionError):
        return "invalid"
    return version if isinstance(version, str) else "invalid"


def _removal(identifier, version):
    """The command that removes a row by its key, whatever the key holds."""
    words = ["richi", "rules", "remove", shlex.quote(identifier), "--expected-updated-at", shlex.quote(version)]
    if identifier.startswith("-"):
        # Only after -- is a word that begins with a dash read as the ID.
        words = words[:3] + words[4:] + ["--", words[3]]
    return " ".join(words)


def scan(conn):
    """(rules: global first, then by project and ID; rows that cannot be read; rules a newer Richi must serve)."""
    rows = conn.execute("SELECT key, value FROM metadata WHERE 1=1 AND key LIKE 'rule:%' ORDER BY key").fetchall()
    rules, invalid, unsupported = [], [], []
    for key, value in rows:
        if not key.startswith(PREFIX):
            continue
        identifier = key[len(PREFIX):]
        try:
            rules.append(_decode(key, value))
        except Unsupported as exc:
            # updated_at: what removing the rule takes, for a user who will not update.
            unsupported.append({"id": identifier, "requires": exc.level, "error": str(exc),
                                "updated_at": _stored_version(value), "next": "richi update"})
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            version = _stored_version(value)
            invalid.append({"id": identifier, "error": str(exc) or type(exc).__name__, "updated_at": version,
                            "next": _removal(identifier, version)})
    rules.sort(key=lambda rule: (rule["scope"] != "global", rule["project_id"] or "", rule["id"]))
    return rules, invalid, unsupported


def load(conn):
    """Every stored rule this version serves."""
    return scan(conn)[0]


def _left_out(result, invalid, unsupported):
    """Say which stored rules are not among the returned ones: the user set them, and agents do not get them."""
    if invalid:
        result["invalid_rules"] = invalid
    if unsupported:
        result["unsupported_rules"] = unsupported
    return result


def _project(conn, project_id, api):
    if conn.execute("SELECT 1 FROM projects WHERE 1=1 AND id = ?", (project_id,)).fetchone() is None:
        raise api.NotFoundError("Unknown project in this workspace: " + project_id + "; see project list")
    return project_id


def _check_version(identifier, stored, expected, api):
    if expected != stored:
        raise _failure(api, "Rule update conflict for {}: expected {} but the current version is {}; "
                       "read it again with rules list".format(identifier, expected, stored), "conflict")


def _check_total(conn, api, replaced=None, added=""):
    total = sum(len(rule["text"]) for rule in load(conn) if rule["id"] != replaced) + len(added)
    if total > MAX_TOTAL:
        raise _failure(api, "Rules of one workspace may hold at most %d characters in all (would be %d); "
                       "shorten or remove rules first" % (MAX_TOTAL, total), "limit")


def _store(conn, rule, api):
    value = json.dumps(rule, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(value) > MAX_STORED:
        # It could not be read back: _decode refuses a record of this size.
        raise _failure(api, "The rule with all it stores would exceed %d characters" % MAX_STORED, "limit")
    conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (PREFIX + rule["id"], value))


def _record(conn, api, action, identifier, before, after, author):
    """Keep the change in the log, and only the latest MAX_LOG changes."""
    at = api.now()
    entry = {"at": at, "id": identifier, "action": action, "by": author.by, "agent": author.agent,
             "before": before, "after": after}
    conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (LOG_PREFIX + at + ":" + identifier, json.dumps(entry, ensure_ascii=False, sort_keys=True,
                                                               separators=(",", ":"))))
    conn.execute("DELETE FROM metadata WHERE 1=1 AND key LIKE 'rule-log:%' AND key NOT IN "
                 "(SELECT key FROM metadata WHERE 1=1 AND key LIKE 'rule-log:%' ORDER BY key DESC LIMIT ?)", (MAX_LOG,))


def log(conn, identifier=None, entries=20):
    """The latest changes, newest first."""
    rows = conn.execute("SELECT value FROM metadata WHERE 1=1 AND key LIKE 'rule-log:%' ORDER BY key DESC").fetchall()
    found = []
    for (value,) in rows:
        try:
            entry = json.loads(value)
        except (ValueError, RecursionError):
            continue
        if isinstance(entry, dict) and (identifier is None or entry.get("id") == identifier):
            found.append(entry)
            if len(found) == entries:
                break
    return {"view": "rules-log", "entries": found, "kept": MAX_LOG}


def clear_log(conn, identifier=None):
    """Delete the recorded changes, all or one rule's: the log keeps rule texts, removed rules included."""
    rows = conn.execute("SELECT key, value FROM metadata WHERE 1=1 AND key LIKE 'rule-log:%'").fetchall()
    keys = []
    for key, value in rows:
        try:
            entry = json.loads(value)
        except (ValueError, RecursionError):
            entry = None
        # An entry that cannot be read belongs to no rule, so only clearing everything removes it.
        if identifier is None or (isinstance(entry, dict) and entry.get("id") == identifier):
            keys.append((key,))
    conn.executemany("DELETE FROM metadata WHERE 1=1 AND key = ?", keys)
    return {"view": "rules-log", "results": [{"status": "cleared", "entries": len(keys)}]}


def show(conn, api, workspace, project_id, agent=None, unmatched_path=None):
    rules, invalid, unsupported = scan(conn)
    if project_id is not None:
        _project(conn, project_id, api)
    applicable = [{field: rule[field] for field in SHOWN} for rule in rules
                  if rule["scope"] == "global" or rule["project_id"] == project_id]
    if agent is not None:
        applicable = [dict(rule, use_skill=skill_for(rule, agent)) for rule in applicable]
    result = {"view": "rules", "workspace": workspace, "project": project_id, "agent": agent, "rules": applicable,
              "note": NOTE.format(project=" and project " + project_id if project_id else "",
                                  skills=SKILL_NOTE_AGENT if agent is not None
                                  else SKILL_NOTE_ANY.format(agents=_known_agents()))}
    if project_id is None:
        others = sorted({rule["project_id"] for rule in rules if rule["scope"] == "project"})
        if others:
            result["project_rules_not_shown"] = {
                "projects": others,
                "next": "richi --project-path PATH rules show (for work in one of these projects)"}
    if unmatched_path is not None:
        # The global rules above still bind the agent in a repository this workspace does not know.
        result["project_path_problem"] = {
            "error": unmatched_path,
            "next": "These are the global rules only. Ask the user whether this repository belongs to the "
                    "workspace (richi project add PATH registers it), or pass --project ID."}
    return _left_out(result, invalid, unsupported)


def _stdin_text(stdin):
    """Text piped to the command; the newline that ends the pipe is not part of the rule."""
    text = (stdin or sys.stdin).read(MAX_CHANGE + 1)
    return text[:-1] if text.endswith("\n") else text


def read_request(args, api, stdin=None):
    """The change object of an add, update, remove or apply command; None for any other.

    It may wait for standard input or a file, so it is read before the store
    is locked for writing: a command waiting for its text keeps nobody out.
    """
    if args.action not in {"add", "update", "remove", "apply"}:
        return None
    if args.action == "apply":
        try:
            if args.json == "-":
                data = (stdin or sys.stdin).read(MAX_CHANGE + 1)
            else:
                with open(args.json, encoding="utf-8") as handle:
                    data = handle.read(MAX_CHANGE + 1)
        except (OSError, UnicodeError) as exc:
            raise api.MemoryError("Cannot read the rule change: " + str(exc)) from exc
        try:
            if len(data) > MAX_CHANGE:
                raise ValueError("more than %d characters" % MAX_CHANGE)
            return json.loads(data)
        except (ValueError, RecursionError) as exc:
            raise api.MemoryError("Invalid rule change JSON: " + (str(exc) or "nested too deeply")) from exc
    request = {"action": args.action, "id": args.id}
    if args.action == "add":
        request.update(scope="global" if args.global_scope else "project", project=args.project)
    else:
        request["expected_updated_at"] = args.expected_updated_at
    if args.action != "remove":
        if args.text is not None:
            request["text"] = _stdin_text(stdin) if args.text == "-" else args.text
        if args.skill is not None:
            request["skill"], request["skills"] = _skill_options(args.skill, api)
        if args.source is not None:
            request["source"] = args.source
    return request


def change(conn, request, api, checked_project=None, author=Author()):
    """Add, update or remove one rule as the change object says.

    checked_project is the --project-path project; author is who makes the
    change (current_author), recorded with the rule and in the change log.
    """
    if not isinstance(request, dict) or request.get("action") not in CHANGE_FIELDS \
            or set(request) - CHANGE_FIELDS[request["action"]]:
        raise api.MemoryError("A rule change is an object with action add, update or remove, id and that "
                              "action's fields only")
    action, identifier = request["action"], request.get("id")
    if not isinstance(identifier, str):
        raise api.MemoryError(ID_HELP)
    row = conn.execute("SELECT key, value FROM metadata WHERE 1=1 AND key = ?", (PREFIX + identifier,)).fetchone()
    # Only removal takes a key that is not an ID: such a row cannot be read and must not stay forever.
    if _NAME.fullmatch(identifier) is None and not (action == "remove" and row is not None):
        raise api.MemoryError(ID_HELP)
    if action == "add":
        if row is not None:
            raise _failure(api, "Rule " + identifier + " already exists; change it with rules update", "exists")
        scope, project = request.get("scope"), request.get("project")
        if not (scope == "global" and project is None) and not (scope == "project" and isinstance(project, str)):
            raise api.MemoryError("Choose scope global, or scope project with a project")
        if scope == "project" and checked_project is not None and project != checked_project:
            raise api.MemoryError("Rule for project " + project + " does not match the checked project "
                                  + checked_project)
        text = _text(request.get("text"), api)
        timestamp = api.now()
        skill, skills = _skill_setting(request.get("skill"), request.get("skills"), api)
        rule = {"id": identifier, "scope": scope,
                "project_id": _project(conn, project, api) if scope == "project" else None,
                "text": text, "skill": skill, "skills": skills, "source": _source(request.get("source"), api),
                "changed_by": author.by, "created_at": timestamp, "updated_at": timestamp}
        _check_total(conn, api, added=text)
        _store(conn, rule, api)
        _record(conn, api, "created", identifier, None, rule, author)
        return {"results": [{"id": identifier, "status": "created"}], "rule": rule}
    if row is None:
        raise api.NotFoundError("Unknown rule: " + identifier + "; see rules list")
    expected = request.get("expected_updated_at")
    try:
        rule = _decode(row[0], row[1])
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        # A row this version cannot read or may not serve can still be removed, but not changed.
        if action != "remove":
            problem = ("Rule " + identifier + " " + str(exc) + "; update Richi to change it"
                       if isinstance(exc, Unsupported) else "Invalid stored rule " + identifier + ": " + str(exc))
            raise api.MemoryError(problem + "; remove it with " + _removal(identifier, _stored_version(row[1]))) from exc
        _check_version(identifier, _stored_version(row[1]), expected, api)
        conn.execute("DELETE FROM metadata WHERE 1=1 AND key = ?", (row[0],))
        _record(conn, api, "removed", identifier, {"id": identifier, "unreadable": row[1][:MAX_TEXT * 2]}, None, author)
        return {"results": [{"id": identifier, "status": "removed"}], "rule": None}
    if checked_project is not None and rule["scope"] == "project" and rule["project_id"] != checked_project:
        raise api.MemoryError("Rule " + rule["id"] + " belongs to project " + rule["project_id"]
                              + ", not the checked project " + checked_project)
    _check_version(identifier, rule["updated_at"], expected, api)
    if action == "remove":
        conn.execute("DELETE FROM metadata WHERE 1=1 AND key = ?", (row[0],))
        _record(conn, api, "removed", identifier, rule, None, author)
        return {"results": [{"id": identifier, "status": "removed"}], "rule": rule}
    if not {"text", "skill", "skills", "source"} & set(request):
        raise api.MemoryError("Nothing to change; give the text, the skills, the source or several")
    changed = dict(rule)
    if "text" in request:
        changed["text"] = _text(request["text"], api)
    if {"skill", "skills"} & set(request):
        # The skills of a rule are replaced together: what the change leaves out is removed.
        changed["skill"], changed["skills"] = _skill_setting(request.get("skill"), request.get("skills"), api,
                                                             kept=rule["skills"] or ())
    if "source" in request:
        changed["source"] = _source(request["source"], api)
    if changed == rule:
        return {"results": [{"id": identifier, "status": "unchanged"}], "rule": rule}
    _check_total(conn, api, replaced=identifier, added=changed["text"])
    changed.update(changed_by=author.by, updated_at=api.now())
    _store(conn, changed, api)
    _record(conn, api, "updated", identifier, rule, changed, author)
    return {"results": [{"id": identifier, "status": "updated"}], "rule": changed}


def run(conn, args, api, workspace=None, checked_project=None, stdin=None, unmatched_path=None, request=None):
    """Run a rules command; checked_project is the --project-path project ID.

    request is the change read beforehand (read_request); the caller that
    locks the store passes it, so that no input is awaited under the lock.
    """
    project_id = getattr(args, "project", None) or checked_project
    if args.action == "show":
        return show(conn, api, workspace, project_id, args.agent or agents.detect(), unmatched_path)
    if args.action == "log":
        return log(conn, args.id, args.entries)
    if args.action == "clear-log":
        return clear_log(conn, args.id)
    if args.action == "list":
        rules, invalid, unsupported = scan(conn)
        if args.global_scope:
            rules = [rule for rule in rules if rule["scope"] == "global"]
        elif args.project is not None:
            _project(conn, args.project, api)
            rules = [rule for rule in rules if rule["project_id"] == args.project]
        projects = [{"id": row[0], "name": row[1]}
                    for row in conn.execute("SELECT id, name FROM projects WHERE 1=1 ORDER BY id")]
        return _left_out({"view": "rules", "workspace": workspace, "rules": rules, "agents": agents.describe(),
                          "projects": projects, "characters": sum(len(rule["text"]) for rule in rules),
                          "limit_characters": MAX_TOTAL}, invalid, unsupported)
    if request is None:
        request = read_request(args, api, stdin)
    return change(conn, request, api, checked_project, current_author())
