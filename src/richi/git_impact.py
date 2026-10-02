"""Bounded, read-only file-change associations with saved knowledge.

This pilot compares explicit endpoints. It does not infer a merge base, causal
impact, invalid knowledge, or release state. Renames are represented as D/A so
both recorded paths remain discoverable without similarity heuristics.
"""
import copy
import json
import time

from . import compact
from . import git_evidence
from . import git_legacy
from . import git_sources


MAX_PATHS = 200
MAX_CHANGE_OUTPUT = 1024 * 1024
MAX_RECORDS = 5000
MAX_RECORD_SOURCES = 128 * 1024
MAX_SOURCE_CHARS = 8 * 1024 * 1024
MAX_LEGACY_RESOLUTIONS = 20
MAX_REPO_PROBES = 100
MAX_SECONDS = 30
MAX_ERROR_DETAILS = 40
MAX_MATCH_DETAIL_CHARS = 65536
MAX_MATCH_PATH_CHARS = 16384
NOTICE = ("Direct stored file associations only; no causal impact or stale-state inference. "
          "All knowledge states are retained. No graph expansion, fetch, checkout, or knowledge updates.")


def _repository(conn, args, api):
    project = api.require_project(conn, args.project)
    if not project.get("repo_path"):
        raise api.MemoryError("Project has no registered repository")
    registered = git_evidence.identify(project["repo_path"])
    if getattr(args, "repo", None):
        selected = git_evidence.identify(args.repo)
        if selected["common_dir"] != registered["common_dir"]:
            raise api.MemoryError("--repo must be a linked worktree, not a separate clone")
        return selected
    return registered


def _changes(repo, base, target, worktree):
    if worktree:
        # Git can execute attribute clean/process drivers during a working-tree
        # diff, even with external diff/textconv disabled. Fail closed before
        # reading content through Git; do not execute repository programs.
        filters = git_evidence._git(repo, ["config", "--null", "--get-regexp",
                                          r"^filter\..*\.(clean|process)$"],
                                    allowed=(0, 1), maximum=65536)[0]
        if any(item.partition(b"\n")[2].strip() for item in filters.split(b"\0") if item):
            raise git_evidence.GitEvidenceError(
                "Worktree impact is unavailable while Git clean/process filters are configured; "
                "use an explicit committed --target. No filter was executed.")
    args = ["diff", "--name-status", "-z", "--no-renames", "--no-ext-diff",
            "--no-textconv", "--ignore-submodules=none", base]
    if not worktree:
        args.append(target)
    raw = git_evidence._git(repo, args + ["--"], maximum=MAX_CHANGE_OUTPUT)[0]
    fields = raw.split(b"\0")
    if fields[-1] == b"":
        fields.pop()
    if len(fields) % 2:
        raise git_evidence.GitEvidenceError("Unexpected changed-path output")
    found, invalid = {}, 0
    for offset in range(0, len(fields), 2):
        status = git_evidence._decode(fields[offset])
        if status not in {"A", "D", "M", "T", "U", "X", "B"}:
            raise git_evidence.GitEvidenceError("Unexpected change status")
        try:
            path = git_evidence._path(git_evidence._decode(fields[offset + 1]))
        except git_evidence.GitEvidenceError:
            invalid += 1
            continue
        found[path] = {"path": path, "status": status}
    if worktree:
        raw = git_evidence._git(repo, ["ls-files", "--others", "--exclude-standard", "-z"],
                                maximum=MAX_CHANGE_OUTPUT)[0]
        for item in raw.split(b"\0"):
            if not item:
                continue
            try:
                path = git_evidence._path(git_evidence._decode(item))
            except git_evidence.GitEvidenceError:
                invalid += 1
                continue
            # A staged removal followed by a new untracked file can have both
            # observations; never overwrite that evidence with a single status.
            if path in found:
                found[path]["also_untracked"] = True
            else:
                found[path] = {"path": path, "status": "?"}
    ordered = [found[path] for path in sorted(found)]
    return ordered[:MAX_PATHS], len(ordered), invalid


def _inventory(conn, coverage, deadline):
    tables = [("entry", "entries", "summary", "title", "work_state", "NULL", "NULL")]
    if conn.execute("PRAGMA user_version").fetchone()[0] == 2:
        tables += [("entity", "entities", "summary", "title", "NULL", "NULL", "NULL"),
                   ("edge", "graph_edges", "description", "kind", "NULL", "from_ref", "to_ref")]
    for family, table, summary, title, work, origin, destination in tables:
        remaining = MAX_RECORDS - coverage["records_scanned"]
        rows = conn.execute(f"""
SELECT
    id
    , kind
    , substr({title}, 1, 301) AS title
    , substr({summary}, 1, 6001) AS summary
    , length({summary}) AS summary_length
    , {work} AS work_state
    , {origin} AS from_ref
    , {destination} AS to_ref
    , knowledge_state
    , verified_at
    , updated_at
    , substr(sources, 1, ?) AS sources
FROM {table}
ORDER BY
    id
LIMIT ?
;
""",
            (MAX_RECORD_SOURCES + 1, remaining + 1))
        for index, row in enumerate(rows):
            if index >= remaining or coverage["source_chars_scanned"] >= MAX_SOURCE_CHARS:
                coverage["inventory_complete"] = False
                return
            if time.monotonic() >= deadline:
                coverage["time_limit_reached"] = True
                coverage["inventory_complete"] = False
                return
            record = dict(row)
            coverage["records_scanned"] += 1
            coverage["source_chars_scanned"] += len(record["sources"])
            if len(record["sources"]) > MAX_RECORD_SOURCES:
                coverage["oversized_records"] += 1
                coverage["inventory_complete"] = False
                continue
            record["sources"] = json.loads(record["sources"])
            if family == "entry":
                record["project_ids"] = [r[0] for r in conn.execute("""
SELECT
    project_id
FROM entry_projects
WHERE 1=1
    AND entry_id = ?
ORDER BY
    project_id
;
""", (record["id"],))]
            yield family, record


def _match_rank(match, base, target):
    direct = match["match_reason"] != "legacy_commit_changed_file"
    at_target = target is not None and match["captured_commit"] == target
    at_base = match["captured_commit"] == base
    return (-(4 * direct + 8 * at_target + 2 * at_base), match["source_index"])


def _record_rank(record, matches, base, target, ref):
    direct_paths = {path for match in matches if match["match_reason"] != "legacy_commit_changed_file"
                    for path in match["changed_paths"]}
    at_target = any(target is not None and match["captured_commit"] == target for match in matches)
    at_base = any(match["captured_commit"] == base for match in matches)
    return (record["knowledge_state"] != "confirmed", not bool(direct_paths), not at_target,
            not at_base, -len(direct_paths), ref)


def _card(family, record, matches, match_count, base, target):
    summary, partial = compact.excerpt(record["summary"], 800)
    partial = partial or record["summary_length"] > len(record["summary"])
    card = {"ref": family + ":" + record["id"], "title": record["title"],
            "kind": record["kind"], "summary_excerpt": summary, "is_excerpt": partial,
            "knowledge_state": record["knowledge_state"], "work_state": record["work_state"],
            "verified_at": record["verified_at"], "updated_at": record["updated_at"],
            "matches": matches, "matches_omitted": match_count - len(matches),
            "read_more": {"command": family + " get", "id": record["id"]}}
    direct = any(match["match_reason"] != "legacy_commit_changed_file" for match in matches)
    at_target = any(target is not None and match["captured_commit"] == target for match in matches)
    card["match_reasons"] = (["direct_file_source"] if direct else ["commit_changed_file_source"])
    if at_target:
        card["match_reasons"].append("source_at_target_commit")
    card["_rank"] = _record_rank(record, matches, base, target, card["ref"])
    if partial:
        card["excerpt_note"] = compact.EXCERPT_NOTE
    if len(record["title"]) > 300:
        card["title_truncated"] = True
    if record["knowledge_state"] == "superseded":
        card["state_note"] = "Historical superseded knowledge; not the current recommendation."
    elif record["knowledge_state"] == "hypothesis":
        card["state_note"] = "Unconfirmed hypothesis; association does not confirm the claim."
    if family == "edge":
        card.update(from_ref=record["from_ref"], to_ref=record["to_ref"])
    return card


def _bound(response, maximum):
    value = copy.deepcopy(response)
    value["budget"] = {"max_chars": maximum, "output_chars": 0, "omitted_results": 0,
                       "omitted_changes": 0, "omitted_unmatched_paths": 0, "omitted_errors": 0}
    budget = value["budget"]

    def fits():
        for _ in range(8):
            size = len(git_sources._render(value))
            if budget["output_chars"] == size:
                return size <= maximum
            budget["output_chars"] = size
        return len(git_sources._render(value)) <= maximum

    if fits():
        return value
    value["truncated"] = True
    # Error/unsupported counts and unmatched_path_count remain available after
    # diagnostic details are removed. Preserve knowledge cards ahead of them.
    for key in ("errors", "unmatched_paths"):
        budget["omitted_" + key] = len(value[key])
        value[key] = []
        if fits():
            return value
    for summary_limit in (300, 120):
        for card in value["results"]:
            if len(card["summary_excerpt"]) > summary_limit:
                card["summary_excerpt"], _ = compact.excerpt(card["summary_excerpt"], summary_limit)
                card["is_excerpt"] = True
                card["excerpt_note"] = compact.EXCERPT_NOTE
        if fits():
            return value
    # Preserve breadth first, then use remaining room for stronger secondary
    # evidence. Extra sources on one card must not crowd out another decision.
    original_matches = {card["ref"]: (copy.deepcopy(card["matches"]), card["matches_omitted"])
                        for card in value["results"]}
    for card in value["results"]:
        card["matches_omitted"] += len(card["matches"]) - 1
        del card["matches"][1:]
    if not fits():
        for card in value["results"]:
            match = card["matches"][0]
            match["changed_paths_omitted"] += max(0, len(match["changed_paths"]) - 8)
            del match["changed_paths"][8:]
    # Optional changed-path detail is cheaper to reconstruct than an omitted
    # knowledge card. Its total count and the matching paths remain explicit.
    if not fits() and value["changes"]:
        budget["omitted_changes"] += len(value["changes"])
        value["changes"] = []
    # Keep endpoint identities, coverage, counts, and the no-match distinction.
    # Remove lowest-ranked cards, never a fragment of a source reference.
    while True:
        if fits():
            break
        key = "results" if value["results"] else "changes" if value["changes"] else None
        if key is not None:
            value[key].pop()
            budget["omitted_" + key] += 1
            continue
        # Very long user-controlled paths/refs can make even the envelope too
        # large. Return a truthful minimal envelope, retaining full commit IDs.
        value.pop("notice", None)
        value["comparison"].pop("repo_root", None)
        value["comparison"].pop("common_dir", None)
        value["comparison"].pop("base_ref", None)
        value["comparison"].pop("target_ref", None)
        if len(git_sources._render(value)) > maximum:
            raise ValueError("Impact metadata exceeds --max-chars; choose a larger budget")

    # Round-robin additions keep all retained records represented. The bounded
    # attempt count prevents expensive quadratic packing for pathological data.
    attempts = 0
    for ordinal in range(1, 100):
        attempted = False
        for card in value["results"]:
            originals, already_omitted = original_matches[card["ref"]]
            if ordinal >= len(originals) or attempts >= 200:
                continue
            attempted = True
            attempts += 1
            candidate = copy.deepcopy(originals[ordinal])
            card["matches"].append(candidate)
            card["matches_omitted"] = already_omitted + len(originals) - len(card["matches"])
            if not fits():
                candidate["changed_paths_omitted"] += max(0, len(candidate["changed_paths"]) - 8)
                del candidate["changed_paths"][8:]
                if not fits():
                    card["matches"].pop()
                    card["matches_omitted"] += 1
        if not attempted or attempts >= 200:
            break
    # Source/path additions can change the output_chars digit count after a
    # rejected attempt. Recompute the exact printed size of the final response.
    fits()
    return value


def impact(conn, args, api, database, *, bounded=True, states=None):
    """Called inside the CLI's read-only SQLite transaction."""
    limit, maximum = getattr(args, "limit", 12), getattr(args, "max_chars", 16000)
    if not 1 <= limit <= 200 or not 2000 <= maximum <= 100000:
        raise api.MemoryError("Impact requires limit 1–200 and max-chars 2000–100000")
    started = time.monotonic()
    deadline = started + MAX_SECONDS
    repo = _repository(conn, args, api)
    base = git_evidence._resolve(repo["root"], args.base)
    worktree = bool(getattr(args, "worktree", False))
    target_ref = None if worktree else (getattr(args, "target", None) or "HEAD")
    target = None if worktree else git_evidence._resolve(repo["root"], target_ref)
    changes, path_count, invalid_paths = _changes(repo["root"], base, target, worktree)
    paths = {item["path"] for item in changes}
    coverage = {"inventory_complete": True, "records_scanned": 0, "source_chars_scanned": 0,
                "oversized_records": 0, "changed_paths_omitted": path_count - len(changes),
                "invalid_paths": invalid_paths, "legacy_resolutions": 0, "repository_probes": 0,
                "legacy_skipped": 0, "legacy_files_omitted": 0, "time_limit_reached": False,
                "graph_expanded": False, "source_errors": 0, "error_details_omitted": 0,
                "unscoped_legacy_sources": 0, "unsupported_legacy_sources": 0}
    results, errors, matched = [], [], set()
    matching_records = 0
    resolutions, files_cache = {}, {}
    for family, record in (_inventory(conn, coverage, deadline) if paths else ()):
        if states is not None and record["knowledge_state"] not in states:
            continue
        ref, matches, match_count, match_chars = family + ":" + record["id"], [], 0, 0
        for index, source in enumerate(record["sources"], 1):
            anchor = source.get("git")
            mode, source_paths, commit = "structured", set(), None
            try:
                if anchor is not None:
                    if not isinstance(anchor, dict) or anchor.get("repo_id") != args.project:
                        continue
                    git_evidence.validate_anchor(source)
                    source_paths, commit = {anchor["path"]}, anchor["commit"]
                elif isinstance(source.get("reference"), str) and source["reference"].startswith("git:"):
                    try:
                        parsed = git_legacy.parse(source)
                    except ValueError:
                        parsed = None
                    # A valid non-overlapping path needs no repository scope.
                    # A malformed reference is only reported after scope check.
                    if parsed is not None and parsed[1] is not None and parsed[1] not in paths:
                        continue
                    try:
                        scope = tuple(git_legacy.project_ids(conn, record, ref, api))
                    except git_legacy.LegacyGitError as exc:
                        if exc.code == "legacy_repository_unavailable" and "no explicit project scope" in str(exc):
                            coverage["unscoped_legacy_sources"] += 1
                            continue
                        raise
                    if args.project not in scope:
                        continue
                    if parsed is None:
                        coverage["unsupported_legacy_sources"] += 1
                        continue
                    commit, path = parsed
                    key = (scope, commit)
                    if key not in resolutions:
                        if (coverage["legacy_resolutions"] >= MAX_LEGACY_RESOLUTIONS or
                                coverage["repository_probes"] + len(scope) > MAX_REPO_PROBES or
                                time.monotonic() >= deadline):
                            coverage["legacy_skipped"] += 1
                            coverage["time_limit_reached"] |= time.monotonic() >= deadline
                            continue
                        coverage["legacy_resolutions"] += 1
                        coverage["repository_probes"] += len(scope)
                        try:
                            resolutions[key] = git_legacy.resolve(conn, record, ref, source, api,
                                                                  deadline=deadline)
                        except (ValueError, OSError) as exc:
                            resolutions[key] = exc
                    resolved = resolutions[key]
                    if isinstance(resolved, Exception):
                        raise resolved
                    if resolved["repo_id"] != args.project:
                        continue
                    if path is None:
                        mode = "legacy_commit_changed_file"
                        file_key = (resolved["repo_id"], commit)
                        if file_key not in files_cache:
                            if time.monotonic() >= deadline:
                                coverage["legacy_skipped"] += 1
                                coverage["time_limit_reached"] = True
                                continue
                            files_cache[file_key] = git_legacy.commit_files(resolved["repo"], commit)
                            coverage["legacy_files_omitted"] += files_cache[file_key]["files_omitted"]
                        source_paths = {item["path"] for item in files_cache[file_key]["files"]}
                    else:
                        mode, source_paths = "legacy_file", {path}
                else:
                    continue
            except (ValueError, OSError) as exc:
                if isinstance(exc, TimeoutError):
                    coverage["time_limit_reached"] = True
                coverage["source_errors"] += 1
                if len(errors) < MAX_ERROR_DETAILS:
                    errors.append({"ref": ref, "source_index": index,
                                   "reason": getattr(exc, "code", "source_unavailable"), "error": str(exc)[:300]})
                else:
                    coverage["error_details_omitted"] += 1
                continue
            overlap = sorted(paths & source_paths)
            if overlap:
                matched.update(overlap)
                match_count += 1
                displayed_paths, path_chars = [], 0
                for path in overlap:
                    if displayed_paths and path_chars + len(path) > MAX_MATCH_PATH_CHARS:
                        break
                    displayed_paths.append(path)
                    path_chars += len(path)
                match = {"source_index": index, "reference": source["reference"],
                                    "repo_id": args.project, "captured_commit": commit,
                                    "observation_mode": anchor["mode"] if anchor else "commit",
                                    "captured_dirty": anchor["dirty"] if anchor else False,
                                    "changed_paths": displayed_paths,
                                    "changed_paths_omitted": len(overlap) - len(displayed_paths),
                                    "match_reason": mode}
                match_size = len(git_sources._render(match))
                matches.append(match)
                match_chars += match_size
                matches.sort(key=lambda item: _match_rank(item, base, target))
                while match_chars > MAX_MATCH_DETAIL_CHARS and len(matches) > 1:
                    match_chars -= len(git_sources._render(matches.pop()))
        if match_count:
            matching_records += 1
            rank = lambda card: card["_rank"]
            key = _record_rank(record, matches, base, target, ref)
            if len(results) < limit or key < rank(results[-1]):
                results.append(_card(family, record, matches, match_count, base, target))
                results.sort(key=rank)
                del results[limit:]
    complete = (coverage["inventory_complete"] and not coverage["source_errors"] and not any(coverage[key] for key in
                ("oversized_records", "changed_paths_omitted", "invalid_paths", "legacy_skipped",
                 "legacy_files_omitted", "time_limit_reached", "unscoped_legacy_sources",
                 "unsupported_legacy_sources")))
    for card in results:
        card.pop("_rank")
    coverage["complete"] = complete
    coverage["limits"] = {"paths": MAX_PATHS, "records": MAX_RECORDS,
                          "source_chars": MAX_SOURCE_CHARS, "legacy_resolutions": MAX_LEGACY_RESOLUTIONS,
                          "repository_probes": MAX_REPO_PROBES, "cooperative_seconds": MAX_SECONDS}
    response = {"action": "impact", "project": args.project,
                "comparison": {"base_ref": args.base, "base_commit": base,
                               "target_ref": target_ref, "target_commit": target,
                               "mode": "worktree" if worktree else "commits",
                               "repo_root": repo["root"], "common_dir": repo["common_dir"],
                               "semantics": "Endpoint diff; renames are deletion/addition. " +
                               ("Worktree is mutable; tracked and untracked reads are sequential; ignored files excluded."
                                if worktree else "Both endpoint commits were resolved before diff.")},
                "changed_path_count": path_count, "changes": changes,
                "matching_records": matching_records, "limit_omitted": matching_records - len(results),
                "results": results, "unmatched_paths": sorted(paths - matched),
                "unmatched_path_count": len(paths - matched),
                "no_matches": complete and not matching_records, "coverage": coverage, "errors": errors,
                "truncated": (not complete or matching_records > len(results) or any(
                    card["matches_omitted"] or any(match["changed_paths_omitted"] for match in card["matches"])
                    for card in results)), "notice": NOTICE}
    return _bound(response, maximum) if bounded else response
