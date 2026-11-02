"""Codex-specific paths, instruction precedence and legacy skill adoption."""
import os
from pathlib import Path
import shutil
import uuid

from . import setup_files as files
from .config import ConfigError

LABEL = "Codex"
INSTRUCTION_LIMIT = 32768


def profile_dir():
    return Path(os.environ.get("CODEX_HOME") or Path.home().resolve() / ".codex").expanduser()


def detected():
    return bool(shutil.which("codex") or profile_dir().is_dir())


def paths(current=None):
    profile = files.safe_path(profile_dir() / "AGENTS.md").parent
    override = profile / "AGENTS.override.md"
    if current:
        instructions = Path(current["instructions"])
        if instructions.parent != profile:
            raise ConfigError("Setup belongs to another CODEX_HOME; use its original profile")
        if instructions.name == "AGENTS.md" and files.contents(files.snapshot(override)).strip():
            raise ConfigError("AGENTS.override.md now shadows the managed instructions; reconcile the profile before setup")
    else:
        instructions = override if files.contents(files.snapshot(override)).strip() else profile / "AGENTS.md"
    return profile, instructions, Path.home().resolve() / ".agents/skills/project-memory"


def setup_operations(ctx, profile, current, replace_skill):
    legacy = profile / "skills/project-memory"
    if files.snapshot(legacy)["kind"] == "absent":
        return {}, []
    if current or not replace_skill:
        raise ConfigError("A legacy project-memory skill exists at " + str(legacy) +
                          "; use --replace-skill on first setup to back it up and avoid duplicate skills")
    operation = files.change(legacy, {"kind": "absent"})
    operation["backup"] = str(profile / "richi-skill-backups" / ("project-memory-" + uuid.uuid4().hex))
    return {}, [operation]


def activation_operations(ctx, record, site):
    return {}, []
