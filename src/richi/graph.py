"""Additive knowledge graph API. All calls use the caller's SQLite transaction."""

import argparse
from collections import deque
import json


ENTITY_KINDS = {"pull_request", "metric", "component", "concept", "document", "jira"}
ENTITY_FIELDS = ("id", "kind", "title", "summary", "sources", "knowledge_state", "verified_at",
                 "tags", "aliases", "created_at", "updated_at")
EDGE_FIELDS = ("id", "from_ref", "to_ref", "kind", "description", "sources", "knowledge_state",
               "verified_at", "created_at", "updated_at")
MAX_NODES = 2000
MAX_EDGES = 10000


def require_v2(conn, api):
    if conn.execute("PRAGMA user_version").fetchone()[0] not in {2, 3}:
        raise api.MemoryError("Graph commands require schema 2 or 3; make a backup and run migrate explicitly")


def migrate(conn, path, api):
    with api.transaction(conn):
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in {1, 2, 3}:
            raise api.MemoryError("Cannot migrate unknown schema version: " + str(version))
        api.backend(conn)
        if version == 3:
            require_v2(conn, api)
            return {"status": "existing", "schema_version": 3, "database": str(path)}
        expected = {"projects", "entries", "entry_projects", "relations", "entry_history", "entry_events", "metadata"}
        existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE 1=1 AND type = 'table'")}
        if not expected.issubset(existing):
            raise api.MemoryError("Schema 1 is missing required tables; migration refused")
        if conn.execute("PRAGMA foreign_key_check").fetchone():
            raise api.MemoryError("Existing foreign key violations; migration refused")
        if [row[0] for row in conn.execute("PRAGMA integrity_check")] != ["ok"]:
            raise api.MemoryError("Existing integrity errors; migration refused")
        if version == 1:
            api.execute_script(conn, (api.BASE / "graph_schema.sql").read_text(encoding="utf-8"))
        api.execute_script(conn, (api.BASE / "research_schema.sql").read_text(encoding="utf-8"))
        if conn.execute("PRAGMA foreign_key_check").fetchone():
            raise api.MemoryError("Migration foreign key check failed")
        conn.execute("PRAGMA user_version = 3")
    return {"status": "migrated", "from_version": version, "schema_version": 3, "database": str(path)}


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
    from . import research
    native = (kind == 'edge' and research.native_edge(conn, record) or
              kind == 'entity' and research.enabled(conn) and conn.execute(
                  "SELECT 1 FROM graph_edges g JOIN entry_research r ON g.from_ref='entry:'||r.entry_id "
                  "AND r.role='track' WHERE g.to_ref=? AND g.kind='pursues_goal' "
                  "AND g.knowledge_state <> 'superseded' LIMIT 1", ('entity:' + identity,)).fetchone() is not None)
    if native:
        record["revision"] = research.revision(conn, kind + ":" + identity)
    if history:
        record["history"] = []
        for row in conn.execute("SELECT * FROM " + history_table + " WHERE 1=1 AND object_id = ? ORDER BY id", (identity,)):
            change = dict(row)
            before = change.pop("before_json")
            change["before"] = json.loads(before) if before else None
            change["after"] = json.loads(change.pop("after_json"))
            record["history"].append(change)
    return record


def put(conn, obj, kind, api, *, _research_bundle=False):
    if not conn.in_transaction:
        with api.transaction(conn):
            return put(conn, obj, kind, api, _research_bundle=_research_bundle)
    conn.execute('SAVEPOINT richi_graph_write')
    try:
        result = _put(conn, obj, kind, api, _research_bundle=_research_bundle)
        conn.execute('RELEASE richi_graph_write')
        return result
    except BaseException:
        conn.execute('ROLLBACK TO richi_graph_write')
        conn.execute('RELEASE richi_graph_write')
        raise


def _put(conn, obj, kind, api, *, _research_bundle=False):
    from . import research
    columns = ENTITY_FIELDS if kind == "entity" else EDGE_FIELDS
    api.fields(obj, set(columns) - {"created_at", "updated_at"} | {"expected_updated_at", "expected_revision"})
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
    if kind == "edge":
        research.validate_edge(conn, record, api, old=old, bundle=_research_bundle)
    elif old and old['kind'] != record['kind'] and research.enabled(conn):
        if conn.execute("SELECT 1 FROM graph_edges g JOIN entry_research r ON g.from_ref='entry:'||r.entry_id "
                        "AND r.role='track' WHERE g.to_ref=? AND g.kind='pursues_goal' "
                        "AND g.knowledge_state <> 'superseded' LIMIT 1", ('entity:' + record['id'],)).fetchone():
            raise api.MemoryError('A referenced business-goal document cannot change kind')
    comparable = {key: value for key, value in old.items() if key not in {"created_at", "updated_at", "revision"}} if old else None
    status = "created" if old is None else "unchanged" if comparable == record else "updated"
    native = kind == 'edge' and research.native_edge(conn, record)
    if kind == 'entity' and research.enabled(conn):
        native = conn.execute("SELECT 1 FROM graph_edges g JOIN entry_research r ON g.from_ref='entry:'||r.entry_id "
                              "AND r.role='track' WHERE g.to_ref=? AND g.kind='pursues_goal' "
                              "AND g.knowledge_state <> 'superseded' LIMIT 1", ('entity:' + record['id'],)).fetchone() is not None
    research.check_revision(conn, obj, kind + ':' + record['id'], api,
                            required=old is not None and status == 'updated' and native)
    if status == "unchanged":
        return {"id": record["id"], "status": status}
    if "expected_updated_at" in obj and expected != (old["updated_at"] if old else None):
        raise api.MemoryError("{} update conflict: expected_updated_at does not match; read the current record".format(kind))
    record["updated_at"] = api.now()
    record["created_at"] = old["created_at"] if old else record["updated_at"]
    stored = dict(record)
    for key in ("sources", "tags", "aliases") if kind == "entity" else ("sources",):
        stored[key] = api.dumps(record[key])
    scopes = (research.edge_scopes(conn, old) | research.edge_scopes(conn, record) if kind == 'edge'
              else research.track_scopes(conn, 'entity:' + record['id']))
    api.upsert(conn, table, columns, stored)
    scopes |= (research.edge_scopes(conn, old) | research.edge_scopes(conn, record) if kind == 'edge'
               else research.track_scopes(conn, 'entity:' + record['id']))
    event_kind = kind + '_' + status
    if kind == 'edge' and record['kind'] == 'in_track' and native:
        was_active = old is not None and old['knowledge_state'] != 'superseded'
        active = record['knowledge_state'] != 'superseded'
        if active and not was_active:
            event_kind = 'attached'
        elif was_active and not active:
            event_kind = 'detached'
    research.notify(conn, kind + ':' + record['id'], event_kind, record['updated_at'], scopes, api)
    if native:
        record['revision'] = research.revision(conn, kind + ':' + record['id'])
    conn.execute("INSERT INTO " + history_table + "(object_id, changed_at, before_json, after_json) VALUES (?, ?, ?, ?)",
                 (record["id"], record["updated_at"], api.dumps(old) if old else None, api.dumps(record)))
    response = {"id": record["id"], "status": status}
    if native:
        response['revision'] = record['revision']
    return response


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
    return {"schema_version": 2, "generated_at": api.now(), "nodes": [node(conn, item, api) for item in selected],
            "edges": result_edges, "truncated": truncated or edges_truncated,
            "limits": {"max_nodes": cap, "max_edges": MAX_EDGES, "nodes_truncated": truncated, "edges_truncated": edges_truncated},
            "scope": {"root": root, "depth": args.depth if is_neighbors else None,
                      "selection": "neighbors" if is_neighbors else "connected_component" if root else "all", "knowledge_states": states}}


def depth(value):
    result = int(value)
    if not 1 <= result <= 3:
        raise argparse.ArgumentTypeError("depth must be between 1 and 3")
    return result


def add_commands(commands, api):
    commands.add_parser("migrate")
    for name in ("entity", "edge"):
        group = commands.add_parser(name).add_subparsers(dest="action", required=True)
        group.add_parser("put").add_argument("--json", required=True)
        read = group.add_parser("get")
        read.add_argument("id")
        read.add_argument("--history", action="store_true", help="Include full revision history")
    graph = commands.add_parser("graph").add_subparsers(dest="action", required=True)
    full = graph.add_parser("export")
    full.add_argument("--project")
    neighbors = graph.add_parser("neighbors")
    neighbors.add_argument("ref")
    neighbors.add_argument("--depth", type=depth, default=1)
    neighbors.add_argument("--limit", type=api.limit, default=200)
    neighbors.add_argument("--view", choices=("full", "compact"), default="full")
    from . import compact
    neighbors.add_argument("--max-chars", type=compact.max_chars,
                           help="Rendered JSON budget; requires --view compact (default: 16000)")
    for command in (full, neighbors):
        command.add_argument("--include-hypotheses", action="store_true")
        command.add_argument("--include-superseded", action="store_true")
