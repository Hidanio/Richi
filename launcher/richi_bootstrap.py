"""Protocol-1 entry point kept in the original dedicated virtual environment.

This module deliberately imports neither Richi package before obtaining a
process lease. Its file also serves as the seed generation's permanent lease.
"""
import hashlib
import json
import os
from pathlib import Path
import sys

BOOTSTRAP_PROTOCOL = 1
OWNER_FILE = ".richi-installation.json"
LEASE_ENV = "RICHI_INSTALLATION_LEASE_FD"
ROOT_ENV = "RICHI_INSTALLATION_ROOT"
GENERATION_ENV = "RICHI_INSTALLATION_GENERATION"


class InstallationError(ValueError):
    pass


def _object(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise InstallationError("Cannot read installation metadata " + str(path) + ": " + str(exc)) from exc
    if not isinstance(value, dict):
        raise InstallationError("Installation metadata must be an object: " + str(path))
    return value


def platform_data():
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Richi"
    raw = os.environ.get("XDG_DATA_HOME", "")
    base = Path(raw) if raw and Path(raw).is_absolute() else Path.home() / ".local" / "share"
    return base / "richi"


def installation_root(prefix):
    identity = hashlib.sha256(str(Path(prefix).resolve()).encode("utf-8")).hexdigest()[:24]
    return platform_data().resolve() / "installations" / identity


def _absolute(value, label):
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise InstallationError("Invalid installation " + label)
    path = Path(value)
    if path.resolve() != path:
        raise InstallationError("Installation " + label + " must be canonical and must not contain symlinks")
    return path


def context():
    """Resolve installation identity without opening a workspace or writing files."""
    prefix = Path(sys.prefix).resolve()
    anchor = Path(__file__).resolve()
    try:
        anchor.relative_to(prefix)
    except ValueError:
        raise InstallationError("Managed updates require a fixed Richi installation in a dedicated virtual environment")
    if sys.platform not in {"darwin", "linux"} or prefix == Path(sys.base_prefix).resolve():
        raise InstallationError("Managed updates require a dedicated macOS or Linux virtual environment")
    marker = prefix / OWNER_FILE
    if marker.exists():
        owner = _object(marker)
        original = _absolute(owner.get("original_prefix"), "original prefix")
        root = _absolute(owner.get("root"), "root")
        if (owner.get("format_version") != 1 or owner.get("kind") not in {"seed", "release"}
                or root.name != hashlib.sha256(str(original).encode("utf-8")).hexdigest()[:24]
                or root.parent.name != "installations"):
            raise InstallationError("Invalid installation owner marker")
        generation = owner.get("generation")
        if owner["kind"] == "seed":
            if original != prefix or generation != "seed":
                raise InstallationError("Invalid seed owner marker")
        elif not _generation_id(generation) or prefix != root / "versions" / generation:
            raise InstallationError("Invalid managed release owner marker")
    else:
        original, root = prefix, installation_root(prefix)
    return {"original_prefix": str(original), "root": str(root), "prefix": str(prefix),
            "bootstrap": str(anchor), "site": str(anchor.parent)}


def _generation_id(value):
    return isinstance(value, str) and bool(value) and len(value) < 100 and all(
        c in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for c in value
    ) and value not in {".", ".."}


def read_state(ctx):
    state = _object(Path(ctx["root"]) / "state.json")
    if state.get("format_version") != 1 or state.get("updater_protocol") != BOOTSTRAP_PROTOCOL:
        raise InstallationError("Unsupported installation state protocol")
    if state.get("original_prefix") != ctx["original_prefix"]:
        raise InstallationError("Installation state belongs to a different original environment")
    records = state.get("generations")
    if not isinstance(records, dict) or not records:
        raise InstallationError("Invalid installation generations")
    original = Path(ctx["original_prefix"])
    root = Path(ctx["root"])
    for key, record in records.items():
        if not _generation_id(key) or not isinstance(record, dict):
            raise InstallationError("Invalid installation generation")
        expected = original if key == "seed" else root / "versions" / key
        if record.get("prefix") != str(expected) or record.get("kind") != ("seed" if key == "seed" else "release"):
            raise InstallationError("Generation path does not match its installation ownership")
        site = _absolute(record.get("site"), "package directory")
        try:
            site.relative_to(expected)
        except ValueError as exc:
            raise InstallationError("Generation package directory is outside its environment") from exc
        if not isinstance(record.get("version"), str):
            raise InstallationError("Invalid installation version")
    current, previous = state.get("current"), state.get("previous")
    if (not _generation_id(current) or current not in records
            or (previous is not None and (not _generation_id(previous) or previous not in records or previous == current))):
        raise InstallationError("Invalid current or previous installation generation")
    seed_anchor = _absolute(state.get("seed_bootstrap"), "seed bootstrap")
    try:
        seed_anchor.relative_to(original)
    except ValueError as exc:
        raise InstallationError("Seed bootstrap is outside the original environment") from exc
    if seed_anchor.name != "richi_bootstrap.py":
        raise InstallationError("Invalid seed bootstrap filename")
    return state


def lease_fds():
    """Descriptors every Richi subprocess must preserve using pass_fds."""
    value = os.environ.get(LEASE_ENV)
    if value is None:
        return ()
    try:
        fd = int(value)
        if fd < 3:
            return ()
        os.fstat(fd)
        return (fd,)
    except (OSError, ValueError):
        return ()


def _lease(path):
    import fcntl
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        os.set_inheritable(descriptor, True)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def select(ctx, previous=False):
    """Return the selected generation while holding its inheritable lease."""
    import fcntl
    root = Path(ctx["root"])
    if not (root / "state.json").exists():
        fd = _lease(Path(ctx["bootstrap"]))
        # Initialization may race this first invocation. Recheck after locking.
        if not (root / "state.json").exists():
            return {"prefix": ctx["prefix"], "site": ctx["site"]}, "seed", fd
        os.close(fd)
    lock_fd = os.open(str(root / "state.lock"), os.O_RDONLY)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
        state = read_state(ctx)
        generation = state["previous"] if previous else state["current"]
        if generation is None:
            raise InstallationError("No previous Richi release is available for rollback")
        record = state["generations"][generation]
        lease = Path(state["seed_bootstrap"]) if generation == "seed" else root / "leases" / (generation + ".lock")
        descriptor = _lease(lease)
        if not (Path(record["site"]) / "richi_launcher" / "cli.py").is_file():
            os.close(descriptor)
            raise InstallationError("Selected Richi release is missing; installation repair is required")
        return record, generation, descriptor
    finally:
        os.close(lock_fd)


def _rollback_requested(argv):
    # The launcher accepts a chat identity for all commands, but updates never
    # consult the chat registry. Preserve that spelling for emergency recovery.
    position = 0
    while position < len(argv):
        if argv[position].startswith("--chat="):
            position += 1
        elif argv[position] == "--chat" and position + 1 < len(argv):
            position += 2
        else:
            break
    return argv[position:] == ["update", "rollback"]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        try:
            ctx = context()
        except InstallationError:
            # Source and ordinary pip installations continue to run. Update
            # commands will report the unsupported layout explicitly.
            if (Path(sys.prefix) / OWNER_FILE).exists():
                raise
            from richi_launcher.cli import main as launch
            return launch(argv)
        # Recovery must not import the generation it is rolling back. The
        # previous protocol-1 manager can atomically swap the installation even
        # if the current launcher's files or imports have become unusable.
        record, generation, fd = select(ctx, previous=_rollback_requested(argv))
        os.environ[LEASE_ENV] = str(fd)
        os.environ[ROOT_ENV] = ctx["root"]
        os.environ[GENERATION_ENV] = generation
        if Path(record["prefix"]) != Path(sys.prefix).resolve():
            python = str(Path(record["prefix"]) / "bin" / "python")
            code = "import sys; from richi_launcher.cli import main; sys.exit(main(sys.argv[1:]))"
            os.execve(python, [python, "-I", "-B", "-c", code, *argv], dict(os.environ))
        from richi_launcher.cli import main as launch
        return launch(argv)
    except (InstallationError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
