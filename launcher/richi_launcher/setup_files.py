"""Recoverable, compare-and-swap edits to user-owned integration files.

The journal and backups are private local files. An interrupted setup is undone;
an interrupted release activation is completed or undone according to state.json.
Callers hold the installation update and state locks through the transaction.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

from .config import ConfigError

JOURNAL = "setup-pending.json"
MAX_FILE = 1024 * 1024


def safe_path(path):
    path = Path(os.path.abspath(os.path.expanduser(str(path))))
    if any(ord(c) < 32 or ord(c) == 127 for c in str(path)):
        raise ConfigError("Setup paths cannot contain control characters")
    if path.parent.resolve() != path.parent:
        raise ConfigError("Setup refuses symlinked parent directories: " + str(path))
    return path


def snapshot(path):
    path = safe_path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"kind": "absent"}
    if stat.S_ISLNK(info.st_mode):
        return {"kind": "link", "target": os.readlink(path)}
    if stat.S_ISDIR(info.st_mode):
        return {"kind": "directory", "device": info.st_dev, "inode": info.st_ino}
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
        raise ConfigError("Setup requires a regular file of at most 1 MiB: " + str(path))
    return {"kind": "file", "data": base64.b64encode(path.read_bytes()).decode("ascii"),
            "mode": stat.S_IMODE(info.st_mode)}


def file_value(data, mode=0o600):
    if len(data) > MAX_FILE:
        raise ConfigError("Setup output exceeds 1 MiB")
    return {"kind": "file", "data": base64.b64encode(data).decode("ascii"), "mode": mode}


def contents(value):
    if value["kind"] == "absent":
        return b""
    if value["kind"] != "file":
        raise ConfigError("Instruction and shell files must be regular files; symlinks are not overwritten")
    return base64.b64decode(value["data"], validate=True)


def change(path, after):
    path = safe_path(path)
    return {"path": str(path), "before": snapshot(path), "after": after}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _sync(directory):
    descriptor = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def replace(path, expected, value):
    path = safe_path(path)
    if snapshot(path) != expected:
        raise ConfigError("File changed during setup; preserved it: " + str(path))
    if value == expected:
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if value["kind"] == "absent":
        path.unlink()
    elif value["kind"] in {"file", "link"}:
        temporary = path.parent / (".richi-" + uuid.uuid4().hex)
        try:
            if value["kind"] == "link":
                temporary.symlink_to(value["target"])
            else:
                with temporary.open("xb") as stream:
                    os.fchmod(stream.fileno(), value["mode"])
                    stream.write(contents(value))
                    stream.flush()
                    os.fsync(stream.fileno())
            if snapshot(path) != expected:
                raise ConfigError("File changed during setup; preserved it: " + str(path))
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    else:
        raise ConfigError("Cannot replace a directory with file contents")
    _sync(path.parent)


def _apply(op, forward):
    path = Path(op["path"])
    if "backup" in op:
        backup = safe_path(op["backup"])
        current, saved = snapshot(path), snapshot(backup)
        absent = {"kind": "absent"}
        if forward:
            if current == op["before"] and saved == absent:
                backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.replace(path, backup)
                _sync(path.parent)
                _sync(backup.parent)
                current, saved = absent, op["before"]
            if saved != op["before"] or current not in (absent, op["after"]):
                raise ConfigError("Backup or destination changed; preserved both: " + str(path))
            if current == absent:
                replace(path, absent, op["after"])
        else:
            if current == op["before"] and saved == absent:
                return
            if saved != op["before"] or current not in (absent, op["after"]):
                raise ConfigError("Backup or destination changed; preserved both: " + str(path))
            if current != absent:
                replace(path, current, absent)
            os.replace(backup, path)
            _sync(backup.parent)
        _sync(path.parent)
        return
    before, after = (op["before"], op["after"]) if forward else (op["after"], op["before"])
    if snapshot(path) == after:
        return
    replace(path, before, after)


def recover(ctx):
    """Caller holds both locks; never overwrite edits made after an interruption."""
    from . import installation
    journal = Path(ctx["root"]) / JOURNAL
    if not journal.exists():
        return
    raw = contents(snapshot(journal))
    try:
        transaction = json.loads(raw)
        if transaction["format_version"] != 1 or transaction["root"] != ctx["root"]:
            raise ValueError("journal ownership")
        committed = transaction.get("committed", False)
        if transaction.get("generation") is not None:
            committed = installation._state(ctx)["current"] == transaction["generation"]
        operations = transaction["operations"]
        if not isinstance(operations, list) or len(operations) > 20:
            raise ValueError("journal operations")
        # Reverse only operations which were applied. Untouched operations
        # already match their before-state and are idempotently skipped.
        for op in operations if committed else reversed(operations):
            _apply(op, committed)
    except (ValueError, KeyError, TypeError) as exc:
        raise ConfigError("Cannot recover setup journal; preserved files: " + str(exc)) from exc
    journal.unlink()
    _sync(journal.parent)


def begin(ctx, operations, generation=None):
    from . import installation
    journal = Path(ctx["root"]) / JOURNAL
    if journal.exists():
        raise ConfigError("Pending setup transaction must be recovered first")
    operations = [op for op in operations if op.get("before") != op.get("after")]
    for op in operations:
        if snapshot(op["path"]) != op["before"]:
            raise ConfigError("Setup plan changed; rerun setup: " + op["path"])
    transaction = {"format_version": 1, "root": ctx["root"], "generation": generation,
                   "committed": False, "operations": operations}
    if len((json.dumps(transaction, indent=2, sort_keys=True) + "\n").encode()) > MAX_FILE:
        raise ConfigError("Setup journal exceeds 1 MiB; no files were changed")
    # Back up existing instruction bytes even after successful setup; do not
    # make users reconstruct their original files from a managed block.
    backups = Path(ctx["root"]) / "setup-backups"
    backups.mkdir(parents=True, exist_ok=True, mode=0o700)
    installation._atomic_json(backups / (uuid.uuid4().hex + ".json"), transaction)
    installation._atomic_json(journal, transaction)
    try:
        for op in operations:
            _apply(op, True)
    except BaseException:
        recover(ctx)
        raise
    return transaction


def finish(ctx, transaction):
    from . import installation
    if transaction.get("generation") is None:
        installation._atomic_json(Path(ctx["root"]) / JOURNAL, dict(transaction, committed=True))
    recover(ctx)
