"""Find, verify, stop and hand off running Richi maps.

A map is identified only through its loopback ``/api/health`` answer: the
application name, the process ID it reports and its runtime payload. Before any
signal, the process with that ID must belong to this user and run Richi's
runtime runner with exactly that payload in its argument vector, so an unrelated
listener, a forged answer or a reused PID is never signalled.

Nothing here opens a knowledge store or reads a workspace configuration. A map
restarted on a newer generation is started through the installation's stable
entry point with the storage selection the running map itself reports.
"""
import json
import os
from pathlib import Path
import sys
import time

APPLICATION = "project-memory-map"
# Maps from this release onwards follow the installation's current generation by
# themselves and re-execute through the stable entry point on this signal.
HANDOFF_CAPABILITY = "installation_handoff"
# Not SIGHUP: a foreground map must still end when its terminal closes.
HANDOFF_SIGNAL = "SIGUSR1"
PORT_RANGE = 11
STOP_TIMEOUT = 20
EXIT_TIMEOUT = 10
HANDOFF_TIMEOUT = 25
# Without an explicit request, a map waits this long after seeing another
# current generation, so an updater that hands it off directly goes first.
HANDOFF_GRACE = 5
REGISTRY = "maps"
MARKER = "handoff.json"
LEASE_ENV = "RICHI_INSTALLATION_LEASE_FD"
ROOT_ENV = "RICHI_INSTALLATION_ROOT"
GENERATION_ENV = "RICHI_INSTALLATION_GENERATION"
_RUNNER_MARKER = 'os.environ["RICHI_ACTIVE_RUNTIME"] = payload'
_BOOTSTRAP = ("import sys; sys.path.insert(0, sys.argv[1]); "
              "from richi_bootstrap import main; sys.exit(main(sys.argv[2:]))")
# Child map processes receive their storage explicitly; nothing ambient may
# redirect them or keep the parent's lease or generation.
_CHILD_UNSET = ("RICHI_ACTIVE_RUNTIME", "RICHI_CHAT_ID", "CODEX_THREAD_ID", "RICHI_DB",
                "RICHI_DATA_DIR", "RICHI_PORT", "RICHI_WORKSPACE", "RICHI_CONFIG",
                "RICHI_ACTIVE_CONFIG", "RICHI_ACTIVE_WORKSPACE", LEASE_ENV, ROOT_ENV, GENERATION_ENV)


def url(port):
    return "http://127.0.0.1:%d/" % port


def read_health(port, timeout=0.5):
    from http.client import HTTPException
    from urllib.request import ProxyHandler, build_opener
    try:
        with build_opener(ProxyHandler({})).open(url(port) + "api/health", timeout=timeout) as response:
            return json.loads(response.read(65537))
    except (OSError, HTTPException, ValueError, AttributeError, TypeError):
        return None


def is_map(health):
    """A loopback answer that claims to be a Richi map; proves nothing about its PID."""
    return (isinstance(health, dict) and health.get("application") == APPLICATION
            and type(health.get("pid")) is int and health["pid"] > 1
            and isinstance(health.get("runtime"), dict)
            and isinstance(health["runtime"].get("identity"), str)
            and isinstance(health.get("capabilities"), list))


def serves(health, database, config_file):
    """The map serves this exact storage selection, whatever runtime it runs."""
    return (is_map(health) and health.get("database") == str(database)
            and health.get("config_file") == (str(config_file) if config_file is not None else None))


def port_free(port):
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # Match HTTPServer so TIME_WAIT connections do not keep the URL busy.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _darwin_arguments(pid):
    import ctypes
    import ctypes.util
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                                ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        libc.sysctl.restype = ctypes.c_int
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
        size = ctypes.c_size_t(0)
        if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or size.value < 8:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
            return None
    except (OSError, AttributeError, TypeError, ValueError):
        return None
    raw = buffer.raw[:size.value]
    count = int.from_bytes(raw[:4], sys.byteorder)
    position = raw.find(b"\0", 4)  # executable path, then alignment padding
    if position < 0 or not 0 < count < 4096:
        return None
    while position < len(raw) and raw[position] == 0:
        position += 1
    arguments = []
    while len(arguments) < count:
        end = raw.find(b"\0", position)
        if end < 0:
            return None
        arguments.append(raw[position:end].decode("utf-8", "surrogateescape"))
        position = end + 1
    return arguments


def process_arguments(pid):
    """The exact argument vector of a live process, or None if it cannot be read."""
    if sys.platform.startswith("linux"):
        try:
            data = Path("/proc/%d/cmdline" % pid).read_bytes()
        except OSError:
            return None
        # Zombies and kernel threads have no arguments.
        return [part.decode("utf-8", "surrogateescape") for part in data[:-1].split(b"\0")] if data else None
    if sys.platform == "darwin":
        return _darwin_arguments(pid)
    return None


def process_uid(pid):
    if sys.platform.startswith("linux"):
        try:
            return os.stat("/proc/%d" % pid).st_uid
        except OSError:
            return None
    import subprocess
    try:
        result = subprocess.run(["/bin/ps", "-o", "uid=", "-p", str(pid)], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=5)
        return int(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def runner_launcher(arguments, runtime):
    """Launcher directory of a Richi runner serving a map with exactly this runtime."""
    payload = json.dumps(runtime)
    for index in range(len(arguments) - 4):
        if (arguments[index] != "-c" or _RUNNER_MARKER not in arguments[index + 1]
                or arguments[index + 3] != payload):
            continue
        action, rest = arguments[index + 4], arguments[index + 5:]
        if action == "serve" or (action == "cli" and any(
                rest[position:position + 2] == ["map", "serve"] for position in range(len(rest)))):
            return arguments[index + 2]
        return None
    return None


def verify(health):
    """Return (launcher directory, None) for a verified map process, else (None, reason)."""
    if not is_map(health):
        return None, "not_richi_map"
    pid = health["pid"]
    if pid == os.getpid():
        return None, "unverified_process"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None, "process_exited"
    except PermissionError:
        return None, "foreign_user"
    except OSError:
        return None, "unverified_process"
    if process_uid(pid) != os.getuid():
        return None, "foreign_user"
    arguments = process_arguments(pid)
    launcher = runner_launcher(arguments, health["runtime"]) if arguments else None
    if launcher is None:
        return None, "unverified_process"
    return launcher, None


REASONS = {
    "not_richi_map": "The process on this port is not a Richi map; it was not stopped",
    "other_workspace": "The Richi map on this port serves another database or configuration; select that workspace to stop it",
    "unverified_process": "The process reported by the map could not be verified as Richi's map; it was not stopped",
    "foreign_user": "The map process belongs to another user; it was not stopped",
    "process_exited": "The map process exited while it was being verified",
    "changed": "The map changed while it was being stopped; run the command again",
    "timeout": "The map did not release its port in time; it may still be finishing requests",
}


def refusal(reason, port, **extra):
    return dict({"status": "refused", "reason": reason, "error": REASONS[reason],
                 "url": url(port)}, **extra)


def _same_process(port, health):
    current = read_health(port)
    return is_map(current) and current["pid"] == health["pid"] and current["runtime"] == health["runtime"]


def _alive_as(pid, runtime):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    arguments = process_arguments(pid)
    return bool(arguments) and runner_launcher(arguments, runtime) is not None


def stop(port, health, timeout=STOP_TIMEOUT):
    """Stop one verified map with SIGTERM; the map closes its port, then exits.

    Its version lease is the map's own descriptor, so it is released only when
    that process exits. Never escalates to SIGKILL.
    """
    import signal
    launcher, reason = verify(health)
    if launcher is None:
        return refusal(reason, port, pid=health.get("pid") if isinstance(health, dict) else None)
    pid = health["pid"]
    # The answer must still come from the same verified process right before signalling.
    if not _same_process(port, health) or runner_launcher(process_arguments(pid) or [], health["runtime"]) is None:
        return refusal("changed", port, pid=pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + timeout
    while True:
        live = read_health(port, timeout=0.2)
        if not (is_map(live) and live["pid"] == pid) and port_free(port):
            break
        if time.monotonic() > deadline:
            return refusal("timeout", port, pid=pid)
        time.sleep(0.05)
    deadline = time.monotonic() + EXIT_TIMEOUT
    exited = not _alive_as(pid, health["runtime"])
    while not exited and time.monotonic() < deadline:
        time.sleep(0.05)
        exited = not _alive_as(pid, health["runtime"])
    return {"status": "stopped", "url": url(port), "pid": pid,
            "version": health["runtime"].get("version"), "exited": exited}


def _context():
    import richi_bootstrap as bootstrap
    return bootstrap.context()


def stable_command(argv):
    """The installation's public entry point, which selects and leases the current generation.

    Returns (command, managed). Outside a managed layout the running launcher
    is the entry point, as for an ordinary map reload.
    """
    import richi_bootstrap as bootstrap
    try:
        ctx = _context()
        if (Path(ctx["root"]) / "state.json").exists():
            anchor = Path(bootstrap.read_state(ctx)["seed_bootstrap"])
        else:
            anchor = Path(ctx["bootstrap"])
        python = Path(ctx["original_prefix"]) / "bin" / "python"
        if not python.exists() or not anchor.is_file():
            raise bootstrap.InstallationError("The installation entry point is unavailable")
    except (bootstrap.InstallationError, OSError, KeyError, ValueError):
        from .runtime import bootstrap_command
        return bootstrap_command(argv), False
    return [str(python), "-I", "-B", "-c", _BOOTSTRAP, str(anchor.parent), *map(str, argv)], True


def selection(health):
    """Command-line storage selection and environment for the map this health describes."""
    config_file, workspace = health.get("config_file"), health.get("workspace")
    environment = dict(os.environ)
    for key in _CHILD_UNSET:
        environment.pop(key, None)
    arguments = []
    if config_file is not None:
        environment["RICHI_ACTIVE_CONFIG"] = config_file
        if workspace is not None:
            environment["RICHI_ACTIVE_WORKSPACE"] = workspace
    if config_file is not None and Path(config_file).is_file():
        arguments += ["--config", config_file]
        environment["RICHI_CONFIG"] = config_file
    else:
        # Only the implicit default may lack a configuration file.
        environment["RICHI_WORKSPACE"] = "default"
    return arguments + ["--db", health["database"]], environment


def _atomic_json(path, value):
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


def _read_json(path, limit=65536):
    try:
        if path.is_symlink():
            return None
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        value = json.loads(data) if len(data) <= limit else None
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


def _registry(root, create=False):
    directory = Path(root) / REGISTRY
    if create:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink() or not directory.is_dir():
        return None
    return directory


def register(port, pid=None):
    """Record a running map so updaters can find it on any port; never fatal."""
    root, generation = os.environ.get(ROOT_ENV), os.environ.get(GENERATION_ENV)
    if not root or not generation or not Path(root).is_absolute():
        return None
    try:
        directory = _registry(root, create=True)
        if directory is None:
            return None
        path = directory / ("%d.json" % port)
        _atomic_json(path, {"format_version": 1, "port": port, "pid": pid or os.getpid(),
                            "generation": generation, "started_at": int(time.time())})
        return path
    except OSError:
        return None


def unregister(path, pid=None):
    if path is None:
        return
    entry = _read_json(Path(path))
    if entry is not None and entry.get("pid") == (pid or os.getpid()):
        try:
            Path(path).unlink()
        except OSError:
            pass


def registered_ports(root):
    directory = _registry(root)
    if directory is None:
        return []
    ports = []
    for path in directory.glob("*.json"):
        if path.stem.isdigit() and 1 <= int(path.stem) <= 65535:
            ports.append(int(path.stem))
    return sorted(ports)


def _prune(root, port):
    """Drop an entry only when its recorded process no longer exists."""
    directory = _registry(root)
    if directory is None:
        return
    path = directory / ("%d.json" % port)
    entry = _read_json(path)
    if entry is None or type(entry.get("pid")) is not int:
        return
    try:
        os.kill(entry["pid"], 0)
    except ProcessLookupError:
        unregister(path, entry["pid"])
    except OSError:
        pass


def current_generation(root):
    """The installation's current generation from its atomically replaced state."""
    state = _read_json(Path(root) / "state.json", limit=1 << 20)
    value = state.get("current") if state else None
    return value if isinstance(value, str) else None


def _within(path, parent):
    try:
        Path(path).relative_to(parent)
        return True
    except ValueError:
        return False


def _signal_handoff(port, health):
    import signal
    if not _same_process(port, health):
        return "changed"
    try:
        os.kill(health["pid"], getattr(signal, HANDOFF_SIGNAL))
    except ProcessLookupError:
        return "process_exited"
    except OSError:
        return "unverified_process"
    return None


def _spawn_replacement(port, health):
    import subprocess
    arguments, environment = selection(health)
    command, _managed = stable_command([*arguments, "map", "--no-open", "--port", str(port)])
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                timeout=90, env=environment, close_fds=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "failed", "error": str(exc)[:500]}
    try:
        payload = json.loads(result.stdout if result.returncode == 0 else result.stderr)
    except ValueError:
        payload = {"error": (result.stderr or result.stdout)[-500:].strip()}
    if result.returncode or not isinstance(payload, dict):
        return {"status": "failed", "error": str(payload.get("error") if isinstance(payload, dict) else payload)[:500]}
    if payload.get("url") != url(port):
        return {"status": "failed", "error": "The map started at " + str(payload.get("url"))}
    return {"status": "restarted"}


def _valid_selection(health):
    database, config_file, workspace = (health.get(key) for key in ("database", "config_file", "workspace"))
    return (isinstance(database, str) and Path(database).is_absolute()
            and (config_file is None or (isinstance(config_file, str) and Path(config_file).is_absolute()))
            and (workspace is None or isinstance(workspace, str)))


def reconcile(ports=(), *, replace=False, timeout=HANDOFF_TIMEOUT):
    """Hand off this installation's running maps that are not on its current generation.

    Maps from this release onwards re-execute themselves on request, keeping
    their PID and URL. Older maps cannot; with ``replace`` they are stopped and
    started again on the same port through the stable entry point. Maps of other
    installations, unverifiable processes and current maps are never touched.
    """
    import richi_bootstrap as bootstrap
    ctx = _context()
    root = Path(ctx["root"])
    if not (root / "state.json").exists():
        return []
    state = bootstrap.read_state(ctx)
    current = state["current"]
    current_prefix = Path(state["generations"][current]["prefix"])
    owned = (Path(ctx["original_prefix"]), root / "versions")
    results, pending = [], []
    for port in sorted(set(registered_ports(root)) | {port for port in ports if 1 <= port <= 65535}):
        health = read_health(port)
        if not is_map(health):
            _prune(root, port)
            continue
        launcher, _reason = verify(health)
        if (launcher is None or not any(_within(launcher, path) for path in owned)
                or not _valid_selection(health)):
            continue
        if health.get("installation_generation") == current and _within(launcher, current_prefix):
            continue
        entry = {"url": url(port), "pid": health["pid"], "from_version": health["runtime"].get("version"),
                 "from_generation": health.get("installation_generation")}
        if HANDOFF_CAPABILITY in health["capabilities"]:
            problem = _signal_handoff(port, health)
            if problem is None:
                pending.append((port, entry))
                continue
            entry.update(status="failed", error=REASONS[problem])
        elif replace:
            entry.update(_spawn_replacement(port, health))
        else:
            entry.update(status="restart_required",
                         error="This map predates automatic handoff; it is restarted by the next richi command or richi map")
        results.append(entry)
    deadline = time.monotonic() + timeout
    while pending:
        for port, entry in list(pending):
            health = read_health(port)
            if is_map(health) and health.get("installation_generation") == current:
                entry.update(status="restarted", pid=health["pid"], version=health["runtime"].get("version"),
                             generation=current)
                results.append(entry)
                pending.remove((port, entry))
        if not pending:
            break
        if time.monotonic() > deadline:
            for port, entry in pending:
                entry.update(status="pending", error="The map was asked to restart but has not reported the new version yet")
                results.append(entry)
            break
        time.sleep(0.1)
    for entry in results:
        if entry.get("status") == "restarted" and "version" not in entry:
            health = read_health(int(entry["url"].rstrip("/").rsplit(":", 1)[1]))
            if is_map(health):
                entry.update(pid=health["pid"], version=health["runtime"].get("version"),
                             generation=health.get("installation_generation"))
    return sorted(results, key=lambda item: item["url"])


def _reset_marker(root):
    directory = _registry(root)
    if directory is not None:
        try:
            (directory / MARKER).unlink()
        except FileNotFoundError:
            pass


def after_activation(result):
    """Updater step after a committed activation; it never fails the update.

    Registered maps are asked to restart on the activated generation. Older
    unregistered maps are handled at the next richi command, which reconciles
    once per generation, or by richi map itself.
    """
    if not isinstance(result, dict) or result.get("status") not in {"updated", "rolled_back"}:
        return result
    try:
        _reset_marker(_context()["root"])
        maps = reconcile()
    except Exception as exc:  # The activation is already committed.
        result["maps"] = []
        result["maps_error"] = str(exc)[:500]
        result["restart_maps_required"] = True
        return result
    result["maps"] = maps
    result["restart_maps_required"] = any(entry.get("status") != "restarted" for entry in maps)
    cleanup = result.get("cleanup")
    if (any(entry.get("status") == "restarted" for entry in maps)
            and isinstance(cleanup, dict) and cleanup.get("deferred")):
        # Restarted maps released older leases; collect what is no longer leased.
        try:
            from . import installation
            retry = installation.cleanup()
            result["cleanup"] = {"removed": cleanup.get("removed", []) + retry["cleanup"]["removed"],
                                 "deferred": retry["cleanup"]["deferred"],
                                 "failures": cleanup.get("failures", []) + retry["cleanup"]["failures"]}
            result["retired"] = retry["retired"]
        except Exception:
            pass
    return result


def _settled(recorded, generation, replace):
    return (recorded is not None and recorded.get("generation") == generation
            and (not recorded.get("replacement_pending") or not replace))


def first_invocation(port, replace=True):
    """Once per generation, the first command running it hands off stale maps.

    Covers activations by updaters that cannot do it themselves, such as the
    frozen managers up to 1.1.0. The fast path reads one small file. Without
    ``replace`` (an agent's command, possibly sandboxed) maps are only asked to
    hand off themselves; older maps wait for a terminal command or richi map.
    """
    root, generation = os.environ.get(ROOT_ENV), os.environ.get(GENERATION_ENV)
    if not root or not generation:
        return
    marker = Path(root) / REGISTRY / MARKER
    if _settled(_read_json(marker), generation, replace):
        return
    if not (Path(root) / "state.json").exists() or current_generation(root) != generation:
        return
    import fcntl
    directory = _registry(root, create=True)
    if directory is None:
        return
    descriptor = os.open(str(directory / "handoff.lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        if _settled(_read_json(marker), generation, replace):
            return
        # Claim first: replacement commands are richi invocations themselves.
        _atomic_json(marker, {"format_version": 1, "generation": generation, "status": "running",
                              "time": int(time.time())})
        maps = reconcile(range(port, min(port + PORT_RANGE, 65536)), replace=replace)
        _atomic_json(marker, {"format_version": 1, "generation": generation, "status": "done",
                              "time": int(time.time()), "maps": maps,
                              "replacement_pending": any(entry.get("status") == "restart_required"
                                                         for entry in maps)})
    finally:
        os.close(descriptor)
