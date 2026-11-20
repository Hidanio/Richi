"""Codex-specific paths, instruction precedence and legacy skill adoption."""
import os
from pathlib import Path
import shutil
import stat

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


def other_skill_roots(profile, discovery):
    root = profile / "skills"
    return [root] if root.resolve() != discovery.resolve() else []


def _toml_reader():
    try:
        import tomllib
        return tomllib
    except ImportError:  # Python 3.9 and 3.10
        try:
            from pip._vendor import tomli
            return tomli
        except ImportError:
            return None


def _skill_settings(config):
    try:
        if not stat.S_ISREG(config.stat().st_mode):
            raise ConfigError("Cannot inspect Codex skill settings in " + str(config))
        with config.open("rb") as stream:
            data = stream.read(files.MAX_FILE + 1)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ConfigError("Cannot inspect Codex skill settings in " + str(config)) from exc
    if len(data) > files.MAX_FILE:
        raise ConfigError("Codex config exceeds 1 MiB; cannot verify disabled skills in " + str(config))
    if b"false" not in data:
        return []
    # A quoted TOML key can encode the word skills using Unicode escapes.
    if b"skills" not in data and b"\\u" not in data and b"\\U" not in data:
        return []
    reader = _toml_reader()
    if reader is None:
        raise ConfigError("Cannot verify possible disabled Codex skills in " + str(config)
                          + " on this Python version; use a Richi installation on Python 3.11+"
                          " to verify the same settings before moving the skill")
    try:
        return reader.loads(data.decode("utf-8")).get("skills", {}).get("config", [])
    except (AttributeError, ValueError) as exc:
        raise ConfigError("Cannot parse Codex skill settings in " + str(config)) from exc


def check_skill_change(instructions, previous, skill, target, replace_skill=False):
    """Preserve Codex's path-based opt-out when discovery or its target moves."""
    profile = Path(instructions).parent
    names = ("project-memory", "richi")
    old = {Path(previous)} if previous else {skill.with_name(name) for name in names}
    if replace_skill or previous is None:
        for root in [skill.parent, *other_skill_roots(profile, skill.parent)]:
            old.update(root / name for name in names if os.path.lexists(root / name))
    # An unchanged link keeps every setting applicable to its current target.
    if old == {skill} and skill.is_symlink() and os.readlink(skill) == str(target):
        return
    config = profile / "config.toml"
    settings = _skill_settings(config)
    if not isinstance(settings, list):
        raise ConfigError("Cannot verify Codex skills.config in " + str(config))
    if not settings:
        return
    new_file = skill / "SKILL.md"
    # A user-owned link can be dangling or cyclic. It can still be backed up
    # by setup even if it has no discoverable SKILL.md to compare.
    new_target = os.path.realpath(Path(target) / "SKILL.md")
    previous_files = {directory / "SKILL.md" for directory in old}
    previous_targets = {os.path.realpath(path) for path in previous_files}
    was_disabled = False
    remains_disabled = False
    could_enable = False
    for entry in settings:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if not isinstance(path, str):
            continue
        path = Path(path).expanduser()
        if not path.is_absolute():
            path = profile / path
        if entry.get("enabled") is not False:
            if path == new_file or os.path.realpath(path) == new_target:
                could_enable = True
            continue
        if path in (old - {skill}) or path in previous_files or os.path.realpath(path) in previous_targets:
            was_disabled = True
        # Codex matches SKILL.md. A directory-only entry cannot establish that
        # the destination stays disabled. An old symlink might disappear.
        if path == new_file or str(path) == new_target:
            remains_disabled = True
    if was_disabled and (not remains_disabled or could_enable):
        raise ConfigError("Moving the Codex skill would bypass a disabled skill setting in " + str(config)
                          + ". Keep its existing [[skills.config]] entry and add one with enabled = false"
                          + " for " + str(new_file) + "; reconcile conflicting enabled entries, then retry")


def _profile_skills(profile, discovery, replace_skill):
    ops = []
    for root in other_skill_roots(profile, discovery):
        for name in ("richi", "project-memory"):
            ops.extend(files.duplicate_skill(root / name, replace_skill))
    return {}, ops


def setup_operations(ctx, profile, current, replace_skill):
    return _profile_skills(profile, Path.home().resolve() / ".agents/skills", replace_skill)


def activation_operations(ctx, record, site):
    return _profile_skills(Path(record["instructions"]).parent, Path(record["skill"]).parent, False)
