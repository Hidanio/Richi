"""Identify a running map through its health answer and signal it only once verified."""
import json
import os
import signal
import time
from typing import NamedTuple

from .processes import listens, process_arguments, process_owner, process_uid, runner_launcher, runs_map
from .protocol import APPLICATION, EXIT_TIMEOUT, HANDOFF_SIGNAL, RETRYABLE, STOP_TIMEOUT, Reason


def url(port):
    return "http://127.0.0.1:%d/" % port


def read_health(port, timeout=0.5):
    """A loopback map's health answer, or None. Never goes through a proxy."""
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


class Verified(NamedTuple):
    """A process proven to be this user's Richi map runner."""
    launcher: str   # The runner's launcher directory: which installation environment runs it.
    terminal: bool  # It has a controlling terminal, such as a foreground richi map serve.


class Refused(Exception):
    """A map was not signalled or stopped."""

    def __init__(self, reason, port, pid=None):
        super().__init__(reason.message)
        self.reason, self.port, self.pid = reason, port, pid

    @property
    def retryable(self):
        return self.reason in RETRYABLE

    def as_dict(self):
        return {"status": "refused", "reason": self.reason.value, "error": self.reason.message,
                "url": url(self.port), "pid": self.pid}


class Stopped(NamedTuple):
    port: int
    pid: int
    version: object
    exited: bool  # The process is gone, not only its port.

    def as_dict(self):
        return {"status": "stopped", "url": url(self.port), "pid": self.pid, "version": self.version,
                "exited": self.exited}


def inspect(health, port):
    """(Verified, None) for this user's map serving this port with exactly this runtime, else (None, Reason).

    The health answer names the PID; the process must belong to this user,
    run Richi's runtime runner with the reported runtime payload and itself
    hold the loopback socket listening on the port that answered. So an
    unrelated listener, an answer naming another map's PID, a forged answer
    or a reused PID is never trusted, and neither is the storage it reports.
    """
    if not is_map(health):
        return None, Reason.NOT_RICHI_MAP
    pid = health["pid"]
    if pid == os.getpid():
        return None, Reason.UNVERIFIED_PROCESS
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None, Reason.PROCESS_EXITED
    except PermissionError:
        # The same user's process in an environment that may not signal it, such as a sandbox.
        return None, Reason.NOT_PERMITTED if process_uid(pid) == os.getuid() else Reason.FOREIGN_USER
    except OSError:
        return None, Reason.UNVERIFIED_PROCESS
    uid, terminal = process_owner(pid)
    if uid != os.getuid():
        return None, Reason.FOREIGN_USER
    arguments = process_arguments(pid)
    launcher = runner_launcher(arguments, health["runtime"]) if arguments else None
    if launcher is None:
        return None, Reason.UNVERIFIED_PROCESS
    listening = listens(pid, port)
    if listening is None:
        return None, Reason.NOT_PERMITTED
    if not listening:
        return None, Reason.UNVERIFIED_PROCESS
    return Verified(launcher, bool(terminal)), None


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
    return runs_map(pid, runtime)


def stop(port, health, timeout=STOP_TIMEOUT):
    """Stop one verified map with SIGTERM and return Stopped; raise Refused otherwise.

    The map closes its port, then exits. Its version lease is the map's own
    descriptor, so it is released only when that process exits. Never
    escalates to SIGKILL.
    """
    verified, reason = inspect(health, port)
    pid = health.get("pid") if isinstance(health, dict) else None
    if verified is None:
        if reason is Reason.UNVERIFIED_PROCESS and is_map(health) and not _same_process(port, health):
            # It answers differently now, for example while it re-executes: look again.
            reason = Reason.CHANGED
        raise Refused(reason, port, pid)
    # The answer must still come from the same verified process right before signalling.
    if not _same_process(port, health) or not runs_map(pid, health["runtime"]):
        raise Refused(Reason.CHANGED, port, pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        raise Refused(Reason.NOT_PERMITTED, port, pid) from None
    deadline = time.monotonic() + timeout
    while True:
        live = read_health(port, timeout=0.2)
        if not (is_map(live) and live["pid"] == pid) and port_free(port):
            break
        if time.monotonic() > deadline:
            raise Refused(Reason.TIMEOUT, port, pid)
        time.sleep(0.05)
    deadline = time.monotonic() + EXIT_TIMEOUT
    exited = not _alive_as(pid, health["runtime"])
    while not exited and time.monotonic() < deadline:
        time.sleep(0.05)
        exited = not _alive_as(pid, health["runtime"])
    return Stopped(port, pid, health["runtime"].get("version"), exited)


def request_handoff(port, health):
    """Ask a verified map to re-execute on the current generation; return a Reason if it was not asked."""
    if not _same_process(port, health):
        return Reason.CHANGED
    try:
        os.kill(health["pid"], getattr(signal, HANDOFF_SIGNAL))
    except ProcessLookupError:
        return Reason.PROCESS_EXITED
    except PermissionError:
        return Reason.NOT_PERMITTED
    except OSError:
        return Reason.UNVERIFIED_PROCESS
    return None
