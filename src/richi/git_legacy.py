"""Read-only navigation for pre-anchor git:<commit>[:<path>] source references.

Resolution is temporary: no source hash, repository identity, or observation date
is backfilled into the knowledge store. Only explicitly scoped local repositories
are considered, and an ambiguous commit never silently chooses one of them.
"""
import copy
import re
import time

from . import git_evidence
from . import git_sources


MAX_REPOSITORIES = 20
MAX_FILES = 200
MAX_CHANGE_OUTPUT = 1024 * 1024
REFERENCE = re.compile(r"git:([0-9a-f]{40}|[0-9a-f]{64})(?::(.+))?\Z")
NOTE = ("Legacy Git reference resolved from the recorded full commit in an explicitly scoped local repository. "
        "File hashes are calculated for this read; the original source and its observation date were not changed.")


class LegacyGitError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def parse(source):
    if not isinstance(source, dict) or "git" in source:
        raise LegacyGitError("invalid_source", "Select a structured Git source or a legacy git:<full commit>[:<path>] reference")
    reference = source.get("reference")
    match = REFERENCE.fullmatch(reference) if isinstance(reference, str) else None
    if not match:
        raise LegacyGitError("invalid_source", "Legacy Git sources require a full lowercase commit SHA")
    commit, path = match.groups()
    if path is not None:
        git_evidence._path(path)
    return commit, path


def parse_reference(reference):
    """Parse a legacy reference without reading repositories or changing memory."""
    return parse({"reference": reference})


def resolve_repository(conn, ref, commit, api=None, deadline=None):
    """Resolve an exact commit only within the record's explicit project scope.

    Returns repo_id/repo/commit/path (path is None). Missing, incomplete or
    ambiguous scope raises LegacyGitError; unrelated repositories are never used.
    """
    if api is None:
        from . import memory as api
    if not isinstance(commit, str) or not git_evidence.OID.fullmatch(commit):
        raise LegacyGitError("invalid_source", "Repository resolution requires a full lowercase commit SHA")
    _, record = git_sources._record(conn, ref, api)
    return resolve(conn, record, ref, {"reference": "git:" + commit}, api, deadline=deadline)


def _node_projects(conn, ref, api):
    family, _, identity = ref.partition(":")
    if family == "project":
        api.require_project(conn, identity)
        return [identity]
    if family == "entry":
        return api.entry_get(conn, identity)["project_ids"]
    if family == "entity":
        # Only direct, sourced, confirmed outgoing used_in edges establish scope.
        rows = conn.execute("""
            SELECT DISTINCT
                ge.to_ref
            FROM graph_edges ge
            WHERE 1=1
                AND ge.from_ref = ?
                AND ge.kind = 'used_in'
                AND ge.knowledge_state = 'confirmed'
                AND json_array_length(ge.sources) > 0
                AND substr(ge.to_ref, 1, 8) = 'project:'
            ORDER BY ge.to_ref
            LIMIT ?
        """, (ref, MAX_REPOSITORIES + 1)).fetchall()
        return [row[0][8:] for row in rows]
    return []


def project_ids(conn, record, ref, api):
    family = ref.partition(":")[0]
    if family == "entry":
        values = record["project_ids"]
    elif family == "entity":
        values = _node_projects(conn, ref, api)
    else:
        values = (_node_projects(conn, record["from_ref"], api)
                  + _node_projects(conn, record["to_ref"], api))
    projects = sorted(set(values))
    if not projects:
        raise LegacyGitError("legacy_repository_unavailable", "This legacy source has no explicit project scope; attach a structured Git source with a repository ID", 503)
    if len(projects) > MAX_REPOSITORIES:
        raise LegacyGitError("legacy_repository_ambiguous", "Too many explicitly scoped repositories; attach a structured Git source with a repository ID", 409)
    return projects


def resolve(conn, record, ref, source, api, deadline=None):
    def check_deadline():
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("Legacy Git repository resolution exceeded the time limit")

    check_deadline()
    commit, path = parse(source)
    matches, unavailable = [], []
    for repo_id in project_ids(conn, record, ref, api):
        check_deadline()
        project = api.require_project(conn, repo_id)
        if not project.get("repo_path"):
            continue
        try:
            identified = git_evidence.identify(project["repo_path"])
            check_deadline()
        except TimeoutError:
            raise
        except (git_evidence.GitEvidenceError, OSError):
            unavailable.append(repo_id)
            continue
        try:
            object_type, status = git_evidence._git(identified["root"], ["cat-file", "-t", commit],
                                                   allowed=(0, 128), maximum=8192)
            check_deadline()
        except TimeoutError:
            raise
        except git_evidence.GitEvidenceError:
            unavailable.append(repo_id)
            continue
        if status == 0 and object_type.strip() == b"commit":
            matches.append((repo_id, identified["root"]))
    if len(matches) > 1:
        raise LegacyGitError("legacy_repository_ambiguous", "The recorded commit exists in multiple scoped repositories: " + ", ".join(item[0] for item in matches) + ". Attach a structured Git source to select the repository", 409)
    if unavailable:
        raise LegacyGitError("legacy_repository_unavailable", "Repository resolution is incomplete because these scoped local repositories could not be inspected: " + ", ".join(unavailable) + ". Attach a structured Git source or restore the local repository", 503)
    if not matches:
        raise LegacyGitError("legacy_repository_unavailable", "The recorded commit is unavailable in the source's explicitly scoped local repositories; no fetch was performed", 503)
    return {"repo_id": matches[0][0], "repo": matches[0][1], "commit": commit, "path": path}


def commit_files(repo, commit):
    metadata = git_evidence._git(repo, ["log", "-1", "--no-show-signature",
        "--format=%H%x00%P%x00%aI%x00%s", commit, "--"], maximum=65536)[0]
    parts = git_evidence._decode(metadata).rstrip("\n").split("\0", 3)
    if len(parts) != 4 or parts[0] != commit:
        raise git_evidence.GitEvidenceError("Git commit metadata is unavailable")
    parents = parts[1].split() if parts[1] else []
    if any(not git_evidence.OID.fullmatch(parent) for parent in parents):
        raise git_evidence.GitEvidenceError("Invalid Git parent commit")
    revisions = [parents[0], commit] if parents else [commit]
    raw = git_evidence._git(repo, ["diff-tree", "--root", "--no-commit-id", "--raw", "-z", "-r",
        "--no-renames", "--no-ext-diff", "--no-textconv"] + revisions + ["--"], maximum=MAX_CHANGE_OUTPUT)[0]
    chunks, files, omitted = raw.split(b"\0"), [], 0
    if chunks and chunks[-1] == b"":
        chunks.pop()
    if len(chunks) % 2:
        raise git_evidence.GitEvidenceError("Unexpected Git changed-file output")
    for offset in range(0, len(chunks), 2):
        fields = chunks[offset].split()
        if len(fields) != 5 or not fields[0].startswith(b":"):
            raise git_evidence.GitEvidenceError("Unexpected Git change metadata")
        old_mode, new_mode, status = fields[0][1:], fields[1], fields[4]
        if new_mode not in (b"100644", b"100755") and old_mode not in (b"100644", b"100755"):
            omitted += 1
            continue
        try:
            path = git_evidence._path(git_evidence._decode(chunks[offset + 1]))
        except git_evidence.GitEvidenceError:
            omitted += 1
            continue
        if len(files) >= MAX_FILES:
            omitted += 1
            continue
        available = new_mode in (b"100644", b"100755")
        item = {"path": path, "status": git_evidence._decode(status), "available": available}
        if not available:
            item["reason"] = "The file is deleted or is not a regular file in this commit"
        files.append(item)
    return {"commit": commit, "subject": parts[3], "authored_at": parts[2], "parents": parents,
            "first_parent": len(parents) > 1, "files": files, "truncated": bool(omitted),
            "files_omitted": omitted, "file_limit": MAX_FILES}


def _bounded(response, maximum):
    response = copy.deepcopy(response)
    files = response.get("result", {}).get("files")
    if files is not None:
        # Leave space for the standard budget envelope; large commit lists retain
        # explicit omitted accounting instead of failing or emitting unbounded JSON.
        while files and len(git_sources._render(response)) > maximum - 300:
            files.pop()
            response["result"]["files_omitted"] += 1
            response["result"]["truncated"] = True
    result = git_sources._bound(response, maximum)
    if files is not None and result["result"]["truncated"]:
        result["budget"]["truncated"] = True
        result["budget"]["items_omitted"] += result["result"]["files_omitted"]
        # Exact JSON character count is part of the public response contract.
        while result["budget"]["output_chars"] != len(git_sources._render(result)):
            result["budget"]["output_chars"] = len(git_sources._render(result))
    return result


def navigate(conn, record, options, api, database):
    original = record["sources"][options["source"] - 1]
    resolved = resolve(conn, record, options["ref"], original, api)
    if options.get("repo"):
        resolved["repo"] = git_sources._repository(conn, resolved["repo_id"], options["repo"], api)
    action, path = options["action"], resolved["path"]
    if action not in {"show", "history", "diff", "check", "commit"}:
        raise LegacyGitError("invalid_action", "Unsupported Git navigation action")
    chosen_path = options.get("path")
    if chosen_path is not None and path is not None:
        raise LegacyGitError("invalid_source", "An attached file source cannot select another path")
    if action == "commit" and (path is not None or chosen_path is not None):
        raise LegacyGitError("invalid_source", "Commit file selection is available only for a bare legacy commit reference")
    resolution = {"reference": original["reference"], "note": NOTE}
    if path is None:
        changes = commit_files(resolved["repo"], resolved["commit"])
        if action == "commit":
            return _bounded({"action": action, "ref": options["ref"], "repo_id": resolved["repo_id"],
                "captured_commit": resolved["commit"], "result": changes, "legacy_resolution": resolution,
                "notice": git_sources.NOTICE}, options["max_chars"])
        if chosen_path is None:
            raise LegacyGitError("legacy_path_required", "Select a changed file from this commit first")
        git_evidence._path(chosen_path)
        selected = next((item for item in changes["files"] if item["path"] == chosen_path), None)
        if selected is None or not selected["available"]:
            raise LegacyGitError("legacy_path_unavailable", "Select an available regular file listed in this commit's changed files")
        path = chosen_path
    source = git_evidence.capture(resolved["repo"], resolved["repo_id"], path,
                                  revision=resolved["commit"], worktree=False)
    # capture's current observation time is accurate for this temporary read.
    # Keep historical source metadata solely in legacy_resolution, without
    # presenting a newly calculated hash as an old observation.
    if original.get("observed_at"):
        resolution["original_observed_at"] = original["observed_at"]
    artifacts = git_sources._artifacts(database)
    if action == "show":
        result = git_evidence.show(source, resolved["repo"], max_bytes=options["max_chars"])
    elif action == "history":
        result = git_evidence.history(source, resolved["repo"], limit=options["limit"], target=options.get("target"))
    elif action == "diff":
        result = git_evidence.diff(source, resolved["repo"], target=options.get("target") or "HEAD",
            worktree=options["worktree"], artifact_dir=artifacts, max_chars=options["max_chars"])
    else:
        result = git_evidence.check(source, resolved["repo"], target=options.get("target") or "HEAD",
            worktree=options["worktree"], artifact_dir=artifacts)
    return _bounded({"action": action, "ref": options["ref"], "repo_id": resolved["repo_id"],
        "path": path, "captured_commit": resolved["commit"], "captured_dirty": False,
        "result": result, "resolved_source": source, "legacy_resolution": resolution,
        "notice": git_sources.NOTICE}, options["max_chars"])
