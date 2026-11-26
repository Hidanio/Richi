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
    except OSError:
        return False


def _executable(path):
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def detected():
    if shutil.which("agy") or shutil.which("antigravity"):
        return True
    try:
        home = Path.home().resolve()
    except OSError:
        return False
    if _is_dir(profile_dir()):
        return True
    raw_app = (os.environ.get("ANTIGRAVITY_APP_DATA_DIR") or "").strip()
    if raw_app and (raw_app == "~" or raw_app.startswith("~/") or raw_app.startswith("/")):
        if _is_dir(home / raw_app[2:].lstrip("/") if raw_app.startswith("~") else Path(raw_app)):
            return True
    if any(_is_dir(home / ".gemini" / name) for name in ("antigravity", "antigravity-cli", "antigravity-ide")):
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

Antigravity supplies `ANTIGRAVITY_CONVERSATION_ID` and `ANTIGRAVITY_AGENT=1`
to command shells, and Richi identifies this conversation as
`antigravity:<conversation_id>`. If `ANTIGRAVITY_CONVERSATION_ID` is unavailable
or Richi reports conflicting agent environment variables (`chat_id_required`),
pass `--chat antigravity:<conversation_id>` explicitly on every Richi command;
never use an inherited ID from another agent. Each delegated subagent, workflow
agent or swarm worker receives its own `ANTIGRAVITY_CONVERSATION_ID`: pass
`--chat antigravity:<conversation_id>` when delegating memory work and use that
caller-supplied `--chat` on every Richi call in the child, or skip Richi
startup checks and binding when no memory task or `--chat` ID was delegated.

""" + block + b"\n"
    if len(content) > INSTRUCTION_LIMIT:
        raise ConfigError("Managed Antigravity rule exceeds the 24 KB rule limit")
    return content


def setup_operations(ctx, profile, current, replace_skill):
    return {}, []


def activation_operations(ctx, record, site):
    return {}, []
