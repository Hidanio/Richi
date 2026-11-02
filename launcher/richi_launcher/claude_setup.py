"""Claude Code SessionStart adapter. No knowledge-store access."""
import json
import os
from pathlib import Path
import shlex
import shutil

from . import setup_files as files
from .config import ConfigError

LABEL = "Claude Code"
INSTRUCTION_LIMIT = None


def profile_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home().resolve() / ".claude").expanduser()


def detected():
    return bool(shutil.which("claude") or profile_dir().is_dir())


def paths(current=None):
    profile = files.safe_path(profile_dir() / "CLAUDE.md").parent
    instructions = profile / "CLAUDE.md"
    if current and Path(current["instructions"]) != instructions:
        raise ConfigError("Setup belongs to another CLAUDE_CONFIG_DIR; use its original profile")
    return profile, instructions, profile / "skills/project-memory"


def setup_operations(ctx, profile, current, replace_skill):
    return hook_operations(ctx, profile, current)


def activation_operations(ctx, record, site):
    from .agent_adapters import literal
    content = literal(Path(site) / "richi_launcher/claude_setup.py", "HOOK")
    if not isinstance(content, str):
        raise ConfigError("Invalid Claude session hook in selected release")
    return hook_operations(ctx, Path(record["instructions"]).parent, record, content)


# A standalone hook uses the permanent installation's Python, not shell PATH.
HOOK = '''import json, os, re, shlex, sys
payload = json.load(sys.stdin)
session = payload.get("session_id")
if payload.get("hook_event_name") != "SessionStart" or not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", session):
    raise SystemExit("Richi: invalid Claude session identity")
chat = "claude:" + session
path = os.environ.get("CLAUDE_ENV_FILE")
if path:
    with open(path, "a", encoding="utf-8") as stream:
        stream.write("export RICHI_CHAT_ID=" + shlex.quote(chat) + "\\n")
print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext":
    "Richi chat identity: " + chat + ". Use richi --chat " + chat + " for every Richi call if RICHI_CHAT_ID is unavailable. Never invent another identity or select a workspace without the user's choice."}}))
'''


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate settings key: " + key)
        result[key] = value
    return result


def hook_operations(ctx, profile, current, content=None):
    script = profile / "richi/session_start.py"
    value = files.snapshot(script)
    content = (HOOK if content is None else content).encode()
    if current:
        if files.digest(files.contents(value)) != current.get("hook_sha256"):
            raise ConfigError("Managed Claude hook was edited or removed; preserved it")
    elif value["kind"] != "absent":
        raise ConfigError("Another file owns the Claude hook path; preserved it")
    command = shlex.quote(str(Path(ctx["original_prefix"]) / "bin/python")) + " -I " + shlex.quote(str(script))
    hook = {"hooks": [{"type": "command", "command": command}]}
    settings = profile / "settings.json"
    before = files.snapshot(settings)
    try:
        data = json.loads(files.contents(before), object_pairs_hook=_unique_keys) if before["kind"] != "absent" else {}
        if not isinstance(data, dict):
            raise ValueError("settings must be an object")
        hooks = data.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise ValueError("hooks must be an object")
        starts = hooks.setdefault("SessionStart", [])
        if not isinstance(starts, list):
            raise ValueError("SessionStart must be a list")
        if current:
            if starts.count(current["hook_entry"]) != 1:
                raise ValueError("managed SessionStart hook was edited or removed")
            starts[starts.index(current["hook_entry"])] = hook
        else:
            if any(str(script) in json.dumps(item) for item in starts):
                raise ValueError("unmanaged Richi hook already exists")
            starts.append(hook)
    except (ValueError, KeyError, TypeError) as exc:
        raise ConfigError("Cannot safely merge Claude settings: " + str(exc)) from exc
    encoded = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode()
    # Preserve exact bytes when the semantic settings did not change.
    if before["kind"] != "absent" and json.loads(files.contents(before)) == data:
        encoded = files.contents(before)
    return {"hook_sha256": files.digest(content), "hook_entry": hook}, [
        files.change(script, files.file_value(content)),
        files.change(settings, files.file_value(encoded, before.get("mode", 0o600)))]
