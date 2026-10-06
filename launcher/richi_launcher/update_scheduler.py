"""Per-user OS timers for one original Richi installation (no workspace access)."""
import hashlib
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import tempfile

from .config import ConfigError

TICK_SECONDS = 900
COMMAND_TIMEOUT = 20


def _canonical(value, name):
    path = Path(value)
    if (not path.is_absolute() or path.resolve() != path
            or any(ord(char) < 32 or ord(char) == 127 for char in str(path))):
        raise ConfigError("Scheduler " + name + " must be an absolute canonical path without control characters")
    return path


def _quote(value):
    # systemd command lines have their own quoting, specifiers and variables;
    # they are not shell commands. Escape all three interpretation layers.
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def _plan(ctx):
    if sys.platform not in {"darwin", "linux"}:
        raise ConfigError("Automatic updates require macOS launchd or Linux systemd --user")
    original = _canonical(ctx["original_prefix"], "original environment")
    root = _canonical(ctx["root"], "installation root")
    identity = hashlib.sha256(str(original).encode("utf-8")).hexdigest()[:24]
    if root.name != identity or root.parent.name != "installations":
        raise ConfigError("Scheduler installation identity does not match its original environment")
    home = _canonical(str(Path.home().resolve()), "home")
    environment = ["HOME=" + str(home), "PATH=/usr/bin:/bin"]
    if sys.platform == "darwin":
        if root != home / "Library" / "Application Support" / "Richi" / "installations" / identity:
            raise ConfigError("Scheduler installation belongs to a different macOS home")
        directory = home / "Library" / "LaunchAgents"
    else:
        if root.parent.parent.name != "richi":
            raise ConfigError("Scheduler installation root is not a Richi data directory")
        environment.append("XDG_DATA_HOME=" + str(root.parents[2]))
        raw_config = os.environ.get("XDG_CONFIG_HOME", "")
        config = Path(raw_config) if raw_config and Path(raw_config).is_absolute() else home / ".config"
        directory = config / "systemd" / "user"
    directory = _canonical(str(directory), "service directory")
    label = "io.github.hidanio.richi.update." + identity
    # env -i prevents a manager's imported shell/chat environment selecting an
    # unrelated store. Enter the retained bootstrap directly: the seed Richi
    # namespace may be replaced by GC before it could acquire a process lease.
    argv = ["/usr/bin/env", "-i", *environment, str(original / "bin" / "python"),
            "-I", "-B", "-c", "from richi_bootstrap import main; raise SystemExit(main())",
            "update", "auto", "run"]
    if sys.platform == "darwin":
        files = {directory / (label + ".plist"): plistlib.dumps({
            "Label": label, "ProgramArguments": argv, "RunAtLoad": True,
            "StartInterval": TICK_SECONDS, "ProcessType": "Background",
            "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null",
        }, sort_keys=True)}
        binary = "/bin/launchctl"
    else:
        service = ("[Unit]\nDescription=Richi release update check\n\n[Service]\nType=oneshot\n"
                   "ExecStart=" + " ".join(_quote(part) for part in argv) + "\n"
                   "TimeoutStartSec=30min\nStandardOutput=null\nStandardError=null\n")
        timer = ("[Unit]\nDescription=Richi release update timer\n\n[Timer]\nOnActiveSec=1min\n"
                 "OnUnitInactiveSec=15min\nAccuracySec=1min\nUnit=" + label + ".service\n\n"
                 "[Install]\nWantedBy=timers.target\n")
        files = {directory / (label + ".service"): service.encode("utf-8"),
                 directory / (label + ".timer"): timer.encode("utf-8")}
        binary = "/usr/bin/systemctl"
    return {"backend": "launchd" if sys.platform == "darwin" else "systemd",
            "label": label, "files": files, "directory": directory, "binary": binary,
            "target": "gui/" + str(os.getuid()) + "/" + label,
            "domain": "gui/" + str(os.getuid())}


def _inventory(plan):
    present = []
    for path, content in plan["files"].items():
        _canonical(str(path), "service file")
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022 or info.st_size > 65536 or path.read_bytes() != content):
            raise ConfigError("Refusing unowned or modified scheduler file: " + str(path))
        present.append(path)
    return present


def _run(plan, args, checked=True):
    command = [plan["binary"], *args]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=COMMAND_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError("Cannot contact " + plan["backend"] + " user scheduler: " + str(exc)) from exc
    if checked and result.returncode:
        detail = (result.stderr or result.stdout or "command failed").strip()[:1000]
        raise ConfigError(plan["backend"] + " scheduler command failed: " + detail)
    return result


def _query(plan):
    if plan["backend"] == "launchd":
        result = _run(plan, ["print", plan["target"]], checked=False)
        if result.returncode:
            # Distinguish a missing service from an unavailable GUI session.
            _run(plan, ["print", plan["domain"]])
            return {"registered": False, "active": False, "enabled": False}
        # launchctl has no structured origin query. Treat its diagnostic path
        # as a prerequisite only: unfamiliar or ambiguous output fails closed,
        # rather than authorizing control of another job with the same label.
        origins = [line.split("=", 1)[1].strip() for line in result.stdout.splitlines()
                   if line.lstrip().startswith("path = ")]
        expected = str(next(iter(plan["files"])))
        if origins != [expected]:
            raise ConfigError("Cannot verify launchd job ownership: missing, ambiguous or unowned plist path")
        return {"registered": True, "active": True, "enabled": True}
    result = _run(plan, ["--user", "show", plan["label"] + ".timer", "--no-pager",
                         "--property=LoadState", "--property=ActiveState",
                         "--property=UnitFileState", "--property=FragmentPath",
                         "--property=DropInPaths"], checked=False)
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if values.get("LoadState") == "not-found":
        return {"registered": False, "active": False, "enabled": False}
    if result.returncode or values.get("LoadState") != "loaded":
        raise ConfigError("Cannot inspect systemd user timer: " + (result.stderr or result.stdout).strip()[:1000])
    expected = str(plan["directory"] / (plan["label"] + ".timer"))
    if values.get("FragmentPath") != expected:
        raise ConfigError("Refusing scheduler unit loaded from an unowned path")
    if values.get("DropInPaths") != "":
        raise ConfigError("Refusing scheduler timer with unverified drop-in configuration")
    # A timer can target a separately overridden service. Validate both units
    # before enable/disable or stop can affect a process.
    service = _run(plan, ["--user", "show", plan["label"] + ".service", "--no-pager",
                          "--property=LoadState", "--property=FragmentPath",
                          "--property=DropInPaths"], checked=False)
    service_values = dict(line.split("=", 1) for line in service.stdout.splitlines() if "=" in line)
    if (service.returncode or service_values.get("LoadState") != "loaded"
            or service_values.get("FragmentPath") != str(plan["directory"] / (plan["label"] + ".service"))):
        raise ConfigError("Refusing scheduler service loaded from an unowned or unknown path")
    if service_values.get("DropInPaths") != "":
        raise ConfigError("Refusing scheduler service with unverified drop-in configuration")
    return {"registered": True, "active": values.get("ActiveState") == "active",
            "enabled": values.get("UnitFileState") == "enabled"}


def status(ctx):
    """Read files and OS registration without creating data, locks or folders."""
    result = {"supported": False, "installed": False, "registered": None, "active": None}
    try:
        plan = _plan(ctx)
        result.update({"supported": True, "backend": plan["backend"], "label": plan["label"],
                       "files": [str(path) for path in plan["files"]], "tick_seconds": TICK_SECONDS})
        present = _inventory(plan)
        result["installed"] = len(present) == len(plan["files"])
        result.update(_query(plan))
        if result["registered"] and not result["installed"]:
            result["reason"] = "Registered scheduler has missing ownership files; repair is required"
    except (ConfigError, OSError) as exc:
        result["reason"] = str(exc)
    return result


def _write(path, content):
    _canonical(str(path.parent), "service directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".richi-update-", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # Link is atomic and refuses to replace a concurrent foreign file.
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


def _stop(plan):
    if plan["backend"] == "launchd":
        _run(plan, ["bootout", plan["target"]])
    else:
        _run(plan, ["--user", "disable", "--now", plan["label"] + ".timer"])
        _run(plan, ["--user", "stop", plan["label"] + ".service"])


def install(ctx):
    """Register owned files; on failure restore the previous files and timer."""
    plan = _plan(ctx)
    python = Path(ctx["original_prefix"]) / "bin" / "python"
    if not python.is_file() or not os.access(str(python), os.X_OK):
        raise ConfigError("Automatic updates need the original installation's executable Python: " + str(python))
    present = _inventory(plan)
    before = _query(plan)
    complete = len(present) == len(plan["files"])
    if before["registered"] and not complete:
        raise ConfigError("Refusing registered scheduler without complete owned files")
    if complete and before["active"] and before["enabled"]:
        return {"status": "already_installed", "backend": plan["backend"], "label": plan["label"]}
    created = []
    activation_attempted = False
    try:
        for path, content in plan["files"].items():
            if path not in present:
                _write(path, content)
                created.append(path)
        if plan["backend"] == "launchd":
            activation_attempted = True
            _run(plan, ["bootstrap", plan["domain"], str(next(iter(plan["files"])))])
        else:
            if created:
                _run(plan, ["--user", "daemon-reload"])
            activation_attempted = True
            _run(plan, ["--user", "enable", "--now", plan["label"] + ".timer"])
        after = _query(plan)
        if not (after["registered"] and after["active"] and after["enabled"]):
            raise ConfigError("Scheduler did not become active after registration")
        return {"status": "installed", "backend": plan["backend"], "label": plan["label"]}
    except (ConfigError, OSError) as exc:
        rollback_errors = []
        try:
            if activation_attempted and _query(plan)["registered"]:
                _stop(plan)
        except ConfigError as failure:
            rollback_errors.append(str(failure))
        for path in reversed(created):
            try:
                _inventory(plan)
                path.unlink()
            except (ConfigError, OSError) as failure:
                rollback_errors.append(str(failure))
        if plan["backend"] == "systemd":
            try:
                if created:
                    _run(plan, ["--user", "daemon-reload"])
                if before["enabled"]:
                    _run(plan, ["--user", "enable", plan["label"] + ".timer"])
                if before["active"]:
                    _run(plan, ["--user", "start", plan["label"] + ".timer"])
            except ConfigError as failure:
                rollback_errors.append(str(failure))
        detail = "; rollback needs attention: " + "; ".join(rollback_errors) if rollback_errors else "; prior scheduler files restored"
        raise ConfigError(str(exc) + detail) from exc


def remove(ctx):
    """Stop this owned job only. Engine disables auto state before calling us."""
    plan = _plan(ctx)
    present = _inventory(plan)
    before = _query(plan)
    if before["registered"]:
        if len(present) != len(plan["files"]):
            raise ConfigError("Refusing to stop scheduler without complete owned files")
        _stop(plan)
    for path in present:
        _inventory(plan)
        path.unlink()
    if present and plan["backend"] == "systemd":
        _run(plan, ["--user", "daemon-reload"])
    return {"status": "removed" if present or before["registered"] else "already_removed",
            "backend": plan["backend"], "label": plan["label"]}
