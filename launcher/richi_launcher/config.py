"""Portable configuration resolution and explicit atomic configuration updates.

Precedence: command-line values, RICHI_* environment, JSON configuration,
platform user directories. Relative JSON paths belong to the config directory;
relative command-line and environment paths belong to the working directory.
Resolving settings never creates directories or opens a database.
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Optional


class ConfigError(ValueError):
    """Invalid or unreadable configuration; suitable for a JSON CLI error."""


@dataclass(frozen=True)
class Settings:
    database: Path
    data_dir: Path
    port: int
    config_file: Path
    dev: bool = False
    development_source: Optional[Path] = None

    def as_dict(self):
        return {"database": str(self.database), "data_dir": str(self.data_dir),
                "port": self.port, "config_file": str(self.config_file),
                "dev": self.dev,
                "development": {"source": (str(self.development_source)
                                             if self.development_source is not None else None)}}


def _path(value, label, base=None, resolve_links=True):
    if not isinstance(value, (str, os.PathLike)):
        raise ConfigError(label + " must be a nonempty path")
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise ConfigError(label + " must be a nonempty path without NUL bytes")
    try:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (base if base is not None else Path.cwd()) / path
        return path.resolve() if resolve_links else Path(os.path.abspath(path))
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigError("Invalid " + label + ": " + str(exc)) from exc


def _xdg_dir(name, fallback):
    # XDG treats unset, empty, and non-absolute values as unconfigured. This
    # differs from explicit RICHI_* values, which are validated as user input.
    value = os.environ.get(name)
    if not value or not Path(value).is_absolute():
        return fallback
    return _path(value, name)


def _platform_dirs():
    home = Path.home()
    if sys.platform == "darwin":
        common = home / "Library" / "Application Support" / "Richi"
        return common, common
    if sys.platform == "win32":
        config = _path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")), "APPDATA")
        data = _path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")), "LOCALAPPDATA")
        return config / "Richi", data / "Richi"
    config = _xdg_dir("XDG_CONFIG_HOME", home / ".config")
    data = _xdg_dir("XDG_DATA_HOME", home / ".local" / "share")
    return config / "richi", data / "richi"


def _port(value, label, json_value=False):
    if isinstance(value, bool) or (json_value and not isinstance(value, int)):
        raise ConfigError(label + " must be an integer between 1 and 65535")
    if not isinstance(value, (str, int)):
        raise ConfigError(label + " must be an integer between 1 and 65535")
    try:
        result = int(value)
    except ValueError as exc:
        raise ConfigError(label + " must be an integer between 1 and 65535") from exc
    if not 1 <= result <= 65535:
        raise ConfigError(label + " must be an integer between 1 and 65535")
    return result


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("Duplicate config key: " + key)
        result[key] = value
    return result


def _read_config(path, required):
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        if not required:
            return {}
        raise ConfigError("Config file does not exist: " + str(path)) from exc
    except (OSError, UnicodeError) as exc:
        raise ConfigError("Cannot read config file " + str(path) + ": " + str(exc)) from exc
    try:
        config = json.loads(text, object_pairs_hook=_unique_object)
    except (ValueError, ConfigError) as exc:
        raise ConfigError("Invalid config file " + str(path) + ": " + str(exc)) from exc
    if not isinstance(config, dict):
        raise ConfigError("Config must be a JSON object: " + str(path))
    return config


def _validate_config(raw, path):
    # Keep the original JSON values intact for the explicit config writer.
    config = dict(raw)
    unknown = set(config) - {"database", "data_dir", "port", "dev", "development"}
    if unknown:
        raise ConfigError("Unknown config keys: " + ", ".join(sorted(unknown)))
    # Validate every supplied setting, even when a higher-priority value overrides it.
    for key in ("database", "data_dir"):
        if key in config:
            config[key] = _path(config[key], "config " + key, base=path.parent)
    if "port" in config:
        config["port"] = _port(config["port"], "config port", json_value=True)
    if "dev" in config and type(config["dev"]) is not bool:
        raise ConfigError("config dev must be a boolean")
    if "development" in config:
        development = config["development"]
        if not isinstance(development, dict):
            raise ConfigError("config development must be an object")
        unknown = set(development) - {"source"}
        if unknown:
            raise ConfigError("Unknown development config keys: " + ", ".join(sorted(unknown)))
        source = development.get("source")
        config["development"] = {"source": (_path(source, "config development.source", base=path.parent,
                                                  resolve_links=False)
                                             if source is not None else None)}
    return config


def _load(path, required):
    return _validate_config(_read_config(path, required), path)


def _config_path(config_file=None):
    config_dir, _ = _platform_dirs()
    configured_file = config_file if config_file is not None else os.environ.get("RICHI_CONFIG")
    return _path(configured_file if configured_file is not None else config_dir / "config.json",
                 "config file"), configured_file is not None


def resolve_settings(db=None, config_file=None, port=None):
    """Resolve settings without writes, checkout inspection, imports, or database access."""
    _, platform_data = _platform_dirs()
    path, required = _config_path(config_file)
    values = _load(path, required=required)
    if "RICHI_DATA_DIR" in os.environ:
        data_dir = _path(os.environ["RICHI_DATA_DIR"], "RICHI_DATA_DIR")
    else:
        data_dir = values.get("data_dir", platform_data.resolve())
    if db is not None:
        database = _path(db, "--db")
    elif "RICHI_DB" in os.environ:
        database = _path(os.environ["RICHI_DB"], "RICHI_DB")
    else:
        database = values.get("database", data_dir / "memory.sqlite3")
    if port is not None:
        resolved_port = _port(port, "--port")
    elif "RICHI_PORT" in os.environ:
        resolved_port = _port(os.environ["RICHI_PORT"], "RICHI_PORT")
    else:
        resolved_port = values.get("port", 8765)
    return Settings(database=database, data_dir=data_dir, port=resolved_port, config_file=path,
                    dev=values.get("dev", False),
                    development_source=values.get("development", {}).get("source"))


def set_config_value(key, value, config_file=None):
    """Set dev or development.source atomically, returning the selected config path.

    Source paths supplied here belong to the caller's working directory. The
    selected checkout need not exist or import successfully: disabling dev must
    remain possible when development code is unavailable.
    """
    if key == "dev":
        if type(value) is not bool:
            raise ConfigError("config dev must be a boolean")
    elif key == "development.source":
        value = str(_path(value, "development.source", resolve_links=False)) if value is not None else None
    else:
        raise ConfigError("Only dev and development.source can be set")
    path, _ = _config_path(config_file)
    try:
        import fcntl
    except ImportError as exc:
        raise ConfigError("Config updates require macOS or Linux file locks") from exc
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = path.with_name(path.name + ".lock")
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(lock_fd, "a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            raw = _read_config(path, required=False)
            if key == "dev":
                raw["dev"] = value
            else:
                development = raw.get("development", {})
                if not isinstance(development, dict):
                    raise ConfigError("config development must be an object")
                raw["development"] = dict(development, source=value)
            _validate_config(raw, path)
            mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
            fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), mode)
                json.dump(raw, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
    except OSError as exc:
        raise ConfigError("Cannot update config file " + str(path) + ": " + str(exc)) from exc
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    return path
