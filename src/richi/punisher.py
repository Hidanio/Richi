"""Read-only, bounded maintenance candidates. No inference, repair, or source I/O.

The caller supplies a SQLite read transaction. A project selects entry membership
and only direct, confirmed, sourced entity -> project ``used_in`` edges. Database
integrity checks remain global and are labelled separately from scoped inspection.
"""

from collections import Counter, defaultdict
from datetime import date, datetime, timezone
import hashlib
import json
import unicodedata


RECORD_SCAN_LIMIT = 5000
EDGE_SCAN_LIMIT = 10000
GROUP_MEMBER_LIMIT = 8
INTEGRITY_MESSAGE_LIMIT = 20
LEVEL_ORDER = {"error": 0, "review": 1, "info": 2}
STATES = {"confirmed", "hypothesis", "superseded"}


def _rows(conn, sql, params=()):
    cursor = conn.execute(sql, params)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def _render(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _short(value, limit=220):
    value = str(value)
    return value if len(value) <= limit else value[:limit] + "…"


def _normal(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _valid_source(item):
    return (isinstance(item, dict) and isinstance(item.get("reference"), str)
            and bool(item["reference"].strip()))


def _source_list(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return [], False
    if not isinstance(value, list):
        return [], False
    return [item for item in value if _valid_source(item)], all(_valid_source(item) for item in value)


def _when(value):
    if not isinstance(value, str):
        raise ValueError("timestamp is not a string")
    if len(value) == 10:
        return date.fromisoformat(value)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return parsed.astimezone(timezone.utc).date()


def _card(record):
    result = {
        "ref": record["ref"], "title": _short(record.get("title", record["id"])),
        "knowledge_state": record.get("knowledge_state"),
        "project_ids": record.get("project_ids", [])[:8],
    }
    if len(str(record.get("title", ""))) > 220:
        result["title_truncated"] = True
    if len(record.get("project_ids", [])) > 8:
        result["project_ids_omitted"] = len(record["project_ids"]) - 8
    return result


def _group(code, reason, records, **details):
    records = sorted(records, key=lambda record: record["ref"])
    members = [_card(record) for record in records[:GROUP_MEMBER_LIMIT]]
    return {"code": code, "level": "review", "reason": reason,
            "refs": [member["ref"] for member in members],
            "details": dict(details, member_count=len(records), members=members,
                            omitted_members=max(0, len(records) - len(members)))}


def _fit(response, limit, maximum):
    """Fit *actual* pretty-printed CLI JSON; never truncate identifiers."""
    findings = response["findings"]
    response["candidate_count"] = len(findings)
    response["findings"] = findings[:limit]
    response["budget"] = {"max_chars": maximum, "output_chars": 0, "omitted_findings": 0}

    def measure():
        response["budget"]["omitted_findings"] = response["candidate_count"] - len(response["findings"])
        response["truncated"] = bool(
            not response["coverage"]["complete"] or response["budget"]["omitted_findings"]
            or response["health"]["integrity_messages_truncated"]
            or response["health"]["foreign_keys"]["samples_omitted"]
            or response.get("finding_counts_omitted", False)
            or any(item.get("details", {}).get("omitted_members")
                   or item.get("details", {}).get("alias_truncated") for item in response["findings"])
            or any(member.get("title_truncated") or member.get("project_ids_omitted")
                   for item in response["findings"] for member in item.get("details", {}).get("members", []))
        )
        for _ in range(8):
            size = len(_render(response))
            if response["budget"]["output_chars"] == size:
                return size
            response["budget"]["output_chars"] = size
        return len(_render(response))

    while response["findings"] and measure() > maximum:
        response["findings"].pop()
    # Diagnostic samples are optional; the full violation count is retained.
    while response["health"]["foreign_keys"]["samples"] and measure() > maximum:
        response["health"]["foreign_keys"]["samples"].pop()
        response["health"]["foreign_keys"]["samples_omitted"] += 1
    if measure() > maximum:
        response.pop("notes", None)
    if measure() > maximum:
        response["health"]["integrity_messages_truncated"] = True
        response["health"]["integrity"] = []
    if measure() > maximum:
        response["finding_counts_omitted"] = True
        response["finding_counts"] = {}
    if measure() > maximum:
        # Extremely small budget plus unusually long project ID. Counts and
        # scope/coverage remain; only descriptive, nonessential metadata goes.
        response["coverage"].pop("scope_rule", None)
        response["coverage"].pop("source_content_checked", None)
    size = measure()
    if size > maximum:
        raise ValueError("Punisher metadata could not fit max_chars; increase the budget")
    return response


def punisher(conn, args, api):
    """Return maintenance diagnostics; args: project, limit, max_chars, stale_days, as_of."""
    limit = getattr(args, "limit", 20)
    maximum = getattr(args, "max_chars", 16000)
    stale_days = getattr(args, "stale_days", 180)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise api.MemoryError("Punisher limit must be between 1 and 200")
    if not isinstance(maximum, int) or isinstance(maximum, bool) or not 2000 <= maximum <= 100000:
        raise api.MemoryError("max_chars must be between 2000 and 100000")
    if not isinstance(stale_days, int) or isinstance(stale_days, bool) or not 1 <= stale_days <= 36500:
        raise api.MemoryError("stale_days must be between 1 and 36500")
    raw_as_of = getattr(args, "as_of", None)
    try:
        as_of = date.fromisoformat(raw_as_of) if raw_as_of is not None else datetime.now(timezone.utc).date()
        if raw_as_of is not None and as_of.isoformat() != raw_as_of:
            raise ValueError("non-canonical date")
    except (TypeError, ValueError):
        raise api.MemoryError("as_of must be an ISO date (YYYY-MM-DD)")
    project = getattr(args, "project", None)
    if project is not None:
        project = api.identifier(project, "project")
        api.require_project(conn, project)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version not in (1, 2):
        raise api.MemoryError("Punisher supports schema 1 and 2; no migration was attempted")

    tables = ("projects", "entries", "relations") + (("entities", "graph_edges") if version == 2 else ())
    totals = {}
    for table in tables:
        # All interpolated table names are constants from the tuple above.
        totals[table] = conn.execute("SELECT\n    count(*)\nFROM " + table + "\n;").fetchone()[0]
    integrity = [row[0] for row in conn.execute("PRAGMA integrity_check(%d)" % INTEGRITY_MESSAGE_LIMIT)]
    integrity_ok = integrity == ["ok"]
    foreign_count, foreign_samples = 0, []
    for row in conn.execute("PRAGMA foreign_key_check"):
        foreign_count += 1
        if len(foreign_samples) < 3:
            foreign_samples.append({"table": row[0], "rowid": row[1], "parent": row[2], "constraint": row[3]})

    findings = []

    def add(code, level, reason, ref=None, **details):
        value = {"code": code, "level": level, "reason": reason, "refs": [ref] if ref else []}
        if details:
            value["details"] = details
        findings.append(value)

    if not integrity_ok:
        add("sqlite_integrity", "error", "SQLite integrity_check reported a database inconsistency.")
    if foreign_count:
        add("foreign_key_violation", "error", "Database-wide foreign key violations were found.", count=foreign_count)

    raw_edges = _rows(conn, """
SELECT
    *
FROM graph_edges
ORDER BY id
LIMIT ?
;""", (EDGE_SCAN_LIMIT,)) if version == 2 else []
    entities = _rows(conn, """
SELECT
    *
FROM entities
ORDER BY id
LIMIT ?
;""", (RECORD_SCAN_LIMIT,)) if version == 2 else []
    entries_sql = """
SELECT
    e.*
FROM entries e
WHERE 1=1
"""
    params = []
    if project is not None:
        entries_sql += """    AND EXISTS (
        SELECT
            1
        FROM entry_projects ep
        WHERE 1=1
            AND ep.entry_id = e.id
            AND ep.project_id = ?
    )
"""
        params.append(project)
    entries_sql += "ORDER BY e.id\nLIMIT ?\n;"
    entries = _rows(conn, entries_sql, (*params, RECORD_SCAN_LIMIT))
    entry_total = conn.execute("""
SELECT
    count(*)
FROM entries e
WHERE 1=1
    AND EXISTS (
        SELECT
            1
        FROM entry_projects ep
        WHERE 1=1
            AND ep.entry_id = e.id
            AND ep.project_id = ?
    )
;""", (project,)).fetchone()[0] if project is not None else totals["entries"]
    raw_relations = _rows(conn, """
SELECT
    *
FROM relations
WHERE 1=1
    AND (? IS NULL OR from_project = ? OR to_project = ?)
ORDER BY id
LIMIT ?
;""", (project, project, project, RECORD_SCAN_LIMIT))
    relation_total = conn.execute("""
SELECT
    count(*)
FROM relations
WHERE 1=1
    AND (? IS NULL OR from_project = ? OR to_project = ?)
;""", (project, project, project)).fetchone()[0]

    # Sources are parsed before using edges for scope. Malformed/empty evidence
    # cannot silently turn a related entity into this project's own concept.
    scopes = defaultdict(set)
    for edge in raw_edges:
        valid_sources, sources_well_formed = _source_list(edge["sources"])
        if (edge["kind"] == "used_in" and edge["knowledge_state"] == "confirmed"
                and valid_sources and sources_well_formed and edge["from_ref"].startswith("entity:")
                and edge["to_ref"].startswith("project:")):
            scopes[edge["from_ref"]].add(edge["to_ref"][len("project:"):])
    scoped_entities = [record for record in entities if project is None or project in scopes["entity:" + record["id"]]]
    records = []
    for family, members in (("entry", entries), ("entity", scoped_entities)):
        for record in members:
            record["ref"] = family + ":" + record["id"]
            record["family"] = family
            if family == "entity":
                record["project_ids"] = sorted(scopes[record["ref"]])
            else:
                record["project_ids"] = [row[0] for row in conn.execute("""
SELECT
    project_id
FROM entry_projects
WHERE 1=1
    AND entry_id = ?
ORDER BY project_id
;""", (record["id"],))]
            records.append(record)
    selected = {record["ref"] for record in records}
    if project is not None:
        selected.add("project:" + project)
    scoped_edges = [edge for edge in raw_edges if project is None or edge["from_ref"] in selected or edge["to_ref"] in selected]

    all_scanned = {}
    for family, members in (("entry", entries), ("entity", entities)):
        for record in members:
            all_scanned[family + ":" + record["id"]] = record
    knowledge_counts = Counter(record["knowledge_state"] for record in records)
    exact_content = defaultdict(list)
    aliases = defaultdict(dict)

    def inspect_source_and_dates(record, ref, is_relation=False):
        state = record.get("knowledge_state")
        if state not in STATES:
            add("invalid_knowledge_state", "error", "Record has an unsupported knowledge state.", ref, state=_short(state))
        raw = record.get("sources", "[]")
        if is_relation:
            source = record.get("source")
            good_sources = [{"reference": source}] if isinstance(source, str) and source.strip() else []
            well_formed = source is None or isinstance(source, str)
        else:
            good_sources, well_formed = _source_list(raw)
        if not well_formed:
            add("malformed_sources", "error", "Sources are not a valid array of reference objects.", ref)
        git_indices = []
        for index, source in enumerate(good_sources if well_formed else [], 1):
            if "git" not in source:
                continue
            from . import git_evidence
            try:
                git_evidence.validate_anchor(source)
                git_indices.append(index)
            except ValueError as exc:
                add("invalid_git_anchor", "error", "Git source metadata is invalid; no repository was read.",
                    ref, source_index=index, error=_short(exc))
        if git_indices:
            add("git_evidence_available", "info", "Captured Git evidence can be checked explicitly; this is not a stale-knowledge finding.",
                ref, source_indices=git_indices, source_content_checked=False,
                check_arguments=["sources", "git-check", "--ref", ref])
        if not good_sources:
            add("missing_sources", "error" if state == "confirmed" else "info",
                "Confirmed knowledge lacks a source reference." if state == "confirmed"
                else "No source is recorded; this state does not assert confirmed knowledge.", ref,
                knowledge_state=state)
        verified = record.get("verified_at")
        if not verified:
            if state == "confirmed":
                add("verification_date_missing", "review", "No verification date is recorded; relevance needs a contextual check.", ref)
            return
        try:
            verified_date = _when(verified)
        except (TypeError, ValueError, OverflowError):
            add("invalid_verification_date", "error", "verified_at is not a valid ISO date or timezone-aware timestamp.", ref,
                verified_at=_short(verified))
            return
        age = (as_of - verified_date).days
        if age < 0:
            add("verification_after_as_of", "review", "Verification is later than the report date; check chronology or as_of.", ref,
                verified_at=verified, age_days=age)
        elif state == "confirmed" and age >= stale_days:
            add("verification_age", "review", "Recorded verification is old; age alone does not invalidate this knowledge.", ref,
                verified_at=verified, age_days=age)

    for record in records:
        ref = record["ref"]
        inspect_source_and_dates(record, ref)
        if record["knowledge_state"] != "confirmed":
            add("nonconfirmed_knowledge", "info", "This state is intentional context, not a defect or deletion candidate.", ref,
                knowledge_state=record["knowledge_state"])
        if version == 2 and not conn.execute("""
SELECT
    1
FROM graph_nodes
WHERE 1=1
    AND ref = ?
;""", (ref,)).fetchone():
            add("graph_node_missing", "error", "The record has no graph registry node.", ref)
        summary = record.get("summary")
        if isinstance(summary, str) and summary.strip():
            # The complete exact string is the key: titles, whitespace, and
            # scopes are not normalized into an assumed semantic duplicate.
            exact_content[(record["family"], record["kind"], summary)].append(record)
        try:
            names = json.loads(record["aliases"])
        except (TypeError, ValueError):
            names = None
        if not isinstance(names, list) or any(not isinstance(name, str) or not name.strip() for name in names):
            add("malformed_aliases", "error", "Aliases are not a valid list of nonempty strings.", ref)
        else:
            for name in names:
                aliases[_normal(name)][ref] = record
        if record["family"] == "entity" and not conn.execute("""
SELECT
    1
FROM graph_edges
WHERE 1=1
    AND (from_ref = ? OR to_ref = ?)
LIMIT 1
;""", (ref, ref)).fetchone():
            add("orphan_entity", "review", "No explicit incoming or outgoing edge exists; an isolated concept may still be useful.", ref)

    for (family, kind, summary), members in exact_content.items():
        if len(members) < 2:
            continue
        findings.append(_group("exact_content_candidate",
                               "Current summaries match exactly within one family and kind; review scope and evidence before any consolidation.",
                               members, family=family, kind=kind,
                               summary_sha256=hashlib.sha256(summary.encode("utf-8")).hexdigest(),
                               mixed_states=len({member["knowledge_state"] for member in members}) > 1))
    for alias, members in sorted(aliases.items()):
        if len(members) < 2:
            continue
        group = _group("shared_alias", "Several records use this alias. This is ambiguity, not duplicate evidence.",
                       members.values(), alias=_short(alias), alias_truncated=len(alias) > 220)
        group["level"] = "info"
        findings.append(group)

    for edge in scoped_edges:
        ref = "edge:" + edge["id"]
        inspect_source_and_dates(edge, ref)
        if edge["knowledge_state"] == "confirmed":
            # An incident edge can cross project scope or the record scan cap.
            # Read endpoint *state* directly; do not inspect its full content or
            # silently treat a skipped endpoint as confirmed.
            for endpoint in (edge["from_ref"], edge["to_ref"]):
                if endpoint in all_scanned:
                    continue
                family, _, identity = endpoint.partition(":")
                table = {"entry": "entries", "entity": "entities"}.get(family)
                if table:
                    row = conn.execute("SELECT\n    knowledge_state\nFROM " + table + "\nWHERE 1=1\n    AND id = ?\n;",
                                       (identity,)).fetchone()
                    if row:
                        all_scanned[endpoint] = {"knowledge_state": row[0]}
            endpoints = [endpoint for endpoint in (edge["from_ref"], edge["to_ref"])
                         if endpoint in all_scanned and all_scanned[endpoint]["knowledge_state"] != "confirmed"]
            if endpoints:
                add("confirmed_edge_nonconfirmed_endpoint", "review",
                    "A confirmed edge touches nonconfirmed knowledge; default confirmed-only traversal may omit it. This is not proof the edge is wrong.",
                    ref, endpoints=endpoints)
    for relation in raw_relations:
        inspect_source_and_dates(relation, "relation:" + relation["id"], is_relation=True)

    complete = (len(entries) == entry_total and len(entities) == totals.get("entities", 0)
                and len(raw_edges) == totals.get("graph_edges", 0) and len(raw_relations) == relation_total)
    findings.sort(key=lambda item: (LEVEL_ORDER[item["level"]], item["code"], item["refs"]))
    counts = dict(sorted(Counter(item["code"] for item in findings).items()))
    response = {
        "schema_version": version, "as_of": as_of.isoformat(), "project": project, "stale_days": stale_days,
        "health": {
            "scope": "database", "status": "ok" if integrity_ok and not foreign_count else "error",
            "integrity": [_short(message, 300) for message in integrity],
            "integrity_messages_truncated": (len(integrity) >= INTEGRITY_MESSAGE_LIMIT
                                             or any(len(message) > 300 for message in integrity)),
            "foreign_keys": {"count": foreign_count, "samples": foreign_samples,
                             "samples_omitted": foreign_count - len(foreign_samples)},
            "counts": totals,
        },
        "coverage": {
            "complete": complete, "scope_rule": "entry membership; entity direct confirmed sourced used_in; incident explicit edges",
            "scanned": {"entries": len(entries), "entities": len(entities), "edges": len(raw_edges), "relations": len(raw_relations)},
            "eligible_entries": entry_total,
            "inspected": {"entries": len(entries), "entities": len(scoped_entities), "edges": len(scoped_edges), "relations": len(raw_relations)},
            "limits": {"records_per_table": RECORD_SCAN_LIMIT, "edges": EDGE_SCAN_LIMIT},
            "source_content_checked": False,
        },
        "knowledge_states": dict(sorted(knowledge_counts.items())), "finding_counts": counts, "findings": findings,
        "notes": ["Review/info findings are candidates, not defects. No records were changed.",
                  "Integrity is database-wide; maintenance candidates use the stated scope. Source paths/content were not checked."],
    }
    return _fit(response, limit, maximum)
