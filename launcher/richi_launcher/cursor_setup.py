"""Local Cursor Agent integration for the user's ~/.cursor profile."""
import json
import os
from pathlib import Path
import shlex
import shutil
import sys

from . import setup_files as files
from .agent_adapters import literal
from .config import ConfigError, _unique_object

LABEL = "Cursor"
INSTRUCTION_LIMIT = None
SYSTEM_APPLICATIONS = Path("/Applications")


def profile_dir():
    return Path.home().resolve() / ".cursor"


def detected():
    if shutil.which("cursor") or shutil.which("cursor-agent") or profile_dir().is_dir():
        return True
    if sys.platform == "darwin":
        for directory in (SYSTEM_APPLICATIONS, Path.home() / "Applications"):
            executable = directory / "Cursor.app/Contents/MacOS/Cursor"
            if executable.is_file() and os.access(executable, os.X_OK):
                return True
    return False


def paths(current=None):
    profile = profile_dir()
    instructions = files.safe_path(profile / "rules/richi-project-memory.mdc")
    if current and Path(current["instructions"]) != instructions:
        raise ConfigError("Setup belongs to another Cursor profile; use its original home")
    return profile, instructions, profile / "skills/project-memory"


def instruction_file(block):
    return b"""---
description: Use Richi project memory in this Cursor chat
alwaysApply: true
globs:
---

For every Richi command below, insert the exact `--chat cursor:...` supplied by
the native Cursor sessionStart hook immediately after the executable name. This
includes the first chat and workspace checks. If that identity is unavailable,
continue without accessing Richi memory until Cursor supplies it. Never use an
inherited environment ID or a different agent's hook to identify a Cursor chat.

""" + block + b"\n"


# Cursor guarantees env for subsequent hooks. The agent's shell gets its
# explicit chat selector via additional_context, independently of shell env.
HOOK = '''import json, re, sys
payload = json.load(sys.stdin)
if not isinstance(payload, dict) or payload.get("hook_event_name") != "sessionStart":
    raise SystemExit("Richi: expected Cursor sessionStart")
session = payload.get("session_id")
if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,192}", session):
    raise SystemExit("Richi: invalid Cursor session identity")
if payload.get("conversation_id", session) != session:
    raise SystemExit("Richi: conflicting Cursor session and conversation identities")
chat = "cursor:" + session
command = "richi --chat " + chat
context = (
    "Use the project-memory skill in this Cursor chat. Use " + command +
    " for every Richi command, even if other agent hooks or environment variables provide an ID. "
    "If richi is absent from PATH, use ~/.local/bin/" + command + ". "
    "Start with " + command + " chat current and " + command + " workspace list. "
    "If unbound, ask the user once to select a workspace unless they have already chosen one "
    "in this chat. Bind their choice before accessing memory and reuse it across turns. "
    "Once bound, load the user's standing rules with " + command + " rules show --agent cursor "
    "(for repository work: " + command + " --project-path PATH rules show --agent cursor)."
)
print(json.dumps({"env": {"RICHI_CHAT_ID": chat}, "additional_context": context}))
'''


def setup_operations(ctx, profile, current, replace_skill):
    return _hook_operations(ctx, profile, current, HOOK)


def activation_operations(ctx, record, site):
    content = literal(Path(site) / "richi_launcher/cursor_setup.py", "HOOK")
    if not isinstance(content, str):
        raise ConfigError("Invalid Cursor session hook in selected release")
    profile = Path(record["instructions"]).parent.parent
    return _hook_operations(ctx, profile, record, content)


def _calls_script(entry, profile, script):
    command = entry.get("command") if isinstance(entry, dict) else None
    if not isinstance(command, str):
        return False
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        args = list(lexer)
    except ValueError:
        return False
    for arg in args:
        root, slash, tail = arg.partition("/")
        path = profile.parent / tail if slash and root in {"~", "$HOME", "${HOME}"} else profile / arg
        if os.path.normpath(path) == str(script):
            return True
    return False


def _hook_operations(ctx, profile, current, content):
    script = profile / "richi/session_start.py"
    script_bytes = content.encode()
    expected = current.get("hook_sha256") if current else None
    if current and not isinstance(expected, str):
        raise ConfigError("Missing digest for the managed Cursor session hook")
    script_op = files.managed_file(script, script_bytes, expected=expected, label="Cursor session hook")

    command = shlex.join([str(Path(ctx["original_prefix"]) / "bin/python"), "-I", str(script)])
    hook = {"command": command}
    path = profile / "hooks.json"
    before = files.snapshot(path)
    raw = files.contents(before)
    try:
        data = json.loads(raw, object_pairs_hook=_unique_object) if before["kind"] != "absent" else {}
        if not isinstance(data, dict):
            raise ValueError("expected an object")
        version = data.setdefault("version", 1)
        if type(version) is not int or version != 1:
            raise ValueError("unsupported hooks version")
        hooks = data.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise ValueError("hooks must be an object")
        starts = hooks.setdefault("sessionStart", [])
        if not isinstance(starts, list):
            raise ValueError("sessionStart must be a list")
        related = [entry for entry in starts if _calls_script(entry, profile, script)]
        if current:
            previous = current["hook_entry"]
            if related != [previous]:
                raise ValueError("managed sessionStart hook was edited, removed or duplicated")
            starts[starts.index(previous)] = hook
        else:
            if related:
                raise ValueError("unmanaged Richi sessionStart hook already exists")
            starts.append(hook)
        # Refuse a configuration Cursor's JSON reader could not load. This
        # also catches numbers the Python decoder converted to infinity.
        encoded = (json.dumps(data, indent=2, allow_nan=False) + "\n").encode()
        if raw and json.loads(raw) == data:
            encoded = raw
    except (KeyError, ValueError, RecursionError) as exc:
        raise ConfigError("Cannot safely merge Cursor hooks: " + str(exc)) from exc
    return {"hook_sha256": files.digest(script_bytes), "hook_entry": hook}, [
        script_op,
        files.change(path, files.file_value(encoded, before.get("mode", 0o600)), before=before),
    ]
