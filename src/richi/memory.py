#!/usr/bin/env python3
"""Local project memory. Python 3.9+, stdlib only; see README.md."""

import argparse
from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from . import graph
from . import compact


VERSION = 2
BASE = Path(__file__).resolve().parent
KINDS = {"task", "decision", "fact", "note", "investigation", "experiment", "procedure"}
STATES = {"planned", "in_progress", "implemented", "merged", "released", "blocked", "done"}
KNOWLEDGE = {"confirmed", "hypothesis", "superseded"}
PROJECT_FIELDS = ("id", "name", "repo_path", "description", "source", "verified_at")
ENTRY_FIELDS = ("id", "kind", "title", "summary", "jira_key", "work_state",
                "knowledge_state", "sources", "tags", "aliases", "created_at", "updated_at", "verified_at")
RELATION_FIELDS = ("id", "from_project", "to_project", "kind", "description", "source",
                   "knowledge_state", "verified_at")
FTS_SQL = """
CREATE VIRTUAL TABLE entries_fts USING fts5(
    id, title, summary, jira_key, tags, aliases, content='entries', content_rowid='rowid'
)
;
CREATE TRIGGER entries_fts_insert AFTER INSERT ON entries BEGIN
    INSERT INTO entries_fts(rowid, id, title, summary, jira_key, tags, aliases)
    VALUES (new.rowid, new.id, new.title, new.summary, new.jira_key, new.tags, new.aliases);
END
;
CREATE TRIGGER entries_fts_delete AFTER DELETE ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, id, title, summary, jira_key, tags, aliases)
    VALUES ('delete', old.rowid, old.id, old.title, old.summary, old.jira_key, old.tags, old.aliases);
END
;
CREATE TRIGGER entries_fts_update AFTER UPDATE ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, id, title, summary, jira_key, tags, aliases)
    VALUES ('delete', old.rowid, old.id, old.title, old.summary, old.jira_key, old.tags, old.aliases);
    INSERT INTO entries_fts(rowid, id, title, summary, jira_key, tags, aliases)
    VALUES (new.rowid, new.id, new.title, new.summary, new.jira_key, new.tags, new.aliases);
END
;
"""


class MemoryError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def string(value, field, nullable=False, empty=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or (not empty and not value.strip()) or "\x00" in value:
        raise MemoryError("{} must be {}string".format(field, "a nonempty " if not empty else "a "))
    return value


def identifier(value, field="id"):
    value = string(value, field)
    if len(value) > 200 or any(ord(ch) < 32 for ch in value):
        raise MemoryError("{} must be at most 200 characters without control characters".format(field))
    return value


def timestamp(value, field):
    if value is None:
        return None
    string(value, field)
    try:
        if len(value) == 10:
            date.fromisoformat(value)
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timezone required")
    except ValueError:
        raise MemoryError("{} must be an ISO date or ISO timestamp with timezone".format(field))
    return value


def enum(value, field, choices, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or value not in choices:
        raise MemoryError("{} must be one of: {}".format(field, ", ".join(sorted(choices))))
    return value


def fields(obj, allowed):
    unknown = set(obj) - set(allowed)
    if unknown:
        raise MemoryError("Unknown fields: " + ", ".join(sorted(unknown)))


def sources(value):
    if not isinstance(value, list) or len(value) > 100:
        raise MemoryError("sources must be an array of at most 100 objects")
    result = []
    for source in value:
        if not isinstance(source, dict):
            raise MemoryError("Each source must be an object with a reference")
        fields(source, {"reference", "label", "observed_at", "type", "revision", "locator", "sha256", "git"})
        item = {"reference": string(source.get("reference"), "source.reference")}
        for key in ("label", "type"):
            if key in source:
                item[key] = string(source[key], "source." + key)
        for key in ("revision", "locator"):
            if key in source:
                text = string(source[key], "source." + key)
                if len(text) > 1024 or any(ord(char) < 32 for char in text):
                    raise MemoryError("source.{} must be at most 1024 characters without control characters".format(key))
                item[key] = text
        if "sha256" in source:
            digest = string(source["sha256"], "source.sha256")
            if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                raise MemoryError("source.sha256 must contain exactly 64 hexadecimal characters")
            item["sha256"] = digest.lower()
        if "observed_at" in source:
            item["observed_at"] = timestamp(source["observed_at"], "source.observed_at")
        if "git" in source:
            from . import git_evidence
            item["git"] = source["git"]
            git_evidence.validate_anchor(item)
        result.append(item)
    return result


def string_list(value, field):
    if not isinstance(value, list) or len(value) > 100:
        raise MemoryError(field + " must be an array of at most 100 nonempty strings")
    return sorted(set(string(item, field) for item in value))


def payload(path):
    try:
        if path == "-":
            data = sys.stdin.read(10_000_001)
        else:
            with open(Path(path).expanduser(), encoding="utf-8") as handle:
                data = handle.read(10_000_001)
        if len(data) > 10_000_000:
            raise MemoryError("JSON input exceeds 10 million characters")
        value = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise MemoryError("Invalid JSON: " + str(exc))
    items = value if isinstance(value, list) else [value]
    if not items or len(items) > 1000 or not all(isinstance(x, dict) for x in items):
        raise MemoryError("JSON input must be an object or an array of 1–1000 objects")
    ids = [identifier(x.get("id")) for x in items]
    if len(ids) != len(set(ids)):
        raise MemoryError("Duplicate IDs in one batch are not allowed")
    return items


@contextmanager
def transaction(conn, write=True):
    conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def execute_script(conn, script):
    # Unlike executescript(), this preserves our explicit atomic transaction.
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""
    if statement.strip():
        raise MemoryError("Incomplete schema statement")


def connect(path, create=False, readonly=False):
    if create and readonly:
        raise MemoryError("Cannot create a read-only database")
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    else:
        if not path.is_file():
            raise MemoryError("Database does not exist; run init first: " + str(path))
        mode = "ro" if readonly else "rw"
        conn = sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    if readonly:
        conn.execute("PRAGMA query_only = ON")
    return conn


def backend(conn):
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version not in {1, VERSION}:
        raise MemoryError("Unsupported schema version {}; this CLI supports 1 and {}. No migration was attempted.".format(version, VERSION))
    row = conn.execute("SELECT value FROM metadata WHERE 1=1 AND key = 'search_backend'").fetchone()
    if row is None or row[0] not in {"fts5", "scan"}:
        raise MemoryError("Missing or invalid search_backend metadata")
    return row[0]


def initialize(conn, path):
    # Validate before changing even the journal mode of an existing database.
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 1:
        raise MemoryError("Existing schema 1 requires an explicit migrate command; make a backup first")
    if version not in {0, VERSION}:
        backend(conn)
    if version == 0 and conn.execute(
            "SELECT name FROM sqlite_master WHERE 1=1 AND name NOT LIKE 'sqlite_%'").fetchone():
        raise MemoryError("Refusing to initialize a nonempty database with an unknown schema")
    if version == VERSION:
        backend(conn)
    conn.execute("PRAGMA journal_mode = WAL")
    with transaction(conn):
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == VERSION:
            return {"database": str(path), "schema_version": VERSION, "search_backend": backend(conn), "status": "existing"}
        if version != 0:
            backend(conn)
        existing = conn.execute("SELECT name FROM sqlite_master WHERE 1=1 AND name NOT LIKE 'sqlite_%'").fetchall()
        if existing:
            raise MemoryError("Refusing to initialize a nonempty database with an unknown schema")
        execute_script(conn, (BASE / "schema.sql").read_text(encoding="utf-8"))
        execute_script(conn, (BASE / "graph_schema.sql").read_text(encoding="utf-8"))
        conn.execute("SAVEPOINT optional_fts")
        try:
            execute_script(conn, FTS_SQL)
            search_backend = "fts5"
        except sqlite3.OperationalError as exc:
            if "no such module: fts5" not in str(exc).lower():
                raise
            conn.execute("ROLLBACK TO optional_fts")
            search_backend = "scan"
        conn.execute("RELEASE optional_fts")
        conn.execute("INSERT INTO metadata(key, value) VALUES ('search_backend', ?)", (search_backend,))
        conn.execute("PRAGMA user_version = 2")
    return {"database": str(path), "schema_version": VERSION, "search_backend": search_backend, "status": "created"}


def upsert(conn, table, columns, record):
    # Table and column names come exclusively from constants in this module.
    placeholders = ", ".join("?" for _ in columns)
    assignments = ", ".join("{0} = excluded.{0}".format(key) for key in columns if key != "id")
    conn.execute("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT(id) DO UPDATE SET {}".format(
        table, ", ".join(columns), placeholders, assignments), [record[key] for key in columns])


def project_put(conn, obj):
    fields(obj, PROJECT_FIELDS)
    record = {"id": identifier(obj.get("id")), "name": string(obj.get("name"), "name"),
              "repo_path": string(obj.get("repo_path"), "repo_path", nullable=True),
              "description": string(obj.get("description", ""), "description", empty=True),
              "source": string(obj.get("source"), "source", nullable=True),
              "verified_at": timestamp(obj.get("verified_at"), "verified_at")}
    old = conn.execute("SELECT * FROM projects WHERE 1=1 AND id = ?", (record["id"],)).fetchone()
    status = "created" if old is None else "unchanged" if dict(old) == record else "updated"
    if status != "unchanged":
        upsert(conn, "projects", PROJECT_FIELDS, record)
    return {"id": record["id"], "status": status}


def require_project(conn, project_id):
    row = conn.execute("SELECT * FROM projects WHERE 1=1 AND id = ?", (project_id,)).fetchone()
    if row is None:
        raise MemoryError("Unknown project: " + project_id)
    return dict(row)


def entry_record(conn, entry_id):
    row = conn.execute("SELECT * FROM entries WHERE 1=1 AND id = ?", (entry_id,)).fetchone()
    if row is None:
        return None
    record = dict(row)
    for key in ("sources", "tags", "aliases"):
        record[key] = json.loads(record[key])
    record["project_ids"] = [x[0] for x in conn.execute(
        "SELECT project_id FROM entry_projects WHERE 1=1 AND entry_id = ? ORDER BY project_id", (entry_id,))]
    return record


def entry_put(conn, obj):
    allowed = set(ENTRY_FIELDS) - {"created_at", "updated_at"} | {"project_ids", "event_id", "expected_updated_at"}
    fields(obj, allowed)
    record = {"id": identifier(obj.get("id")), "kind": enum(obj.get("kind"), "kind", KINDS),
              "title": string(obj.get("title"), "title"), "summary": string(obj.get("summary"), "summary"),
              "jira_key": string(obj.get("jira_key"), "jira_key", nullable=True),
              "work_state": enum(obj.get("work_state"), "work_state", STATES, nullable=True),
              "knowledge_state": enum(obj.get("knowledge_state", "hypothesis"), "knowledge_state", KNOWLEDGE),
              "sources": sources(obj.get("sources", [])),
              "tags": string_list(obj.get("tags", []), "tags"),
              "aliases": string_list(obj.get("aliases", []), "aliases"),
              "verified_at": timestamp(obj.get("verified_at"), "verified_at")}
    projects = obj.get("project_ids", [])
    if not isinstance(projects, list) or len(projects) > 100:
        raise MemoryError("project_ids must be an array of at most 100 IDs")
    record["project_ids"] = sorted(set(identifier(x, "project_ids") for x in projects))
    if record["knowledge_state"] == "confirmed" and record["kind"] in {"fact", "decision"} and not record["sources"]:
        raise MemoryError("Confirmed facts and decisions require at least one source")
    event_id = identifier(obj["event_id"], "event_id") if "event_id" in obj else None
    expected_updated_at = timestamp(obj.get("expected_updated_at"), "expected_updated_at")
    digest = hashlib.sha256(dumps(record).encode("utf-8")).hexdigest()
    if event_id:
        event = conn.execute("SELECT * FROM entry_events WHERE 1=1 AND event_id = ?", (event_id,)).fetchone()
        if event:
            if event["entry_id"] != record["id"] or event["payload_hash"] != digest:
                raise MemoryError("event_id already used with a different payload: " + event_id)
            return {"id": record["id"], "status": "replayed"}
    for project_id in record["project_ids"]:
        require_project(conn, project_id)
    old = entry_record(conn, record["id"])
    comparable = {k: v for k, v in old.items() if k not in {"created_at", "updated_at"}} if old else None
    status = "created" if old is None else "unchanged" if comparable == record else "updated"
    if status != "unchanged" and "expected_updated_at" in obj:
        actual_updated_at = old["updated_at"] if old else None
        if expected_updated_at != actual_updated_at:
            raise MemoryError("Entry update conflict for {}: expected_updated_at does not match the current record; read it again before updating".format(record["id"]))
    if status != "unchanged":
        changed_at = now()
        record["created_at"] = old["created_at"] if old else changed_at
        record["updated_at"] = changed_at
        stored = dict(record)
        for key in ("sources", "tags", "aliases"):
            stored[key] = dumps(record[key])
        upsert(conn, "entries", ENTRY_FIELDS, stored)
        conn.execute("DELETE FROM entry_projects WHERE 1=1 AND entry_id = ?", (record["id"],))
        conn.executemany("INSERT INTO entry_projects(entry_id, project_id) VALUES (?, ?)",
                         [(record["id"], x) for x in record["project_ids"]])
        conn.execute("INSERT INTO entry_history(entry_id, changed_at, before_json, after_json, event_id) VALUES (?, ?, ?, ?, ?)",
                     (record["id"], changed_at, dumps(old) if old else None, dumps(record), event_id))
    if event_id:
        conn.execute("INSERT INTO entry_events(event_id, entry_id, payload_hash) VALUES (?, ?, ?)",
                     (event_id, record["id"], digest))
    return {"id": record["id"], "status": status}


def relation_put(conn, obj):
    fields(obj, RELATION_FIELDS)
    record = {"id": identifier(obj.get("id")),
              "from_project": identifier(obj.get("from_project"), "from_project"),
              "to_project": identifier(obj.get("to_project"), "to_project"),
              "kind": string(obj.get("kind"), "kind"),
              "description": string(obj.get("description", ""), "description", empty=True),
              "source": string(obj.get("source"), "source", nullable=True),
              "knowledge_state": enum(obj.get("knowledge_state", "hypothesis"), "knowledge_state", KNOWLEDGE),
              "verified_at": timestamp(obj.get("verified_at"), "verified_at")}
    if record["knowledge_state"] == "confirmed" and not record["source"]:
        raise MemoryError("Confirmed relations require a source")
    if record["from_project"] == record["to_project"]:
        raise MemoryError("A relation must connect two different projects")
    require_project(conn, record["from_project"])
    require_project(conn, record["to_project"])
    old = conn.execute("SELECT * FROM relations WHERE 1=1 AND id = ?", (record["id"],)).fetchone()
    status = "created" if old is None else "unchanged" if dict(old) == record else "updated"
    if status != "unchanged":
        upsert(conn, "relations", RELATION_FIELDS, record)
    return {"id": record["id"], "status": status}


def entry_get(conn, entry_id, history=False):
    record = entry_record(conn, entry_id)
    if record is None:
        raise MemoryError("Unknown entry: " + entry_id)
    if not history:
        return record
    record["history"] = []
    for row in conn.execute("SELECT * FROM entry_history WHERE 1=1 AND entry_id = ? ORDER BY id", (entry_id,)):
        item = dict(row)
        before_json = item.pop("before_json")
        item["before"] = json.loads(before_json) if before_json else None
        item["after"] = json.loads(item.pop("after_json"))
        record["history"].append(item)
    return record


def search(conn, args, search_backend):
    query = string(args.query, "query")
    if len(query) > 2000:
        raise MemoryError("Search query must be at most 2000 characters")
    tokens = re.findall(r"[^\W_]+", query, flags=re.UNICODE)
    if len(tokens) > 64:
        raise MemoryError("Search query must contain at most 64 words")
    if args.project:
        require_project(conn, args.project)
    if not tokens:
        return {"entries": [], "search_backend": search_backend, "limit": args.limit}
    parameters = []
    where = " WHERE 1=1"
    if not args.include_superseded:
        where += " AND e.knowledge_state <> 'superseded'"
    if args.project:
        where += " AND EXISTS (SELECT 1 FROM entry_projects ep WHERE 1=1 AND ep.entry_id = e.id AND ep.project_id = ?)"
        parameters.append(args.project)
    if search_backend == "fts5":
        sql = "SELECT e.id FROM entries e JOIN entries_fts ON entries_fts.rowid = e.rowid" + where
        sql += " AND entries_fts MATCH ? ORDER BY bm25(entries_fts), e.updated_at DESC, e.id LIMIT ?"
        parameters.append(" OR ".join('"' + word.replace('"', '""') + '"*' for word in tokens))
    else:
        # Python casefold also handles Cyrillic, unlike SQLite's default LOWER/LIKE.
        conn.create_function("casefold", 1, lambda value: str(value or "").casefold())
        sql = "SELECT e.id FROM entries e" + where
        conditions = []
        for token in tokens:
            conditions.append("instr(casefold(e.id || ' ' || e.title || ' ' || e.summary || ' ' || coalesce(e.jira_key, '') || ' ' || e.tags || ' ' || e.aliases), ?) > 0")
            parameters.append(token.casefold())
        sql += " AND (" + " OR ".join(conditions) + ")"
        sql += " ORDER BY e.updated_at DESC, e.id LIMIT ?"
    parameters.append(args.limit)
    ids = [row[0] for row in conn.execute(sql, parameters)]
    return {"entries": [entry_record(conn, entry_id) for entry_id in ids], "search_backend": search_backend,
            "match_mode": "any_word_prefix" if search_backend == "fts5" else "any_word_substring", "limit": args.limit}


def context(conn, args):
    project = require_project(conn, args.project)
    relation_sql = "SELECT * FROM relations WHERE 1=1 AND (from_project = ? OR to_project = ?)"
    if not args.include_superseded:
        relation_sql += " AND knowledge_state <> 'superseded'"
    relations = [dict(row) for row in conn.execute(relation_sql + " ORDER BY id", (args.project, args.project))]
    neighbors = sorted({r["to_project"] if r["from_project"] == args.project else r["from_project"] for r in relations})
    ids = [args.project] + neighbors
    sql = "SELECT e.id FROM entries e WHERE 1=1"
    if not args.include_superseded:
        sql += " AND e.knowledge_state <> 'superseded'"
    sql += " AND EXISTS (SELECT 1 FROM entry_projects ep WHERE 1=1 AND ep.entry_id = e.id AND ep.project_id IN ({}))".format(
        ", ".join("?" for _ in ids))
    sql += " ORDER BY EXISTS (SELECT 1 FROM entry_projects ep WHERE 1=1 AND ep.entry_id = e.id AND ep.project_id = ?) DESC, e.updated_at DESC, e.id LIMIT ?"
    rows = conn.execute(sql, ids + [args.project, args.limit]).fetchall()
    return {"project": project, "relations": relations, "related_projects": [require_project(conn, x) for x in neighbors],
            "entries": [entry_record(conn, row[0]) for row in rows], "limit": args.limit,
            "selection": "Entries linked to the project first, then direct neighbors; newest updated first within each group."}


def backup(conn, database, output):
    destination = Path(output).expanduser().resolve() if output else database.parent / "backups" / (database.stem + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".sqlite3")
    if destination in {database, Path(str(database) + "-wal"), Path(str(database) + "-shm")}:
        raise MemoryError("Backup destination must differ from the active database and sidecar files")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(destination), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        target = sqlite3.connect(str(destination))
        try:
            conn.backup(target)
            integrity = target.execute("PRAGMA integrity_check").fetchall()
            if integrity != [("ok",)]:
                raise MemoryError("Backup integrity check failed")
        finally:
            target.close()
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return {"backup": str(destination), "status": "created"}


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)

    def error(self, message):
        raise MemoryError(message)


def limit(value):
    number = int(value)
    if not 1 <= number <= 200:
        raise argparse.ArgumentTypeError("limit must be between 1 and 200")
    return number


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return number


def parser():
    cli = Parser(description=__doc__)
    cli.add_argument("--db", help="SQLite path (overrides environment and config; place before command)")
    cli.add_argument("--config", help="JSON configuration file (place before command)")
    from . import __version__
    cli.add_argument("--version", action="version", version="Richi " + __version__)
    commands = cli.add_subparsers(dest="command", required=True)
    configuration = commands.add_parser("config", help="Inspect effective settings without opening a database")
    config_commands = configuration.add_subparsers(dest="action", required=True)
    config_commands.add_parser("show")
    config_set = config_commands.add_parser("set", help="Set dev or development.source")
    config_set.add_argument("key", choices=("dev", "development.source"))
    config_set.add_argument("value")
    map_cmd = commands.add_parser("map", help="Start/reuse the local map; use serve for a foreground server")
    map_cmd.add_argument("action", nargs="?", choices=("serve",))
    map_cmd.add_argument("--db", default=argparse.SUPPRESS, help="SQLite path")
    map_cmd.add_argument("--config", default=argparse.SUPPRESS, help="JSON configuration file")
    map_cmd.add_argument("--port", help="Loopback HTTP port")
    map_cmd.add_argument("--no-open", action="store_true", help="Launch without opening a browser")
    map_cmd.add_argument("--open", action="store_true", help="Open the foreground server in a browser (serve only)")
    commands.add_parser("init")
    for name, action in (("project", "upsert"), ("entry", "put"), ("relation", "put")):
        group = commands.add_parser(name).add_subparsers(dest="action", required=True)
        group.add_parser(action).add_argument("--json", required=True, help="UTF-8 JSON file, or - for stdin")
        if name == "project":
            group.add_parser("list")
        elif name == "entry":
            read = group.add_parser("get")
            read.add_argument("id")
            read.add_argument("--history", action="store_true", help="Include full revision history")
            short = group.add_parser("read")
            short.add_argument("id")
            short.add_argument("--max-chars", type=compact.max_chars, default=6000)
    query = commands.add_parser("search")
    query.add_argument("query")
    query.add_argument("--project")
    ctx = commands.add_parser("context")
    ctx.add_argument("--project", required=True)
    for command in (query, ctx):
        command.add_argument("--limit", type=limit, default=20)
        command.add_argument("--include-superseded", action="store_true")
    commands.add_parser("backup").add_argument("--output")
    commands.add_parser("check")
    recall_cmd = commands.add_parser("recall", help="Bounded question retrieval with explicit graph evidence")
    recall_cmd.add_argument("query")
    recall_cmd.add_argument("--project")
    recall_cmd.add_argument("--limit", type=limit, default=8)
    recall_cmd.add_argument("--max-chars", type=compact.max_chars, default=16000)
    recall_cmd.add_argument("--include-hypotheses", action="store_true")
    recall_cmd.add_argument("--include-superseded", action="store_true")
    recall_cmd.add_argument("--explain", action="store_true", help="Explain the actual retrieval stages within the output budget")
    recall_cmd.add_argument("--expect", help="Diagnose one expected entry:<id> or entity:<id>; implies --explain")
    brief_cmd = commands.add_parser("brief", help="Task context from a question, optional Git changes and explicit evidence links")
    brief_cmd.add_argument("query")
    brief_cmd.add_argument("--project", help="Recall project boost; scopes optional Git source checks and identifies a diff repository")
    brief_cmd.add_argument("--base", help="Explicit endpoint diff base; without base or --check-sources no Git operations run")
    brief_mode = brief_cmd.add_mutually_exclusive_group()
    brief_mode.add_argument("--target", help="Local target ref with --base or --check-sources (default: HEAD)")
    brief_mode.add_argument("--worktree", action="store_true")
    brief_cmd.add_argument("--repo", help="Linked worktree; requires --project and --base or --check-sources")
    brief_cmd.add_argument("--check-sources", action="store_true", help="Compare selected cards' Git evidence without verifying claims or updating memory")
    brief_cmd.add_argument("--check-limit", type=positive_int, help="Maximum source check attempts, 1-50 (default: 12); requires --check-sources")
    brief_cmd.add_argument("--limit", type=limit, default=8)
    brief_cmd.add_argument("--max-chars", type=compact.max_chars, default=16000)
    brief_cmd.add_argument("--include-hypotheses", action="store_true")
    brief_cmd.add_argument("--include-superseded", action="store_true")
    punisher_cmd = commands.add_parser("punisher", help="Read-only health and manual maintenance candidates")
    punisher_cmd.add_argument("--project", help="Inspect strict membership/scope, not the connected component")
    punisher_cmd.add_argument("--limit", type=limit, default=20)
    punisher_cmd.add_argument("--max-chars", type=compact.max_chars, default=16000)
    punisher_cmd.add_argument("--stale-days", type=positive_int, default=180)
    punisher_cmd.add_argument("--as-of", help="ISO date for reproducible age checks (default: today in UTC)")
    source_cmd = commands.add_parser("sources", help="Explicit snapshots and drift checks; no network access")
    source_actions = source_cmd.add_subparsers(dest="action", required=True)
    source_snapshot = source_actions.add_parser("snapshot", help="Save a new source/revision manifest without changing knowledge")
    source_snapshot.add_argument("--ref", dest="refs", action="append", help="Exact project:<id>, entry:<id> or entity:<id>; repeatable")
    source_snapshot.add_argument("--project", help="Snapshot the project's strict scope")
    source_snapshot.add_argument("--output", required=True, help="NEW manifest JSON file; existing files are never overwritten")
    source_check = source_actions.add_parser("check", help="Compare a manifest with current records, edges and local source files")
    source_check.add_argument("--manifest", required=True)
    source_check.add_argument("--max-chars", type=compact.max_chars, default=16000)
    capture = source_actions.add_parser("capture", help="Capture a Git-backed source; default: observed working-copy bytes")
    capture.add_argument("--project", required=True, help="Registered repository project ID")
    capture.add_argument("--path", required=True, help="Repository-relative file path")
    capture_mode = capture.add_mutually_exclusive_group()
    capture_mode.add_argument("--rev", help="Capture committed bytes at this revision instead of the working copy")
    capture_mode.add_argument("--worktree", action="store_true", help="Capture working-copy bytes (default)")
    capture.add_argument("--repo", help="Optional linked worktree of the registered repository")
    capture.add_argument("--output", help="NEW source JSON file for attach/show; never overwritten")
    capture.add_argument("--max-chars", type=compact.max_chars, default=16000)
    attach = source_actions.add_parser("attach", help="Append a captured source using optimistic concurrency and existing history")
    attach.add_argument("--ref", required=True, help="entry:<id>, entity:<id>, or edge:<id>")
    attach.add_argument("--json", dest="json_file", required=True, help="Captured source JSON file")
    attach.add_argument("--expected-updated-at", required=True)
    attach.add_argument("--max-chars", type=compact.max_chars, default=16000)
    git_check = source_actions.add_parser("git-check", help="Compare attached Git evidence with a chosen local revision or working copy")
    git_scope = git_check.add_mutually_exclusive_group(required=True)
    git_scope.add_argument("--ref", dest="refs", action="append", help="Record reference; repeatable")
    git_scope.add_argument("--project", help="Repository project ID referenced by source.git.repo_id")
    git_check_mode = git_check.add_mutually_exclusive_group()
    git_check_mode.add_argument("--target", default="HEAD", help="Local target ref (default: HEAD); never fetches")
    git_check_mode.add_argument("--worktree", action="store_true", help="Compare with current working-copy bytes")
    git_check.add_argument("--repo", help="Optional linked worktree of the registered repository")
    git_check.add_argument("--source", type=positive_int, help="Check one source in a single --ref, including legacy Git references")
    git_check.add_argument("--path", help="Changed file to check from a bare legacy commit; requires --source and one --ref")
    git_check.add_argument("--max-chars", type=compact.max_chars, default=16000)
    for action in ("show", "history", "diff", "commit"):
        operation = source_actions.add_parser(action, help="Read Git evidence " + action + " without checkout")
        selection = operation.add_mutually_exclusive_group(required=True)
        selection.add_argument("--ref", help="Entry/entity/edge reference containing the source")
        if action != "commit":
            selection.add_argument("--json", dest="json_file", help="Captured source JSON file")
        operation.add_argument("--source", type=positive_int, default=1, help="1-based index in the full record's sources array")
        operation.add_argument("--repo", help="Optional linked worktree of the registered repository")
        operation.add_argument("--max-chars", type=compact.max_chars, default=16000)
        if action != "commit":
            operation.add_argument("--path", help="Select an available changed file from a bare legacy git:FULL_SHA source")
        if action == "history":
            operation.add_argument("--limit", type=limit, default=20)
            operation.add_argument("--target", help="History ending at this local ref (default: captured commit)")
        elif action == "diff":
            comparison = operation.add_mutually_exclusive_group()
            comparison.add_argument("--target", default="HEAD", help="Local target ref (default: HEAD)")
            comparison.add_argument("--worktree", action="store_true")
    related = source_actions.add_parser("related", help="Find knowledge attached to a repository path and optional commit")
    related.add_argument("--project", required=True, help="Repository project ID")
    related.add_argument("--path", required=True, help="Exact repository-relative path")
    related.add_argument("--commit", help="Exact full commit object ID")
    related.add_argument("--limit", type=limit, default=20)
    related.add_argument("--max-chars", type=compact.max_chars, default=16000)
    impact = source_actions.add_parser("impact", help="Find recorded knowledge for changed files; direct source associations, not semantic impact")
    impact.add_argument("--project", required=True, help="Registered repository project ID")
    impact.add_argument("--repo", help="Optional linked worktree of the registered repository")
    impact.add_argument("--base", required=True, help="Local base ref; compares exact endpoints, not an inferred merge base")
    impact_mode = impact.add_mutually_exclusive_group()
    impact_mode.add_argument("--target", default="HEAD", help="Local target ref (default: HEAD)")
    impact_mode.add_argument("--worktree", action="store_true", help="Compare base with working-copy files, including non-ignored untracked files")
    impact.add_argument("--limit", type=limit, default=12)
    impact.add_argument("--max-chars", type=compact.max_chars, default=16000)
    graph.add_commands(commands, sys.modules[__name__])
    return cli


def run(args):
    from richi_launcher.config import resolve_settings
    settings = resolve_settings(db=args.db, config_file=getattr(args, "config", None))
    if args.command == "config":
        raise MemoryError("Use the installed richi config command")
    database = settings.database
    new_file = not database.exists()
    source_attach = args.command == "sources" and getattr(args, "action", None) == "attach"
    writing = args.command in {"init", "migrate"} or getattr(args, "action", None) in {"put", "upsert"} or source_attach
    conn = connect(database, create=args.command == "init", readonly=not writing)
    try:
        if args.command == "init":
            if new_file:
                os.chmod(database, 0o600)
            return initialize(conn, database)
        search_backend = backend(conn)
        if args.command == "migrate":
            return graph.migrate(conn, database, sys.modules[__name__])
        if args.command == "backup":
            return backup(conn, database, args.output)
        writing = getattr(args, "action", None) in {"put", "upsert"}
        items = payload(args.json) if writing else None
        with transaction(conn, write=writing or source_attach):
            if args.command == "brief":
                from . import task_brief
                return task_brief.brief(conn, args, sys.modules[__name__], database)
            if args.command == "sources" and args.action == "impact":
                from . import git_impact
                return git_impact.impact(conn, args, sys.modules[__name__], database)
            if args.command == "sources" and args.action not in {"snapshot", "check"}:
                from . import git_sources
                return git_sources.run(conn, args, sys.modules[__name__], database)
            if args.command in {"entity", "edge", "graph"}:
                graph.require_v2(conn, sys.modules[__name__])
                if writing:
                    return {"results": [graph.put(conn, item, args.command, sys.modules[__name__]) for item in items]}
                if args.command == "graph":
                    return graph.export(conn, args, sys.modules[__name__])
                return graph.get(conn, args.command, args.id, sys.modules[__name__], history=getattr(args, "history", False))
            if writing:
                put = {"project": project_put, "entry": entry_put, "relation": relation_put}[args.command]
                return {"results": [put(conn, item) for item in items]}
            if args.command == "project":
                return {"projects": [dict(row) for row in conn.execute("SELECT * FROM projects ORDER BY id")]}
            if args.command == "entry":
                record = entry_get(conn, args.id, history=getattr(args, "history", False))
                if args.action == "read":
                    return compact.fit_response({"entry": compact.card(record, "entry:" + args.id)}, args.max_chars)
                return record
            if args.command == "recall":
                from . import recall
                return recall.recall(conn, args, sys.modules[__name__])
            if args.command == "punisher":
                from . import punisher
                return punisher.punisher(conn, args, sys.modules[__name__])
            if args.command == "sources":
                from . import source_watch
                if args.action == "snapshot":
                    return source_watch.snapshot(conn, db_path=database, refs=args.refs, project=args.project, output=args.output)
                return source_watch.check(conn, db_path=database, manifest=args.manifest, max_chars=args.max_chars)
            if args.command == "search":
                return search(conn, args, search_backend)
            if args.command == "context":
                return context(conn, args)
            if args.command == "check":
                integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
                foreign_keys = [list(row) for row in conn.execute("PRAGMA foreign_key_check")]
                if integrity != ["ok"] or foreign_keys:
                    raise MemoryError("Database check failed: " + dumps({"integrity": integrity, "foreign_keys": foreign_keys}))
                return {"status": "ok", "schema_version": conn.execute("PRAGMA user_version").fetchone()[0], "search_backend": search_backend,
                        "integrity": integrity, "foreign_key_violations": foreign_keys}
        raise MemoryError("Unknown command")
    finally:
        conn.close()


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        if args.command == "map":
            from .cli import run_map
            return run_map(args)
        result = run(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (MemoryError, sqlite3.Error, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
