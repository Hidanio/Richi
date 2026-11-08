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
DEFAULT_PORT = 8765
STOP_TIMEOUT = 20
EXIT_TIMEOUT = 10
HANDOFF_TIMEOUT = 25
# Without an explicit request, a map waits this long after seeing another
# current generation, so an updater that hands it off directly goes first.
HANDOFF_GRACE = 5
# Tests set this to decide deterministically who hands a map off.
GRACE_ENV = "RICHI_MAP_HANDOFF_GRACE"
# How long the entry point may take to start before a map replaces itself.
HANDOFF_CHECK_TIMEOUT = 15
# How long richi map waits for a registered map that is re-executing.
RESTART_TIMEOUT = 15
REGISTRY = "maps"
MARKER = "handoff.json"
LEASE_ENV = "RICHI_INSTALLATION_LEASE_FD"
ROOT_ENV = "RICHI_INSTALLATION_ROOT"
GENERATION_ENV = "RICHI_INSTALLATION_GENERATION"
# Set for the entry point check a map runs before it hands itself off: that
# command must not look for maps, including the one waiting for it.
PASSIVE_ENV = "RICHI_MAPS_PASSIVE"
_RUNNER_MARKER = 'os.environ["RICHI_ACTIVE_RUNTIME"] = payload'
_BOOTSTRAP = ("import sys; sys.path.insert(0, sys.argv[1]); "
              "from richi_bootstrap import main; sys.exit(main(sys.argv[2:]))")
# Child map processes receive their storage explicitly; nothing ambient may
# redirect them or keep the parent's lease or generation.
_CHILD_UNSET = ("RICHI_ACTIVE_RUNTIME", "RICHI_CHAT_ID", "CODEX_THREAD_ID", "RICHI_DB",
                "RICHI_DATA_DIR", "RICHI_PORT", "RICHI_WORKSPACE", "RICHI_CONFIG",
                "RICHI_ACTIVE_CONFIG", "RICHI_ACTIVE_WORKSPACE", PASSIVE_ENV,
                LEASE_ENV, ROOT_ENV, GENERATION_ENV)
# Options of the richi command line before a command, and of richi map, as the
# launcher passes them to the runtime runner. Anything else is not a map.
_GLOBAL_OPTIONS = {"--db", "--config", "--workspace", "-w", "--project-path", "--chat"}
_GLOBAL_FLAGS = {"--compact-json"}
_MAP_OPTIONS = {"--db", "--config", "--port"}
_MAP_FLAGS = {"--open", "--no-open"}


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


def _process_owner(pid):
    """(uid, has a controlling terminal) of a process; either is None if unknown."""
    if sys.platform.startswith("linux"):
        try:
            uid = os.stat("/proc/%d" % pid).st_uid
        except OSError:
            return None, None
        try:
            # The command name is parenthesized and may contain spaces.
            fields = Path("/proc/%d/stat" % pid).read_text().rpartition(")")[2].split()
            return uid, int(fields[4]) != 0
        except (OSError, IndexError, ValueError):
            return uid, None
    import subprocess
    try:
        result = subprocess.run(["/bin/ps", "-o", "uid=,tty=", "-p", str(pid)], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=5)
        fields = result.stdout.split()
        if result.returncode or len(fields) != 2:
            return None, None
        return int(fields[0]), fields[1] not in {"?", "??", "-"}
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, None


def process_uid(pid):
    return _process_owner(pid)[0]


def _skip_options(arguments, position, options, flags):
    """Position of the first positional argument after leading options, or None."""
    while position < len(arguments):
        token = arguments[position]
        name = token.partition("=")[0]
        if token in flags:
            position += 1
        elif name in options:
            position += 1 if "=" in token else 2
        elif "-w" in options and token.startswith("-w") and not token.startswith("--"):
            position += 1  # -wNAME
        elif token.startswith("-"):
            return None
        else:
            return position
    return None


def _serves_map(arguments):
    """These richi command-line arguments run the map server: richi [options] map [options] serve."""
    position = _skip_options(arguments, 0, _GLOBAL_OPTIONS, _GLOBAL_FLAGS)
    if position is None or arguments[position] != "map":
        return False
    position = _skip_options(arguments, position + 1, _MAP_OPTIONS, _MAP_FLAGS)
    return position is not None and arguments[position] == "serve"


def runner_launcher(arguments, runtime):
    """Launcher directory of a Richi runner serving a map with exactly this runtime.

    The runner is ``python -I -B [-X pycache_prefix=...] -c CODE LAUNCHER PAYLOAD
    ACTION ...``; a map runs ACTION ``serve``, or ``cli`` with ``map serve``.
    """
    if not isinstance(arguments, list) or arguments[1:3] != ["-I", "-B"]:
        return None
    position = 3
    if arguments[position:position + 1] == ["-X"] and len(arguments) > position + 1 \
            and arguments[position + 1].startswith("pycache_prefix="):
        position += 2
    if (len(arguments) < position + 5 or arguments[position] != "-c"
            or _RUNNER_MARKER not in arguments[position + 1]
            or arguments[position + 3] != json.dumps(runtime)):
        return None
    action, rest = arguments[position + 4], arguments[position + 5:]
    if action == "serve" or (action == "cli" and _serves_map(rest)):
        return arguments[position + 2]
    return None


def _inspect(health):
    """({launcher, terminal}, None) for a verified map process, else (None, reason)."""
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
        # The same user's process in an environment that may not signal it, such as a sandbox.
        return None, "not_permitted" if process_uid(pid) == os.getuid() else "foreign_user"
    except OSError:
        return None, "unverified_process"
    uid, terminal = _process_owner(pid)
    if uid != os.getuid():
        return None, "foreign_user"
    arguments = process_arguments(pid)
    launcher = runner_launcher(arguments, health["runtime"]) if arguments else None
    if launcher is None:
        return None, "unverified_process"
    return {"launcher": launcher, "terminal": bool(terminal)}, None


def verify(health):
    """Return (launcher directory, None) for a verified map process, else (None, reason)."""
    details, reason = _inspect(health)
    return (details["launcher"], None) if details is not None else (None, reason)


REASONS = {
    "not_richi_map": "The process on this port is not a Richi map; it was not stopped",
    "unverified_process": "The process reported by the map could not be verified as Richi's map; it was not stopped",
    "foreign_user": "The map process belongs to another user; it was not stopped",
    "not_permitted": "This environment may not signal the map process (for example a sandbox); it was not stopped",
    "process_exited": "The map process exited while it was being verified",
    "changed": "The map changed while it was being checked; run the command again",
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
    except PermissionError:
        return refusal("not_permitted", port, pid=pid)
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
    # The same seam as the updater, so fixtures can substitute the installation.
    from . import installation
    return installation._context()


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


def handoff_grace():
    try:
        value = float(os.environ.get(GRACE_ENV, HANDOFF_GRACE))
    except ValueError:
        return float(HANDOFF_GRACE)
    return min(max(value, 0.0), 3600.0) if value == value else float(HANDOFF_GRACE)


def handoff_problem(schema=None):
    """Why a map must stay on its version instead of following the current one, or None.

    Unlike the per-second check, this validates the state as the stable entry
    point will: the current release must be complete and able to read the store.
    """
    import richi_bootstrap as bootstrap
    try:
        ctx = _context()
        if not (Path(ctx["root"]) / "state.json").exists():
            return None
        state = bootstrap.read_state(ctx)
        record = state["generations"][state["current"]]
        site = Path(record["site"])
        if not (site / "richi_launcher" / "cli.py").is_file() or not (site / "richi" / "memory.py").is_file():
            return "release {} is incomplete; repair the installation".format(record.get("version"))
        if schema is not None and schema not in bootstrap.generation_schemas(record):
            return "release {} cannot read database schema {}".format(record.get("version"), schema)
    except (bootstrap.InstallationError, OSError, KeyError, ValueError, TypeError) as exc:
        return "the installation state cannot be read: " + str(exc)
    return None


def handoff_command(selection_arguments, arguments, environment, schema=None,
                    timeout=HANDOFF_CHECK_TIMEOUT):
    """(command, None) to run ``arguments`` on the current generation, or (None, problem).

    A working map is replaced only after the installation's stable entry point
    resolved the same storage selection and runtime once with that release's
    own code (``richi --version``); the release must also be complete and
    declare support for the store's schema.
    """
    import subprocess
    problem = handoff_problem(schema)
    if problem is not None:
        return None, problem
    command, managed = stable_command([*selection_arguments, *arguments])
    if not managed:
        # The running launcher would only start this same generation again.
        return None, "the installation entry point is unavailable; repair the installation"
    check, _managed = stable_command([*selection_arguments, "--version"])
    child = dict(environment)
    for key in ("RICHI_ACTIVE_RUNTIME", LEASE_ENV, ROOT_ENV, GENERATION_ENV):
        child.pop(key, None)
    child[PASSIVE_ENV] = "1"
    try:
        result = subprocess.run(check, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                timeout=timeout, env=child, close_fds=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "the current release did not start: " + str(exc)[:300]
    if result.returncode:
        return None, "the current release did not start: " + (result.stderr or result.stdout).strip()[-300:]
    return command, None


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
        directory = _registry(root, create=True)
        if directory is None:
            return None
        path = directory / ("%d.json" % port)
        _atomic_json(path, {"format_version": 1, "port": port, "pid": pid or os.getpid(),
                            "generation": generation, "started_at": int(time.time())})
        return path
    except OSError:
        return None


def mark_restarting(path, pid=None):
    """Keep a map's entry while it re-executes, so richi map waits instead of taking its port."""
    if path is None:
        return
    entry = _read_json(Path(path))
    if entry is None or entry.get("pid") != (pid or os.getpid()):
        return
    try:
        _atomic_json(Path(path), dict(entry, restarting=int(time.time())))
    except OSError:
        pass


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


def wait_for_restarts(ports, timeout=RESTART_TIMEOUT):
    """Wait while a registered map of this installation re-executes on one of these ports.

    Between closing its port and listening again on the new version a map has
    no health; without this, richi map could take its port or move to the next.
    """
    root = os.environ.get(ROOT_ENV)
    directory = _registry(root) if root else None
    if directory is None:
        return
    deadline = time.monotonic() + timeout
    for port in ports:
        entry = _read_json(directory / ("%d.json" % port))
        if (entry is None or type(entry.get("pid")) is not int or type(entry.get("restarting")) is not int
                or abs(time.time() - entry["restarting"]) > 2 * timeout):
            continue
        while time.monotonic() < deadline and not is_map(read_health(port, timeout=0.2)):
            try:
                os.kill(entry["pid"], 0)
            except ProcessLookupError:
                break
            except OSError:
                pass
            time.sleep(0.1)


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


def installation_owns(launcher):
    """The launcher directory belongs to one of this installation's environments."""
    try:
        ctx = _context()
    except Exception:
        return False
    return any(_within(launcher, path) for path in
               (Path(ctx["original_prefix"]), Path(ctx["root"]) / "versions"))


def running_current_generation():
    """This process runs the installation's current generation."""
    root, generation = os.environ.get(ROOT_ENV), os.environ.get(GENERATION_ENV)
    return bool(root and generation) and current_generation(root) == generation


def request_handoff(port, health):
    """Ask a verified map to re-execute on the current generation; return a refusal reason or None."""
    import signal
    if not _same_process(port, health):
        return "changed"
    try:
        os.kill(health["pid"], getattr(signal, HANDOFF_SIGNAL))
    except ProcessLookupError:
        return "process_exited"
    except PermissionError:
        return "not_permitted"
    except OSError:
        return "unverified_process"
    return None


def _spawn_replacement(port, health):
    """Run richi map for this map's storage in the background; it replaces the map on its URL.

    The command that found the map does not wait for it. richi map checks the
    new runtime before it stops the old map, and reports its own errors.
    """
    import subprocess
    arguments, environment = selection(health)
    command, _managed = stable_command([*arguments, "map", "--no-open", "--port", str(port)])
    try:
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, env=environment, close_fds=True, start_new_session=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "failed", "error": str(exc)[:500]}
    return {"status": "replacing"}


def _valid_selection(health):
    database, config_file, workspace = (health.get(key) for key in ("database", "config_file", "workspace"))
    return (isinstance(database, str) and Path(database).is_absolute()
            and (config_file is None or (isinstance(config_file, str) and Path(config_file).is_absolute()))
            and (workspace is None or isinstance(workspace, str)))


def _port_of(entry):
    return int(entry["url"].rstrip("/").rsplit(":", 1)[1])


def reconcile(ports=(), *, replace=False, wait=True, timeout=HANDOFF_TIMEOUT):
    """Hand off this installation's running maps that are not on its current generation.

    Maps from this release onwards re-execute themselves on request, keeping
    their PID and URL; with ``wait`` the call waits until they report the
    current generation. Older maps cannot; with ``replace`` a background richi
    map replaces each one on its port, except a map attached to a terminal,
    which its user restarts there. Maps of other installations, unverifiable
    processes and maps attached to no storage are never touched; maps already
    on the current generation are reported as ``current``.
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
        details, _reason = _inspect(health)
        if (details is None or not any(_within(details["launcher"], path) for path in owned)
                or not _valid_selection(health)):
            continue
        version = health["runtime"].get("version")
        if health.get("installation_generation") == current and _within(details["launcher"], current_prefix):
            results.append({"url": url(port), "pid": health["pid"], "status": "current",
                            "version": version, "generation": current})
            continue
        entry = {"url": url(port), "pid": health["pid"], "from_version": version,
                 "from_generation": health.get("installation_generation")}
        if HANDOFF_CAPABILITY in health["capabilities"]:
            problem = request_handoff(port, health)
            if problem is None and wait:
                pending.append(entry)
                continue
            if problem is None:
                entry.update(status="requested")
            else:
                entry.update(status="failed", error=REASONS[problem])
        elif details["terminal"]:
            entry.update(status="restart_required", terminal=True,
                         error="This map predates automatic handoff and runs in a terminal; stop it there and "
                               "start richi map serve again, or run richi map restart")
        elif replace:
            entry.update(_spawn_replacement(port, health))
        else:
            entry.update(status="restart_required",
                         error="This map predates automatic handoff; the next richi command in a terminal "
                               "or richi map replaces it")
        results.append(entry)
    deadline = time.monotonic() + timeout
    while pending:
        for entry in list(pending):
            health = read_health(_port_of(entry))
            if is_map(health) and health.get("installation_generation") == current:
                entry.update(status="restarted", pid=health["pid"], version=health["runtime"].get("version"),
                             generation=current)
                results.append(entry)
                pending.remove(entry)
        if not pending:
            break
        if time.monotonic() > deadline:
            for entry in pending:
                entry.update(status="pending",
                             error="The map was asked to restart but has not reported the new version yet; "
                                   "its log (runtime/map-server.log beside its database, or the terminal "
                                   "running it) says why")
                results.append(entry)
            break
        time.sleep(0.1)
    return sorted(results, key=lambda item: item["url"])


def _reset_marker(root):
    directory = _registry(root)
    if directory is not None:
        try:
            (directory / MARKER).unlink()
        except FileNotFoundError:
            pass


def _default_ports():
    """The launcher's default port range; the updater reads no workspace configuration."""
    try:
        first = int(os.environ.get("RICHI_PORT", DEFAULT_PORT))
    except ValueError:
        first = DEFAULT_PORT
    if not 1 <= first <= 65535:
        first = DEFAULT_PORT
    return range(first, min(first + PORT_RANGE, 65536))


def after_activation(result):
    """Updater step after a committed activation; it never fails the update.

    Maps found in the registry or on the default ports are asked to restart on
    the activated generation; older maps there are reported. Maps elsewhere are
    handled by the next terminal command, which reconciles once per generation
    and port range, or by richi map itself.
    """
    if not isinstance(result, dict) or result.get("status") not in {"updated", "rolled_back"}:
        return result
    try:
        _reset_marker(_context()["root"])
        maps = reconcile(_default_ports())
    except Exception as exc:  # The activation is already committed.
        result["maps"] = []
        result["maps_error"] = str(exc)[:500]
        result["restart_maps_required"] = True
        return result
    result["maps"] = maps
    result["restart_maps_required"] = any(entry.get("status") not in {"restarted", "current"} for entry in maps)
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


def _ports(value):
    return sorted({item for item in value if type(item) is int and 1 <= item <= 65535}) \
        if isinstance(value, list) else []


def _settled(recorded, generation, first, interactive):
    """This generation's maps around ``first`` were handled, as far as this command may."""
    return (recorded is not None and recorded.get("format_version") == 2
            and recorded.get("generation") == generation and first in _ports(recorded.get("scanned"))
            and not (interactive and _ports(recorded.get("pending"))))


def first_invocation(port, interactive=True):
    """Once per generation and port range, a command running it hands off stale maps.

    Covers activations by updaters that cannot do it themselves, such as the
    frozen managers up to 1.1.0. The fast path reads one small file and the
    command never waits for a map: newer maps are asked to switch, older ones
    are replaced in the background. Only an ``interactive`` command (a terminal
    without a chat identity) starts replacements; others record them as pending
    for the next terminal command, since agents may run sandboxed.
    """
    root, generation = os.environ.get(ROOT_ENV), os.environ.get(GENERATION_ENV)
    if not root or not generation or os.environ.get(PASSIVE_ENV) == "1":
        return
    first = port if 1 <= port <= 65535 else DEFAULT_PORT
    marker = Path(root) / REGISTRY / MARKER
    if _settled(_read_json(marker), generation, first, interactive):
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
            return  # Another command is handling it right now.
        recorded = _read_json(marker)
        if _settled(recorded, generation, first, interactive):
            return
        if recorded is None or recorded.get("format_version") != 2 or recorded.get("generation") != generation:
            recorded = {}
        scanned, pending = _ports(recorded.get("scanned")), _ports(recorded.get("pending"))
        ports = set() if first in scanned else set(range(first, min(first + PORT_RANGE, 65536)))
        if interactive:
            ports |= set(pending)
        maps = reconcile(ports, replace=interactive, wait=False)
        # Older maps this command could not replace wait for a terminal command;
        # pending ports it did not look at stay pending.
        waiting = {_port_of(entry) for entry in maps
                   if entry.get("status") == "restart_required" and not entry.get("terminal")}
        # Written last: an interrupted command leaves the work to the next one.
        _atomic_json(marker, {"format_version": 2, "generation": generation, "time": int(time.time()),
                              "scanned": sorted(set(scanned) | {first})[-64:],
                              "pending": sorted((set(pending) - ports) | waiting), "maps": maps})
    finally:
        os.close(descriptor)
