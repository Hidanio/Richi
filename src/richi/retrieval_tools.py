"""Progressive read tools: small discovery routes, then whole bounded records."""


def add_commands(commands, api):
    discover_cmd = commands.add_parser("discover", help="Find small routing cards, then fetch selected full records")
    discover_cmd.add_argument("query")
    discover_cmd.add_argument("--project")
    discover_cmd.add_argument("--limit", type=api.limit, default=8)
    discover_cmd.add_argument("--max-chars", type=api.compact.max_chars, default=8000)
    discover_cmd.add_argument("--include-hypotheses", action="store_true")
    discover_cmd.add_argument("--include-superseded", action="store_true")
    fetch_cmd = commands.add_parser("fetch", help="Read whole current records in one bounded snapshot")
    fetch_cmd.add_argument("--ref", dest="refs", action="append", required=True,
                           help="Exact entry:<id>, entity:<id>, edge:<id> or project:<id>; repeatable, at most 32 unique refs")
    fetch_cmd.add_argument("--offset", type=int, default=0,
                           help="Continue at this index in the original deduplicated ref list")
    fetch_cmd.add_argument("--max-chars", type=api.compact.max_chars, default=16000)


def run(conn, args, api):
    if args.command not in {"discover", "fetch"}:
        raise api.MemoryError("Unknown retrieval command")
    if not isinstance(args.max_chars, int) or not 2000 <= args.max_chars <= 100000:
        raise api.MemoryError("max_chars must be between 2000 and 100000")
    operation = discover if args.command == "discover" else fetch
    if conn.in_transaction:
        return operation(conn, args, api)
    with api.transaction(conn, write=False):
        return operation(conn, args, api)


def _measure(value, api):
    """Count exact rendered JSON including output_chars digits and newline."""
    for _ in range(8):
        size = len(api.output.rendered(value))
        if value["budget"]["output_chars"] == size:
            return size
        value["budget"]["output_chars"] = size
    return len(api.output.rendered(value))


def _pointer(ref):
    family, _, identity = ref.partition(":")
    # Structured arguments handle native IDs containing colons or leading dashes.
    args = (["fetch", "--ref", ref] if family == "project" else
            [family, "get", "--", identity])
    return {"args": args}


def discover(conn, args, api):
    from . import recall
    response = recall.recall(conn, args, api, bounded=False)
    results = []
    for card in response["results"]:
        item = {key: card[key] for key in (
            "id", "ref", "kind", "title", "knowledge_state", "work_state",
            "verified_at", "updated_at", "project_ids", "retrieval_score",
            "needs_recheck", "recheck_reasons", "summary_chars", "source_count")}
        item["summary_excerpt"], partial = api.compact.excerpt(card["summary_excerpt"], 300)
        item.update(is_excerpt=bool(card["is_excerpt"] or partial),
                    sources_omitted=card["source_count"], full_record=False,
                    read_more=_pointer(card["ref"]))
        item["match_reasons"] = [reason for reason in card["match_reasons"] if len(reason) <= 240][:8]
        item["match_reasons_omitted"] = len(card["match_reasons"]) - len(item["match_reasons"])
        for key in ("title_truncated", "project_ids_omitted"):
            if key in card:
                item[key] = card[key]
        results.append(item)
    edges = []
    for edge in response["edges"]:
        item = {key: edge[key] for key in (
            "id", "origin", "from_ref", "to_ref", "kind", "knowledge_state",
            "verified_at", "source_count")}
        native_id = edge["id"].partition(":")[2]
        item.update(sources_omitted=edge["source_count"], full_record=False,
                    provenance={"table": "graph_edges", "record_id": native_id},
                    read_more=_pointer("edge:" + native_id))
        edges.append(item)
    response.update(view="discovery", full_records=False, results=results, edges=edges,
                    notice="Routing cards only. Sources and conditions are omitted; fetch relevant full records before conclusions or updates.")
    selected_count, edge_count = len(results), len(edges)
    response["budget"] = {"max_chars": args.max_chars, "output_chars": 0,
                          "omitted_results": 0, "omitted_edges": 0}

    def measure():
        response["budget"]["omitted_results"] = response["candidate_count"] - len(response["results"])
        response["budget"]["omitted_edges"] = edge_count - len(response["edges"])
        ambiguity = response.get("ambiguity")
        if ambiguity:
            ambiguity["omitted_candidates"] = ambiguity["concept_count"] - len(ambiguity["candidates"])
        response["truncated"] = bool(response["budget"]["omitted_results"] or
                                      response["budget"]["omitted_edges"] or
                                      any(c["is_excerpt"] or c["sources_omitted"] or c.get("title_truncated") or
                                          c.get("project_ids_omitted") or c["match_reasons_omitted"]
                                          for c in response["results"]) or
                                      (ambiguity and (ambiguity["omitted_candidates"] or ambiguity.get("aliases_truncated"))))
        return _measure(response, api)

    if measure() <= args.max_chars:
        return response
    for item in response["results"]:
        if item["summary_excerpt"]:
            item["summary_excerpt"] = ""
            item["is_excerpt"] = True
    while response["edges"] and measure() > args.max_chars:
        response["edges"].pop()
    while len(response["results"]) > 1 and measure() > args.max_chars:
        response["results"].pop()
    ambiguity = response.get("ambiguity")
    if ambiguity:
        while ambiguity["candidates"] and measure() > args.max_chars:
            ambiguity["candidates"].pop()
    if measure() <= args.max_chars:
        return response
    # Legal metadata alone may exceed the minimum budget. Preserve honest
    # retrieval and ambiguity outcomes, and at least one exact read pointer.
    fallback = {"view": "discovery", "full_records": False, "results": [], "edges": [],
                "candidate_count": response["candidate_count"], "no_match": response["no_match"],
                "truncated": True,
                "notice": "Matching details exceeded this budget. Increase --max-chars or fetch the exact ref; discovery is not evidence for conclusions.",
                "read_more": [_pointer(results[0]["ref"])] if selected_count else [],
                "budget": {"max_chars": args.max_chars, "output_chars": 0,
                           "omitted_results": response["candidate_count"], "omitted_edges": edge_count}}
    if response.get("coverage_note"):
        fallback["coverage_note"] = response["coverage_note"]
    if ambiguity:
        fallback["ambiguity"] = {"detected": True, "concept_count": ambiguity["concept_count"],
                                 "candidates": [], "omitted_candidates": ambiguity["concept_count"]}
    _measure(fallback, api)
    return fallback


def _refs(values, api):
    if not isinstance(values, (tuple, list)) or not values:
        raise api.MemoryError("fetch requires at least one --ref")
    refs, seen = [], set()
    for ref in values:
        api.string(ref, "ref")
        family, separator, identity = ref.partition(":")
        if not separator or family not in {"entry", "entity", "edge", "project"}:
            raise api.MemoryError("ref must be entry:<id>, entity:<id>, edge:<id> or project:<id>")
        api.identifier(identity, "ref ID")
        if ref not in seen:
            refs.append(ref)
            seen.add(ref)
        if len(refs) > 32:
            raise api.MemoryError("fetch accepts at most 32 unique refs")
    return refs


def _probe(conn, family, identity, maximum, api):
    """Measure in SQLite before transferring unrestricted fields to Python.

    Encoded JSON size is not exact rendered size; a separate read_limit status
    honestly bounds decoding unusually huge stored arrays without loading them.
    """
    if family in {"entity", "edge"}:
        api.graph.require_v2(conn, api)
    table = {"entry": "entries", "entity": "entities", "edge": "graph_edges", "project": "projects"}[family]
    fields = {"entry": api.ENTRY_FIELDS, "entity": api.graph.ENTITY_FIELDS,
              "edge": api.graph.EDGE_FIELDS, "project": api.PROJECT_FIELDS}[family]
    arrays = {"sources", "tags", "aliases"} if family != "project" else set()
    text_fields = [name for name in fields if name not in arrays]
    expression = lambda names: " + ".join("coalesce(length(" + name + "),0)" for name in names) or "0"
    row = conn.execute("SELECT " + expression(fields) + ", " + expression(text_fields) +
                       " FROM " + table + " WHERE id = ?", (identity,)).fetchone()
    if row is None:
        return "missing", None
    if row[1] > maximum:
        return "oversize", {"minimum_record_chars": row[1]}
    read_limit = max(131072, maximum * 8)
    if row[0] > read_limit:
        return "read_limit", {"stored_chars": row[0], "read_limit_chars": read_limit}
    if family == "entry":
        record = api.entry_get(conn, identity, history=False)
    elif family == "project":
        record = dict(api.require_project(conn, identity))
    else:
        record = api.graph.get(conn, family, identity, api, history=False)
    return "found", record


def fetch(conn, args, api):
    refs = _refs(args.refs, api)
    offset = getattr(args, "offset", 0)
    if not isinstance(offset, int) or not 0 <= offset <= len(refs):
        raise api.MemoryError("offset must be between zero and the number of unique refs")
    response = {"view": "full_records", "snapshot": "one read snapshot for this call; continuation uses a new snapshot",
                "requested_count": len(refs), "offset": offset, "processed_count": 0,
                "remaining_count": len(refs) - offset, "next_offset": offset,
                "request_complete": False, "all_requested_records_returned": False,
                "results": [], "budget": {"max_chars": args.max_chars, "output_chars": 0},
                "continuation": "Repeat the original --ref list in the same order with --offset next_offset; duplicates are removed in first-occurrence order."}

    def measure(next_index, start=None):
        start = offset if start is None else start
        response.update(offset=start, processed_count=next_index - start, remaining_count=len(refs) - next_index,
                        next_offset=next_index if next_index < len(refs) else None,
                        request_complete=next_index == len(refs),
                        all_requested_records_returned=(start == 0 and next_index == len(refs) and
                            all(item["status"] == "found" for item in response["results"])))
        return _measure(response, api)

    index = offset
    while index < len(refs):
        ref = refs[index]
        placeholder = {"ref": ref, "status": "read_limit", "stored_chars": 9999999999999999999,
                       "read_limit_chars": 800000, "read_more": _pointer(ref)}
        response["results"].append(placeholder)
        fits_metadata = measure(index + 1) <= args.max_chars
        response["results"].pop()
        if not fits_metadata:
            break
        family, _, identity = ref.partition(":")
        status, value = _probe(conn, family, identity, args.max_chars, api)
        item = {"ref": ref, "status": status}
        if status == "found":
            item["record"] = value
            response["results"].append(item)
            fits = measure(index + 1) <= args.max_chars
            response["results"].pop()
            if not fits:
                saved = response["results"]
                response["results"] = [item]
                required = measure(index + 1, start=index)
                response["results"] = saved
                if required <= args.max_chars:
                    break
                item = {"ref": ref, "status": "oversize", "required_chars": required,
                        "read_more": _pointer(ref)}
        else:
            item.update(value or {})
            item["read_more"] = _pointer(ref)
        response["results"].append(item)
        index += 1
    measure(index)
    return response
