"""Bounded, read-only workspace metadata and local project-path diagnostics."""
from collections import Counter, defaultdict
from pathlib import Path
import sqlite3
import stat
import time

from . import git_evidence, memory, projects


OPERATION_SECONDS = 10
MAX_PROJECTS = 10000
MAX_ITEMS = 1000
MAX_CONFLICT_SAMPLES = 20


class _Deadline(Exception):
    pass


def _public(row):
    if row is None:
        return None
    return {key: row.get(key) for key in ("id", "name", "repo_path")}


def _issue(code, message, *, severity="warning", project_id=None, path=None, suggestion=None):
    result = {"code": code, "severity": severity, "message": message}
    for key, value in (("project_id", project_id), ("path", path), ("suggestion", suggestion)):
        if value is not None:
            result[key] = value
    return result


class _Registry:
    """One registry read and at most one Git identity lookup per unique path."""

    def __init__(self, conn, deadline):
        self.deadline = deadline
        self.total = conn.execute("SELECT count(*) FROM projects").fetchone()[0]
        self.rows = [dict(row) for row in conn.execute(
            "SELECT id, name, repo_path FROM projects ORDER BY id LIMIT ?", (MAX_PROJECTS,))]
        self.complete = self.total == len(self.rows)
        self.paths = {}
        self.identities = {}
        self.diagnostics = {}
        self.path_results = {}
        for row in self.rows:
            self._check_time()
            value = row["repo_path"]
            try:
                self.paths[row["id"]] = Path(value).expanduser().absolute() if value else None
            except (OSError, ValueError, RuntimeError, TypeError):
                self.paths[row["id"]] = None

    def _check_time(self):
        if time.monotonic() >= self.deadline:
            raise _Deadline()

    def inspect_path(self, value):
        self._check_time()
        key = str(value)
        if key in self.path_results:
            return self.path_results[key]
        result = {"status": "unavailable", "resolved_path": None, "repository": None}
        try:
            path = Path(value).expanduser().resolve(strict=True)
            result["resolved_path"] = str(path)
            mode = path.stat().st_mode
            if not stat.S_ISDIR(mode):
                result["status"] = "not_directory"
            elif projects._marker(path):
                try:
                    repository = projects._repository(path)
                    result.update(status="ready", kind="git", repository=repository)
                except (OSError, ValueError, RuntimeError) as exc:
                    self._check_time()
                    result.update(status="invalid_repository", error=str(exc))
            else:
                result.update(status="ready", kind="directory")
        except FileNotFoundError:
            result["status"] = "missing"
        except NotADirectoryError:
            result["status"] = "not_directory"
        except PermissionError as exc:
            result["error"] = str(exc)
        except (OSError, ValueError, RuntimeError, TypeError) as exc:
            result["error"] = str(exc)
        self._check_time()
        self.path_results[key] = result
        if result["resolved_path"] is not None:
            self.path_results[result["resolved_path"]] = result
            self.identities[Path(result["resolved_path"])] = result["repository"]
        return result

    def inspect_row(self, row):
        self._check_time()
        if row["id"] not in self.diagnostics:
            result = {"project": _public(row), "status": "no_local_path", "resolved_path": None,
                      "repository": None}
            if row["repo_path"]:
                result.update(self.inspect_path(row["repo_path"]))
            self.diagnostics[row["id"]] = result
            if result["resolved_path"]:
                self.paths[row["id"]] = Path(result["resolved_path"])
        return self.diagnostics[row["id"]]

    def identity(self, path):
        self._check_time()
        return self.inspect_path(path)["repository"] if path is not None else None

    def inspect_rows(self):
        for row in self.rows:
            self.inspect_row(row)

    def duplicates(self):
        groups = defaultdict(list)
        for item in self.diagnostics.values():
            if item["status"] != "ready":
                continue
            repository = item["repository"]
            identity = ("git", repository["git_common_dir"]) if repository else ("path", item["resolved_path"])
            groups[identity].append(item)
        for group in groups.values():
            if len(group) > 1:
                ids = [item["project"]["id"] for item in group]
                sample = ids[:MAX_CONFLICT_SAMPLES + 1]
                for item in group:
                    conflicts = [value for value in sample if value != item["project"]["id"]][:MAX_CONFLICT_SAMPLES]
                    item.update(status="ambiguous", conflicts=conflicts, conflict_count=len(ids) - 1,
                                conflicts_omitted=len(ids) - 1 - len(conflicts))

    def check(self, path, expected):
        self._check_time()
        # Canonical paths and identity failures are cached before reusing the
        # regular project membership rules, including nearest Git boundaries.
        self.inspect_rows()
        if not self.complete:
            raise _Deadline()
        repository = None
        folder = path if path.is_dir() else path.parent
        for candidate in (folder, *folder.parents):
            self._check_time()
            if projects._marker(candidate):
                inspected = self.inspect_path(candidate)
                if inspected["status"] != "ready":
                    raise ValueError(inspected.get("error", "Cannot inspect nearest Git repository"))
                repository = inspected["repository"]
                break
        result = projects._Projects.check(self, path, repository, expected)
        result["project"] = _public(result["project"])
        result["candidates"] = [_public(row) for row in result["candidates"]]
        return result


def _target(registry, path, project_id):
    result = {"path": str(path), "status": "unknown", "project": None, "match": None,
              "repository": None, "candidates": [], "errors": []}
    if project_id is not None:
        result["expected_project_id"] = project_id
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
        if not (stat.S_ISDIR(resolved.stat().st_mode) or stat.S_ISREG(resolved.stat().st_mode)):
            raise ValueError("Expected a regular file or directory")
        return registry.check(resolved, project_id)
    except _Deadline:
        result["reason"] = "inspection_incomplete"
    except FileNotFoundError:
        result["reason"] = "missing_path"
    except PermissionError:
        result["reason"] = "path_unavailable"
    except (OSError, ValueError, RuntimeError) as exc:
        result["reason"] = "path_unavailable"
        result["errors"].append({"path": str(path), "error": str(exc)})
    return result


def inspect(database, project_path=None, project_id=None, all_projects=False, limit=50):
    """Inspect one selected store; never create it or examine knowledge tables.

    ``limit`` bounds returned project items, not identity checking. Aggregate
    counts describe checked rows; omitted includes unchecked and hidden rows.
    The deadline covers SQLite work and Git identity checks. Filesystem calls
    may still block on an unresponsive operating-system mount.
    """
    if type(limit) is not int or not 1 <= limit <= MAX_ITEMS:
        raise memory.MemoryError("status limit must be between 1 and {}".format(MAX_ITEMS))
    if project_id is not None:
        project_id = memory.identifier(project_id)
    database = Path(database).expanduser().absolute()
    deadline = time.monotonic() + OPERATION_SECONDS
    report = {"database": {"path": str(database), "status": "unavailable"}, "project": None,
              "projects": {"status": "not_checked", "total": None, "checked": 0, "omitted": 0,
                           "counts": {}, "items": [], "limit": limit}, "issues": []}
    if project_path is not None:
        report["project"] = {"path": str(project_path), "status": "unknown", "project": None,
                             "reason": "database_unavailable"}
    conn = None
    try:
        try:
            mode = database.stat().st_mode
        except FileNotFoundError:
            report["database"]["status"] = "missing"
            report["issues"].append(_issue("database_missing", "Workspace database is not initialized.",
                                           path=str(database), suggestion="Initialize this workspace explicitly with richi init."))
            return report
        if not stat.S_ISREG(mode):
            raise ValueError("Database path is not a regular file")
        conn = memory.connect(database, readonly=True)
        conn.execute("PRAGMA busy_timeout = 1000")
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        report["database"]["schema_version"] = version
        if version not in {1, memory.VERSION}:
            report["database"]["status"] = "unsupported_schema"
            report["issues"].append(_issue("unsupported_schema", "Unsupported database schema; no migration was attempted.",
                                           severity="error", path=str(database)))
            return report
        memory.backend(conn)
        with git_evidence.operation_deadline(deadline):
            registry = _Registry(conn, deadline)
            report["database"]["status"] = "ready"
            report["projects"]["total"] = registry.total
            if project_path is not None:
                report["project"] = _target(registry, project_path, project_id)
                candidates = report["project"].get("candidates", [])
                report["project"].update(candidate_count=len(candidates), candidates=candidates[:limit],
                                          candidates_omitted=max(0, len(candidates) - limit))
                status = report["project"]["status"]
                if status != "matched":
                    severity = "error" if (status in {"ambiguous", "mismatch"}
                                             or report["project"].get("reason") == "path_unavailable") else "warning"
                    report["issues"].append(_issue("project_" + status, "Current path membership is {} in this workspace.".format(status),
                                                   severity=severity,
                                                   path=str(project_path), suggestion="Check the selected workspace and its project registration."))
            if all_projects:
                complete = registry.complete
                try:
                    registry.inspect_rows()
                except _Deadline:
                    complete = False
                registry.duplicates()
                severity = {"ready": None, "no_local_path": "info", "missing": "warning",
                            "not_directory": "warning", "ambiguous": "error",
                            "unavailable": "error", "invalid_repository": "error"}
                rank = {"error": 0, "warning": 1, "info": 2, None: 3}
                items = sorted(registry.diagnostics.values(),
                               key=lambda item: (rank[severity[item["status"]]], item["project"]["id"]))
                report["projects"].update(status="complete" if complete else "partial", checked=len(items),
                                           omitted=max(0, registry.total - min(len(items), limit)),
                                           counts=dict(sorted(Counter(item["status"] for item in items).items())),
                                           items=items[:limit])
                for item in items[:limit]:
                    if item["status"] != "ready":
                        suggestion = ("Remote-only projects may remain without a local path."
                                      if item["status"] == "no_local_path"
                                      else "Review the registered path; Richi has not changed it.")
                        report["issues"].append(_issue("project_path_" + item["status"],
                            "Registered project path is {}.".format(item["status"]), project_id=item["project"]["id"],
                            severity=severity[item["status"]],
                            path=item["project"]["repo_path"], suggestion=suggestion))
                hidden_issues = [item for item in items[limit:] if item["status"] != "ready"]
                if hidden_issues:
                    report["issues"].append(_issue("project_issues_omitted", "{} additional project diagnostics omitted by the output limit.".format(len(hidden_issues)),
                                                   severity=severity[hidden_issues[0]["status"]], suggestion="Increase --limit to see more project details."))
                if not complete:
                    report["issues"].append(_issue("project_inspection_incomplete", "Project diagnostics reached the time or registry limit."))
            else:
                report["projects"]["omitted"] = registry.total
    except (sqlite3.Error, OSError, ValueError, RuntimeError, memory.MemoryError, _Deadline) as exc:
        report["database"].update(status="unavailable", error=str(exc) or "Inspection time limit exceeded")
        report["issues"].append(_issue("database_unavailable", "Cannot inspect workspace metadata: " + (str(exc) or "time limit exceeded"),
                                       severity="error", path=str(database)))
    finally:
        if conn is not None:
            conn.close()
    return report
