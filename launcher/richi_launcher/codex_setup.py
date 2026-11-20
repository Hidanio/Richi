"""Codex-specific paths, instruction precedence and legacy skill adoption."""
import os
from pathlib import Path
import shutil

from . import setup_files as files
from .config import ConfigError

LABEL = "Codex"
PROFILE_ENV = "CODEX_HOME"
INSTRUCTION_LIMIT = 32768


def profile_dir():
    return Path(os.environ.get(PROFILE_ENV) or Path.home().resolve() / ".codex").expanduser()


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


def other_skill_roots(profile, discovery):
    root = profile / "skills"
    return [root] if root.is_dir() and os.path.realpath(root) != os.path.realpath(discovery) else []


def check_skill_change(record, previous, skill, target, *, check_alternates=False):
    """Preserve Codex's path-based opt-out when discovery or its target moves."""
    profiles = {(profile_dir(), Path.home() / ".agents/skills")}
    if record:
        profiles.add((Path(record["instructions"]).parent, Path(record["skill"]).parent))
    directory = os.path.realpath(skill.parent)
    for profile, shared in sorted(profiles):
        if any(os.path.realpath(root) == directory for root in (shared, profile / "skills")):
            _check_profile_skill(profile, previous, skill, target, check_alternates)


def _check_profile_skill(profile, previous, skill, target, check_alternates):
    names = ("project-memory", "richi")
    old = {Path(previous)} if previous else {skill.with_name(name) for name in names}
    if check_alternates:
        for root in [skill.parent, *other_skill_roots(profile, skill.parent)]:
            old.update(root / name for name in names if os.path.lexists(root / name))
    # An unchanged link keeps every setting applicable to its current target.
    if old == {skill} and skill.is_symlink() and os.readlink(skill) == str(target):
        return
    config = profile / "config.toml"
    from richi import codex_skill_settings
    try:
        settings = codex_skill_settings.entries(profile, Path.home())
    except codex_skill_settings.SettingsError as exc:
        raise ConfigError(str(exc)) from exc
    if not settings:
        return
    new_file = skill / "SKILL.md"
    discovery_file = Path(os.path.realpath(skill.parent)) / skill.name / "SKILL.md"
    # A user-owned link can be dangling or cyclic. It can still be backed up
    # by setup even if it has no discoverable SKILL.md to compare.
    new_target = os.path.realpath(Path(target) / "SKILL.md")
    previous_files = {directory / "SKILL.md" for directory in old}
    previous_targets = {os.path.realpath(path) for path in previous_files}
    was_disabled = False
    remains_disabled = False
    could_enable = False
    for path, enabled in settings:
        # Resolve profile-relative paths and aliases of the discovery root.
        # Keep the skill directory itself lexical: its symlink is about to move.
        location = Path(os.path.realpath(path.parent.parent)) / path.parent.name / path.name
        if enabled:
            if location == discovery_file or os.path.realpath(path) == new_target:
                could_enable = True
            continue
        if path in (old - {skill}) or path in previous_files or os.path.realpath(path) in previous_targets:
            was_disabled = True
        # Codex matches SKILL.md. A directory-only entry cannot establish that
        # the destination stays disabled. An old symlink might disappear.
        if location == discovery_file or str(location) == new_target:
            remains_disabled = True
    if was_disabled and (not remains_disabled or could_enable):
        raise ConfigError("Moving the Codex skill would bypass a disabled skill setting in " + str(config)
                          + ". Keep its existing [[skills.config]] entry and add one with enabled = false"
                          + " for " + str(new_file) + "; reconcile conflicting enabled entries, then retry")


def setup_operations(ctx, profile, current, replace_skill):
    return {}, []


def activation_operations(ctx, record, site):
    return {}, []
