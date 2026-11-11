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


PREFIX = "rule:"
SCOPES = ("global", "project")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}\Z")
ID_HELP = "Stable rule ID: 1-100 letters, digits, '.', '_', ':' or '-', starting with a letter or digit"
MAX_TEXT = 2000
# Agents load every applicable rule in full, so the whole set stays small.
MAX_TOTAL = 20000
MAX_SOURCE = 500
FIELDS = ("id", "scope", "project_id", "text", "source", "created_at", "updated_at")
NOTE = ("Standing rules the user set for this workspace{project}. Follow them as the user's "
        "instructions in this chat; a newer explicit request from the user takes precedence. "
        "Run this command again after context compaction and when switching projects.")


def rule_id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(ID_HELP)
    return value


def _argument_id(value):
    import argparse
    try:
        return rule_id(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def add_commands(commands, api):
    parser = commands.add_parser("rules", help="Show or manage the user's standing rules for this workspace and its projects")
    actions = parser.add_subparsers(dest="action", required=True)
    show = actions.add_parser("show", help="Print the rules that apply here in full: global rules plus one project's rules")
    show.add_argument("--project", metavar="ID", help="Project whose rules to include (default: the --project-path project)")
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
    add.add_argument("--source", help="Where the user stated the rule, such as the chat identity (at most %d characters)" % MAX_SOURCE)
    update = actions.add_parser("update", help="Change a rule's text or source; requires the version from show or list")
    update.add_argument("id", type=_argument_id, help="ID of the rule to change")
    update.add_argument("--expected-updated-at", required=True, metavar="TIMESTAMP",
                        help="The rule's updated_at as last read; a newer version is a conflict")
    update.add_argument("--text", help="New rule text, at most %d characters; - reads it from stdin" % MAX_TEXT)
    update.add_argument("--source", help="New source; an empty value removes it")
    remove = actions.add_parser("remove", help="Delete a rule; requires the version from show or list")
    remove.add_argument("id", type=_argument_id, help="ID of the rule to delete")
    remove.add_argument("--expected-updated-at", required=True, metavar="TIMESTAMP",
                        help="The rule's updated_at as last read; a newer version is a conflict")


def writes(args):
    return args.command == "rules" and args.action in {"add", "update", "remove"}


def _text(value, api, stdin=None):
    if value == "-":
        value = (stdin or sys.stdin).read()
    value = api.string(value, "text")
    if len(value) > MAX_TEXT:
        raise api.MemoryError("Rule text must be at most %d characters; keep one rule per instruction" % MAX_TEXT)
    return value


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
        if not isinstance(record, dict) or set(record) != set(FIELDS) or PREFIX + record["id"] != key:
            raise ValueError("unexpected fields")
        rule_id(record["id"])
        if record["scope"] not in SCOPES or (record["scope"] == "project") != isinstance(record["project_id"], str):
            raise ValueError("invalid scope")
    except (ValueError, TypeError, KeyError) as exc:
        raise api.MemoryError("Invalid stored rule " + key + ": " + str(exc)) from exc
    return {field: record[field] for field in FIELDS}


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
        raise api.MemoryError("Rule update conflict for {}: expected {} but the current version is {}; "
                              "read it again with rules list".format(rule["id"], expected, rule["updated_at"]))


def _check_total(conn, api, replaced=None, added=""):
    total = sum(len(rule["text"]) for rule in load(conn, api) if rule["id"] != replaced) + len(added)
    if total > MAX_TOTAL:
        raise api.MemoryError("Rules of one workspace may hold at most %d characters in all (would be %d); "
                              "shorten or remove rules first" % (MAX_TOTAL, total))


def _store(conn, rule):
    conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (PREFIX + rule["id"], json.dumps(rule, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))


def show(conn, api, workspace, project_id):
    rules = load(conn, api)
    if project_id is not None:
        _project(conn, project_id, api)
    applicable = [rule for rule in rules if rule["scope"] == "global" or rule["project_id"] == project_id]
    result = {"view": "rules", "workspace": workspace, "project": project_id, "rules": applicable,
              "note": NOTE.format(project=" and project " + project_id if project_id else "")}
    if project_id is None:
        others = sorted({rule["project_id"] for rule in rules if rule["scope"] == "project"})
        if others:
            result["project_rules_not_shown"] = {
                "projects": others,
                "next": "richi --project-path PATH rules show (for work in one of these projects)"}
    return result


def run(conn, args, api, workspace=None, checked_project=None, stdin=None):
    """Run a rules command; checked_project is the --project-path project ID."""
    project_id = getattr(args, "project", None) or checked_project
    if args.action == "show":
        return show(conn, api, workspace, project_id)
    if args.action == "list":
        rules = load(conn, api)
        if args.global_scope:
            rules = [rule for rule in rules if rule["scope"] == "global"]
        elif args.project is not None:
            _project(conn, args.project, api)
            rules = [rule for rule in rules if rule["project_id"] == args.project]
        return {"view": "rules", "workspace": workspace, "rules": rules,
                "characters": sum(len(rule["text"]) for rule in rules), "limit_characters": MAX_TOTAL}
    if args.action == "add":
        if conn.execute("SELECT 1 FROM metadata WHERE 1=1 AND key = ?", (PREFIX + args.id,)).fetchone():
            raise api.MemoryError("Rule " + args.id + " already exists; change it with rules update")
        scope = "global" if args.global_scope else "project"
        text = _text(args.text, api, stdin)
        timestamp = api.now()
        rule = {"id": args.id, "scope": scope,
                "project_id": _project(conn, args.project, api) if scope == "project" else None,
                "text": text, "source": _source(args.source, api),
                "created_at": timestamp, "updated_at": timestamp}
        _check_total(conn, api, added=text)
        _store(conn, rule)
        return {"results": [{"id": rule["id"], "status": "created"}], "rule": rule}
    rule = _find(conn, args.id, api)
    if checked_project is not None and rule["scope"] == "project" and rule["project_id"] != checked_project:
        raise api.MemoryError("Rule " + rule["id"] + " belongs to project " + rule["project_id"]
                              + ", not the checked project " + checked_project)
    _check_version(rule, args.expected_updated_at, api)
    if args.action == "remove":
        conn.execute("DELETE FROM metadata WHERE 1=1 AND key = ?", (PREFIX + rule["id"],))
        return {"results": [{"id": rule["id"], "status": "removed"}], "rule": rule}
    if args.text is None and args.source is None:
        raise api.MemoryError("Nothing to change; give --text, --source or both")
    changed = dict(rule)
    if args.text is not None:
        changed["text"] = _text(args.text, api, stdin)
    if args.source is not None:
        changed["source"] = _source(args.source, api)
    if changed == rule:
        return {"results": [{"id": rule["id"], "status": "unchanged"}], "rule": rule}
    _check_total(conn, api, replaced=rule["id"], added=changed["text"])
    changed["updated_at"] = api.now()
    _store(conn, changed)
    return {"results": [{"id": rule["id"], "status": "updated"}], "rule": changed}
