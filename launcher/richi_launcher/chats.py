"""Chat-local workspace choices; bounded metadata only, never knowledge access."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re

from .config import ConfigError, _platform_dirs, _unique_object, resolve_settings
from .workspaces import _atomic_json, _name, _read_registry as workspace_registry


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")
_MAX_BYTES = 4 * 1024 * 1024
_MAX_CHATS = 10000
_STORAGE_ENV = ("RICHI_CONFIG", "RICHI_DB", "RICHI_DATA_DIR")
CHAT_ID_VARS = ("RICHI_CHAT_ID", "CODEX_THREAD_ID", "ANTIGRAVITY_CONVERSATION_ID")
AGENT_SHELL_VARS = ("CLAUDECODE", "CURSOR_AGENT", "ANTIGRAVITY_AGENT")
PREFIXED_AGENTS = ("cursor", "claude", "antigravity")
_MULTI_AGENT_MSG = ("Multiple agent chat environments detected; "
                    "pass --chat with the active client's conversation ID.")


class ChatError(ConfigError):
    """Machine-readable action needed before a chat may access knowledge."""
    def __init__(self, message, code, **details):
        super().__init__(message)
        self.details = dict(error=message, code=code, **details)


def _chat_id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ChatError("Chat ID must be 1–200 ASCII letters, digits, '.', '_', ':' or '-', starting with a letter or digit",
                        "invalid_chat_id")
    return value


def detect_chat(explicit=None):
    """Explicit IDs support other clients; no directory/session-based identity."""
    if explicit is not None:
        return _chat_id(explicit)
    raw_richi = os.environ.get("RICHI_CHAT_ID")
    has_codex = "CODEX_THREAD_ID" in os.environ
    has_claude = os.environ.get("CLAUDECODE") == "1"
    has_agy_id = "ANTIGRAVITY_CONVERSATION_ID" in os.environ
    has_agy = has_agy_id or bool(os.environ.get("ANTIGRAVITY_AGENT"))
    # Cursor's session hook supplies the ID to the conversation. Shell
    # variables may be absent or inherited from a different conversation.
    if os.environ.get("CURSOR_AGENT"):
        if (has_agy or has_codex or has_claude
                or (raw_richi and not raw_richi.startswith("cursor:"))):
            raise ChatError(_MULTI_AGENT_MSG, "chat_id_required")
        raise ChatError("Pass --chat with the active client's conversation ID in a Cursor Agent shell. "
                        "Cursor supplies --chat cursor:<id> in its sessionStart hook.",
                        "chat_id_required")
    if has_agy:
        raw_agy = os.environ.get("ANTIGRAVITY_CONVERSATION_ID", "")
        if (has_codex or has_claude
                or (has_agy_id and raw_richi is not None and raw_richi != "antigravity:" + raw_agy)
                or (not has_agy_id and raw_richi and not raw_richi.startswith("antigravity:"))):
            raise ChatError(_MULTI_AGENT_MSG, "chat_id_required")
        if not has_agy_id:
            raise ChatError("Pass --chat with the active conversation ID in an Antigravity shell when "
                            "ANTIGRAVITY_CONVERSATION_ID is unavailable.",
                            "chat_id_required")
        return _chat_id("antigravity:" + _chat_id(raw_agy))
    if (has_claude and (has_codex or (raw_richi is not None and raw_richi.startswith(("cursor:", "antigravity:"))))
            or (has_codex and raw_richi is not None and raw_richi.startswith(("claude:", "cursor:", "antigravity:")))):
        raise ChatError(_MULTI_AGENT_MSG, "chat_id_required")
    if raw_richi is not None:
        return _chat_id(raw_richi)
    if has_codex:
        return _chat_id(os.environ["CODEX_THREAD_ID"])
    return None


def _registry_path():
    return _platform_dirs()[0] / "chat-workspaces.json"


def _read_registry():
    path = _registry_path()
    try:
        with path.open("rb") as stream:
            data = stream.read(_MAX_BYTES + 1)
    except FileNotFoundError:
        return {"version": 1, "chats": {}}
    except OSError as exc:
        raise ChatError("Cannot read chat bindings: " + str(exc), "chat_registry_invalid") from exc
    if len(data) > _MAX_BYTES:
        raise ChatError("Chat binding registry exceeds 4 MiB", "chat_registry_invalid")
    try:
        raw = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
        if (not isinstance(raw, dict) or set(raw) != {"version", "chats"}
                or type(raw["version"]) is not int or raw["version"] != 1
                or not isinstance(raw["chats"], dict) or len(raw["chats"]) > _MAX_CHATS):
            raise ValueError("expected version 1 and at most 10000 chat records")
        for chat_id, record in raw["chats"].items():
            _chat_id(chat_id)
            if not isinstance(record, dict) or set(record) != {"workspace"}:
                raise ValueError("each chat requires exactly one workspace")
            _name(record["workspace"])
    except (ValueError, UnicodeError) as exc:
        raise ChatError("Invalid chat binding registry " + str(path) + ": " + str(exc),
                        "chat_registry_invalid") from exc
    return raw


@contextmanager
def _registry_lock():
    try:
        import fcntl
    except ImportError as exc:
        raise ConfigError("Chat binding updates require macOS or Linux file locks") from exc
    path = _registry_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(path.with_name(path.name + ".lock")), os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "a") as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            yield
    except OSError as exc:
        raise ChatError("Cannot update chat bindings: " + str(exc), "chat_registry_write_failed") from exc


def _cli_current(registry):
    # Environment workspace selection is part of the ordinary CLI's current
    # choice. Custom config/storage cannot become a registered chat workspace.
    return os.environ.get("RICHI_WORKSPACE", registry["current"])


def needs_explicit_chat(chat_id):
    if chat_id is None:
        return False
    if chat_id.startswith("cursor:") or os.environ.get("CURSOR_AGENT"):
        return True
    try:
        detected = detect_chat()
    except ChatError:
        return True
    if chat_id.startswith(("claude:", "antigravity:")):
        return detected != chat_id
    return detected is not None and detected != chat_id


def _next_commands(chat_id, bound):
    need_chat = needs_explicit_chat(chat_id)
    cli = "richi --chat " + _chat_id(chat_id) if need_chat else "richi"
    if not bound:
        return [cli + " chat bind --current", cli + " chat bind NAME"]
    prefix = chat_id.split(":", 1)[0] if chat_id is not None and ":" in chat_id else None
    rules = "rules show --agent " + prefix if prefix in PREFIXED_AGENTS and need_chat else "rules show"
    return [cli + " " + rules, cli + " --project-path PATH " + rules]


def current_chat(chat_id):
    registry = workspace_registry()
    current = _cli_current(registry)
    bound = _read_registry()["chats"].get(chat_id) if chat_id is not None else None
    workspace = bound["workspace"] if bound else None
    if workspace is not None and workspace not in registry["workspaces"]:
        raise ChatError("Chat is bound to an unknown workspace: " + workspace,
                        "chat_workspace_missing", chat_id=chat_id, workspace=workspace)
    return {"chat_id": chat_id, "detected": chat_id is not None, "bound": bound is not None,
            "workspace": workspace, "cli_current": current,
            "cli_default": registry["current"],
            "workspaces": sorted(registry["workspaces"], key=lambda name: (name != "default", name)),
            "next_commands": _next_commands(chat_id, bound is not None)}


def storage_override_error(overrides):
    """The error of a chat that names storage of its own: OVERRIDES are the
    options and variables to remove."""
    return ChatError("Chat workspaces use registered storage; remove/unset " + ", ".join(overrides),
                     "chat_storage_override", selectors=overrides)


def reject_storage_overrides(selectors):
    overrides = [flag for key, flag in (("config_file", "--config"), ("db", "--db"))
                 if selectors.get(key) is not None]
    overrides.extend(key for key in _STORAGE_ENV if key in os.environ)
    if overrides:
        raise storage_override_error(overrides)


def bind_chat(chat_id, workspace=None, current=False, replace=False):
    if chat_id is None:
        raise ChatError("No chat ID detected; use --chat ID or set RICHI_CHAT_ID",
                        "chat_id_required")
    _chat_id(chat_id)
    reject_storage_overrides({})
    if current:
        # Resolve once before acquiring the chat lock: a concurrent CLI default
        # change cannot redirect the saved choice.
        workspace = resolve_settings().workspace
    elif workspace is None:
        raise ChatError("Choose the current CLI workspace with --current, or provide a workspace name",
                        "chat_workspace_required")
    workspace = _name(workspace)
    settings = resolve_settings(workspace=workspace)
    with _registry_lock():
        registry = _read_registry()
        previous = registry["chats"].get(chat_id)
        if previous is not None and previous["workspace"] != workspace and not replace:
            raise ChatError("Chat is already bound to " + previous["workspace"]
                            + "; changing it requires chat bind NAME --replace",
                            "chat_rebind_required", chat_id=chat_id,
                            workspace=previous["workspace"], requested_workspace=workspace)
        changed = previous != {"workspace": workspace}
        if changed:
            if previous is None and len(registry["chats"]) >= _MAX_CHATS:
                raise ChatError("Chat binding registry has reached 10000 chats", "chat_registry_full")
            registry["chats"][chat_id] = {"workspace": workspace}
            if len(json.dumps(registry, ensure_ascii=False, indent=2).encode("utf-8")) + 1 > _MAX_BYTES:
                raise ChatError("Chat binding registry would exceed 4 MiB", "chat_registry_full")
            _atomic_json(_registry_path(), registry)
    # Do not re-read the binding: the result belongs to this transaction even if
    # another process deliberately rebinds immediately after releasing its lock.
    return {"chat_id": chat_id, "detected": True, "bound": True, "workspace": workspace,
            "changed": changed, "replaced": previous is not None and changed,
            "config_file": str(settings.config_file), "database": str(settings.database),
            "data_dir": str(settings.data_dir), "next_commands": _next_commands(chat_id, True)}


def pin_chat(chat_id, selectors, metadata=False):
    """Snapshot one chat selection for the entire invocation before runtime load."""
    if chat_id is None:
        return selectors
    return pin_chat_state(current_chat(chat_id), selectors, metadata)


def pin_chat_state(state, selectors, metadata=False):
    """Validate a selection already snapshotted by this invocation."""
    if not state["detected"]:
        return selectors
    chat_id = state["chat_id"]
    if not state["bound"]:
        if metadata:
            return selectors
        raise ChatError("Choose a workspace for this chat before accessing knowledge",
                        "chat_workspace_required", **state)
    reject_storage_overrides(selectors)
    selected = state["workspace"]
    for label, value in (("--workspace", selectors.get("workspace")),
                         ("RICHI_WORKSPACE", os.environ.get("RICHI_WORKSPACE"))):
        if value is not None and value != selected:
            raise ChatError(label + " conflicts with this chat's workspace " + selected,
                            "chat_workspace_conflict", chat_id=chat_id,
                            workspace=selected, requested_workspace=value, selector=label)
    return dict(selectors, workspace=selected)
