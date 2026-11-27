"""Local Antigravity integration for the user's ~/.gemini/config profile."""
import os
from pathlib import Path
import shutil
import sys

from . import setup_files as files
from .config import ConfigError

LABEL = "Antigravity"
INSTRUCTION_LIMIT = 24000
SYSTEM_APPLICATIONS = Path("/Applications")


def profile_dir():
    return Path.home().resolve() / ".gemini/config"


def _is_dir(path):
    try:
        return path.is_dir()
    except (OSError, ValueError):
        return False


def _safe_app_dir(path, home):
    if not _is_dir(path):
        return False
    try:
        return path.resolve() not in {Path("/"), home}
    except (OSError, RuntimeError, ValueError):
        return False


def _executable(path):
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except (OSError, ValueError):
        return False


def detected():
    if shutil.which("agy") or shutil.which("antigravity"):
        return True
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    if _is_dir(home / ".gemini/config"):
        return True
    raw_app = (os.environ.get("ANTIGRAVITY_APP_DATA_DIR") or "").strip()
    if raw_app.startswith("/") and "\x00" not in raw_app and _safe_app_dir(Path(raw_app), home):
        return True
    if any(_safe_app_dir(home / ".gemini" / name, home) for name in ("antigravity", "antigravity-ide", "antigravity-cli")):
        return True
    if _executable(home / "Library/Application Support/Antigravity/bin/agy"):
        return True
    if sys.platform == "darwin":
        for directory in (SYSTEM_APPLICATIONS, home / "Applications"):
            if _executable(directory / "Antigravity.app/Contents/MacOS/Antigravity"):
                return True
    return False


def paths(current=None):
    profile = profile_dir()
    instructions = files.safe_path(profile / "rules/richi-project-memory.md")
    if current and Path(current["instructions"]) != instructions:
        raise ConfigError("Setup belongs to another Antigravity profile; use its original home")
    return profile, instructions, files.safe_path(profile / "skills/richi")


def instruction_file(block):
    content = b"""---
trigger: always_on
---

Richi reads `ANTIGRAVITY_CONVERSATION_ID` as `antigravity:<conversation_id>`.
If it is unavailable or Richi reports `chat_id_required`, pass
`--chat antigravity:<conversation_id>` on every Richi command; never use another
agent's ID. Delegated subagents, workflows, swarm workers and unattended
`/automation` runs each receive a fresh `ANTIGRAVITY_CONVERSATION_ID`: pass
`--chat antigravity:<conversation_id>` when delegating memory work, and in a
delegated or `/automation` run use Richi only with an explicit caller- or
prompt-supplied `--chat` (otherwise skip Richi startup checks, binding and
memory access). If sandboxing blocks Richi's user directories, `~/.gemini/config`
or a worktree's main `.git` directory, request unsandboxed execution instead of
overriding `HOME`, storage options or chat variables.

""" + block + b"\n"
    if len(content) > INSTRUCTION_LIMIT:
        raise ConfigError("Managed Antigravity rule exceeds the 24 KB rule limit")
    return content


def setup_operations(ctx, profile, current, replace_skill):
    return {}, []


def activation_operations(ctx, record, site):
    return {}, []
