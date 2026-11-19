"""Codex-specific paths, instruction precedence and legacy skill adoption."""
import os
from pathlib import Path
import shutil

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
    return profile, instructions, Path.home().resolve() / ".agents/skills/richi"


def _profile_skills(profile, discovery, replace_skill):
    ops = []
    if profile / "skills" != discovery:
        for name in ("richi", "project-memory"):
            ops.extend(files.duplicate_skill(profile / "skills" / name, replace_skill))
    return {}, ops


def setup_operations(ctx, profile, current, replace_skill):
    return _profile_skills(profile, Path.home().resolve() / ".agents/skills", replace_skill)


def activation_operations(ctx, record, site):
    return _profile_skills(Path(record["instructions"]).parent, Path(record["skill"]).parent, False)
