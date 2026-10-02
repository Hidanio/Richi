"""Explicit, isolated workspaces, with a small atomic user-local registry.

A missing registry means the existing default store. Reads never create files or
open SQLite. New workspaces own a fresh directory; selecting one never copies,
initializes, migrates, or combines knowledge stores.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import tempfile

from .config import ConfigError, _load, _path, _platform_dirs, _unique_object


_NAME = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")


def _name(value):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ConfigError("Workspace name must match [a-z][a-z0-9_-]{0,47}")
    return value


def _registry_path():
    return _platform_dirs()[0] / "workspaces.json"


def _default_config():
    return _path(_platform_dirs()[0] / "config.json", "default config")


def _implicit_registry():
    return {"version": 1, "current": "default", "workspaces": {
        "default": {"config_file": str(_default_config())}}}


def _read_registry():
    path = _registry_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _implicit_registry()
    except (OSError, UnicodeError) as exc:
        raise ConfigError("Cannot read workspace registry " + str(path) + ": " + str(exc)) from exc
    try:
        raw = json.loads(text, object_pairs_hook=_unique_object)
    except ValueError as exc:
        raise ConfigError("Invalid workspace registry " + str(path) + ": " + str(exc)) from exc
    if (not isinstance(raw, dict) or set(raw) != {"version", "current", "workspaces"}
            or type(raw["version"]) is not int or raw["version"] != 1
            or not isinstance(raw["workspaces"], dict)):
        raise ConfigError("Invalid workspace registry: expected version 1, current and workspaces")
    current = _name(raw["current"])
    if "default" not in raw["workspaces"] or current not in raw["workspaces"]:
        raise ConfigError("Workspace registry must contain default and its current workspace")
    records = {}
    seen = set()
    for name, record in raw["workspaces"].items():
        _name(name)
        if (not isinstance(record, dict) or set(record) != {"config_file"}
                or not isinstance(record["config_file"], str)
                or not Path(record["config_file"]).is_absolute()):
            raise ConfigError("Workspace " + name + " requires an absolute config_file")
        config = _path(record["config_file"], "workspace config")
        if name != "default":
            expected = _path(_platform_dirs()[1], "workspace data directory") / "workspaces" / name / "config.json"
            if config != expected:
                raise ConfigError("Workspace " + name + " must use its managed config directory")
        if config in seen:
            raise ConfigError("Workspaces cannot share a config file: " + str(config))
        seen.add(config)
        records[name] = {"config_file": str(config)}
    if Path(records["default"]["config_file"]) != _default_config():
        raise ConfigError("Default workspace must keep the platform default config file")
    return {"version": 1, "current": current, "workspaces": records}


def choose_config(workspace=None, config_file=None):
    """Return (config path, required, workspace name), without filesystem writes.

    Explicit config bypasses workspace selection. Explicit workspace beats the
    environment config. Environment config then workspace precede the saved
    current workspace. The default config remains optional.
    """
    if workspace is not None and config_file is not None:
        raise ConfigError("--workspace and --config cannot be combined")
    if config_file is not None:
        return _path(config_file, "config file"), True, None
    if workspace is None and "RICHI_CONFIG" in os.environ:
        return _path(os.environ["RICHI_CONFIG"], "RICHI_CONFIG"), True, None
    if workspace is None:
        workspace = os.environ.get("RICHI_WORKSPACE")
    if workspace is not None:
        _name(workspace)
    registry = _read_registry()
    name = workspace if workspace is not None else registry["current"]
    if name not in registry["workspaces"]:
        raise ConfigError("Unknown workspace: " + name)
    return Path(registry["workspaces"][name]["config_file"]), name != "default", name


def _validate_storage(name, path, values):
    """Managed workspace stores cannot be redirected into another directory."""
    if name in (None, "default"):
        return
    directory = path.parent
    database = directory / "memory.sqlite3"
    if (values.get("data_dir", directory) != directory
            or values.get("database", database) != database
            or database.is_symlink()):
        raise ConfigError("Workspace " + name + " must use isolated storage in " + str(directory))
    # A pre-existing customized default may already own this path. Do not treat
    # it as a fresh isolated store, even though its registry config differs.
    try:
        default = _load(_default_config(), required=False)
    except ConfigError:
        return  # A broken default does not prevent recovery into another workspace.
    default_dir = default.get("data_dir", _path(_platform_dirs()[1], "workspace data directory"))
    default_db = default.get("database", default_dir / "memory.sqlite3")
    if default_dir == directory or default_db == database:
        raise ConfigError("Workspace " + name + " storage overlaps the default workspace")


def _status(name, record):
    """Display stored paths and presence only, ignoring ambient storage overrides."""
    result = {"name": name, "config_file": record["config_file"]}
    try:
        path = Path(record["config_file"])
        values = _load(path, required=name != "default")
        _validate_storage(name, path, values)
        data_dir = values.get("data_dir", path.parent if name != "default" else _path(_platform_dirs()[1], "workspace data directory"))
        database = values.get("database", data_dir / "memory.sqlite3")
        result.update(data_dir=str(data_dir), database=str(database), initialized=database.is_file())
    except (ConfigError, OSError, RuntimeError) as exc:
        result.update(initialized=False, error=str(exc))
    return result


def list_workspaces():
    registry = _read_registry()
    return {"current": registry["current"], "workspaces": [
        _status(name, registry["workspaces"][name])
        for name in sorted(registry["workspaces"], key=lambda value: (value != "default", value))]}


@contextmanager
def _registry_lock():
    try:
        import fcntl
    except ImportError as exc:
        raise ConfigError("Workspace updates require macOS or Linux file locks") from exc
    path = _registry_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(path.with_name(path.name + ".lock")), os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            yield
    except OSError as exc:
        raise ConfigError("Cannot update workspace registry " + str(path) + ": " + str(exc)) from exc


def _atomic_json(path, value):
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def create_workspace(name):
    name = _name(name)
    if name == "default":
        raise ConfigError("The default workspace already exists and cannot be recreated")
    # Resolve only settings, never development imports or SQLite. Resolving before
    # the lock keeps reads independent from the transaction; dev settings are a
    # snapshot of the caller's current selection, not shared configuration.
    from .config import resolve_settings
    inherited = resolve_settings()
    directory = _path(_path(_platform_dirs()[1], "workspace data directory") / "workspaces" / name, "workspace directory", resolve_links=False)
    if _path(directory.parent, "workspace parent") != directory.parent:
        raise ConfigError("Workspace parent directory cannot redirect through a symlink")
    path = directory / "config.json"
    raw = {"database": "memory.sqlite3", "data_dir": ".", "port": 8765,
           "dev": inherited.dev,
           "development": {"source": (str(inherited.development_source)
                                        if inherited.development_source is not None else None)}}
    _validate_storage(name, path, {"data_dir": directory, "database": directory / "memory.sqlite3"})
    with _registry_lock():
        registry = _read_registry()
        if name in registry["workspaces"]:
            raise ConfigError("Workspace already exists: " + name)
        directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # lexists also rejects a dangling symlink. Never reuse an abandoned or
        # manually created directory, even if it happens to look empty.
        if os.path.lexists(str(directory)):
            raise ConfigError("Workspace directory already exists: " + str(directory))
        directory.mkdir(mode=0o700)
        try:
            _atomic_json(path, raw)
            registry["workspaces"][name] = {"config_file": str(path)}
            _atomic_json(_registry_path(), registry)
        except BaseException:
            # Roll back only the files this transaction created. Unknown files
            # are preserved; rmdir cannot remove a directory that acquired data.
            try:
                path.unlink(missing_ok=True)
                directory.rmdir()
            except OSError:
                pass
            raise
    return dict(_status(name, {"config_file": str(path)}), current=registry["current"])


def use_workspace(name):
    name = _name(name)
    with _registry_lock():
        registry = _read_registry()
        if name not in registry["workspaces"]:
            raise ConfigError("Unknown workspace: " + name)
        registry["current"] = name
        _atomic_json(_registry_path(), registry)
    return dict(_status(name, registry["workspaces"][name]), current=name)
