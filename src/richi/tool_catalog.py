"""Read-only tool catalog, with evidence-backed project/task drill-down.

Project membership is not tool usage. Only a sourced, confirmed uses_tool
edge from an entry identifies an application; part_of links may connect that
application to a task. No title, Jira-key or arbitrary graph-path inference.
"""

from collections import deque

from . import graph


MAX_RECORDS = 200
MAX_ANCESTORS = 200
MAX_PROJECTS = 100
MAX_TASKS = 200
MAX_PROJECT_EVIDENCE = 20


def project_scope_sql(reference, entry_states="'confirmed'"):
    """The single definition of a tool's project scope.

    Both arguments are SQL fragments built by our callers, never user text:
    `reference` yields the tool node ref and `entry_states` lists the accepted
    usage-entry states as literals or placeholders. Bound placeholders follow
    the two references in statement order. The catalog and recall pass the
    selected states; strict filters keep confirmed usage only.
    """
    return """
        SELECT substr(t_scope.to_ref, 9) AS id
        FROM graph_edges t_scope JOIN projects t_project
            ON t_project.id = substr(t_scope.to_ref, 9) AND substr(t_scope.to_ref, 1, 8) = 'project:'
        WHERE t_scope.from_ref = """ + reference + """
            AND t_scope.kind = 'used_in' AND t_scope.knowledge_state = 'confirmed'
            AND json_array_length(t_scope.sources) > 0
        UNION
        SELECT t_membership.project_id AS id
        FROM graph_edges t_usage
            JOIN entries t_entry ON t_entry.id = substr(t_usage.from_ref, 7)
                AND substr(t_usage.from_ref, 1, 6) = 'entry:'
            JOIN entry_projects t_membership ON t_membership.entry_id = t_entry.id
        WHERE t_usage.to_ref = """ + reference + """
            AND t_usage.kind = 'uses_tool' AND t_usage.knowledge_state = 'confirmed'
            AND json_array_length(t_usage.sources) > 0
            AND t_entry.knowledge_state IN (""" + entry_states + """)
    """


def project_ids(conn, node_ref, states=("confirmed",)):
    states = list(states)
    sql = project_scope_sql("?", ",".join("?" for _ in states))
    return sorted(row[0] for row in conn.execute(sql, [node_ref, node_ref, *states]))


def _records(conn, rows):
    """Attach explicit project memberships to at most MAX_RECORDS entry rows.

    One membership query per batch replaces two point queries per entry; every
    caller passes at most 200 rows, well below SQLite's variable limit.
    """
    records = [{"id": row["id"], "kind": row["kind"], "title": row["title"],
                "knowledge_state": row["knowledge_state"], "ref": "entry:" + row["id"], "project_ids": []}
               for row in rows]
    if records:
        by_id = {record["id"]: record for record in records}
        for row in conn.execute(
                "SELECT entry_id, project_id FROM entry_projects WHERE entry_id IN (" +
                ",".join("?" for _ in by_id) + ") ORDER BY entry_id, project_id", list(by_id)):
            by_id[row["entry_id"]]["project_ids"].append(row["project_id"])
    return records


def _sort(record):
    return (record.get("title", record.get("name", "")).casefold(), record["id"])


def _applications(conn, tool, states, api, detailed):
    placeholders = ",".join("?" for _ in states)
    rows = conn.execute("""
        SELECT DISTINCT e.id, e.kind, e.title, e.knowledge_state FROM graph_edges link
            JOIN entries e ON e.id = substr(link.from_ref, 7) AND substr(link.from_ref, 1, 6) = 'entry:'
        WHERE link.to_ref = ? AND link.kind = 'uses_tool'
            AND link.knowledge_state = 'confirmed' AND json_array_length(link.sources) > 0
            AND e.knowledge_state IN (""" + placeholders + """ )
        ORDER BY e.title COLLATE NOCASE, e.id LIMIT ?
    """, [tool["ref"], *states, MAX_RECORDS + 1]).fetchall()
    records = _records(conn, rows[:MAX_RECORDS])
    truncated = len(rows) > MAX_RECORDS
    for record in records if detailed else []:
        evidence = conn.execute("""
                SELECT id FROM graph_edges WHERE from_ref = ? AND to_ref = ?
                    AND kind = 'uses_tool' AND knowledge_state = 'confirmed'
                    AND json_array_length(sources) > 0 ORDER BY id LIMIT 21
            """, (record["ref"], tool["ref"])).fetchall()
        record["usage_evidence"] = [graph.get(conn, "edge", row[0], api, history=False) for row in evidence[:20]]
        record["usage_evidence_truncated"] = len(evidence) > 20
        truncated = truncated or record["usage_evidence_truncated"]
    return records, truncated


def _task_ancestors(conn, record, states):
    """Follow only outgoing part_of edges, stopping at tasks; cycles are harmless."""
    queue = deque([(record, [])])
    visited, tasks, truncated = {record["id"]}, [], False
    placeholders = ",".join("?" for _ in states)
    while queue:
        current, path = queue.popleft()
        if current["kind"] == "task":
            tasks.append((current, path))
            continue
        remaining = MAX_ANCESTORS - (len(visited) - 1)
        visited_ids = sorted(visited)
        rows = conn.execute("""
            SELECT e.id, e.kind, e.title, e.knowledge_state, min(link.id) AS edge_id FROM graph_edges link
                JOIN entries e ON e.id = substr(link.to_ref, 7) AND substr(link.to_ref, 1, 6) = 'entry:'
            WHERE link.from_ref = ? AND link.kind = 'part_of'
                AND link.knowledge_state = 'confirmed' AND json_array_length(link.sources) > 0
                AND e.knowledge_state IN (""" + placeholders + """ )
                AND e.id NOT IN (""" + ",".join("?" for _ in visited_ids) + """ )
            GROUP BY e.id ORDER BY edge_id LIMIT ?
        """, [current["ref"], *states, *visited_ids, remaining + 1]).fetchall()
        if len(rows) > remaining:
            truncated = True
        rows = rows[:remaining]
        for row, parent in zip(rows, _records(conn, rows)):
            visited.add(row["id"])
            queue.append((parent, path + ["edge:" + row["edge_id"]]))
    return tasks, truncated


def _tool(conn, row, states, api, detailed):
    tool = graph.decode(row, ("sources", "tags", "aliases"))
    tool["ref"] = "entity:" + tool["id"]
    records, truncated = _applications(conn, tool, states, api, detailed)
    # Projects are established either by explicit tool scope or by an actual
    # application. An unrelated task in the same project is never included.
    # Recall and strict filters read this same scope definition.
    memberships = project_ids(conn, tool["ref"], states)
    truncated = truncated or len(memberships) > MAX_PROJECTS
    selected = memberships[:MAX_PROJECTS]
    fields = "*" if detailed else "id, name"
    project_rows = {item["id"]: dict(item) for item in conn.execute(
        "SELECT " + fields + " FROM projects WHERE id IN (" + ",".join("?" for _ in selected) + ")", selected)}
    projects = {}
    for project_id in selected:
        project = project_rows[project_id]
        evidence = conn.execute("""
            SELECT id FROM graph_edges WHERE from_ref = ? AND to_ref = ? AND kind = 'used_in'
                AND knowledge_state = 'confirmed' AND json_array_length(sources) > 0
            ORDER BY id LIMIT ?
        """, (tool["ref"], "project:" + project_id, MAX_PROJECT_EVIDENCE + 1)).fetchall()
        evidence_truncated = len(evidence) > MAX_PROJECT_EVIDENCE
        truncated = truncated or evidence_truncated
        project.update(ref="project:" + project_id, tasks=[], records=[], truncated=False,
                       usage_evidence=["edge:" + item["id"] for item in evidence[:MAX_PROJECT_EVIDENCE]],
                       usage_evidence_truncated=evidence_truncated)
        projects[project_id] = project
    task_maps = {identity: {} for identity in projects}
    selected_tasks = set()
    for record in records:
        tasks, ancestor_truncated = _task_ancestors(conn, record, states) if detailed else ([], False)
        truncated = truncated or ancestor_truncated
        for project_id in record["project_ids"]:
            project = projects.get(project_id)
            if project is None:
                continue
            project["records"].append(record)
            project["truncated"] = project["truncated"] or ancestor_truncated
            for task, path in tasks:
                if project_id not in task["project_ids"]:
                    continue
                if task["id"] not in selected_tasks and len(selected_tasks) >= MAX_TASKS:
                    truncated = True
                    continue
                selected_tasks.add(task["id"])
                target = task_maps[project_id].setdefault(task["id"], dict(task, usage_refs=[], usage_paths=[]))
                target["usage_refs"].append(record["ref"])
                target["usage_paths"].append({"record_ref": record["ref"], "edge_refs": path})
    for project_id, project in projects.items():
        project["tasks"] = sorted(task_maps[project_id].values(), key=_sort)
        project["task_count"] = len(project["tasks"]) if detailed else None
        project["record_count"] = len(project["records"])
        project["truncated"] = project["truncated"] or truncated
        project["records"] = [record["ref"] for record in project["records"]] if detailed else []
        project["tasks"] = [{key: task[key] for key in ("ref", "usage_refs", "usage_paths")} for task in project["tasks"]]
    shared = {}
    if detailed:
        identities = {record["id"] for record in records}
        identities.update(task_id for tasks in task_maps.values() for task_id in tasks)
        for identity in sorted(identities):
            record = api.entry_record(conn, identity)
            shared["entry:" + identity] = dict(record, ref="entry:" + identity)
        for record in records:
            shared[record["ref"]].update({key: record[key] for key in ("usage_evidence", "usage_evidence_truncated")})
    else:
        summary = tool["summary"]
        tool["summary"] = summary[:1000]
        tool["summary_is_excerpt"] = len(summary) > 1000
        tool["sources_omitted"] = len(tool["sources"])
        tool["sources"] = []
    state_flags = (["--include-hypotheses"] if "hypothesis" in states else []) + (["--include-superseded"] if "superseded" in states else [])
    tool.update(projects=sorted(projects.values(), key=_sort), records=[record["ref"] for record in records] if detailed else [],
                project_count=len(projects), record_count=len(records),
                task_count=len({task_id for tasks in task_maps.values() for task_id in tasks}) if detailed else None,
                detail=detailed, read_more={"args": ["tools", "get", *state_flags, "--", tool["id"]]}, truncated=truncated)
    return tool, shared


def run(conn, args, api):
    graph.require_v2(conn, api)
    states = graph.allowed_states(args)
    values = list(states)
    clause = "kind = 'tool' AND knowledge_state IN (" + ",".join("?" for _ in states) + ")"
    maximum = getattr(args, "limit", 100)
    if args.action == "get":
        identity = api.identifier(args.id)
        clause += " AND id = ?"
        values.append(identity)
        maximum = 1
    total = conn.execute("SELECT count(*) FROM entities WHERE " + clause, values).fetchone()[0]
    if args.action == "get" and not total:
        raise api.NotFoundError("Tool not found in the selected knowledge states: " + args.id)
    rows = conn.execute("SELECT * FROM entities WHERE " + clause +
                        " ORDER BY title COLLATE NOCASE, id LIMIT ?", values + [maximum])
    tools, records = [], {}
    for row in rows:
        tool, shared = _tool(conn, row, states, api, args.action == "get")
        tools.append(tool)
        records.update(shared)
    return {"schema_version": 1, "view": "tools", "generated_at": api.now(), "tools": tools,
            "records": records,
            "total_tools": total, "truncated": total > len(tools) or any(tool["truncated"] for tool in tools),
            "limits": {"tools": maximum, "records_per_tool": MAX_RECORDS,
                       "ancestors_per_record": MAX_ANCESTORS, "projects_per_tool": MAX_PROJECTS,
                       "tasks_per_tool": MAX_TASKS, "evidence_edges_per_project": MAX_PROJECT_EVIDENCE},
            "notice": "Only explicit sourced usage is shown. Counts cover the returned selection when truncated."}


def add_commands(commands, limit):
    actions = commands.add_parser("tools", help="Browse tools and their recorded project/task applications").add_subparsers(dest="action", required=True)
    listing = actions.add_parser("list", help="Read the tool catalog with explicit usage")
    listing.add_argument("--limit", type=limit, default=100)
    detail = actions.add_parser("get", help="Read one tool with its projects and related tasks")
    detail.add_argument("id", help="Native entity ID (for example tool:example:tracekit)")
    for command in (listing, detail):
        command.add_argument("--include-hypotheses", action="store_true")
        command.add_argument("--include-superseded", action="store_true")
