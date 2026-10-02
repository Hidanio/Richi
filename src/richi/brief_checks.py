"""Optional byte comparisons for the cards already selected by task brief.

No new search, graph traversal, persistence, capture artifacts or remote reads.
"""
from collections import deque
import json
import time

from . import compact
from . import git_evidence
from . import git_legacy
from . import git_sources


DEFAULT_LIMIT = 12
CHECK_SECONDS = 20
NOTICE = ("Selected Git evidence was compared as reported in source_checks. Unchecked or omitted "
          "sources remain unverified. Byte equality does not verify a knowledge claim, historical "
          "applicability, dependencies or deployment. Working-copy reads are not atomic. "
          "Read full cards before applying a procedure; no knowledge or verified_at was updated.")


def validate(args, api):
    enabled = getattr(args, "check_sources", False)
    limit = getattr(args, "check_limit", None)
    if limit is not None and not enabled:
        raise api.MemoryError("Brief --check-limit requires --check-sources")
    if limit is not None and (type(limit) is not int or not 1 <= limit <= 50):
        raise api.MemoryError("Brief --check-limit must be between 1 and 50")
    return enabled


def refresh_coverage(response):
    """Refresh visible detail accounting after the ordinary brief budget pass."""
    coverage = response.get("source_check_coverage")
    if coverage is None:
        return
    returned = sum(len(card["source_checks"]["results"]) for card in response["results"])
    coverage["results_returned"] = returned
    coverage["results_omitted"] = coverage["checked"] - returned
    coverage["returned_cards"] = len(response["results"])
    coverage["complete"] = not (coverage["not_checked"] or coverage["counts"]["unavailable"]
                                or coverage["results_omitted"] or coverage["time_limit_reached"])
    response["truncated"] |= bool(coverage["results_omitted"] or coverage["not_checked"])


def annotate(conn, response, args, api, database):
    limit = getattr(args, "check_limit", None) or DEFAULT_LIMIT
    project = getattr(args, "project", None)
    worktree = bool(getattr(args, "worktree", False))
    target = getattr(args, "target", None) or "HEAD"
    override = getattr(args, "repo", None)
    deadline = time.monotonic() + CHECK_SECONDS
    coverage = {"requested": True, "scope": "selected_brief_cards", "project": project,
                "source_limit": limit, "scanned_cards": len(response["results"]),
                "git_sources": 0, "non_git_sources": 0, "checked": 0, "not_checked": 0,
                "counts": {key: 0 for key in ("unchanged", "changed", "deleted", "unavailable")},
                "time_limit_seconds": CHECK_SECONDS, "time_limit_reached": False,
                "working_copy_atomic": False if worktree else None,
                "resolved_targets": {}}
    response["source_check_coverage"] = coverage
    response["notice"] = NOTICE
    artifact_dir = git_sources._artifacts(database)
    queues, repositories, observations, resolutions = [], {}, {}, {}

    def skipped(card, reason):
        checks = card["source_checks"]
        checks["not_checked_count"] += 1
        checks["not_checked_reasons"][reason] = checks["not_checked_reasons"].get(reason, 0) + 1
        coverage["not_checked"] += 1

    # Enumerate full source arrays, never the first two compact.card excerpts.
    for card in response["results"]:
        _, record = git_sources._record(conn, card["ref"], api)
        checks = card["source_checks"] = {
            "record_updated_at": record["updated_at"], "source_count": len(record["sources"]),
            "git_source_count": 0, "non_git_source_count": 0,
            "checked_count": 0, "not_checked_count": 0, "not_checked_reasons": {},
            "results": [], "results_omitted": 0}
        candidates = []
        matched = {item["source_index"] for item in card.get("matches", [])}
        for index, source in enumerate(record["sources"], 1):
            if "git" not in source and not source.get("reference", "").startswith("git:"):
                checks["non_git_source_count"] += 1
                coverage["non_git_sources"] += 1
                continue
            checks["git_source_count"] += 1
            coverage["git_sources"] += 1
            if "git" in source:
                repo_id = source["git"].get("repo_id") if isinstance(source["git"], dict) else None
                if project and repo_id and repo_id != project:
                    skipped(card, "outside_project")
                    continue
            else:
                try:
                    _, path = git_legacy.parse(source)
                    if path is None:
                        skipped(card, "bare_commit_requires_path")
                        continue
                except ValueError:
                    pass  # Malformed declared evidence is a bounded unavailable result.
            candidates.append((index, source))
        candidates.sort(key=lambda pair: (pair[0] not in matched, "git" not in pair[1], pair[0]))
        queues.append((card, record, deque(candidates)))

    def repository(repo_id):
        if repo_id not in repositories:
            try:
                root = git_sources._repository(conn, repo_id, override if repo_id == project else None, api)
                # Diff and source comparisons must use the exact same endpoint.
                pinned = response.get("comparison", {}).get("target_commit") if repo_id == project else None
                pinned = pinned or git_evidence._resolve(root, target)
                repositories[repo_id] = {"root": root, "commit": pinned}
                coverage["resolved_targets"][repo_id] = {"commit": pinned, "mode": "worktree" if worktree else "commit"}
            except (ValueError, OSError, api.MemoryError) as exc:
                repositories[repo_id] = {"error": str(exc)[:400]}
        return repositories[repo_id]

    def check(card, record, index, original):
        result = {"source_index": index, "status": "unavailable",
                  "source_kind": "structured" if "git" in original else "legacy_file",
                  "observed_at": original.get("observed_at")}
        if original.get("label"):
            result["label"], result["label_is_excerpt"] = compact.excerpt(original["label"], 360)
        try:
            source = original
            if "git" in source:
                anchor = git_evidence.validate_anchor(source)["git"]
                repo_id, path, captured = anchor["repo_id"], anchor["path"], anchor["commit"]
                result["captured_mode"] = anchor["mode"]
                result["captured_dirty"] = anchor["dirty"]
            else:
                commit, path = git_legacy.parse(source)
                scope = tuple(git_legacy.project_ids(conn, record, card["ref"], api))
                key = (scope, commit, path)
                if key not in resolutions:
                    try:
                        resolutions[key] = git_legacy.resolve(conn, record, card["ref"], source, api, deadline=deadline)
                    except (ValueError, OSError, TimeoutError) as exc:
                        resolutions[key] = {"error": str(exc)[:400]}
                resolved = resolutions[key]
                if "error" in resolved:
                    raise ValueError(resolved["error"])
                repo_id, captured = resolved["repo_id"], resolved["commit"]
                result["captured_mode"] = "commit"
            if project and repo_id != project:
                skipped(card, "outside_project")
                return None
            result.update(repo_id=repo_id, path=path, captured_commit=captured)
            repo = repository(repo_id)
            if "error" in repo:
                raise ValueError(repo["error"])
            result.update(target_commit=repo["commit"], target_mode="worktree" if worktree else "commit")
            if "git" not in original:
                source = git_evidence.capture(repo["root"], repo_id, path, revision=captured)
                if original.get("sha256"):
                    if original["sha256"] != source["sha256"]:
                        raise ValueError("legacy_hash_mismatch: stored source hash differs from the recorded commit file")
                    result["legacy_note"] = "The stored observation hash matches the file at the recorded commit."
                else:
                    result["legacy_note"] = "Hash derived from the recorded commit now; no historical observation hash was recorded."
            key = (repo_id, json.dumps(source["git"], sort_keys=True), source["sha256"], repo["commit"], worktree)
            if key not in observations:
                observations[key] = git_evidence.check(source, repo["root"], target=repo["commit"],
                    worktree=worktree, artifact_dir=artifact_dir)
            observation = observations[key]
            result.update(status=observation["status"], ancestry=observation["ancestry"])
            if observation.get("reason"):
                result["reason"] = observation["reason"][:400]
            if observation.get("rename_candidates"):
                result["rename_candidates"] = observation["rename_candidates"][:3]
                result["rename_candidates_omitted"] = max(0, len(observation["rename_candidates"]) - 3)
            result["read_more"] = {"command": "sources git-check", "ref": card["ref"], "source": index}
            if worktree:
                result["read_more"]["worktree"] = True
            else:
                result["read_more"]["target"] = repo["commit"]
            if override:
                result["read_more"]["repo"] = override
        except (ValueError, OSError, api.MemoryError, TimeoutError) as exc:
            result["reason"] = str(exc)[:400]
        return result

    # One source per card per round; a large task cannot consume the whole limit.
    with git_evidence.operation_deadline(deadline):
        while any(queue for _, _, queue in queues):
            for card, record, queue in queues:
                if not queue:
                    continue
                index, source = queue.popleft()
                if time.monotonic() >= deadline:
                    coverage["time_limit_reached"] = True
                    skipped(card, "time_limit")
                    continue
                if coverage["checked"] >= limit:
                    skipped(card, "source_limit")
                    continue
                result = check(card, record, index, source)
                if result is not None:
                    card["source_checks"]["results"].append(result)
                    card["source_checks"]["checked_count"] += 1
                    coverage["checked"] += 1
                coverage["time_limit_reached"] |= time.monotonic() >= deadline
        # A HEAD move invalidates worktree-to-base association. Committed reads
        # deliberately remain tied to the pinned SHA even when a branch moves.
        if worktree:
            for repo_id, repo in repositories.items():
                if "error" in repo:
                    continue
                try:
                    current = git_evidence._resolve(repo["root"], "HEAD")
                    error = None if current == repo["commit"] else "repository_moved: HEAD changed during working-copy checks"
                except (ValueError, OSError) as exc:
                    error = "worktree_final_head_unavailable: " + str(exc)[:300]
                if error:
                    for card in response["results"]:
                        for result in card["source_checks"]["results"]:
                            if result.get("repo_id") == repo_id:
                                result.update(status="unavailable", reason=error)
    coverage["time_limit_reached"] |= time.monotonic() >= deadline
    for card in response["results"]:
        checks = card["source_checks"]
        statuses = {result["status"] for result in checks["results"]}
        if statuses & {"changed", "deleted"}:
            card["needs_recheck"] = True
            card["recheck_reasons"].append("Referenced code changed or was deleted; assess whether the recorded conclusion still applies.")
        if "unavailable" in statuses or checks["not_checked_count"]:
            card["needs_recheck"] = True
            card["recheck_reasons"].append("Some Git evidence was unavailable or not checked; code applicability remains unassessed.")
        for result in checks["results"]:
            coverage["counts"][result["status"]] += 1
    refresh_coverage(response)
    return response
