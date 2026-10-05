"""Workspace-local repository discovery and explicit project identity checks.

Only the supplied connection's projects table is consulted. Discovery never reads
knowledge, follows directory symlinks, enters a checkout, or contacts a remote.
"""
import os
from pathlib import Path
import stat
import time

from . import git_evidence


MAX_DIRECTORIES = 10000
MAX_DIRECTORY_ENTRIES = 10000
MAX_TOTAL_ENTRIES = 50000
MAX_PROJECTS = 10000
OPERATION_SECONDS = 30
EXCLUDED = {"vendor", "node_modules", "venv", "env", "__pycache__"}


def _error(message):
    from .memory import MemoryError
    return MemoryError(message)


def _path(value, directory=False):
    try:
        path = Path(value).expanduser().resolve(strict=True)
        if directory and not path.is_dir():
            raise ValueError("expected a directory")
        if not directory and not (path.is_dir() or path.is_file()):
            raise ValueError("expected a regular file or directory")
        return path
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        raise _error("Cannot inspect project path: " + str(exc)) from exc


def _marker(path):
    """lstat preserves broken/symlink markers as boundaries, never outer Git."""
    try:
        mode = (path / ".git").lstat().st_mode
    except FileNotFoundError:
        return False
    if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        raise ValueError("Git marker must be a real directory or regular file")
    return True


def _git_path(path, flag):
    output, _ = git_evidence._git(path, ["rev-parse", flag], maximum=16384)
    value = output.decode("utf-8").removesuffix("\n")
    if not value or any(ord(char) < 32 for char in value):
        raise ValueError("Git returned an invalid repository path")
    result = Path(value)
    return (path / result).resolve(strict=True)


def _repository(path, worktrees=False):
    root = _git_path(path, "--show-toplevel")
    if root != path:
        raise ValueError("Git marker does not describe this checkout")
    common = _git_path(root, "--git-common-dir")
    result = {"repo_path": str(root), "git_common_dir": str(common)}
    if worktrees:
        output, _ = git_evidence._git(root, ["worktree", "list", "--porcelain", "-z"], maximum=65536)
        paths = []
        for field in output.decode("utf-8").split("\0"):
            if field.startswith("worktree "):
                value = field[len("worktree "):]
                if any(ord(char) < 32 for char in value):
                    raise ValueError("Git returned an invalid worktree path")
                # These paths are metadata only; discovery never visits them.
                paths.append(os.path.normpath(value))
        result["worktrees"] = sorted(set(paths))
        result["main_worktree"] = paths[0] if paths else None
    return result


def _nearest_repository(path):
    folder = path if path.is_dir() else path.parent
    for candidate in (folder, *folder.parents):
        if _marker(candidate):
            return _repository(candidate)
    return None


def discover(root, max_depth=3, max_repositories=200):
    """Find checkouts, bounded by depth/count/time; a checkout is terminal.

    Linked worktrees share one result. Prefer its main checkout when that path
    was also discovered inside root; out-of-root worktrees are metadata only.
    Depth is zero-based (root itself is depth 0). Exclusions are intentional;
    unvisited normal directories and capacity limits mark the report truncated.
    """
    if type(max_depth) is not int or not 0 <= max_depth <= 20:
        raise _error("max-depth must be between 0 and 20")
    if type(max_repositories) is not int or not 1 <= max_repositories <= 1000:
        raise _error("max-repositories must be between 1 and 1000")
    if Path(root).expanduser().is_symlink():
        raise _error("Scan root must be a real directory, not a symlink")
    root = _path(root, directory=True)
    report = {"root": str(root), "repositories": [], "skipped": [], "errors": [],
              "truncated": False, "limits": {"max_depth": max_depth,
              "max_repositories": max_repositories, "max_directories": MAX_DIRECTORIES,
              "max_directory_entries": MAX_DIRECTORY_ENTRIES,
              "max_total_entries": MAX_TOTAL_ENTRIES,
              "timeout_seconds": OPERATION_SECONDS}, "directories_visited": 0,
              "entries_examined": 0}
    pending = [(root, 0)]
    groups = {}
    deadline = time.monotonic() + OPERATION_SECONDS
    with git_evidence.operation_deadline(deadline):
        while pending:
            path, depth = pending.pop()
            if time.monotonic() >= deadline or report["directories_visited"] >= MAX_DIRECTORIES:
                report["truncated"] = True
                report["skipped"].append({"path": str(path), "reason": "time_limit" if time.monotonic() >= deadline else "directory_limit"})
                break
            report["directories_visited"] += 1
            try:
                # Recheck before walking: directory entries may have changed.
                if path.is_symlink() or path.resolve(strict=True) != path:
                    report["skipped"].append({"path": str(path), "reason": "symlink"})
                    continue
                if _marker(path):
                    repository = _repository(path, worktrees=True)
                    key = repository["git_common_dir"]
                    if key not in groups:
                        if len(groups) >= max_repositories:
                            report["truncated"] = True
                            report["skipped"].append({"path": str(path), "reason": "repository_limit"})
                            break
                        repository["discovered_paths"] = []
                        groups[key] = repository
                    groups[key]["discovered_paths"].append(str(path))
                    continue
                children = []
                with os.scandir(path) as entries:
                    for index, entry in enumerate(entries):
                        report["entries_examined"] += 1
                        if (index >= MAX_DIRECTORY_ENTRIES or report["entries_examined"] > MAX_TOTAL_ENTRIES
                                or time.monotonic() >= deadline):
                            report["truncated"] = True
                            reason = "time_limit" if time.monotonic() >= deadline else "entries_limit" if report["entries_examined"] > MAX_TOTAL_ENTRIES else "directory_entries_limit"
                            report["skipped"].append({"path": str(path), "reason": reason})
                            children = []
                            if reason != "directory_entries_limit":
                                pending = []
                            break
                        if entry.name.startswith(".") or entry.name in EXCLUDED:
                            continue
                        if entry.is_symlink():
                            report["skipped"].append({"path": entry.path, "reason": "symlink"})
                        elif entry.is_dir(follow_symlinks=False):
                            children.append(Path(entry.path))
                for child in sorted(children, reverse=True):
                    if depth >= max_depth:
                        report["truncated"] = True
                        report["skipped"].append({"path": str(child), "reason": "depth_limit"})
                    else:
                        pending.append((child, depth + 1))
            except (OSError, ValueError, RuntimeError) as exc:
                report["errors"].append({"path": str(path), "error": str(exc)})
        for repository in groups.values():
            paths = sorted(repository["discovered_paths"])
            main = repository.pop("main_worktree")
            repository["repo_path"] = main if main in paths else paths[0]
            repository["discovered_paths"] = paths
        report["repositories"] = sorted(groups.values(), key=lambda item: item["repo_path"])
    report["skipped"].sort(key=lambda item: (item["path"], item["reason"]))
    report["errors"].sort(key=lambda item: item["path"])
    return report


class _Projects:
    def __init__(self, conn):
        self.deadline = time.monotonic() + OPERATION_SECONDS
        self.rows = [dict(row) for row in conn.execute("SELECT * FROM projects ORDER BY id LIMIT ?", (MAX_PROJECTS + 1,))]
        if len(self.rows) > MAX_PROJECTS:
            raise _error("Project registry exceeds the identity check limit")
        self.paths = {}
        self.identities = {}
        for row in self.rows:
            try:
                self.paths[row["id"]] = Path(row["repo_path"]).expanduser().resolve() if row["repo_path"] else None
            except (OSError, ValueError, RuntimeError):
                self.paths[row["id"]] = None

    def identity(self, path):
        if time.monotonic() >= self.deadline:
            raise _error("Project identity check exceeded the time limit")
        if path not in self.identities:
            try:
                self.identities[path] = _repository(path) if path and path.is_dir() and _marker(path) else None
            except (OSError, ValueError, RuntimeError) as exc:
                if "time limit" in str(exc):
                    raise _error("Project identity check exceeded the time limit") from exc
                # An unavailable historical path does not establish Git identity.
                self.identities[path] = None
        return self.identities[path]

    def check(self, path, repository, expected=None):
        candidates = []
        scoped = []
        checkout = Path(repository["repo_path"]) if repository else None
        for row in self.rows:
            registered = self.paths[row["id"]]
            if registered is None:
                continue
            if (checkout and checkout in registered.parents
                    and (registered == path or registered in path.parents)):
                # A folder project owns its descendants only inside this
                # checkout. The nearest Git root excludes outer folders when
                # the selected path enters a nested independent repository.
                scoped.append((row, "exact_path" if registered == path else "directory", len(registered.parts)))
                continue
            if registered == path or repository and str(registered) == repository["repo_path"]:
                candidates.append((row, "exact_path", len(registered.parts)))
            elif repository:
                identity = self.identity(registered)
                if identity and identity["git_common_dir"] == repository["git_common_dir"]:
                    candidates.append((row, "git_common_dir", len(registered.parts)))
            elif registered in path.parents and self.identity(registered) is None:
                candidates.append((row, "directory", len(registered.parts)))
        if scoped:
            candidates = scoped
        if (not repository or scoped) and candidates:
            longest = max(item[2] for item in candidates)
            candidates = [item for item in candidates if item[2] == longest]
        result = {"path": str(path), "status": "unregistered", "project": None,
                  "match": None, "repository": repository,
                  "candidates": [item[0] for item in candidates], "errors": []}
        if len(candidates) > 1:
            result["status"] = "ambiguous"
        elif candidates:
            result.update(status="matched", project=candidates[0][0], match=candidates[0][1])
        if expected is not None:
            result["expected_project_id"] = expected
            if result["status"] == "matched" and result["project"]["id"] != expected:
                result["status"] = "mismatch"
        return result


def project_check(conn, path, project_id=None):
    """Resolve only this store's project; expected ID validates, never routes."""
    if project_id is not None:
        from .memory import identifier
        project_id = identifier(project_id)
    path = _path(path)
    with git_evidence.operation_deadline(time.monotonic() + OPERATION_SECONDS):
        try:
            repository = _nearest_repository(path)
        except (OSError, ValueError, RuntimeError) as exc:
            return {"path": str(path), "status": "unregistered", "project": None,
                    "match": None, "repository": None, "candidates": [],
                    "errors": [{"path": str(path), "error": str(exc)}]}
        return _Projects(conn).check(path, repository, project_id)


def ensure_project_context(conn, path, expected_id=None):
    """Return the matched project row, or reject a missing/ambiguous context."""
    result = project_check(conn, path, expected_id)
    if result["status"] != "matched" or result["errors"]:
        detail = result["errors"][0]["error"] if result["errors"] else result["status"]
        raise _error("Project path does not identify one matching project in this workspace: " + detail)
    return result["project"]


def project_scan(conn, root, apply=False, max_depth=3):
    """Preview registrations or atomically insert them in the caller transaction.

    Existing project metadata is never rewritten. Conflicts and incomplete
    discovery prevent every insertion. A savepoint also rolls back a failed
    insertion without undoing unrelated writes in the caller's transaction.
    """
    from .memory import MemoryError, identifier, project_add
    report = discover(root, max_depth=max_depth)
    report.update(applied=False, status="preview", projects=[], conflicts=[])
    if apply:
        conn.execute("SAVEPOINT richi_project_scan")
    try:
        registry = _Projects(conn)
        planned = {}
        by_id = {row["id"]: row for row in registry.rows}
        with git_evidence.operation_deadline(time.monotonic() + OPERATION_SECONDS):
            for repository in report["repositories"]:
                path = Path(repository["repo_path"])
                context = registry.check(path, repository)
                if context["status"] == "matched":
                    report["projects"].append({"status": "unchanged", "project": context["project"], "repository": repository})
                    continue
                if context["status"] == "ambiguous":
                    report["conflicts"].append({"path": str(path), "reason": "ambiguous_project", "project_ids": [row["id"] for row in context["candidates"]]})
                    continue
                try:
                    project_id = identifier(path.name)
                except MemoryError as exc:
                    report["conflicts"].append({"path": str(path), "reason": "invalid_project_id", "error": str(exc)})
                    continue
                if project_id in by_id or project_id in planned:
                    report["conflicts"].append({"path": str(path), "reason": "project_id_collision", "project_id": project_id,
                                                "existing_path": by_id.get(project_id, planned.get(project_id, {})).get("repo_path")})
                    continue
                project = {"id": project_id, "name": path.name, "repo_path": str(path), "description": "", "source": str(path), "verified_at": None}
                planned[project_id] = project
                report["projects"].append({"status": "would_create", "project": project, "repository": repository})
        if report["conflicts"]:
            report["status"] = "conflict"
        elif report["errors"] or report["truncated"]:
            report["status"] = "incomplete"
        if apply:
            if report["status"] in {"conflict", "incomplete"}:
                raise _error("Project scan cannot apply: {}. Run without --apply to inspect conflicts, errors, and limits.".format(report["status"]))
            for item in report["projects"]:
                if item["status"] == "would_create":
                    result = project_add(conn, item["project"]["repo_path"], project_id=item["project"]["id"])
                    item.update(result)
            conn.execute("RELEASE richi_project_scan")
            report.update(applied=True, status="applied")
    except BaseException:
        if apply:
            conn.execute("ROLLBACK TO richi_project_scan")
            conn.execute("RELEASE richi_project_scan")
        raise
    return report
