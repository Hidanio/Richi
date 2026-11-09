"""The installation's map files: one entry per running map and the first-command marker.

``<root>/maps/<port>.json`` exists while a map of a managed installation runs
on that port; it stays, marked ``restarting``, while the map re-executes.
``<root>/maps/handoff.json`` records which port ranges the first command of a
generation has already looked at.
"""
import json
import os
from pathlib import Path
import time

from .control import is_map, read_health
from .protocol import GENERATION_ENV, MARKER, REGISTRY, RESTART_STALE, RESTART_TIMEOUT, ROOT_ENV


def atomic_json(path, value):
    import tempfile
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, sort_keys=True)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def read_json(path, limit=65536):
    try:
        if path.is_symlink():
            return None
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        value = json.loads(data) if len(data) <= limit else None
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


def directory(root, create=False):
    path = Path(root) / REGISTRY
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        return None
    return path


def current_generation(root):
    """The installation's current generation from its atomically replaced state."""
    state = read_json(Path(root) / "state.json", limit=1 << 20)
    value = state.get("current") if state else None
    return value if isinstance(value, str) else None


def register(port, pid=None):
    """Record a running map so updaters can find it on any port; never fatal.

    Only a managed installation has a registry. Before its first update no
    directory is created; such a map is found on its port or follows by itself.
    """
    root, generation = os.environ.get(ROOT_ENV), os.environ.get(GENERATION_ENV)
    if not root or not generation or not Path(root).is_absolute():
        return None
    try:
        if not (Path(root) / "state.json").is_file():
            return None
        entries = directory(root, create=True)
        if entries is None:
            return None
        path = entries / ("%d.json" % port)
        # Readers use pid and restarting; port, generation and started_at help diagnostics.
        atomic_json(path, {"format_version": 1, "port": port, "pid": pid or os.getpid(),
                           "generation": generation, "started_at": int(time.time())})
        return path
    except OSError:
        return None


def mark_restarting(path, pid=None):
    """Keep a map's entry until it answers again, so richi map waits instead of taking its port."""
    if path is None:
        return
    entry = read_json(Path(path))
    if entry is None or entry.get("pid") != (pid or os.getpid()):
        return
    try:
        atomic_json(Path(path), dict(entry, restarting=int(time.time())))
    except OSError:
        pass


def unregister(path, pid=None):
    if path is None:
        return
    entry = read_json(Path(path))
    if entry is not None and entry.get("pid") == (pid or os.getpid()):
        try:
            Path(path).unlink()
        except OSError:
            pass


def registered_ports(root):
    entries = directory(root)
    if entries is None:
        return []
    ports = []
    for path in entries.glob("*.json"):
        if path.stem.isdigit() and 1 <= int(path.stem) <= 65535:
            ports.append(int(path.stem))
    return sorted(ports)


def prune(root, port):
    """Drop an entry only when its recorded process no longer exists."""
    entries = directory(root)
    if entries is None:
        return
    path = entries / ("%d.json" % port)
    entry = read_json(path)
    if entry is None or type(entry.get("pid")) is not int:
        return
    try:
        os.kill(entry["pid"], 0)
    except ProcessLookupError:
        unregister(path, entry["pid"])
    except OSError:
        pass


def wait_for_restarts(ports, timeout=RESTART_TIMEOUT):
    """Wait while a registered map of this installation re-executes on one of these ports.

    Until the new code answers, a re-executing map has no health: its port is
    closed or, when its socket was handed over, holds connections unanswered.
    Without this, richi map could take its port or move to the next one.
    Returns the ports whose map is still re-executing when the time is up;
    with ``timeout=0`` it only looks.
    """
    root = os.environ.get(ROOT_ENV)
    entries = directory(root) if root else None
    if entries is None:
        return []
    deadline, busy = time.monotonic() + timeout, []
    for port in ports:
        entry = read_json(entries / ("%d.json" % port))
        if (entry is None or type(entry.get("pid")) is not int or type(entry.get("restarting")) is not int
                or abs(time.time() - entry["restarting"]) > RESTART_STALE):
            continue
        while not is_map(read_health(port, timeout=0.2)):
            try:
                os.kill(entry["pid"], 0)
            except ProcessLookupError:
                break
            except OSError:
                pass
            if time.monotonic() >= deadline:
                busy.append(port)
                break
            time.sleep(0.1)
    return busy


def read_marker(root):
    return read_json(Path(root) / REGISTRY / MARKER)


def write_marker(root, value):
    atomic_json(Path(root) / REGISTRY / MARKER, value)


def reset_marker(root):
    entries = directory(root)
    if entries is not None:
        try:
            (entries / MARKER).unlink()
        except FileNotFoundError:
            pass
