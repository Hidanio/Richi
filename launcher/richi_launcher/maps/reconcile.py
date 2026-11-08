"""Bring an installation's running maps to its current generation after an activation.

scan() finds and classifies the maps. The updater (after_activation) and the
first command of a new generation (first_invocation) then act on each one
explicitly: ask it to switch, wait for that, replace it in the background, or
report that someone has to restart it. Reports become JSON only on output.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import time
from typing import List, NamedTuple, Optional

import richi_bootstrap as bootstrap

from . import registry
from .control import inspect, is_map, read_health, request_handoff, url
from .handoff import context, environments, stable_command, within
from .protocol import (DEFAULT_PORT, GENERATION_ENV, HANDOFF_CAPABILITY, HANDOFF_TIMEOUT, LEASE_ENV,
                       LISTEN_FD_ENV, PASSIVE_ENV, PORT_RANGE, PROBE_ENV, ROOT_ENV, Status)

# Child map processes receive their storage explicitly; nothing ambient may
# redirect them or keep the parent's lease, generation or socket.
_CHILD_UNSET = ("RICHI_ACTIVE_RUNTIME", "RICHI_CHAT_ID", "CODEX_THREAD_ID", "RICHI_DB",
                "RICHI_DATA_DIR", "RICHI_PORT", "RICHI_WORKSPACE", "RICHI_CONFIG",
                "RICHI_ACTIVE_CONFIG", "RICHI_ACTIVE_WORKSPACE", PASSIVE_ENV, PROBE_ENV, LISTEN_FD_ENV,
                LEASE_ENV, ROOT_ENV, GENERATION_ENV)
_IN_TERMINAL = ("This map predates automatic handoff and runs in a terminal; stop it there and "
                "start richi map serve again, or run richi map restart")
_NEXT_COMMAND = ("This map predates automatic handoff; the next richi command in a terminal "
                 "or richi map replaces it")
_NOT_SWITCHED = ("The map was asked to restart but has not reported the new version yet; its log "
                 "(runtime/map-server.log beside its database, or the terminal running it) says why")


@dataclass
class Found:
    """A verified map of this installation."""
    port: int
    health: dict
    terminal: bool
    current: bool  # It runs the current generation from that generation's environment.

    @property
    def pid(self):
        return self.health["pid"]

    @property
    def version(self):
        return self.health["runtime"].get("version")

    @property
    def follows(self):
        """It re-executes itself on request (this release onwards)."""
        return HANDOFF_CAPABILITY in self.health["capabilities"]


class Scan(NamedTuple):
    generation: Optional[str]  # The installation's current generation; None when unmanaged.
    maps: List[Found]


@dataclass
class Report:
    """What happened to one map."""
    port: int
    pid: int
    status: Status
    from_version: Optional[str] = None
    from_generation: Optional[str] = None
    version: Optional[str] = None
    generation: Optional[str] = None
    error: Optional[str] = None
    terminal: bool = False

    @classmethod
    def of(cls, found, status, **fields):
        generation = found.health.get("installation_generation")
        if status is Status.CURRENT:
            return cls(found.port, found.pid, status, version=found.version, generation=generation, **fields)
        return cls(found.port, found.pid, status, from_version=found.version, from_generation=generation,
                   **fields)

    def as_dict(self):
        value = {"url": url(self.port), "pid": self.pid, "status": self.status.value}
        if self.status is not Status.CURRENT:
            value.update(from_version=self.from_version, from_generation=self.from_generation)
        if self.status in (Status.CURRENT, Status.RESTARTED):
            value.update(version=self.version, generation=self.generation)
        if self.error is not None:
            value["error"] = self.error
        if self.terminal:
            value["terminal"] = True
        return value


def _valid_selection(health):
    database, config_file, workspace = (health.get(key) for key in ("database", "config_file", "workspace"))
    return (isinstance(database, str) and Path(database).is_absolute()
            and (config_file is None or (isinstance(config_file, str) and Path(config_file).is_absolute()))
            and (workspace is None or isinstance(workspace, str)))


def scan(ports=()):
    """This installation's maps on its registered ports and on these.

    Maps of other installations, unverifiable processes and maps attached to
    no valid storage are left out; entries of maps that no longer run are
    pruned. Nothing is signalled.
    """
    ctx = context()
    root = Path(ctx["root"])
    if not (root / "state.json").exists():
        return Scan(None, [])
    state = bootstrap.read_state(ctx)
    current = state["current"]
    current_prefix = Path(state["generations"][current]["prefix"])
    owned = environments(ctx)
    found = []
    for port in sorted(set(registry.registered_ports(root)) | {port for port in ports if 1 <= port <= 65535}):
        health = read_health(port)
        if not is_map(health):
            registry.prune(root, port)
            continue
        verified, _reason = inspect(health)
        if (verified is None or not any(within(verified.launcher, path) for path in owned)
                or not _valid_selection(health)):
            continue
        found.append(Found(port, health, verified.terminal,
                           current=health.get("installation_generation") == current
                           and within(verified.launcher, current_prefix)))
    return Scan(current, found)


def ask(found):
    """Ask a map that follows its installation to switch now: REQUESTED, or FAILED."""
    reason = request_handoff(found.port, found.health)
    if reason is None:
        return Report.of(found, Status.REQUESTED)
    return Report.of(found, Status.FAILED, error=reason.message)


def await_switch(reports, generation, timeout=HANDOFF_TIMEOUT):
    """Wait until the maps asked to switch report ``generation``: RESTARTED, else PENDING."""
    waiting = [report for report in reports if report.status is Status.REQUESTED]
    deadline = time.monotonic() + timeout
    while waiting:
        for report in list(waiting):
            health = read_health(report.port)
            if is_map(health) and health.get("installation_generation") == generation:
                report.status, report.pid = Status.RESTARTED, health["pid"]
                report.version, report.generation = health["runtime"].get("version"), generation
                waiting.remove(report)
        if not waiting:
            break
        if time.monotonic() > deadline:
            for report in waiting:
                report.status, report.error = Status.PENDING, _NOT_SWITCHED
            break
        time.sleep(0.1)
    return reports


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


def replace_in_background(found):
    """Run richi map for this map's storage without waiting: REPLACING, or FAILED.

    richi map checks the new runtime before it stops the old map on its URL,
    and reports its own errors.
    """
    import subprocess
    arguments, environment = selection(found.health)
    command, _through_entry_point = stable_command([*arguments, "map", "--no-open", "--port", str(found.port)])
    try:
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, env=environment, close_fds=True, start_new_session=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return Report.of(found, Status.FAILED, error=str(exc)[:500])
    return Report.of(found, Status.REPLACING)


def leave(found):
    """An older map nobody restarts here: RESTART_REQUIRED."""
    if found.terminal:
        # Its user restarts it there; replacing it would detach it from that terminal.
        return Report.of(found, Status.RESTART_REQUIRED, terminal=True, error=_IN_TERMINAL)
    return Report.of(found, Status.RESTART_REQUIRED, error=_NEXT_COMMAND)


def hand_off(ports=(), timeout=HANDOFF_TIMEOUT):
    """The updater's step: maps that follow switch while it waits; older ones are reported."""
    scanned = scan(ports)
    reports = [Report.of(found, Status.CURRENT) if found.current
               else ask(found) if found.follows else leave(found)
               for found in scanned.maps]
    return sorted(await_switch(reports, scanned.generation, timeout), key=lambda report: report.port)


def nudge(ports=(), replace=False):
    """The first command's step, which never waits: ask maps that follow; replace older ones if allowed."""
    reports = []
    for found in scan(ports).maps:
        if found.current:
            reports.append(Report.of(found, Status.CURRENT))
        elif found.follows:
            reports.append(ask(found))
        elif replace and not found.terminal:
            reports.append(replace_in_background(found))
        else:
            reports.append(leave(found))
    return sorted(reports, key=lambda report: report.port)


def default_ports():
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
    handled by the next terminal command, which looks once per generation and
    port range, or by richi map itself.
    """
    if not isinstance(result, dict) or result.get("status") not in {"updated", "rolled_back"}:
        return result
    try:
        registry.reset_marker(context()["root"])
        reports = hand_off(default_ports())
    except Exception as exc:  # The activation is already committed.
        result["maps"] = []
        result["maps_error"] = str(exc)[:500]
        result["restart_maps_required"] = True
        return result
    result["maps"] = [report.as_dict() for report in reports]
    result["restart_maps_required"] = any(report.status not in (Status.RESTARTED, Status.CURRENT)
                                          for report in reports)
    cleanup = result.get("cleanup")
    if (any(report.status is Status.RESTARTED for report in reports)
            and isinstance(cleanup, dict) and cleanup.get("deferred")):
        # Restarted maps released older leases; collect what is no longer leased.
        try:
            from .. import installation
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
    if _settled(registry.read_marker(root), generation, first, interactive):
        return
    if not (Path(root) / "state.json").exists() or registry.current_generation(root) != generation:
        return
    import fcntl
    entries = registry.directory(root, create=True)
    if entries is None:
        return
    descriptor = os.open(str(entries / "handoff.lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return  # Another command is handling it right now.
        recorded = registry.read_marker(root)
        if _settled(recorded, generation, first, interactive):
            return
        if recorded is None or recorded.get("format_version") != 2 or recorded.get("generation") != generation:
            recorded = {}
        scanned, pending = _ports(recorded.get("scanned")), _ports(recorded.get("pending"))
        ports = set() if first in scanned else set(range(first, min(first + PORT_RANGE, 65536)))
        if interactive:
            ports |= set(pending)
        reports = nudge(ports, replace=interactive)
        # Older maps this command could not replace wait for a terminal command;
        # pending ports it did not look at stay pending.
        waiting = {report.port for report in reports
                   if report.status is Status.RESTART_REQUIRED and not report.terminal}
        # Written last: an interrupted command leaves the work to the next one.
        # time and maps are for diagnostics; scanned and pending drive the hook.
        registry.write_marker(root, {"format_version": 2, "generation": generation, "time": int(time.time()),
                                     "scanned": sorted(set(scanned) | {first})[-64:],
                                     "pending": sorted((set(pending) - ports) | waiting),
                                     "maps": [report.as_dict() for report in reports]})
    finally:
        os.close(descriptor)
