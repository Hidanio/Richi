"""Standing rules: instructions the user set for a workspace or one of its projects.

Rules are not knowledge. Recall, search, discovery and the graph never return
them; an agent loads the applicable set explicitly with `rules show` after it
selects the workspace, and follows it as the user's own instructions. A global
rule applies to every chat bound to the workspace; a project rule applies to
work in one registered project of that workspace.

Each rule is one `rule:<id>` row of the store's metadata table. Every supported
schema has that table, so rules need no migration, backups and workspace
copies carry them, and an older runtime simply ignores them.
"""
import json
import re
import sys

from . import agents


PREFIX = "rule:"
SCOPES = ("global", "project")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}\Z")
ID_HELP = "Stable rule ID: 1-100 letters, digits, '.', '_', ':' or '-', starting with a letter or digit"
MAX_TEXT = 2000
# Agents load every applicable rule in full, so the whole set stays small.
MAX_TOTAL = 20000
MAX_SOURCE = 500
# A skill the rule asks agents to use, named as the agent lists it (e.g. plugin:skill).
_SKILL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}\Z")
SKILL_HELP = "Skill name: 1-100 letters, digits, '.', '_', ':' or '-', starting with a letter or digit"
FIELDS = ("id", "scope", "project_id", "text", "skill", "skills", "source", "created_at", "updated_at")
# Fields a stored rule may lack: rows written before the field existed.
OPTIONAL = {"skill", "skills"}
MAX_CHANGE = 100000
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
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(ID_HELP)
    return value


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
    remove.add_argument("id", type=_argument_id, help="ID of the rule to delete")
    remove.add_argument("--expected-updated-at", required=True, metavar="TIMESTAMP",
                        help="The rule's updated_at as last read; a newer version is a conflict")
    apply = actions.add_parser("apply", help="Add, update or remove a rule described by a JSON object")
    apply.add_argument("--json", required=True, metavar="FILE",
                       help="File with one change object: action (add, update or remove), id and that action's "
                            "fields; - reads it from stdin")


def writes(args):
    return args.command == "rules" and args.action in {"add", "update", "remove", "apply"}


def _text(value, api):
    value = api.string(value, "text")
    if len(value) > MAX_TEXT:
        raise _failure(api, "Rule text must be at most %d characters; keep one rule per instruction" % MAX_TEXT, "limit")
    return value


def valid_skill(value):
    return isinstance(value, str) and _SKILL.fullmatch(value) is not None


def _skill(value, api):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _SKILL.fullmatch(value):
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
    if len(value) > MAX_SOURCE or any(ord(ch) < 32 for ch in value):
        raise api.MemoryError("Rule source must be one line of at most %d characters" % MAX_SOURCE)
    return value


def _decode(key, value, api):
    try:
        record = json.loads(value)
        if not isinstance(record, dict) or not set(FIELDS) - OPTIONAL <= set(record) <= set(FIELDS) \
                or PREFIX + record["id"] != key:
            raise ValueError("unexpected fields")
        rule_id(record["id"])
        if record.get("skill") is not None and not _SKILL.fullmatch(record["skill"]):
            raise ValueError("invalid skill")
        skills = record.get("skills")
        # Any well-formed agent ID: a newer Richi may know agents this one does not.
        if skills is not None and (not isinstance(skills, dict) or not skills
                                   or not all(agents.valid_id(agent) for agent in skills)
                                   or any(name is not None and not _SKILL.fullmatch(name) for name in skills.values())):
            raise ValueError("invalid skills")
        if record["scope"] not in SCOPES or (record["scope"] == "project") != isinstance(record["project_id"], str):
            raise ValueError("invalid scope")
    except (ValueError, TypeError, KeyError) as exc:
        raise api.MemoryError("Invalid stored rule " + key + ": " + str(exc)) from exc
    return {field: record.get(field) for field in FIELDS}


def load(conn, api):
    """Every stored rule, global rules first, then by project and ID."""
    rows = conn.execute("SELECT key, value FROM metadata WHERE 1=1 AND key LIKE 'rule:%' ORDER BY key").fetchall()
    rules = [_decode(row[0], row[1], api) for row in rows if row[0].startswith(PREFIX)]
    return sorted(rules, key=lambda rule: (rule["scope"] != "global", rule["project_id"] or "", rule["id"]))


def _project(conn, project_id, api):
    if conn.execute("SELECT 1 FROM projects WHERE 1=1 AND id = ?", (project_id,)).fetchone() is None:
        raise api.NotFoundError("Unknown project in this workspace: " + project_id + "; see project list")
    return project_id


def _find(conn, identifier, api):
    row = conn.execute("SELECT key, value FROM metadata WHERE 1=1 AND key = ?", (PREFIX + identifier,)).fetchone()
    if row is None:
        raise api.NotFoundError("Unknown rule: " + identifier + "; see rules list")
    return _decode(row[0], row[1], api)


def _check_version(rule, expected, api):
    if expected != rule["updated_at"]:
        raise _failure(api, "Rule update conflict for {}: expected {} but the current version is {}; "
                       "read it again with rules list".format(rule["id"], expected, rule["updated_at"]), "conflict")


def _check_total(conn, api, replaced=None, added=""):
    total = sum(len(rule["text"]) for rule in load(conn, api) if rule["id"] != replaced) + len(added)
    if total > MAX_TOTAL:
        raise _failure(api, "Rules of one workspace may hold at most %d characters in all (would be %d); "
                       "shorten or remove rules first" % (MAX_TOTAL, total), "limit")


def _store(conn, rule):
    conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (PREFIX + rule["id"], json.dumps(rule, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))


def show(conn, api, workspace, project_id, agent=None, unmatched_path=None):
    rules = load(conn, api)
    if project_id is not None:
        _project(conn, project_id, api)
    applicable = [rule for rule in rules if rule["scope"] == "global" or rule["project_id"] == project_id]
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
    return result


def _stdin(value, stdin):
    return (stdin or sys.stdin).read(MAX_CHANGE + 1) if value == "-" else value


def _request(args, api, stdin):
    """The change object of an add, update, remove or apply command."""
    if args.action == "apply":
        if args.json == "-":
            data = _stdin("-", stdin)
        else:
            try:
                with open(args.json, encoding="utf-8") as handle:
                    data = handle.read(MAX_CHANGE + 1)
            except (OSError, UnicodeError) as exc:
                raise api.MemoryError("Cannot read the rule change: " + str(exc)) from exc
        try:
            if len(data) > MAX_CHANGE:
                raise ValueError("more than %d characters" % MAX_CHANGE)
            return json.loads(data)
        except ValueError as exc:
            raise api.MemoryError("Invalid rule change JSON: " + str(exc)) from exc
    request = {"action": args.action, "id": args.id}
    if args.action == "add":
        request.update(scope="global" if args.global_scope else "project", project=args.project)
    else:
        request["expected_updated_at"] = args.expected_updated_at
    if args.action != "remove":
        if args.text is not None:
            request["text"] = _stdin(args.text, stdin)
        if args.skill is not None:
            request["skill"], request["skills"] = _skill_options(args.skill, api)
        if args.source is not None:
            request["source"] = args.source
    return request


def change(conn, request, api, checked_project=None):
    """Add, update or remove one rule as the change object says; checked_project is the --project-path project."""
    if not isinstance(request, dict) or request.get("action") not in CHANGE_FIELDS \
            or set(request) - CHANGE_FIELDS[request["action"]]:
        raise api.MemoryError("A rule change is an object with action add, update or remove, id and that "
                              "action's fields only")
    action = request["action"]
    try:
        identifier = rule_id(request.get("id"))
    except ValueError as exc:
        raise api.MemoryError(str(exc)) from exc
    if action == "add":
        if conn.execute("SELECT 1 FROM metadata WHERE 1=1 AND key = ?", (PREFIX + identifier,)).fetchone():
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
                "created_at": timestamp, "updated_at": timestamp}
        _check_total(conn, api, added=text)
        _store(conn, rule)
        return {"results": [{"id": rule["id"], "status": "created"}], "rule": rule}
    rule = _find(conn, identifier, api)
    if checked_project is not None and rule["scope"] == "project" and rule["project_id"] != checked_project:
        raise api.MemoryError("Rule " + rule["id"] + " belongs to project " + rule["project_id"]
                              + ", not the checked project " + checked_project)
    _check_version(rule, request.get("expected_updated_at"), api)
    if action == "remove":
        conn.execute("DELETE FROM metadata WHERE 1=1 AND key = ?", (PREFIX + rule["id"],))
        return {"results": [{"id": rule["id"], "status": "removed"}], "rule": rule}
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
        return {"results": [{"id": rule["id"], "status": "unchanged"}], "rule": rule}
    _check_total(conn, api, replaced=rule["id"], added=changed["text"])
    changed["updated_at"] = api.now()
    _store(conn, changed)
    return {"results": [{"id": rule["id"], "status": "updated"}], "rule": changed}


def run(conn, args, api, workspace=None, checked_project=None, stdin=None, unmatched_path=None):
    """Run a rules command; checked_project is the --project-path project ID."""
    project_id = getattr(args, "project", None) or checked_project
    if args.action == "show":
        return show(conn, api, workspace, project_id, args.agent or agents.detect(), unmatched_path)
    if args.action == "list":
        rules = load(conn, api)
        if args.global_scope:
            rules = [rule for rule in rules if rule["scope"] == "global"]
        elif args.project is not None:
            _project(conn, args.project, api)
            rules = [rule for rule in rules if rule["project_id"] == args.project]
        return {"view": "rules", "workspace": workspace, "rules": rules, "agents": agents.describe(),
                "characters": sum(len(rule["text"]) for rule in rules), "limit_characters": MAX_TOTAL}
    return change(conn, _request(args, api, stdin), api, checked_project)
