"""Stable, read-only context report before or after choosing a workspace."""
import json
import os
from pathlib import Path
import subprocess
import tempfile

from .chats import current_chat, pin_chat_state
from .config import ConfigError, resolve_settings
from .runtime import ACTIVE_RUNTIME, resolve_runtime, runtime_command, lease_fds


MAX_OUTPUT = 1024 * 1024


def _limit(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise ConfigError("--limit must be an integer between 1 and 200") from exc
    if not 1 <= number <= 200:
        raise ConfigError("--limit must be between 1 and 200")
    return number


def _project_id(value):
    if not value.strip() or len(value) > 200 or any(ord(ch) < 32 for ch in value):
        raise ConfigError("--project must be a nonempty ID of at most 200 characters without control characters")
    return value


def _worker(runtime, arguments):
    environment = dict(os.environ)
    environment.pop(ACTIVE_RUNTIME, None)
    # Storage is an explicit worker argument, never resolved a second time.
    execution = runtime_command(runtime, [json.dumps(arguments)], action="status_worker")
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            result = subprocess.run(execution, stdout=output, stderr=errors,
                                    timeout=30, env=environment, pass_fds=lease_fds())
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ConfigError("Workspace inspection failed: " + str(exc)) from exc
        output.seek(0)
        payload = output.read(MAX_OUTPUT + 1)
        if result.returncode:
            errors.seek(0, 2)
            errors.seek(max(0, errors.tell() - 2000))
            detail = errors.read(2000).decode("utf-8", errors="replace").strip()
            raise ConfigError("Selected runtime cannot inspect workspace: " + (detail or "worker failed"))
    if len(payload) > MAX_OUTPUT:
        raise ConfigError("Workspace inspection exceeded the output size limit")
    try:
        report = json.loads(payload)
        if (not isinstance(report, dict) or not isinstance(report.get("database"), dict)
                or not isinstance(report.get("projects"), dict)
                or not isinstance(report.get("issues"), list)
                or not all(isinstance(issue, dict) and isinstance(issue.get("severity"), str)
                           for issue in report["issues"])
                or "project" not in report
                or (report["project"] is not None and not isinstance(report["project"], dict))
                or not isinstance(report["database"].get("status"), str)
                or not isinstance(report["projects"].get("status"), str)) :
            raise ValueError("incomplete status report")
    except (ValueError, UnicodeError) as exc:
        raise ConfigError("Invalid workspace inspection result: " + str(exc)) from exc
    return report


def command(argv, selectors, chat_id, project_path=None):
    from .cli import Parser
    parser = Parser(prog="richi status", description="Read workspace, runtime and project-path diagnostics without changing settings or knowledge")
    parser.add_argument("--projects", action="store_true", help="Also inspect registered project paths")
    parser.add_argument("--project", type=_project_id, help="Expected project ID for --project-path or the current directory")
    parser.add_argument("--limit", type=_limit, default=50, help="Maximum returned project details (1–200, default 50)")
    args = parser.parse_args(argv)
    state = current_chat(chat_id)
    report = {"schema_version": 1, "read_only": True, "status": "ok",
              "chat": {key: state[key] for key in ("chat_id", "detected", "bound", "workspace")},
              "cli": {"current": state["cli_current"], "default": state["cli_default"]},
              "workspace": None, "selection": None, "storage": None, "runtime": None,
              "diagnostics": None, "issues": [], "next_actions": []}
    if state["detected"] and not state["bound"]:
        report["status"] = "selection_required"
        report["workspaces"] = state["workspaces"][:20]
        report["workspaces_omitted"] = max(0, len(state["workspaces"]) - 20)
        report["issues"].append({"code": "chat_workspace_required", "severity": "info",
                                 "message": "Ask the user which workspace to use for this chat before accessing memory."})
        report["next_actions"] = [
            {"code": "bind_current", "argv": ["richi", "--chat", chat_id, "chat", "bind", "--current"],
             "message": "After the user's choice, bind this chat to the current CLI workspace."},
            {"code": "bind_workspace", "argv": ["richi", "--chat", chat_id, "chat", "bind", "NAME"],
             "message": "After the user's choice, replace NAME with the selected workspace."}]
        return report
    pinned = pin_chat_state(state, selectors)
    custom = selectors.get("config_file") is not None or (
        selectors.get("workspace") is None and "RICHI_CONFIG" in os.environ)
    # Terminal defaults are snapshotted too; a concurrent global switch must not
    # disagree with the CLI selection reported above.
    if not state["detected"] and not custom and pinned.get("workspace") is None:
        pinned = dict(pinned, workspace=state["cli_current"])
    settings = resolve_settings(**pinned)
    report.update(workspace=settings.workspace,
                  selection=("chat" if state["bound"] else "custom_config" if custom else
                             "argument" if selectors.get("workspace") is not None else
                             "environment" if "RICHI_WORKSPACE" in os.environ else "cli_default"),
                  storage={"database": str(settings.database), "config_file": str(settings.config_file),
                           "data_dir": str(settings.data_dir)})
    prefix = ["richi"]
    if state["detected"]:
        prefix += ["--chat", chat_id]
    elif custom:
        prefix += ["--config", str(settings.config_file)]
    else:
        prefix += ["-w", settings.workspace]
    if not state["detected"] and (selectors.get("db") is not None or "RICHI_DB" in os.environ):
        prefix += ["--db", str(settings.database)]
    try:
        runtime = resolve_runtime(settings)
        report["runtime"] = dict(runtime.as_dict(), available=True)
        if project_path is None:
            try:
                project_path = str(Path.cwd())
            except OSError as exc:
                report["issues"].append({"code": "cwd_unavailable", "severity": "warning", "message": str(exc)})
        report["diagnostics"] = _worker(runtime, {
            "database": str(settings.database), "project_path": project_path,
            "project_id": args.project, "all_projects": args.projects, "limit": args.limit})
    except (ConfigError, OSError) as exc:
        report["status"] = "unavailable"
        report["runtime"] = dict(report["runtime"] or {
            "mode": "dev" if settings.dev else "release",
            "source": str(settings.development_source) if settings.development_source else None},
            available=False, error=str(exc))
        report["issues"].append({"code": "runtime_unavailable", "severity": "error", "message": str(exc)})
        if settings.dev:
            report["next_actions"].append({"code": "disable_dev", "argv": prefix + ["config", "set", "dev", "false"],
                                           "message": "If desired, explicitly return this workspace to the installed release."})
        return report
    diagnostics = report["diagnostics"]
    issues = report["issues"] + diagnostics["issues"]
    if (diagnostics["database"].get("status") != "ready"
            or diagnostics["projects"].get("status") == "partial"
            or any(issue.get("severity") in {"warning", "error"} for issue in issues)):
        report["status"] = "attention"
    if diagnostics["database"].get("status") == "missing":
        report["next_actions"].append({"code": "initialize", "argv": prefix + ["init"],
                                       "message": "Initialize the selected workspace if this is a new store."})
    elif diagnostics.get("project") and diagnostics["project"].get("status") != "matched":
        report["next_actions"].append({"code": "review_projects", "argv": prefix + ["project", "list"],
                                       "message": "Review registration in the selected workspace before accessing project memory."})
    return report
