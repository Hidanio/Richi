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
be read is reported and skipped, never allowed to hide the other rules. What
an older reader would misread or refuse must not reach it that way: a rule
that has a field changing when or whether it applies, or a value this version
does not accept (a longer skill name, another scope, an agent ID of another
shape), says so in `requires`, the format a reader has to know, and a version
that does not know it leaves the rule out and reports it instead of serving it.

Rules are instructions agents obey, so every change is also kept in a short
log (`rule-log:` rows): the rule before and after, when, and the chat that
made the change, as far as Richi can tell.
"""
import json
import os
import sys
from typing import NamedTuple, Optional

from . import agents, skill_catalog
from .output import cut_name, shell_word
from .revisions import recovery_version


PREFIX = "rule:"
LOG_PREFIX = "rule-log:"
# IDs of rules and names of skills (as an agent lists them, e.g. plugin:skill) share one shape.
_NAME = agents.SKILL
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
# What a report of a row that cannot be read repeats of it: a longer key is cut, a longer or odd
# version is replaced with a content fingerprint, so one hand-made row cannot flood an agent or the map.
MAX_NAMED = 200
MAX_VERSION = 64
# What `rules show` gives an agent: what it needs to follow a rule and to change it. The stored
# record has more (where the rule came from, who changed it, fields of a newer Richi); rules list has it all.
SHOWN = ("id", "scope", "project_id", "text", "skill", "skills", "updated_at")
# The rule format this version reads. Raise it, and write it as a rule's `requires`, for a rule
# that holds anything this version would misread or refuse: a field that changes when or whether
# the rule applies, or a value outside what it accepts. Older versions then skip that rule; they
# still count its text toward the workspace's limit.
FORMAT = 1
# Fields this version understands, in the order commands print them. A stored
# rule must have the REQUIRED ones; any other field it has is kept as it is.
KNOWN = ("id", "scope", "project_id", "text", "skill", "skills", "source", "changed_by", "changed_by_agent",
         "created_at", "updated_at")
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


def rule_id(value):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError(ID_HELP)
    return value


class Unsupported(ValueError):
    """A stored rule that only a newer Richi may serve."""

    def __init__(self, level, record):
        super().__init__("needs a Richi that reads rule format %d; this one reads format %d" % (level, FORMAT))
        self.level = level
        self.record = record


def _failure(api, message, code, subject=None):
    """A command error with a stable code, so the map can tell a conflict from bad input.

    subject says which of the things a code covers the error is about (for
    limit: text, rules or record), so that nobody has to read the message.
    """
    error = api.MemoryError(message)
    error.code = code
    if subject is not None:
        error.details = {"subject": subject}
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
                  "every agent, agents added later included; AGENT=NAME replaces it for one agent (" + known
                  + "), so repeat it when agents use different skills. A skill given to every known agent is "
                  "stored as the skill for every agent, unless the agent settings cannot be used now; an agent "
                  "named beyond the known ones keeps its own skill or none (AGENT=)")
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


def _readable(value):
    """Text for a message: what UTF-8 cannot carry is written as an escape."""
    return value.encode("utf-8", "backslashreplace").decode("utf-8")


def _text_of(raw):
    """A column read as bytes (CAST AS BLOB) as text, or None when it is not UTF-8.

    Richi writes only text, but another program may store other bytes, and one
    such row must not stop a command that reads all of them.
    """
    try:
        return bytes(raw).decode("utf-8")
    except (UnicodeDecodeError, TypeError):
        return None


def _shown(raw):
    """Bytes as a person may read them: what is not UTF-8 is written as escapes."""
    return bytes(raw).decode("utf-8", "backslashreplace")


def _text(value, api):
    # Line ends are not part of the user's wording: one form, which a page's text field keeps as it is.
    value = api.string(value, "text").replace("\r\n", "\n").replace("\r", "\n")
    if not _storable(value):
        raise api.MemoryError("Rule text must not contain an unpaired surrogate character")
    if len(value) > MAX_TEXT:
        raise _failure(api, "Rule text must be at most %d characters; keep one rule per instruction" % MAX_TEXT,
                       "limit", "text")
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
    if plain and not shared and own:
        # It could only mean "nothing for the other agents", which a rule cannot keep apart from no
        # skill: the same skill for every known agent becomes the skill for every agent anyway.
        raise api.MemoryError("An empty --skill removes the rule's skills and is given alone, without AGENT=NAME")
    return shared, own


def _skill_setting(shared, own, api, kept=(), settings=None):
    """(skill for every agent, skills per agent or None) as a rule stores them.

    An agent may be a known one or one the rule already names (kept). A skill
    given for every agent stays that, also when each known agent gets another
    one: it is for the agents this computer does not list. Without one, a
    skill that every known agent is given is the skill of the action and
    becomes the skill for every agent; an agent the rule names beyond the known
    ones keeps its own skill beside it, or its "none", which is stored so that
    it still counts when that agent is back. No such merge while Richi does not
    know every own agent the user described (settings it cannot use).

    settings: the agent settings (agents.read_settings) this change goes by,
    read once, so that what it stores and what its answer says agree.
    """
    if own is not None and not isinstance(own, dict):
        raise api.MemoryError("skills maps an agent ID to its skill")
    settings = agents.read_settings() if settings is None else settings
    ids = tuple(agent.id for agent in agents.registry(entries=settings.entries))
    known = set(ids)
    unknown = sorted(_readable(str(agent)) for agent in (own or {}) if agent not in known | set(kept))
    if unknown:
        # An own agent is unknown also while its settings cannot be used; then say why.
        problem = _settings_problem({}, settings).get("agent_settings_problem")
        raise api.MemoryError("Unknown agent in skills: " + ", ".join(unknown) + "; known agents: "
                              + ", ".join(ids) + ("; " + problem["error"] if problem else ""))
    shared, own = _skill(shared, api), {agent: _skill(name, api) for agent, name in (own or {}).items()}
    if shared is None and own and known <= set(own) and agents.settings_complete(settings):
        # The skill of the action: the one skill every agent this computer lists is given. An agent the
        # rule names beyond them keeps its own choice beside it, "no skill" included.
        given = {own[agent] for agent in known}
        if len(given) == 1 and None not in given:
            shared = given.pop()
    own = {agent: name for agent, name in own.items() if name != shared or (name is None and agent not in known)}
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


def current_author(environ=None, entries=None):
    """Who makes this change, as far as Richi can tell: the map, or the chat whose shell runs the command.

    It is a record for the user's review, not a proof: anything that can run
    the command can also choose its environment. A command reads it once,
    here, and hands it to change().
    """
    environ = os.environ if environ is None else environ
    marked = environ.get("RICHI_RULES_ACTOR")
    # No control characters, and no half of a surrogate pair (a byte the shell could not decode): it is stored.
    value = "".join(ch for ch in (marked or environ.get("RICHI_CHAT_ID") or "")
                    if ord(ch) >= 32 and not 0xD800 <= ord(ch) <= 0xDFFF)[:200]
    # The map's server may have been started from an agent's shell; a change made in the page is not that agent's.
    return Author(value or None, None if marked else agents.detect(environ, entries=entries))


def _parsed(value):
    """The JSON value of a stored row; ValueError also when it could not be written out again.

    JSON may spell half of a surrogate pair as an escape, and a number too
    large for a float or NaN. Neither can be printed as JSON or stored as
    UTF-8 again, so a row that holds one is unreadable.
    """
    record = json.loads(value)
    try:
        written = json.dumps(record, ensure_ascii=False, allow_nan=False)
    except ValueError:
        raise ValueError("holds a number that is not finite") from None
    if not _storable(written):
        raise ValueError("holds an unpaired surrogate character")
    return record


def _decode(key, value):
    """The stored rule, or ValueError. Fields this version does not know are kept."""
    if len(value) > MAX_STORED:
        raise ValueError("the stored record is larger than %d characters" % MAX_STORED)
    record = _parsed(value)
    if not isinstance(record, dict):
        raise ValueError("not an object")
    # First of all: a newer format may also change the fields checked below.
    level = record.get("requires", FORMAT)
    if type(level) is not int or level < 1:
        raise ValueError("invalid requires")
    if level > FORMAT:
        raise Unsupported(level, record)
    if not set(REQUIRED) <= set(record) or not isinstance(record["id"], str) or PREFIX + record["id"] != key:
        raise ValueError("a required field is missing or the ID does not match")
    rule_id(record["id"])
    text = record["text"]
    if not isinstance(text, str) or not text.strip() or "\x00" in text or len(text) > MAX_TEXT:
        raise ValueError("invalid rule text")
    if not all(_valid_version(record[field]) for field in ("created_at", "updated_at")):
        raise ValueError("invalid rule version")
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


def _valid_version(version):
    return isinstance(version, str) and 0 < len(version) <= MAX_VERSION and version.isprintable()


def _stored_version(raw):
    """Use a stored version if readable, otherwise tie removal to these exact bytes."""
    value = _text_of(raw)
    # A row larger than Richi writes is not parsed at all.
    if value is not None and len(value) <= MAX_STORED:
        try:
            version = _parsed(value)["updated_at"]
            if _valid_version(version):
                return version
        except (ValueError, TypeError, KeyError, RecursionError):
            pass
    return recovery_version(raw)


def _removal(identifier, version):
    """The command that removes a row by its key, whatever the key holds.

    The version follows its option after "=", so a version that begins with a
    dash is still read as one.
    """
    words = ["richi", "rules", "remove", shell_word(identifier), "--expected-updated-at=" + shell_word(version)]
    if identifier.startswith("-"):
        # Only after -- is a word that begins with a dash read as the ID.
        words = words[:3] + words[4:] + ["--", words[3]]
    return " ".join(words)


def _invalid(identifier, error, version, named=True):
    """How a row that cannot be read is reported: bounded, with the command that removes it when it can be named.

    named=False: a CLI argument cannot identify the row.
    """
    if len(identifier) > MAX_NAMED:
        return {"id": cut_name(identifier, MAX_NAMED), "updated_at": version,
                "error": error + "; the key is longer than %d characters, so it is not shown in full: remove the "
                                 "row with an SQLite tool" % MAX_NAMED}
    if not named:
        return {"id": identifier, "updated_at": version, "error": error + "; remove the row with an SQLite tool"}
    return {"id": identifier, "error": error, "updated_at": version, "next": _removal(identifier, version)}


def _unsupported(identifier, exc, version, text):
    """How a rule of a newer format is reported: bounded like an invalid one, with what it counts toward the limit."""
    return {"id": cut_name(identifier, MAX_NAMED), "requires": exc.level, "error": str(exc), "updated_at": version,
            "next": "richi update", "characters": len(text) if isinstance(text, str) else 0}


def _row(conn, identifier, removing, api):
    """(rowid, key or None, the stored value as bytes) of the rule with this ID, or None.

    A key that is not UTF-8 text is found only for removal, by the spelling
    invalid_rules gives it: such a row can be removed, never changed.
    """
    found = conn.execute("SELECT rowid, CAST(value AS BLOB) FROM metadata WHERE 1=1 AND key = ?",
                         (PREFIX + identifier,)).fetchone()
    exact = (found[0], PREFIX + identifier, found[1]) if found is not None else None
    if not removing or "\\x" not in identifier:
        return exact
    # Invalid UTF-8 is displayed as `\xNN`, which can also occur literally in a text key.
    # Refuse an ambiguous spelling even if it became ambiguous after the user read rules list.
    matches = conn.execute("SELECT rowid, CAST(key AS BLOB) FROM metadata WHERE key >= 'rule:' AND key < 'rule;'")
    raw_row = None
    for rowid, raw_key in matches:
        if _text_of(raw_key) is None and _shown(raw_key) == PREFIX + identifier:
            if exact is not None or raw_row is not None:
                raise api.MemoryError("More than one stored rule has this spelling; remove it with an SQLite tool")
            raw_row = rowid
    if raw_row is None:
        return exact
    value = conn.execute("SELECT CAST(value AS BLOB) FROM metadata WHERE rowid = ?", (raw_row,)).fetchone()[0]
    return raw_row, None, value


def _readable_row(key, raw_value):
    """The stored rule of a row read as bytes, or ValueError (Unsupported for a newer format)."""
    if key is None:
        raise ValueError("the key is not UTF-8 text")
    value = _text_of(raw_value)
    if value is None:
        raise ValueError("the stored value is not UTF-8 text")
    return _decode(key, value)


def scan(conn, fields=None):
    """(rules: global first, then by project and ID; rows that cannot be read; rules a newer Richi must serve)."""
    # A key range, not LIKE: LIKE would also match other spellings such as RULE:. ";" follows ":".
    # SQLite orders every BLOB after every text, so a key another program stored as bytes needs
    # a range of its own: x'72756c653a' is "rule:".
    rows = conn.execute("SELECT typeof(key), CAST(key AS BLOB), CAST(value AS BLOB) FROM metadata "
                        "WHERE 1=1 AND ((key >= 'rule:' AND key < 'rule;') "
                        "OR (key >= x'72756c653a' AND key < x'72756c653b')) ORDER BY key")
    rules, invalid, unsupported = [], [], []
    escaped_names = {}
    for kind, raw_key, raw_value in rows:
        key = _text_of(raw_key) if kind == "text" else None
        identifier = (key if key is not None else _shown(raw_key))[len(PREFIX):]
        if kind == "text" and "\\x" in identifier:
            escaped_names[identifier] = escaped_names.get(identifier, 0) + 1
        try:
            if kind != "text":
                raise ValueError("the key is stored as bytes, not as text")
            rule = _readable_row(key, raw_value)
            rules.append(rule if fields is None else {field: rule[field] for field in fields})
        except Unsupported as exc:
            # updated_at: what removing the rule takes, for a user who will not update.
            unsupported.append(_unsupported(identifier, exc, _stored_version(raw_value),
                                            exc.record.get("text")))
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            invalid.append(_invalid(identifier, str(exc) or type(exc).__name__, _stored_version(raw_value),
                                    named=kind == "text" and "\x00" not in identifier))
    for item in invalid:
        if "next" in item and escaped_names.get(item["id"], 0) > 1:
            del item["next"]
            item["error"] += "; more than one key has this spelling: remove the row with an SQLite tool"
    rules.sort(key=lambda rule: (rule["scope"] != "global", rule["project_id"] or "", rule["id"]))
    return rules, invalid, unsupported


def _left_out(result, invalid, unsupported):
    """Say which stored rules are not among the returned ones: the user set them, and agents do not get them."""
    if invalid:
        result["invalid_rules"] = invalid
    if unsupported:
        result["unsupported_rules"] = unsupported
    return result


def _project(conn, project_id, api):
    if not _storable(project_id) or conn.execute("SELECT 1 FROM projects WHERE 1=1 AND id = ?",
                                                 (project_id,)).fetchone() is None:
        raise api.NotFoundError("Unknown project in this workspace: " + _readable(project_id) + "; see project list")
    return project_id


def _check_version(identifier, stored, expected, api):
    if expected != stored:
        raise _failure(api, "Rule update conflict for {}: expected {} but the current version is {}; "
                       "read it again with rules list".format(identifier, expected, stored), "conflict")


def _characters(rules, unsupported, replaced=None):
    """The text agents load from the workspace, and a newer Richi's rules this version only counts."""
    return (sum(len(rule["text"]) for rule in rules if rule["id"] != replaced)
            + sum(item["characters"] for item in unsupported))


def _check_total(conn, api, replaced=None, added=""):
    rules, _invalid_rows, unsupported = scan(conn, ("id", "scope", "project_id", "text"))
    total = _characters(rules, unsupported, replaced) + len(added)
    # Only a change that makes the rules longer is refused: over the limit already (a newer Richi's
    # rules count too), a skill, a source or a shorter text can still be saved.
    if total > MAX_TOTAL and total > _characters(rules, unsupported):
        raise _failure(api, "Rules of one workspace may hold at most %d characters in all (would be %d); "
                       "shorten or remove rules first" % (MAX_TOTAL, total), "limit", "rules")


def _store(conn, rule, api):
    value = json.dumps(rule, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(value) > MAX_STORED:
        # It could not be read back: _decode refuses a record of this size.
        raise _failure(api, "The rule with all it stores would exceed %d characters" % MAX_STORED, "limit", "record")
    conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (PREFIX + rule["id"], value))


def _change_time(conn, api, previous=None):
    """Assign a version in the rule's write transaction, even after a clock adjustment or log clear."""
    row = conn.execute("SELECT CAST(value AS BLOB) FROM metadata WHERE key = 'rule_clock'").fetchone()
    # A previous runtime sharing this store writes log entries without advancing rule_clock.
    # A malformed key can sort ahead of every timestamp. Read just the fixed-width stamps of
    # this bounded log, so its valid versions still count when two runtimes share the store.
    log_times = conn.execute("SELECT CAST(substr(key, 10, 27) AS BLOB) FROM metadata "
                             "WHERE key >= 'rule-log:' AND key < 'rule-log;'")
    at = api.next_updated_at(_text_of(row[0]) if row else None, previous, *(_text_of(raw) for raw, in log_times))
    conn.execute("INSERT INTO metadata(key, value) VALUES ('rule_clock', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (at,))
    return at


def _record(conn, at, action, identifier, before, after, author):
    """Keep the change in the log, and only the latest MAX_LOG changes."""
    entry = {"at": at, "id": identifier, "action": action, "by": author.by, "agent": author.agent,
             "before": before, "after": after}
    conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?)",
                 (LOG_PREFIX + at + ":" + identifier, json.dumps(entry, ensure_ascii=False, sort_keys=True,
                                                               separators=(",", ":"))))
    conn.execute("DELETE FROM metadata WHERE 1=1 AND key >= 'rule-log:' AND key < 'rule-log;' AND key NOT IN "
                 "(SELECT key FROM metadata WHERE 1=1 AND key >= 'rule-log:' AND key < 'rule-log;' "
                 "ORDER BY key DESC LIMIT ?)", (MAX_LOG,))


def _log_text(raw):
    """A log row as text, or None when it is not text or larger than any entry Richi writes (two rules and more)."""
    value = _text_of(raw)
    return value if value is not None and len(value) <= 2 * MAX_STORED + 4 * MAX_SOURCE + 4000 else None


def log(conn, identifier=None, entries=20):
    """The latest changes, newest first; more says whether older ones are kept beyond them."""
    rows = conn.execute("SELECT CAST(value AS BLOB) FROM metadata "
                        "WHERE 1=1 AND key >= 'rule-log:' AND key < 'rule-log;' ORDER BY key DESC")
    found, more = [], False
    for (raw,) in rows:
        try:
            entry = _parsed(_log_text(raw))
        except (ValueError, TypeError, RecursionError):
            continue
        if isinstance(entry, dict) and (identifier is None or entry.get("id") == identifier):
            if len(found) == entries:
                more = True
                break
            found.append(entry)
    return {"view": "rules-log", "entries": found, "more": more, "kept": MAX_LOG}


def clear_log(conn, api, identifier=None):
    """Delete the recorded changes, all or one rule's: the log keeps rule texts, removed rules included."""
    where = " WHERE key >= 'rule-log:' AND key < 'rule-log;'"
    if identifier is None:
        doomed = conn.execute("SELECT rowid FROM metadata" + where).fetchall()
    else:
        doomed = []
        for rowid, raw in conn.execute("SELECT rowid, CAST(value AS BLOB) FROM metadata" + where):
            try:
                entry = _parsed(_log_text(raw))
            except (ValueError, TypeError, RecursionError):
                continue
            if isinstance(entry, dict) and entry.get("id") == identifier:
                doomed.append((rowid,))
    if doomed:
        # An older writer may have advanced only the log. Preserve its revision before removing
        # the last evidence of a previously used timestamp, in the same write transaction.
        _change_time(conn, api)
        conn.executemany("DELETE FROM metadata WHERE rowid = ?", doomed)
    return {"view": "rules-log", "results": [{"status": "cleared", "entries": len(doomed)}]}


# What stops working while Richi cannot use the agent settings, by why (agents.Settings.code).
_SETTINGS_EFFECT = ("Meanwhile Richi knows only its built-in agents, without the user's settings: the user's own agents "
                    "are not recognized from their shells (they still get their skills with rules show --agent ID), "
                    "cannot be named in new rules, and their skill folders are not read; skill sources the user "
                    "switched off are read again; the same skill for every agent is not merged into one.")
SETTINGS_NEXT = {
    "damaged": "Tell the user to fix or remove the file (see richi skill-sources list). " + _SETTINGS_EFFECT,
    "unreadable": "It is read again next time; tell the user if this lasts. " + _SETTINGS_EFFECT,
    "newer": "Tell the user to update Richi (richi update): a newer version wrote the file. " + _SETTINGS_EFFECT,
    # Not proof of damage: a newer Richi may read a larger file.
    "large": "Tell the user to update Richi (richi update) if a newer version wrote the file, or else to make it "
             "smaller (see richi skill-sources list). " + _SETTINGS_EFFECT,
    # Own agents whose settings this version cannot use (agents.missing_agents); a built-in agent's are not
    # reported here: it stays known, named and merged, only without what the user set for it.
    "agents": "Tell the user to update Richi, or to remove those settings (see richi skill-sources list). "
              "Meanwhile those agents are unknown here: they are not recognized from their shells (they still get "
              "their skills with rules show --agent ID), cannot be named in new rules, their skill folders are not "
              "read, and the same skill for every agent is not merged into one.",
}
# How many such agents one answer names: the settings file may hold any number of them.
NAMED_AGENTS = 5


def _settings_problem(result, settings=None):
    """Say when Richi cannot use the user's agent settings, all of them or some own agents' (agents.read_settings)."""
    settings = agents.read_settings() if settings is None else settings
    missing = agents.missing_agents(settings)
    if settings.problem is not None:
        error, code = settings.problem, settings.code
    elif missing:
        named = [cut_name(_readable(identifier), MAX_NAMED) for identifier in missing[:NAMED_AGENTS]]
        more = len(missing) - len(named)
        error, code = ("Settings of own agents " + ", ".join(named) + (" and %d more" % more if more else "")
                       + " cannot be used by this version: a newer Richi wrote them, they are damaged, or this "
                         "version uses fewer own agents; richi skill-sources list says which"), "agents"
    else:
        return result
    result["agent_settings_problem"] = {"error": error, "code": code, "next": SETTINGS_NEXT[code]}
    return result


def show(conn, api, workspace, project_id, agent=None, unmatched_path=None):
    rules, invalid, unsupported = scan(conn, SHOWN)
    settings = agents.read_settings()
    known = agents.describe(entries=settings.entries)
    agent = agent or agents.detect(entries=settings.entries)
    if project_id is not None:
        _project(conn, project_id, api)
    applicable = [rule for rule in rules if rule["scope"] == "global" or rule["project_id"] == project_id]
    if agent is not None:
        applicable = [dict(rule, use_skill=skill_for(rule, agent)) for rule in applicable]
        if any(rule["use_skill"] == skill_catalog.PREVIOUS_RICHI_SKILL for rule in applicable):
            registry = [item for item in agents.registry(entries=settings.entries) if item.id == agent]
            aliases = skill_catalog.catalog(registry=registry, rule_aliases_only=True)["rule_aliases"].get(agent, {})
            for rule in applicable:
                rule["use_skill"] = aliases.get(rule["use_skill"], rule["use_skill"])
    skill_note = SKILL_NOTE_AGENT if agent is not None else SKILL_NOTE_ANY.format(
        agents=", ".join(item["id"] + ": " + item["name"] for item in known))
    result = {"view": "rules", "workspace": workspace, "project": project_id, "agent": agent, "rules": applicable,
              "note": NOTE.format(project=" and project " + project_id if project_id else "", skills=skill_note)}
    if project_id is None:
        others = sorted({rule["project_id"] for rule in rules if rule["scope"] == "project"})
        if others:
            result["project_rules_not_shown"] = {
                "projects": others,
                "next": "richi --project-path PATH rules show (for work in one of these projects)"}
    if agent is not None and agent not in {item["id"] for item in known} and not any(
            agent in (rule["skills"] or {}) for rule in rules):
        # Most likely a misspelt ID: the caller would silently get another agent's skills, or none.
        result["agent_problem"] = {
            "error": "Unknown agent ID: " + agent + "; no agent here and no rule has it",
            "known": known,
            "next": "use_skill is each rule's skill for every agent. If you are one of the known agents, "
                    "run the command again with --agent and its ID."}
    if unmatched_path is not None:
        # The global rules above still bind the agent in a repository this workspace does not know.
        result["project_path_problem"] = {
            "error": unmatched_path,
            "next": "These are the global rules only. Ask the user whether this repository belongs to the "
                    "workspace (richi project add PATH registers it), or pass --project ID."}
    return _left_out(_settings_problem(result, settings), invalid, unsupported)


def _stdin_text():
    """Text piped to the command; the newline that ends the pipe is not part of the rule."""
    text = sys.stdin.read(MAX_CHANGE + 1)
    return text[:-2] if text.endswith("\r\n") else text[:-1] if text.endswith("\n") else text


def read_request(args, api):
    """The change object of an add, update, remove or apply command; None for any other.

    It may wait for standard input or a file, so it is read before the store
    is locked for writing: a command waiting for its text keeps nobody out.
    """
    if args.action not in {"add", "update", "remove", "apply"}:
        return None
    if args.action == "apply":
        try:
            if args.json == "-":
                data = sys.stdin.read(MAX_CHANGE + 1)
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
            request["text"] = _stdin_text() if args.text == "-" else args.text
        if args.skill is not None:
            request["skill"], request["skills"] = _skill_options(args.skill, api)
        if args.source is not None:
            request["source"] = args.source
    return request


def change(conn, request, api, checked_project=None, author=Author(), settings=None):
    """Add, update or remove one rule as the change object says.

    checked_project is the --project-path project; author is who makes the
    change (current_author), recorded with the rule and in the change log.
    """
    action = request.get("action") if isinstance(request, dict) else None
    # A string first: any other JSON value is not an action, and one that cannot be hashed is not a key.
    if not isinstance(action, str) or action not in CHANGE_FIELDS or set(request) - CHANGE_FIELDS[action]:
        raise api.MemoryError("A rule change is an object with action add, update or remove, id and that "
                              "action's fields only")
    identifier, expected = request.get("id"), request.get("expected_updated_at")
    # Read once: the agents the skills are checked and merged against, and what the answer says of them.
    settings = agents.read_settings() if settings is None else settings
    # Text that cannot be UTF-8 reaches neither the store nor a message.
    if not isinstance(identifier, str) or not _storable(identifier):
        raise api.MemoryError(ID_HELP)
    if expected is not None and (not isinstance(expected, str) or not _storable(expected)):
        raise api.MemoryError("expected_updated_at is the rule's updated_at as last read")
    row = _row(conn, identifier, action == "remove", api)
    # Only removal takes a key that is not an ID: such a row cannot be read and must not stay forever.
    if _NAME.fullmatch(identifier) is None and not (action == "remove" and row is not None):
        raise api.MemoryError(ID_HELP)
    if action == "add":
        if row is not None:
            raise _failure(api, "Rule " + identifier + " already exists; change it with rules update", "exists")
        scope, project = request.get("scope"), request.get("project")
        if not (scope == "global" and project is None) \
                and not (scope == "project" and isinstance(project, str) and _storable(project)):
            raise api.MemoryError("Choose scope global, or scope project with a project")
        if scope == "project" and checked_project is not None and project != checked_project:
            raise api.MemoryError("Rule for project " + project + " does not match the checked project "
                                  + checked_project)
        text = _text(request.get("text"), api)
        timestamp = _change_time(conn, api)
        skill, skills = _skill_setting(request.get("skill"), request.get("skills"), api, settings=settings)
        rule = {"id": identifier, "scope": scope,
                "project_id": _project(conn, project, api) if scope == "project" else None,
                "text": text, "skill": skill, "skills": skills, "source": _source(request.get("source"), api),
                "changed_by": author.by, "changed_by_agent": author.agent, "created_at": timestamp,
                "updated_at": timestamp}
        _check_total(conn, api, added=text)
        _store(conn, rule, api)
        _record(conn, timestamp, "created", identifier, None, rule, author)
        return _settings_problem({"results": [{"id": identifier, "status": "created"}], "rule": rule}, settings)
    if row is None:
        raise api.NotFoundError("Unknown rule: " + identifier + "; see rules list")
    rowid, key, raw = row
    try:
        rule = _readable_row(key, raw)
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        # A row this version cannot read or may not serve can still be removed, but not changed.
        version = _stored_version(raw)
        if action != "remove":
            problem = ("Rule " + identifier + " " + str(exc) + "; update Richi to change it"
                       if isinstance(exc, Unsupported) else "Invalid stored rule " + identifier + ": " + str(exc))
            raise api.MemoryError(problem + "; remove it with " + _removal(identifier, version)) from exc
        if checked_project is not None:
            raise api.MemoryError("Cannot verify the project of this stored rule. Remove it at workspace scope "
                                  "without --project-path.")
        _check_version(identifier, version, expected, api)
        conn.execute("DELETE FROM metadata WHERE 1=1 AND rowid = ?", (rowid,))
        # A newer format still has a bounded, readable JSON record. Preserve it for restoration
        # without interpreting it. Only actual damage needs a diagnostic excerpt.
        before = exc.record if isinstance(exc, Unsupported) else {"id": identifier, "unreadable": _shown(raw)[:MAX_TEXT * 2]}
        _record(conn, _change_time(conn, api, version), "removed", identifier, before, None, author)
        return {"results": [{"id": identifier, "status": "removed"}], "rule": None}
    if checked_project is not None and rule["scope"] == "project" and rule["project_id"] != checked_project:
        raise api.MemoryError("Rule " + rule["id"] + " belongs to project " + rule["project_id"]
                              + ", not the checked project " + checked_project)
    _check_version(identifier, rule["updated_at"], expected, api)
    if action == "remove":
        conn.execute("DELETE FROM metadata WHERE 1=1 AND rowid = ?", (rowid,))
        _record(conn, _change_time(conn, api, rule["updated_at"]), "removed", identifier, rule, None, author)
        return {"results": [{"id": identifier, "status": "removed"}], "rule": rule}
    if not {"text", "skill", "skills", "source"} & set(request):
        raise api.MemoryError("Nothing to change; give the text, the skills, the source or several")
    changed = dict(rule)
    if "text" in request:
        changed["text"] = _text(request["text"], api)
    if {"skill", "skills"} & set(request):
        # The skills of a rule are replaced together: what the change leaves out is removed.
        changed["skill"], changed["skills"] = _skill_setting(request.get("skill"), request.get("skills"), api,
                                                             kept=rule["skills"] or (), settings=settings)
    if "source" in request:
        changed["source"] = _source(request["source"], api)
    if changed == rule:
        return _settings_problem({"results": [{"id": identifier, "status": "unchanged"}], "rule": rule}, settings)
    _check_total(conn, api, replaced=identifier, added=changed["text"])
    changed.update(changed_by=author.by, changed_by_agent=author.agent,
                   updated_at=_change_time(conn, api, rule["updated_at"]))
    _store(conn, changed, api)
    _record(conn, changed["updated_at"], "updated", identifier, rule, changed, author)
    return _settings_problem({"results": [{"id": identifier, "status": "updated"}], "rule": changed}, settings)


def listing(conn, api, workspace=None, *, global_scope=False, project=None, fields=None):
    """List stored rules. A reader may select the fields it displays; CLI callers retain full records."""
    settings = agents.read_settings()
    rules, invalid, unsupported = scan(conn, fields)
    # The limit is the workspace's, so the size beside it is too, whatever the list shows.
    characters = _characters(rules, unsupported)
    if global_scope:
        rules = [rule for rule in rules if rule["scope"] == "global"]
    elif project is not None:
        _project(conn, project, api)
        rules = [rule for rule in rules if rule["project_id"] == project]
    projects = [{"id": row[0], "name": row[1]}
                for row in conn.execute("SELECT id, name FROM projects WHERE 1=1 ORDER BY id")]
    result = {"view": "rules", "workspace": workspace, "rules": rules,
              "agents": agents.describe(entries=settings.entries), "projects": projects,
              "characters": characters, "limit_characters": MAX_TOTAL}
    return _left_out(_settings_problem(result, settings), invalid, unsupported)


def run(conn, args, api, workspace=None, checked_project=None, unmatched_path=None, request=None):
    """Run a rules command; checked_project is the --project-path project ID.

    request is the change of an add, update, remove or apply, read beforehand
    with read_request: the caller locks the store for it, and no input may be
    awaited under the lock. A change that is JSON null stays None and is refused.
    """
    if args.action == "show":
        project_id = checked_project if args.project is None else args.project
        return show(conn, api, workspace, project_id, args.agent, unmatched_path)
    if args.action == "log":
        return log(conn, args.id, args.entries)
    if args.action == "clear-log":
        return clear_log(conn, api, args.id)
    if args.action == "list":
        return listing(conn, api, workspace, global_scope=args.global_scope, project=args.project)
    settings = agents.read_settings()
    return change(conn, request, api, checked_project, current_author(entries=settings.entries), settings)
