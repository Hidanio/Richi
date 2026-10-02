"""Registered-repository Git evidence workflows; no checkout, fetch, or truth updates.

The SQLite database remains canonical for knowledge. Git stores committed code;
content-addressed local artifacts preserve explicitly captured working-tree bytes.
"""
import copy
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

from . import git_evidence
from . import graph


MAX_RECORDS = 5000
MAX_REFS = 200
MAX_SELECTED = 500
MAX_SOURCE_JSON = 128 * 1024
MAX_RECORD_SOURCES = 1024 * 1024
MAX_SCAN_SOURCE_CHARS = 8 * 1024 * 1024
NOTICE = ("Git checks compare captured bytes with the selected local revision. "
          "Changed code is a review candidate, not a disproved fact; unchanged code "
          "does not establish dependency or production freshness. No verified_at fields were updated.")


def _render(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _artifacts(database):
    return Path(database).expanduser().resolve().parent / "artifacts" / "git"


def _bound(value, maximum=16000):
    """Bound actual JSON, with exact accounting and explicit omitted payloads."""
    if not 2000 <= maximum <= 100000:
        raise ValueError("max-chars must be between 2000 and 100000")
    result = copy.deepcopy(value)
    budget = result["budget"] = {"max_chars": maximum, "output_chars": 0,
                                  "truncated": False, "items_omitted": 0,
                                  "content_chars_omitted": 0}
    while True:
        actual = len(_render(result))
        if actual <= maximum:
            if budget["output_chars"] == actual:
                return result
            budget["output_chars"] = actual
            continue
        budget["truncated"] = True
        # Long source contents/diffs are prefixes, never semantic summaries.
        strings = [(obj, key) for obj in (result, result.get("result", {}))
                   if isinstance(obj, dict) for key in ("content", "diff", "patch")
                   if isinstance(obj.get(key), str) and obj[key]]
        if strings:
            obj, key = max(strings, key=lambda pair: len(pair[0][pair[1]]))
            old = len(obj[key])
            keep = max(0, old - max(actual - maximum, old // 4, 32))
            if obj.get("encoding") == "base64":
                keep -= keep % 4
            obj[key] = obj[key][:keep]
            obj["truncated"] = True
            budget["content_chars_omitted"] += old - keep
            continue
        lists = [(obj, key) for obj in (result, result.get("result", {}))
                 if isinstance(obj, dict) for key in ("results", "commits", "errors")
                 if isinstance(obj.get(key), list) and obj[key]]
        if lists:
            obj, key = max(lists, key=lambda pair: len(_render(pair[0][pair[1]])))
            obj[key].pop()
            budget["items_omitted"] += 1
            continue
        raise ValueError("Git response metadata exceeds the output budget; choose a larger --max-chars")


def _record(conn, ref, api):
    if not isinstance(ref, str):
        raise api.MemoryError("Use a full entry:, entity:, or edge: reference")
    family, separator, identity = ref.partition(":")
    if not separator or family not in ("entry", "entity", "edge") or not identity:
        raise api.MemoryError("Use a full entry:, entity:, or edge: reference")
    api.identifier(identity)
    if family == "entry":
        return family, api.entry_get(conn, identity)
    graph.require_v2(conn, api)
    return family, graph.get(conn, family, identity, api, history=False)


def _read_source(path, api):
    if not path or str(path) == "-":
        raise api.MemoryError("Pass a source JSON file; stdin is not supported")
    descriptor = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise api.MemoryError("Source JSON must be a regular file")
        data = handle.read(MAX_SOURCE_JSON + 1)
    if len(data) > MAX_SOURCE_JSON:
        raise api.MemoryError("Source JSON exceeds the size limit")
    value = json.loads(data)
    if isinstance(value, dict) and "source" in value and "reference" not in value:
        value = value["source"]
    normalized = api.sources([value])[0]
    return git_evidence.validate_anchor(normalized)


def _json_arg(args):
    return getattr(args, "json_file", None) or getattr(args, "json", None)


def _selected_source(conn, args, api, allow_legacy=False):
    ref, path = getattr(args, "ref", None), _json_arg(args)
    if bool(ref) == bool(path):
        raise api.MemoryError("Specify exactly one --ref or --json")
    if path:
        if getattr(args, "source", 1) != 1:
            raise api.MemoryError("--source is an index into a record's sources and requires --ref")
        return _read_source(path, api), None
    _, record = _record(conn, ref, api)
    index = getattr(args, "source", 1)
    if not isinstance(index, int) or index < 1 or index > len(record["sources"]):
        raise api.MemoryError("--source must be a 1-based index into the full record's sources array")
    source = record["sources"][index - 1]
    if allow_legacy and "git" not in source:
        return source, ref
    return git_evidence.validate_anchor(source), ref


def _repository(conn, repo_id, override, api):
    project = api.require_project(conn, repo_id)
    if not project.get("repo_path"):
        raise api.MemoryError("Project has no registered repo_path: " + repo_id)
    registered = git_evidence.identify(project["repo_path"])
    if override:
        supplied = git_evidence.identify(override)
        if supplied["common_dir"] != registered["common_dir"]:
            raise api.MemoryError("--repo must be the registered repository or one of its linked worktrees; a separate clone is a different repository")
        return supplied["root"]
    return registered["root"]


def _write_source(path, source):
    # O_EXCL also rejects an existing symlink. A failed write removes only our file.
    descriptor = os.open(Path(path).expanduser(), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(_render(source))
    except BaseException:
        Path(path).expanduser().unlink(missing_ok=True)
        raise


def capture(conn, args, api, database):
    repo = _repository(conn, args.project, getattr(args, "repo", None), api)
    revision = getattr(args, "rev", None)
    if revision is not None and getattr(args, "worktree", False):
        raise api.MemoryError("Choose --rev or --worktree")
    source = git_evidence.capture(repo, args.project, args.path,
                                  revision=revision or "HEAD", worktree=revision is None,
                                  artifact_dir=_artifacts(database))
    source = api.sources([source])[0]
    if getattr(args, "output", None):
        _write_source(args.output, source)
    return _bound({"status": "captured", "source": source,
            "output": str(Path(args.output).expanduser().absolute()) if getattr(args, "output", None) else None,
            "notice": "Captured evidence only. No knowledge record or verified_at was changed."},
            getattr(args, "max_chars", 16000))


def attach(conn, args, api, database):
    source = _read_source(_json_arg(args), api)
    # A structurally valid anchor must at least refer to a registered repository.
    repo = _repository(conn, source["git"]["repo_id"], None, api)
    family, record = _record(conn, args.ref, api)
    expected = getattr(args, "expected_updated_at", None)
    if expected is None or api.timestamp(expected, "expected_updated_at") != record["updated_at"]:
        raise api.MemoryError("Source attach conflict: --expected-updated-at must match the current record; read it again")
    # show verifies the complete captured bytes/hash before returning its excerpt.
    git_evidence.show(source, repo, artifact_dir=_artifacts(database), max_bytes=1)
    if source in record["sources"]:
        return _bound({"ref": args.ref, "status": "unchanged", "source_index": record["sources"].index(source) + 1,
                "updated_at": record["updated_at"]}, getattr(args, "max_chars", 16000))
    payload = {key: value for key, value in record.items() if key not in ("created_at", "updated_at", "history")}
    payload["sources"] = record["sources"] + [source]
    payload["expected_updated_at"] = expected
    result = api.entry_put(conn, payload) if family == "entry" else graph.put(conn, payload, family, api)
    _, after = _record(conn, args.ref, api)
    return _bound({"ref": args.ref, "status": result["status"], "source_index": len(after["sources"]),
            "updated_at": after["updated_at"], "verified_at": after["verified_at"],
            "notice": "Appended evidence while preserving all other record fields and history."},
            getattr(args, "max_chars", 16000))


def _scan(conn, api, refs=None, repo_id=None):
    """Bounded declaration inventory, including superseded and hypothesis records."""
    if bool(refs) == bool(repo_id):
        raise api.MemoryError("Specify either one or more --ref values or one --project")
    coverage = {"records_scanned": 0, "record_limit": MAX_RECORDS, "scan_complete": True,
                "oversized_records": 0, "selected_source_limit": MAX_SELECTED,
                "selected_sources_omitted": 0, "source_chars_scanned": 0,
                "source_char_limit": MAX_SCAN_SOURCE_CHARS}
    selected, errors = [], []

    def collect(ref, record):
        coverage["records_scanned"] += 1
        for index, source in enumerate(record["sources"], 1):
            if not isinstance(source, dict) or "git" not in source:
                continue
            anchor = source.get("git")
            if repo_id and (not isinstance(anchor, dict) or anchor.get("repo_id") != repo_id):
                continue
            if len(selected) + len(errors) >= MAX_SELECTED:
                coverage["selected_sources_omitted"] += 1
                continue
            metadata = {"ref": ref, "source_index": index,
                        "title": record.get("title", record.get("description", ""))[:300],
                        "knowledge_state": record.get("knowledge_state"),
                        "updated_at": record.get("updated_at"), "verified_at": record.get("verified_at")}
            try:
                normalized = git_evidence.validate_anchor(source)
            except ValueError as exc:
                errors.append(dict(metadata, status="unavailable", reason="invalid_anchor", error=str(exc)[:500]))
                continue
            selected.append((metadata, normalized))

    if refs:
        if len(refs) > MAX_REFS:
            raise api.MemoryError("At most {} --ref values may be selected".format(MAX_REFS))
        for ref in sorted(set(refs)):
            if coverage["source_chars_scanned"] >= MAX_SCAN_SOURCE_CHARS:
                coverage["scan_complete"] = False
                break
            _, record = _record(conn, ref, api)
            coverage["source_chars_scanned"] += len(json.dumps(record["sources"], ensure_ascii=False))
            collect(ref, record)
    else:
        api.require_project(conn, repo_id)
        tables = [("entry", "entries", "title")]
        if conn.execute("PRAGMA user_version").fetchone()[0] == 2:
            tables += [("entity", "entities", "title"), ("edge", "graph_edges", "description")]
        for family, table, title in tables:
            if coverage["source_chars_scanned"] >= MAX_SCAN_SOURCE_CHARS:
                coverage["scan_complete"] = False
                break
            remaining = MAX_RECORDS - coverage["records_scanned"]
            rows = conn.execute(f"""
SELECT
    id
    , substr({title}, 1, 301) AS title
    , knowledge_state
    , updated_at
    , verified_at
    , substr(sources, 1, ?) AS sources
FROM {table}
ORDER BY id
LIMIT ?
;
""", (MAX_RECORD_SOURCES + 1, remaining + 1))
            for index, row in enumerate(rows):
                if index == remaining or coverage["source_chars_scanned"] >= MAX_SCAN_SOURCE_CHARS:
                    coverage["scan_complete"] = False
                    break
                record = dict(row)
                coverage["source_chars_scanned"] += len(record["sources"])
                if len(record["sources"]) > MAX_RECORD_SOURCES:
                    coverage["records_scanned"] += 1
                    coverage["oversized_records"] += 1
                    coverage["scan_complete"] = False
                    continue
                record["sources"] = json.loads(record["sources"])
                collect(family + ":" + record["id"], record)
    coverage["selected_sources"] = len(selected)
    coverage["invalid_sources"] = len(errors)
    if coverage["selected_sources_omitted"]:
        coverage["scan_complete"] = False
    return selected, errors, coverage


def git_check(conn, args, api, database):
    selected, errors, coverage = _scan(conn, api, getattr(args, "refs", None), getattr(args, "project", None))
    results = list(errors)
    repos, observations, resolved_targets = {}, {}, {}
    target, worktree = getattr(args, "target", None) or "HEAD", getattr(args, "worktree", False)
    artifact_dir = _artifacts(database)
    for metadata, source in selected:
        repo_id = source["git"]["repo_id"]
        if repo_id not in repos:
            try:
                repos[repo_id] = _repository(conn, repo_id, getattr(args, "repo", None), api)
            except (ValueError, OSError, api.MemoryError) as exc:
                repos[repo_id] = {"status": "unavailable", "reason": "repository_unavailable", "error": str(exc)[:500]}
        repository = repos[repo_id]
        if isinstance(repository, dict):
            observation = repository
        else:
            key = json.dumps(source, sort_keys=True, ensure_ascii=False)
            if key not in observations:
                observations[key] = git_evidence.check(source, repository, target=resolved_targets.get(repo_id, target),
                                                        worktree=worktree, artifact_dir=artifact_dir)
                resolved = observations[key].get("target", {}).get("commit")
                if resolved:
                    # Moving branches must not produce a mixed-revision report.
                    resolved_targets.setdefault(repo_id, resolved)
            observation = observations[key]
        results.append(dict(metadata, repo_id=repo_id, path=source["git"]["path"],
                            captured_commit=source["git"]["commit"], captured_dirty=source["git"]["dirty"],
                            **observation))
    counts = {state: sum(item["status"] == state for item in results)
              for state in ("unchanged", "changed", "deleted", "unavailable")}
    status = ("unavailable" if counts["unavailable"] or not coverage["scan_complete"] else
              "changed" if counts["changed"] or counts["deleted"] else
              "unchanged" if results else "no_git_sources")
    return _bound({"status": status, "selection": {"refs": getattr(args, "refs", None),
                    "project": getattr(args, "project", None)},
                   "comparison": {"mode": "worktree" if worktree else "revision", "target": None if worktree else target,
                                  "resolved_targets": resolved_targets},
                   "coverage": coverage, "counts": counts, "results": results, "notice": NOTICE},
                  getattr(args, "max_chars", 16000))


def navigate(conn, args, api, database):
    """Shared read-only attached-source navigation for the CLI and map.

    The HTTP caller checks its expected_updated_at within the same read
    transaction before entering here. Legacy references are resolved only for
    this response; neither surface writes new provenance into stored records.
    """
    action = args.action
    if action not in {"show", "history", "diff", "check", "commit"}:
        raise api.MemoryError("Unknown Git navigation action")
    maximum = getattr(args, "max_chars", 16000)
    if type(maximum) is not int or not 2000 <= maximum <= 100000:
        raise api.MemoryError("max-chars must be between 2000 and 100000")
    limit = getattr(args, "limit", 20)
    if action == "history" and (type(limit) is not int or not 1 <= limit <= 100):
        raise api.MemoryError("History limit must be between 1 and 100")
    chosen_path = getattr(args, "path", None)
    source, ref = _selected_source(conn, args, api, allow_legacy=True)
    if "git" not in source:
        from . import git_legacy
        if ref is None:
            raise api.MemoryError("Legacy Git references require --ref for explicit repository scope")
        _, record = _record(conn, ref, api)
        options = {"action": action, "ref": ref, "source": getattr(args, "source", 1),
                   "max_chars": maximum, "limit": limit,
                   "target": getattr(args, "target", None),
                   "worktree": getattr(args, "worktree", False), "repo": getattr(args, "repo", None)}
        if chosen_path is not None:
            options["path"] = chosen_path
        return git_legacy.navigate(conn, record, options, api, database)
    if chosen_path is not None or action == "commit":
        raise api.MemoryError("An attached file source cannot select another path; commit file selection requires a bare legacy commit reference")
    repo = _repository(conn, source["git"]["repo_id"], getattr(args, "repo", None), api)
    artifact_dir = _artifacts(database)
    if action == "show":
        result = git_evidence.show(source, repo, artifact_dir=artifact_dir, max_bytes=maximum)
    elif action == "history":
        result = git_evidence.history(source, repo, limit=limit, target=getattr(args, "target", None))
    elif action == "check":
        result = git_evidence.check(source, repo, target=getattr(args, "target", None) or "HEAD",
                                    worktree=getattr(args, "worktree", False), artifact_dir=artifact_dir)
    else:
        result = git_evidence.diff(source, repo, target=getattr(args, "target", None) or "HEAD",
                                   worktree=getattr(args, "worktree", False), artifact_dir=artifact_dir,
                                   context=getattr(args, "context", 3), max_chars=maximum)
    return _bound({"action": action, "ref": ref, "repo_id": source["git"]["repo_id"],
                   "path": source["git"]["path"], "captured_commit": source["git"]["commit"],
                   "captured_dirty": source["git"]["dirty"], "result": result, "notice": NOTICE}, maximum)


def related(conn, args, api, database):
    path = args.path
    if (not isinstance(path, str) or not path or len(path) > 4096 or path.startswith("/")
            or "\\" in path or any(ord(char) < 32 or ord(char) == 127 for char in path)
            or not PurePosixPath(path).parts or ".." in PurePosixPath(path).parts
            or any(part.casefold() == ".git" for part in PurePosixPath(path).parts)
            or str(PurePosixPath(path)) != path):
        raise api.MemoryError("--path must be a normalized repository-relative file path")
    commit = getattr(args, "commit", None)
    if commit is not None and not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit):
        raise api.MemoryError("--commit must be a full lowercase Git commit SHA")
    limit = getattr(args, "limit", 20)
    if not 1 <= limit <= 200:
        raise api.MemoryError("limit must be between 1 and 200")
    selected, errors, coverage = _scan(conn, api, repo_id=args.project)
    matches = [dict(metadata, git=source["git"], reference=source["reference"])
               for metadata, source in selected if source["git"]["path"] == path
               and (commit is None or source["git"]["commit"] == commit)]
    return _bound({"project": args.project, "path": path, "commit": commit,
                   "matching_declarations": len(matches), "limit_omitted": max(0, len(matches) - limit),
                   "results": matches[:limit], "errors": errors, "coverage": coverage,
                   "notice": "Exact stored source declarations across all knowledge states; no inference of semantic relevance."},
                  getattr(args, "max_chars", 16000))


def run(conn, args, api, database):
    if args.action == "capture":
        return capture(conn, args, api, database)
    if args.action == "attach":
        return attach(conn, args, api, database)
    if args.action == "git-check":
        if getattr(args, "source", None) is not None or getattr(args, "path", None) is not None:
            refs = getattr(args, "refs", None)
            if (not isinstance(refs, list) or len(refs) != 1 or getattr(args, "project", None)
                    or getattr(args, "source", None) is None):
                raise api.MemoryError("Selected-source git-check requires exactly one --ref and --source; --project is not supported")
            selected_args = copy.copy(args)
            selected_args.action, selected_args.ref = "check", refs[0]
            return navigate(conn, selected_args, api, database)
        return git_check(conn, args, api, database)
    if args.action in ("show", "history", "diff", "check", "commit"):
        return navigate(conn, args, api, database)
    if args.action == "related":
        return related(conn, args, api, database)
    raise api.MemoryError("Unknown Git source command")
