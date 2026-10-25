"""Read-only workspace accounting and explicit, lossless index maintenance."""
import os
from pathlib import Path
import sqlite3
import stat
import time
from contextlib import contextmanager

from .compact import max_chars
from .output import rendered

BASE_TABLES = ("projects", "entries", "entry_projects", "relations", "entry_history", "entry_events")
RESEARCH_TABLES = ("entry_research", "research_revisions", "research_events", "research_event_tracks", "research_requests")
GRAPH_TABLES = ("entities", "graph_nodes", "graph_edges", "entity_history", "edge_history")
LOGICAL_COLUMNS = {
    "entry_research": ("payload",),
    "research_events": ("object_ref", "event_kind", "recorded_at"),
    "research_requests": ("request_id", "payload_hash", "response"),
    "entries": ("title", "summary", "sources", "tags", "aliases"),
    "entities": ("title", "summary", "sources", "tags", "aliases"),
    "graph_edges": ("description", "sources"),
    "entry_history": ("before_json", "after_json"),
    "entity_history": ("before_json", "after_json"),
    "edge_history": ("before_json", "after_json"),
}
FILE_AREAS = ("backups", "artifacts/git", "evaluations", "runtime")
SQL_SECONDS, FILE_SECONDS, FILE_ENTRIES = 5.0, 2.0, 10000
MAX_DIRECTORY_DEPTH, ANALYSIS_LIMIT = 32, 400


def _scope(args, database):
    return {"kind": "workspace_database", "workspace": getattr(args, "storage_workspace", None),
            "database": str(database)}


def _bound(value, maximum, api):
    if type(maximum) is not int or not 2000 <= maximum <= 100000:
        raise api.MemoryError("max_chars must be between 2000 and 100000")
    value["budget"] = {"max_chars": maximum, "output_chars": 0, "omitted_sections": []}
    def measure():
        for _ in range(8):
            size = len(rendered(value))
            if value["budget"]["output_chars"] == size:
                return size
            value["budget"]["output_chars"] = size
        return len(rendered(value))
    for field in ("deep", "files", "counts", "notes"):
        if measure() <= maximum:
            return value
        if field in value:
            value.pop(field)
            value["budget"]["omitted_sections"].append(field)
            value["coverage"]["output_complete"] = False
    if measure() > maximum:
        raise api.MemoryError("Storage report metadata exceeds max_chars; increase the budget")
    return value


@contextmanager
def _deadline(conn, seconds):
    until = time.monotonic() + seconds
    conn.set_progress_handler(lambda: int(time.monotonic() >= until), 1000)
    try:
        yield
    finally:
        conn.set_progress_handler(None, 0)


def _size(path):
    try:
        item = path.lstat()
    except FileNotFoundError:
        return {"status": "absent", "bytes": 0}
    except OSError:
        return {"status": "unavailable", "bytes": None}
    if not stat.S_ISREG(item.st_mode):
        return {"status": "skipped_nonregular", "bytes": None}
    return {"status": "present", "bytes": item.st_size}


def _inventory(database):
    """Use anchored directory descriptors, refusing symlinks at every level."""
    remaining, deadline = [FILE_ENTRIES], time.monotonic() + FILE_SECONDS
    reports, flags = {}, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        root = os.open(str(database.parent), flags)
    except OSError:
        return {"areas": {}, "complete": False, "reason": "workspace_directory_unavailable"}
    def walk(descriptor, report, depth):
        with os.scandir(descriptor) as iterator:
            for item in iterator:
                if remaining[0] <= 0 or time.monotonic() >= deadline:
                    report.update(complete=False, reason="scan_budget")
                    return
                remaining[0] -= 1
                report["entries_examined"] += 1
                try:
                    info = item.stat(follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode):
                        report["skipped_links"] += 1
                        report["complete"] = False
                    elif stat.S_ISREG(info.st_mode):
                        report["file_count"] += 1
                        report["bytes"] += info.st_size
                    elif stat.S_ISDIR(info.st_mode):
                        if depth >= MAX_DIRECTORY_DEPTH:
                            report.update(complete=False, reason="depth_limit")
                            continue
                        child = os.open(item.name, flags, dir_fd=descriptor)
                        try:
                            walk(child, report, depth + 1)
                        finally:
                            os.close(child)
                    else:
                        report["skipped_special"] += 1
                        report["complete"] = False
                except OSError:
                    report["unavailable_entries"] += 1
                    report["complete"] = False
    try:
        for area in FILE_AREAS:
            report = {"file_count": 0, "bytes": 0, "entries_examined": 0, "skipped_links": 0,
                      "skipped_special": 0, "unavailable_entries": 0, "complete": True, "status": "present"}
            reports[area], descriptors, parent = report, [], root
            try:
                for part in area.split("/"):
                    parent = os.open(part, flags, dir_fd=parent)
                    descriptors.append(parent)
                walk(parent, report, 0)
            except FileNotFoundError:
                report["status"] = "absent"
            except OSError:
                report.update(status="unavailable_or_link", complete=False)
            finally:
                for descriptor in reversed(descriptors):
                    os.close(descriptor)
    finally:
        os.close(root)
    return {"areas": reports, "complete": all(item["complete"] for item in reports.values()),
            "limits": {"entries": FILE_ENTRIES, "seconds": FILE_SECONDS, "depth": MAX_DIRECTORY_DEPTH},
            "measurement": "regular-file logical bytes; hard links counted per entry; no contents read"}


def _deep(conn, tables):
    result = {"logical_text_bytes": {}, "logical_complete": True}
    try:
        for table, columns in LOGICAL_COLUMNS.items():
            if table not in tables:
                continue
            expression = " + ".join("coalesce(length(CAST(%s AS BLOB)),0)" % column for column in columns)
            result["logical_text_bytes"][table] = conn.execute(
                "SELECT coalesce(sum(" + expression + "),0) FROM " + table).fetchone()[0]
    except sqlite3.OperationalError as exc:
        if "interrupted" not in str(exc).lower():
            raise
        result["logical_complete"] = False
    try:
        objects = {row[0]: (row[1], row[2]) for row in conn.execute(
            "SELECT name, type, tbl_name FROM sqlite_master WHERE type IN ('table','index')")}
        grouped = {}
        for row in conn.execute("SELECT name, sum(pgsize), sum(unused), count(*) FROM dbstat GROUP BY name ORDER BY name"):
            # Do not expose custom schema names; report only size categories.
            name = row[0]
            kind, table = objects.get(name, (None, None))
            if name.startswith("entries_fts"):
                category = "fts"
            elif table in ("entry_history", "entity_history", "edge_history"):
                category = "history"
            elif table == "entry_events":
                category = "idempotency"
            elif kind == "index":
                category = "secondary_indexes"
            elif table in ("entries", "entities", "graph_edges", "projects", "relations"):
                category = "current_records"
            else:
                category = "metadata_and_links"
            sums = grouped.setdefault(category, {"allocated_bytes": 0, "unused_bytes": 0, "pages": 0})
            for key, count in zip(("allocated_bytes", "unused_bytes", "pages"), row[1:]):
                sums[key] += count
        result["physical_pages"] = {"status": "measured", "by_category": grouped,
                                    "excludes": "freelist, WAL and external files"}
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if not any(phrase in message for phrase in ("no such table: dbstat", "no such module", "interrupted")):
            raise
        result["physical_pages"] = {"status": "incomplete" if "interrupted" in message else "unavailable"}
    result["logical_measurement"] = "UTF-8 bytes of named text columns, not complete rows or physical allocation"
    result["columns"] = {table: list(columns) for table, columns in LOGICAL_COLUMNS.items() if table in tables}
    return result


def _stats(conn, args, api, database):
    report = {"status": "observed", "scope": _scope(args, database), "observed_at": api.now(),
              "sqlite_version": sqlite3.sqlite_version,
              "coverage": {"database_complete": True, "files_requested": bool(args.files),
                           "files_complete": None, "deep_requested": bool(args.deep), "output_complete": True}}
    with api.transaction(conn, write=False), _deadline(conn, SQL_SECONDS):
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        report["schema_version"], report["search_backend"] = version, api.backend(conn)
        pages = {name: conn.execute("PRAGMA " + name).fetchone()[0]
                 for name in ("page_size", "page_count", "freelist_count")}
        report["database_pages"] = dict(pages, allocated_bytes=pages["page_size"] * pages["page_count"],
                                        free_page_bytes=pages["page_size"] * pages["freelist_count"])
        tables = BASE_TABLES + (GRAPH_TABLES if version in (2, 3) else ()) + (RESEARCH_TABLES if version == 3 else ())
        report["counts"] = {}
        try:
            for table in tables:
                report["counts"][table] = conn.execute("SELECT count(*) FROM " + table).fetchone()[0]
        except sqlite3.OperationalError as exc:
            if "interrupted" not in str(exc).lower():
                raise
            report["coverage"]["database_complete"] = False
        if args.deep:
            report["deep"] = _deep(conn, tables)
            if (not report["deep"]["logical_complete"] or report["deep"]["physical_pages"]["status"] != "measured"):
                report["coverage"]["database_complete"] = False
    report["database_files"] = {"main": _size(database), "wal": _size(Path(str(database) + "-wal")),
                                "shm": _size(Path(str(database) + "-shm"))}
    if args.files:
        report["files"] = _inventory(database)
        report["coverage"]["files_complete"] = report["files"]["complete"]
    report["notes"] = ["One observation measures size, not growth rate; file sizes are not an atomic snapshot.",
                       "Freelist bytes are reusable inside SQLite, not a promise of reclaimable disk space.",
                       "No checkpoint, integrity check, deletion or knowledge mutation was performed."]
    return _bound(report, args.max_chars, api)


def _planner_action(version):
    if version < (3, 32, 0):
        return {"action": "planner_optimize", "status": "skipped", "reason": "bounded_analysis_unavailable"}
    return {"action": "planner_optimize", "status": "planned", "analysis_limit": ANALYSIS_LIMIT,
            "pragma": "optimize=0x10002" if version >= (3, 46, 0) else "optimize"}


def _optimize(conn, args, api, database):
    backend = api.backend(conn)
    actions = [{"action": "fts_optimize", "status": "planned" if backend == "fts5" else "skipped",
                "reason": "merge_fts_segments" if backend == "fts5" else "scan_backend"},
               _planner_action(sqlite3.sqlite_version_info)]
    report = {"status": "plan", "scope": _scope(args, database), "actions": actions,
              "coverage": {"output_complete": True}, "backup": None,
              "notice": "Index maintenance only: no deletion, VACUUM, checkpoint or application schema change. Speed and disk savings are not guaranteed."}
    if not args.apply:
        return _bound(report, args.max_chars, api)
    if getattr(args, "project_path", None) is not None:
        raise api.MemoryError("storage optimize --apply is workspace-wide; omit --project-path")
    if conn.in_transaction:
        raise api.MemoryError("Storage maintenance requires no active transaction")
    if conn.execute("PRAGMA query_only").fetchone()[0]:
        raise api.MemoryError("Storage maintenance requires a writable connection")
    # Validate the predictable response before backup/index writes.
    report["backup"] = str(database.parent / "backups" / (database.stem + "-" + "0" * 22 + ".sqlite3"))
    preview = dict(report, status="applied", actions=[dict(action, status="completed")
        if action["status"] == "planned" else dict(action) for action in actions])
    _bound(preview, args.max_chars, api)
    report["backup"] = api.backup(conn, database, None)["backup"]
    original_limit = None
    try:
        with api.transaction(conn), _deadline(conn, 30.0):
            if backend == "fts5":
                conn.execute("INSERT INTO entries_fts(entries_fts) VALUES ('optimize')")
            if actions[1]["status"] == "planned":
                original_limit = conn.execute("PRAGMA analysis_limit").fetchone()[0]
                conn.execute("PRAGMA analysis_limit=" + str(ANALYSIS_LIMIT))
                conn.execute("PRAGMA " + actions[1]["pragma"]).fetchall()
    except (sqlite3.Error, OSError) as exc:
        raise api.MemoryError("Storage optimization failed; index transaction rolled back; backup retained at " +
                              report["backup"] + ": " + str(exc)) from exc
    finally:
        if original_limit is not None:
            conn.execute("PRAGMA analysis_limit=" + str(original_limit))
    for action in actions:
        if action["status"] == "planned":
            action["status"] = "completed"
    report["status"] = "applied"
    return _bound(report, args.max_chars, api)


def add_commands(commands, api):
    parser = commands.add_parser("storage", help="Observe workspace storage or explicitly maintain indexes")
    actions = parser.add_subparsers(dest="action", required=True)
    stats = actions.add_parser("stats", help="Read-only storage size and growth inputs")
    stats.add_argument("--deep", action="store_true", help="Scan text bytes and optional dbstat page attribution")
    stats.add_argument("--files", action="store_true", help="Bounded inventory of known workspace directories")
    stats.add_argument("--max-chars", type=max_chars, default=8000)
    optimize = actions.add_parser("optimize", help="Show a plan; --apply backs up then maintains indexes")
    optimize.add_argument("--apply", action="store_true")
    optimize.add_argument("--max-chars", type=max_chars, default=8000)


def run(conn, args, api, database):
    database = Path(database)
    if args.action == "stats":
        return _stats(conn, args, api, database)
    if args.action == "optimize":
        return _optimize(conn, args, api, database)
    raise api.MemoryError("Unknown storage action")
