"""Read Codex's path-based skill selection for both discovery and setup."""
import os
from pathlib import Path
import stat


class SettingsError(ValueError):
    """The profile may contain an opt-out that this runtime cannot verify."""


MAX_CONFIG = 1024 * 1024


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
            raise SettingsError("Cannot inspect Codex skill settings in " + str(config))
        with config.open("rb") as stream:
            data = stream.read(MAX_CONFIG + 1)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise SettingsError("Cannot inspect Codex skill settings in " + str(config)) from exc
    if len(data) > MAX_CONFIG:
        raise SettingsError("Codex config exceeds 1 MiB; cannot verify disabled skills in " + str(config))
    if b"false" not in data:
        return []
    # A quoted TOML key can encode the word skills using Unicode escapes.
    if b"skills" not in data and b"\\u" not in data and b"\\U" not in data:
        return []
    reader = _toml_reader()
    if reader is None:
        raise SettingsError("Cannot verify possible disabled Codex skills in " + str(config)
                            + " on this Python version; use a Richi installation on Python 3.11+"
                            " to verify the same settings before moving the skill")
    try:
        return reader.loads(data.decode("utf-8")).get("skills", {}).get("config", [])
    except (AttributeError, ValueError) as exc:
        raise SettingsError("Cannot parse Codex skill settings in " + str(config)) from exc


def entries(profile, home):
    """(absolute SKILL.md path, enabled) entries, relative to this profile."""
    config = profile / "config.toml"
    settings = _skill_settings(config)
    if not isinstance(settings, list):
        raise SettingsError("Cannot verify Codex skills.config in " + str(config))
    result = []
    for entry in settings:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            continue
        text = entry["path"]
        try:
            if "\x00" in text:
                raise ValueError("NUL in file path")
            path = home / text[2:].lstrip("/") if text == "~" or text.startswith("~/") else Path(text).expanduser()
        except (ValueError, RuntimeError) as exc:
            raise SettingsError("Cannot verify Codex skill path in " + str(config)) from exc
        result.append((profile / path, entry.get("enabled") is not False))
    return result


def path_settings(profile, home):
    """Canonical paths that may be disabled or enabled, including conflicts."""
    disabled, enabled = set(), set()
    for path, active in entries(profile, home):
        (enabled if active else disabled).add(os.path.realpath(path))
    return disabled, enabled
