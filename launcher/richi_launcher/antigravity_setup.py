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


def _executable(path):
    return path.is_file() and os.access(path, os.X_OK)


def detected():
    home = Path.home().resolve()
    if shutil.which("agy") or profile_dir().is_dir():
        return True
    if any((home / ".gemini" / name).is_dir() for name in ("antigravity", "antigravity-cli", "antigravity-ide")):
        return True
    for wrapper in (home / ".gemini/antigravity/bin/agy",
                    home / "Library/Application Support/Antigravity/bin/agy"):
        if _executable(wrapper):
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
`antigravity:<conversation_id>`. When delegating memory work to a subagent,
pass `--chat antigravity:<conversation_id>` so it uses this conversation's
confirmed workspace; if `ANTIGRAVITY_CONVERSATION_ID` is unavailable, pass
the active conversation's `--chat antigravity:<id>` explicitly. Never use an
inherited environment ID from another agent to identify an Antigravity chat.

""" + block + b"\n"
    if len(content) > INSTRUCTION_LIMIT:
        raise ConfigError("Managed Antigravity rule exceeds the 24 KB rule limit")
    return content


def setup_operations(ctx, profile, current, replace_skill):
    return {}, []


def activation_operations(ctx, record, site):
    return {}, []
