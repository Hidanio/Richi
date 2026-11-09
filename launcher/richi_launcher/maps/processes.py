"""What a process is: its exact argument vector, owner and terminal, read from the OS."""
import json
import os
from pathlib import Path
import sys

_RUNNER_MARKER = 'os.environ["RICHI_ACTIVE_RUNTIME"] = payload'
# Options of the richi command line before a command, and of richi map, as the
# launcher passes them to the runtime runner. Anything else is not a map.
_GLOBAL_OPTIONS = {"--db", "--config", "--workspace", "-w", "--project-path", "--chat"}
_GLOBAL_FLAGS = {"--compact-json"}
_MAP_OPTIONS = {"--db", "--config", "--port"}
_MAP_FLAGS = {"--open", "--no-open"}


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


def process_owner(pid):
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
    return process_owner(pid)[0]


def _loopback_listeners(table, port):
    """Socket inodes listening on 127.0.0.1:port in a /proc/net/tcp table."""
    import socket
    import struct
    inodes = set()
    for line in table.splitlines()[1:]:
        fields = line.split()
        # sl local_address rem_address st ... uid timeout inode; 0A is LISTEN.
        if len(fields) < 10 or fields[3] != "0A":
            continue
        address, _, local_port = fields[1].partition(":")
        try:
            # The kernel prints the address as a native-endian 32-bit number.
            if (int(local_port, 16) == port and len(address) == 8
                    and socket.inet_ntoa(struct.pack("=I", int(address, 16))) == "127.0.0.1"):
                inodes.add("socket:[%s]" % fields[9])
        except (ValueError, struct.error):
            continue
    return inodes


def _linux_listens(pid, port, proc="/proc"):
    try:
        # This process's network namespace, where the health answer came from:
        # a socket of the same port in another namespace is a different listener.
        inodes = _loopback_listeners(Path(proc, "self", "net", "tcp").read_text(encoding="ascii"), port)
        if not inodes:
            return False
        descriptors = Path(proc, str(pid), "fd")
        denied = False
        for name in os.listdir(descriptors):
            try:
                if os.readlink(descriptors / name) in inodes:
                    return True
            except PermissionError:
                denied = True  # For example a Landlock sandbox around this process.
            except OSError:
                continue
    except PermissionError:
        return None
    except (OSError, UnicodeError):
        return False
    return None if denied else False


def _darwin_listens(pid, port):
    import subprocess
    try:
        result = subprocess.run(["/usr/sbin/lsof", "-a", "-n", "-P", "-w", "-p", str(pid),
                                 "-iTCP@127.0.0.1:%d" % port, "-sTCP:LISTEN", "-Fp"],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and "p%d" % pid in result.stdout.split()


def listens(pid, port):
    """Whether the process holds the TCP socket listening on 127.0.0.1:port here.

    Whoever listens on a port answers its health, so this ties an answer to
    the process it names: another listener cannot borrow a real map's PID.
    False when it does not or that cannot be shown; None when this
    environment may not look at the process's descriptors.
    """
    if sys.platform.startswith("linux"):
        return _linux_listens(pid, port)
    if sys.platform == "darwin":
        return _darwin_listens(pid, port)
    return False


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


def runs_map(pid, runtime):
    """The live process with this ID is a Richi runner serving a map with this runtime."""
    arguments = process_arguments(pid)
    return bool(arguments) and runner_launcher(arguments, runtime) is not None
