"""Read-only, bounded composition of question and code-associated knowledge."""
import copy
from types import SimpleNamespace

from . import compact
from . import brief_checks
from . import git_impact
from . import graph
from . import recall


NOTICE = ("Selection is evidence to read, not a verified answer or causal impact analysis. "
          "Excerpts may omit qualifications. Code freshness and release state have not been checked. "
          "Read full cards and authoritative sources before applying a procedure.")
EXACT = {"exact_id", "exact_jira_key", "exact_alias", "exact_concept_alias"}


def _record(conn, ref, states, api):
    if ref.startswith("edge:"):
        record = graph.get(conn, "edge", ref[5:], api, history=False)
        record.update(title=record["kind"], summary=record["description"], work_state=None)
        return record if record["knowledge_state"] in states else None
    return recall.load_node(conn, ref, states, api)


def _connections(ref, edges):
    result = []
    for edge in edges:
        if ref not in (edge["from_ref"], edge["to_ref"]):
            continue
        value = copy.deepcopy(edge)
        value["ref"] = value.pop("id")
        value["read_more"] = {"command": "edge get", "id": value["ref"][5:]}
        value["anchor_ref"] = edge["to_ref"] if edge["from_ref"] == ref else edge["from_ref"]
        family, identity = value["anchor_ref"].split(":", 1)
        value["anchor_read_more"] = {"command": family + " get", "id": identity}
        result.append(value)
    return result


def _bound(response, maximum):
    """Keep connection provenance atomically; never turn budget omission into no-match."""
    value = copy.deepcopy(response)
    total = len(value["results"]) + response.get("budget", {}).get("omitted_results", 0)
    value["budget"] = {"max_chars": maximum, "output_chars": 0, "omitted_results": 0}

    def measure():
        brief_checks.refresh_coverage(value)
        value["budget"]["omitted_results"] = total - len(value["results"])
        value["truncated"] |= bool(value["budget"]["omitted_results"] or any(
            card.get("is_excerpt") or card.get("sources_omitted") or card.get("matches_omitted") or
            card.get("connections_omitted") or card.get("selection_origins_omitted") or card.get("title_truncated") or
            card.get("project_ids_omitted") or
            any(e.get("description_is_excerpt") or e.get("sources_omitted")
                for e in card.get("connections", [])) or
            any(m.get("changed_paths_omitted") for m in card.get("matches", []))
            for card in value["results"]))
        ambiguity = value.get("ambiguity", {})
        value["truncated"] |= bool(ambiguity.get("omitted_candidates") or ambiguity.get("aliases_truncated") or
                                   any(c.get("title_truncated") or c.get("project_ids_omitted")
                                       for c in ambiguity.get("candidates", [])))
        for _ in range(8):
            size = len(compact.rendered(value))
            if size == value["budget"]["output_chars"]:
                return size
            value["budget"]["output_chars"] = size
        return len(compact.rendered(value))

    def finish():
        # After removing lower-priority cards, use remaining space to restore
        # original excerpts, prioritizing newly discovered related knowledge.
        originals = {c["ref"]: c for c in response["results"]}
        for card in sorted(value["results"], key=lambda c: "explicit_relation" not in c["selected_by"]):
            original = originals[card["ref"]]
            if len(original["summary_excerpt"]) <= len(card["summary_excerpt"]):
                continue
            prior = {key: card.get(key) for key in ("summary_excerpt", "is_excerpt", "excerpt_note")}
            for key in prior:
                if key in original:
                    card[key] = original[key]
                else:
                    card.pop(key, None)
            if measure() > maximum:
                for key, old in prior.items():
                    if old is not None:
                        card[key] = old
                    else:
                        card.pop(key, None)
        measure()
        return value

    if measure() <= maximum:
        return finish()
    value["truncated"] = True
    # Preserve retrieved knowledge before optional source-check detail. Counts
    # distinguish executed comparisons from details omitted in the final JSON.
    while measure() > maximum:
        detailed = [c for c in value["results"] if c.get("source_checks", {}).get("results")]
        if not detailed:
            break
        card = max(reversed(detailed), key=lambda c: len(c["source_checks"]["results"]))
        card["source_checks"]["results"].pop()
        card["source_checks"]["results_omitted"] += 1
    if measure() <= maximum:
        return finish()
    for length in (500, 200):
        for card in value["results"]:
            if len(card["summary_excerpt"]) > length:
                card["summary_excerpt"], _ = compact.excerpt(card["summary_excerpt"], length)
                card["is_excerpt"] = True
                card["excerpt_note"] = compact.EXCERPT_NOTE
        if measure() <= maximum:
            return finish()
    # Retain at least one whole path/source/relationship witness per origin.
    for card in value["results"]:
        for field in ("sources", "matches", "connections"):
            if len(card.get(field, [])) > 1:
                card[field + "_omitted"] = card.get(field + "_omitted", 0) + len(card[field]) - 1
                card[field] = card[field][:1]
    if measure() <= maximum:
        return finish()
    # Direct matches already have their own query/path provenance. Repeated
    # relationship objects on both endpoints must not evict related knowledge.
    for card in value["results"]:
        if "explicit_relation" not in card["selected_by"]:
            card["connections_omitted"] += len(card["connections"])
            card["connections"] = []
    if measure() <= maximum:
        return finish()
    # Removing a seed also removes neighbors for which no retained seed provides
    # a witnessed path. Direct query/code matches do not depend on a graph seed.
    def prune_connections():
        retained = {c["ref"] for c in value["results"]}
        for card in list(value["results"]):
            previous = card.get("connections", [])
            card["connections"] = [e for e in previous if e["anchor_ref"] in retained]
            card["connections_omitted"] += len(previous) - len(card["connections"])
            if card["selected_by"] == ["explicit_relation"] and not card["connections"]:
                value["results"].remove(card)
            elif "explicit_relation" in card["selected_by"] and not card["connections"]:
                card["selected_by"].remove("explicit_relation")
                card["selection_origins_omitted"] = 1
    while value["results"] and measure() > maximum:
        anchors = {e["anchor_ref"] for c in value["results"] if "explicit_relation" in c["selected_by"]
                   for e in c["connections"]}
        # Protect exact hits and the seed+neighbor groups before incidental
        # direct matches. Keep one representative of each requested channel.
        removable = []
        for index, card in enumerate(value["results"]):
            if card["ref"] in anchors or card["selected_by"] == ["explicit_relation"]:
                continue
            if EXACT.intersection(card.get("query_match_reasons", [])):
                continue
            if any(sum(origin in c["selected_by"] for c in value["results"]) <= 1
                   for origin in card["selected_by"] if origin != "explicit_relation"):
                continue
            removable.append(index)
        value["results"].pop(removable[-1] if removable else -1)
        prune_connections()
    if not value["results"] and response["results"]:
        value["read_more"] = response["results"][0]["read_more"]
    if measure() <= maximum:
        return finish()
    # Preserve the fact of ambiguity, exact endpoints and coverage at tiny budgets.
    ambiguity = value.get("ambiguity")
    if ambiguity:
        value["ambiguity"] = {"detected": True, "concept_count": ambiguity["concept_count"],
                              "omitted_candidates": ambiguity["concept_count"], "candidates": []}
    value["query"] = ""
    value["query_is_excerpt"] = True
    value["notice"] = brief_checks.NOTICE if "source_check_coverage" in value else NOTICE
    if measure() > maximum:
        raise ValueError("Brief metadata exceeds --max-chars; choose a larger budget")
    return finish()


def brief(conn, args, api, database):
    """Compose in the caller's single read-only SQLite transaction."""
    if not 1 <= args.limit <= 50:
        raise api.MemoryError("Brief limit must be between 1 and 50")
    if not 4000 <= args.max_chars <= 100000:
        raise api.MemoryError("Brief max-chars must be between 4000 and 100000")
    base = getattr(args, "base", None)
    check_sources = brief_checks.validate(args, api)
    git_options = any(getattr(args, key, None) for key in ("target", "worktree", "repo"))
    if not base and git_options and not check_sources:
        raise api.MemoryError("Brief --target, --worktree and --repo require --base or --check-sources")
    if not base and check_sources and git_options and not getattr(args, "project", None):
        raise api.MemoryError("Brief --target, --worktree and --repo with --check-sources require --project")
    if getattr(args, "target", None) and getattr(args, "worktree", False):
        raise api.MemoryError("Choose --target or --worktree")
    if base and not getattr(args, "project", None):
        raise api.MemoryError("Brief --base requires --project as repository identity")
    states = ["confirmed"]
    if getattr(args, "include_hypotheses", False):
        states.append("hypothesis")
    if getattr(args, "include_superseded", False):
        states.append("superseded")
    # With no code input preserve standalone recall's selection, including its
    # exact-key guards and ambiguity handling. Diff mode uses a bounded shortlist.
    stage_limit = min(100, max(12, args.limit * 2)) if base else args.limit
    stage_args = SimpleNamespace(**vars(args))
    stage_args.limit, stage_args.max_chars = stage_limit, 100000
    query = recall.recall(conn, stage_args, api, bounded=False)
    diff = git_impact.impact(conn, stage_args, api, database, bounded=False, states=states) if base else None
    cards, query_order, code_order, related_order = {}, [], [], []

    def ensure(ref):
        if ref not in cards:
            record = _record(conn, ref, states, api)
            if record is None:
                return None
            cards[ref] = compact.card(record, ref, summary_chars=900)
            cards[ref].update(selected_by=[], connections=[], connections_omitted=0)
            rechecks = []
            if recall.CURRENT.search(args.query):
                rechecks.append("Question asks about current status; recorded evidence must be checked against its authoritative source.")
            if not record.get("verified_at"):
                rechecks.append("No verified_at is recorded.")
            cards[ref].update(needs_recheck=bool(rechecks), recheck_reasons=rechecks)
        return cards[ref]

    for item in query["results"]:
        card = ensure(item["ref"])
        reasons = item["match_reasons"]
        direct = any(not r.startswith("graph:") for r in reasons)
        card.update(query_match_reasons=reasons, needs_recheck=item["needs_recheck"],
                    recheck_reasons=item["recheck_reasons"])
        card["selected_by"].append("query" if direct else "explicit_relation")
        card["connections"] = _connections(item["ref"], query["edges"])
        (query_order if direct else related_order).append(item["ref"])
    if diff:
        for item in diff["results"]:
            card = ensure(item["ref"])
            if card is None:
                continue
            if "code_change" not in card["selected_by"]:
                card["selected_by"].append("code_change")
            card.update(matches=item["matches"], matches_omitted=item["matches_omitted"],
                        code_match_reasons=item["match_reasons"])
            if item["ref"].startswith("edge:"):
                card.update(from_ref=item["from_ref"], to_ref=item["to_ref"])
            code_order.append(item["ref"])
    seeds = []
    for ref in code_order:
        if ref.startswith(("entry:", "entity:")):
            seeds.append({"ref": ref, "score": 100, "record": _record(conn, ref, states, api)})
        if len(seeds) == 3:
            break
    neighbors, edges, graph_truncated = recall.graph_candidates(conn, seeds, states, api)
    for item in recall.rank(neighbors.values()):
        card = ensure(item["ref"])
        if card is None:
            continue
        known = {e["ref"] for e in card["connections"]}
        card["connections"].extend(e for e in _connections(item["ref"], edges.values()) if e["ref"] not in known)
        if not card["selected_by"]:
            card["selected_by"].append("explicit_relation")
        if item["ref"] not in related_order:
            related_order.append(item["ref"])

    # Keep exact question anchors; then alternate incomparable search channels.
    direct = [ref for ref in query_order if EXACT.intersection(cards[ref]["query_match_reasons"])]
    for index in range(max(len(query_order), len(code_order))):
        for channel in (query_order, code_order):
            if index < len(channel) and channel[index] not in direct:
                direct.append(channel[index])
    related = [ref for ref in related_order if ref not in direct and cards[ref]["connections"]]
    protected = sum(bool(EXACT.intersection(cards[ref].get("query_match_reasons", []))) for ref in direct)
    room = max(0, args.limit - max(1, protected, min(2, len(direct))))
    related = related[:min(2, room)]
    chosen = direct[:args.limit - len(related)]
    related = [ref for ref in related if any(e["anchor_ref"] in chosen for e in cards[ref]["connections"])]
    # Refill slots when a relationship's seed did not survive selection.
    chosen = direct[:args.limit - len(related)] + related
    selected = set(chosen)
    for ref in chosen:
        card = cards[ref]
        previous = card["connections"]
        card["connections"] = [e for e in previous if e["anchor_ref"] in selected]
        card["connections_omitted"] = len(previous) - len(card["connections"])
        if "explicit_relation" in card["selected_by"] and not card["connections"]:
            card["selected_by"].remove("explicit_relation")
            card["selection_origins_omitted"] = 1
        if card["knowledge_state"] == "hypothesis":
            card["state_note"] = "Unconfirmed hypothesis; do not treat as a verified fact."
        elif card["knowledge_state"] == "superseded":
            card["state_note"] = "Historical superseded knowledge; not a current recommendation."
    query_complete = not (query["selection"].get("fts_candidates_truncated") or
                          query["selection"].get("graph_candidates_truncated"))
    coverage = {"query": {"candidate_count": query["candidate_count"],
                           "shortlist_count": len(query["results"]), "complete": query_complete},
                "changes": {"requested": bool(base)},
                "relations": {"code_seed_count": len(seeds), "depth": 1,
                              "code_candidates_truncated": graph_truncated}}
    if diff:
        coverage["changes"].update(matching_records=diff["matching_records"],
                                    shortlist_omitted=diff["limit_omitted"], **diff["coverage"])
    complete = query_complete and not graph_truncated and (diff is None or diff["coverage"]["complete"])
    matched = query["candidate_count"] > 0 or bool(diff and diff["matching_records"])
    response = {"action": "brief", "schema_version": 1, "query": query["query"],
                "query_is_excerpt": query["query_is_excerpt"], "project": getattr(args, "project", None),
                "knowledge_states": states, "results": [cards[ref] for ref in chosen],
                "candidate_count": len(cards), "selection_omitted": len(cards) - len(chosen),
                "coverage": coverage, "coverage_complete": complete, "no_match": complete and not matched,
                "truncated": not complete or len(cards) > len(chosen) or
                             query["candidate_count"] > len(query["results"]) or bool(diff and diff["limit_omitted"]),
                "notice": NOTICE}
    if diff:
        response["comparison"] = {key: diff["comparison"][key] for key in ("base_commit", "target_commit", "mode", "semantics")}
        response["changed_path_count"] = diff["changed_path_count"]
    if query.get("ambiguity"):
        response["ambiguity"] = query["ambiguity"]
    response = _bound(response, args.max_chars)
    if check_sources:
        response = brief_checks.annotate(conn, response, args, api, database)
        response = _bound(response, args.max_chars)
    return response
