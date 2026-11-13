"""Additive knowledge graph API. All calls use the caller's SQLite transaction."""

import argparse
from collections import deque
import json


ENTITY_KINDS = {"pull_request", "metric", "component", "concept", "document", "jira", "tool"}
MAP_SCHEMA_VERSION = 2
ENTITY_FIELDS = ("id", "kind", "title", "summary", "sources", "knowledge_state", "verified_at",
                 "tags", "aliases", "created_at", "updated_at")
EDGE_FIELDS = ("id", "from_ref", "to_ref", "kind", "description", "sources", "knowledge_state",
               "verified_at", "created_at", "updated_at")
MAX_NODES = 2000
MAX_EDGES = 10000


# The schema migrate produces; tests require it to equal memory.SCHEMA, so a
# schema bump cannot ship without extending migrate.
MIGRATION_TARGET = 3


def require_v2(conn, api):
    if conn.execute("PRAGMA user_version").fetchone()[0] not in {2, 3}:
        raise api.MemoryError("Graph commands require schema 2 or 3; make a backup and run migrate explicitly")


def _migrate_entities_v3(conn, api):
    """Expand the kind constraint without rewriting IDs, history or indexes.

    The caller disables foreign keys outside its transaction, as required by
    SQLite's table-rebuild procedure, and checks every constraint before commit.
    Dependent tables keep referencing the name `entities` throughout the rebuild.
    """
    columns = tuple(row[1] for row in conn.execute("PRAGMA table_info(entities)"))
    if columns != ENTITY_FIELDS:
        raise api.MemoryError("Schema 2 entities columns do not match the migration contract")
    schema_objects = conn.execute("""
        SELECT sql FROM sqlite_master
        WHERE tbl_name = 'entities' AND type IN ('index', 'trigger') AND sql IS NOT NULL
        ORDER BY type, name
    """).fetchall()
    table_sql = (api.BASE / "graph_schema.sql").read_text(encoding="utf-8").split(";", 1)[0]
    conn.execute(table_sql.replace("CREATE TABLE entities (", "CREATE TABLE richi_entities_v3 (", 1))
    fields = ", ".join(ENTITY_FIELDS)
    conn.execute("INSERT INTO richi_entities_v3 (rowid, " + fields + ") SELECT rowid, " + fields + " FROM entities")
    conn.execute("DROP TABLE entities")
    conn.execute("ALTER TABLE richi_entities_v3 RENAME TO entities")
    for row in schema_objects:
        conn.execute(row[0])


def migrate(conn, path, api):
    if conn.in_transaction:
        raise api.MemoryError("Migration requires a connection without an active transaction")
    foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        with api.transaction(conn):
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in {1, 2, 3}:
                raise api.MemoryError("Cannot migrate unknown schema version: " + str(version))
            api.backend(conn)
            if version == MIGRATION_TARGET:
                return {"status": "existing", "schema_version": MIGRATION_TARGET, "database": str(path)}
            expected = {"projects", "entries", "entry_projects", "relations", "entry_history", "entry_events", "metadata"}
            if version == 2:
                expected.update({"entities", "graph_nodes", "graph_edges", "entity_history", "edge_history"})
            existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if not expected.issubset(existing):
                raise api.MemoryError("Schema {} is missing required tables; migration refused".format(version))
            if conn.execute("PRAGMA foreign_key_check").fetchone():
                raise api.MemoryError("Existing foreign key violations; migration refused")
            if [row[0] for row in conn.execute("PRAGMA integrity_check")] != ["ok"]:
                raise api.MemoryError("Existing integrity errors; migration refused")
            if version == 1:
                api.execute_script(conn, (api.BASE / "graph_schema.sql").read_text(encoding="utf-8"))
            else:
                _migrate_entities_v3(conn, api)
            if conn.execute("PRAGMA foreign_key_check").fetchone():
                raise api.MemoryError("Migration foreign key check failed")
            if [row[0] for row in conn.execute("PRAGMA integrity_check")] != ["ok"]:
                raise api.MemoryError("Migration integrity check failed")
            conn.execute("PRAGMA user_version = " + str(MIGRATION_TARGET))
        return {"status": "migrated", "from_version": version, "schema_version": MIGRATION_TARGET, "database": str(path)}
    finally:
        conn.execute("PRAGMA foreign_keys = " + str(foreign_keys))


def ref(value, api):
    value = api.string(value, "node reference")
    family, separator, identity = value.partition(":")
    if not separator or family not in {"project", "entry", "entity"}:
        raise api.MemoryError("Node reference must be project:<id>, entry:<id>, or entity:<id>")
    api.identifier(identity, "node ID")
    return value


def require_node(conn, value, api):
    ref(value, api)
    row = conn.execute("SELECT * FROM graph_nodes WHERE 1=1 AND ref = ?", (value,)).fetchone()
    if row is None:
        raise api.MemoryError("Unknown graph endpoint: " + value)
    return dict(row)


def decode(row, arrays):
    record = dict(row)
    for field in arrays:
        record[field] = json.loads(record[field])
    return record


def get(conn, kind, identity, api, history=True):
    table, history_table = ("entities", "entity_history") if kind == "entity" else ("graph_edges", "edge_history")
    row = conn.execute("SELECT * FROM " + table + " WHERE 1=1 AND id = ?", (identity,)).fetchone()
    if row is None:
        raise api.MemoryError("Unknown {}: {}".format(kind, identity))
    record = decode(row, ("sources", "tags", "aliases") if kind == "entity" else ("sources",))
    if history:
        record["history"] = []
        for row in conn.execute("SELECT * FROM " + history_table + " WHERE 1=1 AND object_id = ? ORDER BY id", (identity,)):
            change = dict(row)
            before = change.pop("before_json")
            change["before"] = json.loads(before) if before else None
            change["after"] = json.loads(change.pop("after_json"))
            record["history"].append(change)
    return record


def put(conn, obj, kind, api):
    columns = ENTITY_FIELDS if kind == "entity" else EDGE_FIELDS
    api.fields(obj, set(columns) - {"created_at", "updated_at"} | {"expected_updated_at"})
    record = {"id": api.identifier(obj.get("id")),
              "sources": api.sources(obj.get("sources", [])),
              "knowledge_state": api.enum(obj.get("knowledge_state", "hypothesis"), "knowledge_state", api.KNOWLEDGE),
              "verified_at": api.timestamp(obj.get("verified_at"), "verified_at")}
    if record["knowledge_state"] == "confirmed" and not record["sources"]:
        raise api.MemoryError("Confirmed {} records require at least one source".format(kind))
    if kind == "entity":
        record.update(kind=api.enum(obj.get("kind"), "kind", ENTITY_KINDS),
                      title=api.string(obj.get("title"), "title"), summary=api.string(obj.get("summary"), "summary"),
                      tags=api.string_list(obj.get("tags", []), "tags"), aliases=api.string_list(obj.get("aliases", []), "aliases"))
        if record["kind"] == "tool" and conn.execute("PRAGMA user_version").fetchone()[0] != 3:
            raise api.MemoryError("Tool entities require schema 3; make a backup and run richi migrate explicitly")
        table, history_table = "entities", "entity_history"
    else:
        record.update(from_ref=ref(obj.get("from_ref"), api), to_ref=ref(obj.get("to_ref"), api),
                      kind=api.identifier(obj.get("kind"), "kind"),
                      description=api.string(obj.get("description", ""), "description", empty=True))
        for endpoint in (record["from_ref"], record["to_ref"]):
            require_node(conn, endpoint, api)
        if record["kind"] == "belongs_to_project" and record["from_ref"].startswith("entry:") and record["to_ref"].startswith("project:"):
            raise api.MemoryError("Entry/project membership is derived; use entry put project_ids instead")
        duplicate = conn.execute("SELECT id FROM graph_links WHERE 1=1 AND origin <> 'explicit' AND from_ref = ? AND to_ref = ? AND kind = ?",
                                 (record["from_ref"], record["to_ref"], record["kind"])).fetchone()
        if duplicate:
            raise api.MemoryError("This edge already exists as a derived relationship: " + duplicate[0])
        table, history_table = "graph_edges", "edge_history"
    expected = api.timestamp(obj.get("expected_updated_at"), "expected_updated_at")
    exists = conn.execute("SELECT id FROM " + table + " WHERE 1=1 AND id = ?", (record["id"],)).fetchone()
    old = get(conn, kind, record["id"], api, history=False) if exists else None
    comparable = {key: value for key, value in old.items() if key not in {"created_at", "updated_at"}} if old else None
    status = "created" if old is None else "unchanged" if comparable == record else "updated"
    if status == "unchanged":
        return {"id": record["id"], "status": status}
    if "expected_updated_at" in obj and expected != (old["updated_at"] if old else None):
        raise api.MemoryError("{} update conflict: expected_updated_at does not match; read the current record".format(kind))
    record["updated_at"] = api.now()
    record["created_at"] = old["created_at"] if old else record["updated_at"]
    stored = dict(record)
    for key in ("sources", "tags", "aliases") if kind == "entity" else ("sources",):
        stored[key] = api.dumps(record[key])
    api.upsert(conn, table, columns, stored)
    conn.execute("INSERT INTO " + history_table + "(object_id, changed_at, before_json, after_json) VALUES (?, ?, ?, ?)",
                 (record["id"], record["updated_at"], api.dumps(old) if old else None, api.dumps(record)))
    return {"id": record["id"], "status": status}


def allowed_states(args):
    states = ["confirmed"]
    if getattr(args, "include_hypotheses", False):
        states.append("hypothesis")
    if getattr(args, "include_superseded", False):
        states.append("superseded")
    return states


def node_state(conn, node_ref):
    row = conn.execute("""
        SELECT coalesce(e.knowledge_state, ent.knowledge_state, 'confirmed')
        FROM graph_nodes gn
            LEFT JOIN entries  e   ON e.id = gn.entry_id
            LEFT JOIN entities ent ON ent.id = gn.entity_id
        WHERE 1=1 AND gn.ref = ?
    """, (node_ref,)).fetchone()
    return row[0] if row else None


def node(conn, node_ref, api):
    registry = require_node(conn, node_ref, api)
    if registry["project_id"] is not None:
        project = api.require_project(conn, registry["project_id"])
        return {"id": node_ref, "native_id": project["id"], "node_type": "project", "kind": "project",
                "title": project["name"], "summary": project["description"], "knowledge_state": "confirmed", "work_state": None,
                "project_ids": [project["id"]], "sources": [{"reference": project["source"]}] if project["source"] else [],
                "tags": [], "aliases": [], "verified_at": project["verified_at"], "repo_path": project["repo_path"]}
    if registry["entry_id"] is not None:
        record = api.entry_record(conn, registry["entry_id"])
        return dict(record, id=node_ref, native_id=record["id"], node_type="entry")
    record = get(conn, "entity", registry["entity_id"], api, history=False)
    return dict(record, id=node_ref, native_id=record["id"], node_type="entity", project_ids=[], work_state=None)


def edge(conn, link, api):
    result = dict(link)
    native_id = result.pop("native_id")
    if result["origin"] == "explicit":
        record = get(conn, "edge", native_id, api, history=False)
        for key in ("description", "sources", "verified_at", "created_at", "updated_at"):
            result[key] = record[key]
    elif result["origin"] == "project_relation":
        record = conn.execute("SELECT * FROM relations WHERE 1=1 AND id = ?", (native_id,)).fetchone()
        result.update(description=record["description"], sources=[{"reference": record["source"]}] if record["source"] else [],
                      verified_at=record["verified_at"])
    else:
        record = api.entry_record(conn, native_id)
        result.update(description="Record is linked to this project; this does not imply a specific impact.",
                      sources=record["sources"], verified_at=record["verified_at"])
    result["provenance"] = {"table": {"explicit": "graph_edges", "project_relation": "relations", "entry_project": "entry_projects"}[result["origin"]],
                            "record_id": native_id}
    return result


def traverse(conn, seed, states, depth, cap):
    if node_state(conn, seed) not in states:
        return [], False
    selected, visited, queue = [seed], {seed}, deque([(seed, 0)])
    placeholders = ",".join("?" for _ in states)
    while queue:
        current, distance = queue.popleft()
        if depth is not None and distance >= depth:
            continue
        rows = conn.execute("SELECT from_ref, to_ref FROM graph_links WHERE 1=1 AND (from_ref = ? OR to_ref = ?) AND knowledge_state IN (" + placeholders + ") ORDER BY id",
                            [current, current] + states)
        for row in rows:
            neighbor = row[1] if row[0] == current else row[0]
            if neighbor not in visited:
                visited.add(neighbor)
                if node_state(conn, neighbor) not in states:
                    continue
                if len(selected) >= cap:
                    return selected, True
                selected.append(neighbor)
                queue.append((neighbor, distance + 1))
    return selected, False


def export(conn, args, api):
    require_v2(conn, api)
    if args.action == "neighbors":
        view = getattr(args, "view", "full")
        maximum = getattr(args, "max_chars", None)
        if view not in {"full", "compact"}:
            raise api.MemoryError("Graph view must be full or compact")
        if view == "full" and maximum is not None:
            raise api.MemoryError("Graph --max-chars requires --view compact")
        if view == "compact":
            from . import graph_view
            return graph_view.neighbors(conn, args, api)
    states = allowed_states(args)
    is_neighbors = args.action == "neighbors"
    cap = args.limit if is_neighbors else MAX_NODES
    root = args.ref if is_neighbors else "project:" + args.project if getattr(args, "project", None) else None
    if root:
        require_node(conn, root, api)
        selected, truncated = traverse(conn, root, states, args.depth if is_neighbors else None, cap)
    else:
        placeholders = ",".join("?" for _ in states)
        rows = conn.execute("""
            SELECT gn.ref
            FROM graph_nodes gn
                LEFT JOIN entries  e   ON e.id = gn.entry_id
                LEFT JOIN entities ent ON ent.id = gn.entity_id
            WHERE 1=1 AND coalesce(e.knowledge_state, ent.knowledge_state, 'confirmed') IN (""" + placeholders + """ )
            ORDER BY CASE WHEN gn.project_id IS NOT NULL THEN 0 WHEN gn.entry_id IS NOT NULL THEN 1 ELSE 2 END, gn.ref
            LIMIT ?
        """, states + [cap + 1]).fetchall()
        selected = [row[0] for row in rows[:cap]]
        truncated = len(rows) > cap
    selected_set, result_edges, seen_ids = set(selected), [], set()
    edges_truncated = False
    # Walk outgoing links of selected nodes only; cap output, never emit dangling edges.
    for origin in ("entry_project", "project_relation", "explicit"):
        for start in selected:
            links = conn.execute("SELECT * FROM graph_links WHERE 1=1 AND from_ref = ? AND origin = ? ORDER BY id", (start, origin))
            for link in links:
                if link["to_ref"] not in selected_set or link["knowledge_state"] not in states or link["id"] in seen_ids:
                    continue
                if len(result_edges) >= MAX_EDGES:
                    edges_truncated = True
                    break
                seen_ids.add(link["id"])
                result_edges.append(edge(conn, link, api))
            if edges_truncated:
                break
        if edges_truncated:
            break
    return {"schema_version": MAP_SCHEMA_VERSION, "generated_at": api.now(), "nodes": [node(conn, item, api) for item in selected],
            "edges": result_edges, "truncated": truncated or edges_truncated,
            "limits": {"max_nodes": cap, "max_edges": MAX_EDGES, "nodes_truncated": truncated, "edges_truncated": edges_truncated},
            "scope": {"root": root, "depth": args.depth if is_neighbors else None,
                      "selection": "neighbors" if is_neighbors else "connected_component" if root else "all", "knowledge_states": states}}


def depth(value):
    result = int(value)
    if not 1 <= result <= 3:
        raise argparse.ArgumentTypeError("depth must be between 1 and 3")
    return result


depth.bounds = (1, 3)


def add_commands(commands, limit):
    commands.add_parser("migrate", help="Upgrade an older store's schema in place (back up first)")
    for name, summary, example in (
            ("entity", "Read or write graph entities (components, concepts, tools...)", "concept:connection-pool"),
            ("edge", "Read or write explicit, sourced links between records", "cache-uses-redis")):
        group = commands.add_parser(name, help=summary).add_subparsers(dest="action", required=True)
        group.add_parser("put", help="Create or replace " + name + " records from JSON").add_argument(
            "--json", required=True, metavar="FILE")
        read = group.add_parser("get", help="Read one whole " + name + " by ID")
        read.add_argument("id", help=name.capitalize() + " ID, for example " + example)
        read.add_argument("--history", action="store_true")
    graph = commands.add_parser("graph", help="Read a record's neighborhood or export the whole graph").add_subparsers(
        dest="action", required=True)
    full = graph.add_parser("export", help="Export the explicit knowledge graph as JSON")
    full.add_argument("--project", help="Only records connected to this project")
    neighbors = graph.add_parser("neighbors", help="Read records linked to one record, up to three hops")
    neighbors.add_argument("ref", help="Start record: project:<id>, entry:<id> or entity:<id>")
    neighbors.add_argument("--depth", type=depth, default=1, help="Link hops to follow, 1-3 (default: %(default)s)")
    neighbors.add_argument("--limit", type=limit, default=200,
                           help="Maximum nodes including the start record, 1-200 (default: %(default)s)")
    neighbors.add_argument("--view", choices=("full", "compact"), default="full",
                           help="full returns whole records; compact fits --max-chars (default: %(default)s)")
    from . import compact
    neighbors.add_argument("--max-chars", type=compact.max_chars,
                           help="Rendered JSON budget, 2000-100000; requires --view compact (default: 16000)")
    for command in (full, neighbors):
        command.add_argument("--include-hypotheses", action="store_true")
        command.add_argument("--include-superseded", action="store_true")
