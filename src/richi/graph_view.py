"""Bounded graph reading; full export and stored records keep their contracts."""

from collections import deque

from . import compact, graph
from .output import rendered


NOTICE = ("Partial reading view, not an update payload. Read full records before "
          "a condition-dependent conclusion. Membership and paths do not establish causality.")


def _node(record):
    # Copy only displayed source objects, not all of a potentially large array.
    native = record["native_id"]
    source_count = len(record["sources"])
    value = compact.card(dict(record, id=native, sources=record["sources"][:2]), record["id"], 700)
    value.update(id=record["id"], native_id=native, node_type=record["node_type"],
                 source_count=source_count, sources_omitted=source_count - len(value["sources"]))
    if record["node_type"] == "project":
        value["read_more"] = {"command": "graph neighbors", "ref": record["id"], "view": "full"}
    return value


def _edge(conn, link, api):
    if link["origin"] == "entry_project":
        value = dict(link)
        native = value.pop("native_id")
        value.update(provenance={"table": "entry_projects", "record_id": native},
                     sources_ref=link["from_ref"], source_mode="entry_record",
                     description_excerpt="Record is linked to this project; this does not imply a specific impact.",
                     description_is_excerpt=False,
                     read_more={"command": "entry get", "id": native})
        return value
    record = graph.edge(conn, link, api)
    description, partial = compact.excerpt(record.pop("description", ""), 500)
    sources = record["sources"]
    record.update(description_excerpt=description, description_is_excerpt=partial,
                  sources=sources[:2], source_count=len(sources), sources_omitted=max(0, len(sources) - 2))
    if partial:
        record["excerpt_note"] = compact.EXCERPT_NOTE
    if link["origin"] == "explicit":
        record["read_more"] = {"command": "edge get", "id": link["native_id"]}
    else:
        # Context returns every project relation, including hypotheses and (with
        # this flag) superseded rows. Its limit applies only to attached entries,
        # unlike graph neighbors whose node cap can hide the requested relation.
        project_id = link["from_ref"].partition(":")[2]
        record["read_more"] = {
            "args": ["context", "--project=" + project_id, "--limit", "1", "--include-superseded"],
            "select": {"collection": "relations", "id": link["native_id"]}}
    return record


def _links(conn, selected, states):
    selected_set, result, seen = set(selected), [], set()
    # Keep the established export ordering and cap before loading edge payloads.
    for origin in ("entry_project", "project_relation", "explicit"):
        for start in selected:
            rows = conn.execute("SELECT * FROM graph_links WHERE from_ref = ? AND origin = ? ORDER BY id", (start, origin))
            for row in rows:
                if row["to_ref"] not in selected_set or row["knowledge_state"] not in states or row["id"] in seen:
                    continue
                if len(result) == graph.MAX_EDGES:
                    return result, True
                seen.add(row["id"])
                result.append(dict(row))
    return result, False


def _connected(root, selected, edges):
    """A BFS prefix and its tree edges remain rooted when the tail is removed."""
    if root not in selected:
        return [], set()
    adjacency = {ref: [] for ref in selected}
    for edge in edges:
        adjacency[edge["from_ref"]].append((edge["to_ref"], edge["id"]))
        adjacency[edge["to_ref"]].append((edge["from_ref"], edge["id"]))
    ordered, seen, tree, queue = [root], {root}, set(), deque([root])
    while queue:
        current = queue.popleft()
        for neighbor, edge_id in adjacency[current]:
            if neighbor not in seen:
                seen.add(neighbor)
                ordered.append(neighbor)
                tree.add(edge_id)
                queue.append(neighbor)
    return ordered, tree


def _partial(value):
    return (value.get("is_excerpt") or value.get("description_is_excerpt") or value.get("sources_omitted")
            or value.get("title_truncated") or value.get("project_ids_omitted") or value.get("payload_omitted"))


def _bound(response, maximum, tree_ids, selected_count, edge_count):
    """Fit actual stdout, pruning complete edges and a connected BFS prefix."""
    value = response
    value["budget"] = {"max_chars": maximum, "output_chars": 0, "omitted_nodes": 0, "omitted_edges": 0}
    initially_truncated = value["truncated"]

    def measure():
        budget = value["budget"]
        budget["omitted_nodes"] = selected_count - len(value["nodes"])
        budget["omitted_edges"] = edge_count - len(value["edges"])
        value["truncated"] = bool(initially_truncated or budget["omitted_nodes"] or budget["omitted_edges"]
                                  or any(_partial(item) for item in value["nodes"] + value["edges"]))
        for _ in range(8):
            size = len(rendered(value))
            if budget["output_chars"] == size:
                return size
            budget["output_chars"] = size
        return len(rendered(value))

    if measure() <= maximum:
        return value
    for items, field, flag in ((value["nodes"], "summary_excerpt", "is_excerpt"),
                               (value["edges"], "description_excerpt", "description_is_excerpt")):
        for item in items:
            if len(item.get(field, "")) > 300:
                item[field], item[flag] = compact.excerpt(item[field], 300)
                item["excerpt_note"] = compact.EXCERPT_NOTE
    if measure() <= maximum:
        return value

    # Extra links can go first; keep a rooted path for each displayed node.
    all_edges = value["edges"]
    tree = [item for item in all_edges if item["id"] in tree_ids]
    extras = [item for item in all_edges if item["id"] not in tree_ids]
    value["edges"] = tree
    if measure() <= maximum:
        low, high = 0, len(extras)
        while low < high:
            middle = (low + high + 1) // 2
            keep = tree_ids | {item["id"] for item in extras[:middle]}
            value["edges"] = [item for item in all_edges if item["id"] in keep]
            if measure() <= maximum:
                low = middle
            else:
                high = middle - 1
        keep = tree_ids | {item["id"] for item in extras[:low]}
        value["edges"] = [item for item in all_edges if item["id"] in keep]
        measure()
        return value

    # Sources are atomic. Omitting them is explicit and keeps a full-read pointer.
    for count in (1, 0):
        for item in value["nodes"] + value["edges"]:
            if "sources" in item:
                item["sources"] = item["sources"][:count]
                item["sources_omitted"] = item["source_count"] - len(item["sources"])
        if measure() <= maximum:
            return value

    # A BFS prefix retains every ancestor; remove incident edges with the tail.
    all_nodes, tree = value["nodes"], value["edges"]
    def prefix(count):
        value["nodes"] = all_nodes[:count]
        retained = {item["id"] for item in value["nodes"]}
        value["edges"] = [item for item in tree if item["from_ref"] in retained and item["to_ref"] in retained]
        return measure()

    low, high = int(bool(all_nodes)), len(all_nodes)
    if prefix(low) <= maximum:
        while low < high:
            middle = (low + high + 1) // 2
            if prefix(middle) <= maximum:
                low = middle
            else:
                high = middle - 1
        prefix(low)
        return value

    # A single root may itself contain giant metadata. Preserve its exact ref and
    # state, not chopped identifiers or evidence. Full read uses scope.root.
    root = all_nodes[0] if all_nodes else None
    value["nodes"] = [{key: root[key] for key in ("id", "node_type", "knowledge_state", "work_state")}
                      if root else {}] if root else []
    if root:
        value["nodes"][0]["payload_omitted"] = True
    value["edges"] = []
    value["notice"] = "Root payload did not fit. Full records require --view full; use scope.root. This view is incomplete."
    # Large repeated IDs are avoided in this emergency envelope. Scope contains
    # the exact root and state filters, sufficient to repeat the same full read.
    if measure() > maximum:
        value.pop("limits", None)
        value.pop("generated_at", None)
        measure()
    return value


def neighbors(conn, args, api):
    maximum = getattr(args, "max_chars", None)
    maximum = 16000 if maximum is None else maximum
    if not 2000 <= maximum <= 100000:
        raise api.MemoryError("max_chars must be between 2000 and 100000")
    if not 1 <= args.limit <= 200 or not 1 <= args.depth <= 3:
        raise api.MemoryError("Graph neighbors requires limit 1-200 and depth 1-3")
    root = args.ref
    graph.require_node(conn, root, api)
    states = graph.allowed_states(args)
    selected, nodes_truncated = graph.traverse(conn, root, states, args.depth, args.limit)
    links, edges_truncated = _links(conn, selected, states)
    connected, tree_ids = _connected(root, selected, links)
    retained = set(connected)
    retained_links = [link for link in links if link["from_ref"] in retained and link["to_ref"] in retained]
    response = {"schema_version": 2, "view": "compact", "view_schema_version": 1,
                "generated_at": api.now(), "nodes": [_node(graph.node(conn, item, api)) for item in connected],
                "edges": [_edge(conn, link, api) for link in retained_links],
                "truncated": nodes_truncated or edges_truncated or len(connected) < len(selected),
                "limits": {"max_nodes": args.limit, "max_edges": graph.MAX_EDGES,
                           "nodes_truncated": nodes_truncated, "edges_truncated": edges_truncated,
                           "selected_nodes": len(selected), "selected_edges": len(links),
                           "disconnected_nodes_omitted": len(selected) - len(connected),
                           "disconnected_edges_omitted": len(links) - len(retained_links)},
                "scope": {"root": root, "root_visible": bool(connected), "depth": args.depth,
                          "selection": "neighbors", "knowledge_states": states}, "notice": NOTICE}
    return _bound(response, maximum, tree_ids, len(connected), len(retained_links))
