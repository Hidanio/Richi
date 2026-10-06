"""Opt-in installation maintenance; never resolves a workspace or opens SQLite.

The system scheduler invokes one bounded tick. A separate run lock prevents
overlap; short policy locks let disabling cancel a download or staged release.
Enable/disable serialize through auto-control.lock. The shared mutation order
is update.lock, auto.lock (policy), then installation state.lock.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import time
import uuid

from . import installation, releases
from .config import ConfigError

DEFAULT_INTERVAL = 6 * 60 * 60
MIN_INTERVAL = 15 * 60
MAX_INTERVAL = 7 * 24 * 60 * 60
MAX_HISTORY = 20


def _now():
    return int(time.time())


def _read(ctx, name, default):
    path = Path(ctx["root"]) / name
    if path.is_symlink():
        raise ConfigError("Automatic update metadata cannot be a symlink: " + name)
    try:
        with path.open("rb") as stream:
            raw = stream.read(65537)
    except FileNotFoundError:
        return default
    except OSError as exc:
        raise ConfigError("Cannot read automatic update metadata: " + str(exc)) from exc
    if len(raw) > 65536:
        raise ConfigError("Automatic update metadata exceeds its size limit")
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ConfigError("Invalid automatic update metadata: " + name) from exc
    if (not isinstance(result, dict) or type(result.get("format_version")) is not int
            or result["format_version"] != 1):
        raise ConfigError("Unsupported automatic update metadata: " + name)
    return result


def _interval(value):
    if type(value) is not int or not MIN_INTERVAL <= value <= MAX_INTERVAL:
        raise ConfigError("Automatic update interval must be 900 through 604800 seconds")
    return value


def _config(ctx):
    value = _read(ctx, "auto.json", {"format_version": 1, "enabled": False,
                                    "interval_seconds": DEFAULT_INTERVAL, "policy_id": None})
    if type(value.get("enabled")) is not bool:
        raise ConfigError("Automatic update enabled must be a boolean")
    _interval(value.get("interval_seconds"))
    if value["enabled"] and (not isinstance(value.get("policy_id"), str)
                             or not 1 <= len(value["policy_id"]) <= 128):
        raise ConfigError("Automatic update policy identity is missing")
    return value


def _state(ctx):
    value = _read(ctx, "auto-state.json", {"format_version": 1, "next_due": 0,
                  "failure_count": 0, "history": [], "in_progress": None,
                  "quarantine": None, "approved_generation": None})
    if (type(value.get("next_due")) is not int or value["next_due"] < 0
            or type(value.get("failure_count")) is not int or not 0 <= value["failure_count"] <= 100
            or not isinstance(value.get("history"), list) or len(value["history"]) > MAX_HISTORY
            or any(not isinstance(item, dict) for item in value["history"])
            or any(value.get(key) is not None and not isinstance(value[key], dict)
                   for key in ("in_progress", "quarantine"))):
        raise ConfigError("Invalid automatic update state")
    if value.get("approved_generation") is not None and not isinstance(value["approved_generation"], str):
        raise ConfigError("Invalid automatic update generation")
    pending = value.get("in_progress")
    if pending is not None:
        if (pending.get("phase") not in {"checking", "applying"}
                or type(pending.get("started_at")) is not int or pending["started_at"] < 0
                or any(not isinstance(pending.get(key), str) or not 1 <= len(pending[key]) <= 128
                       for key in ("attempt_id", "policy_id", "expected_generation"))):
            raise ConfigError("Invalid automatic update attempt journal")
    for record in (value.get("quarantine"), pending if pending and pending["phase"] == "applying" else None):
        if record is not None:
            releases.version_tuple(record.get("version"))
            if not isinstance(record.get("wheel_sha256"), str) or re.fullmatch(r"[0-9a-f]{64}", record["wheel_sha256"]) is None:
                raise ConfigError("Invalid automatic update candidate checksum")
    return value


def _write(ctx, name, value):
    installation._atomic_json(Path(ctx["root"]) / name, value)


@contextmanager
def _lock(ctx, name="auto.lock", blocking=True):
    import fcntl
    root = Path(ctx["root"])
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or root.resolve() != root:
        raise ConfigError("Automatic update directory must be canonical")
    descriptor = os.open(str(root / name), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(descriptor)


def _pause(ctx):
    return _read(ctx, "auto-pause.json", None)


def pause_for_rollback(ctx, generation):
    """Called under update.lock before rollback; deliberately takes no auto.lock."""
    try:
        if not _config(ctx)["enabled"]:
            return
    except ConfigError:
        # A broken policy must never prevent rollback or permit silently
        # undoing it. Retain a conservative pause for explicit enable to clear.
        pass
    _write(ctx, "auto-pause.json", {"format_version": 1, "reason": "manual_rollback",
                                    "time": _now(), "generation": generation})


def _snapshot(ctx, local=None):
    from . import update_scheduler
    config, state, pause = _config(ctx), _state(ctx), _pause(ctx)
    return {"status": ("disabled" if not config["enabled"] else "paused" if pause else "enabled"),
            "enabled": config["enabled"], "interval_seconds": config["interval_seconds"],
            "pause": pause, "next_due": state["next_due"],
            "last_attempt": state.get("last_attempt"), "last_outcome": state.get("last_outcome"),
            "in_progress": state.get("in_progress"), "quarantine": state.get("quarantine"),
            "failure_count": state["failure_count"], "history": list(state["history"]),
            "scheduler": update_scheduler.status(ctx),
            "installation": local if local is not None else installation.status()}


def status():
    """Read policy and recent outcomes without creating any files."""
    local = installation.status()
    if not local.get("supported"):
        return {"status": "unsupported", "enabled": False, "installation": local}
    return _snapshot(installation._context(), local)


def enable(interval_seconds=DEFAULT_INTERVAL):
    """Install the scheduler and reset pause/quarantine after an explicit opt-in."""
    from . import update_scheduler
    _interval(interval_seconds)
    local = installation.status()
    if not local.get("supported"):
        raise ConfigError(local.get("reason", "Managed updates are unavailable"))
    ctx = installation._context()
    # Serialize scheduler registration/removal separately; never wait for the
    # installation lock while holding the short policy lock (auto.lock).
    with _lock(ctx, "auto-control.lock"):
        with installation._locked(ctx):
            with _lock(ctx):
                config, state = _config(ctx), _state(ctx)
                local = installation.status()
                if not local.get("supported"):
                    raise ConfigError(local.get("reason", "Managed updates are unavailable"))
                update_scheduler.install(ctx)
                state.update(next_due=0, failure_count=0, quarantine=None, in_progress=None,
                             approved_generation=local["current"]["generation"])
                _write(ctx, "auto-state.json", state)
                config.update(enabled=True, interval_seconds=interval_seconds,
                              policy_id=uuid.uuid4().hex, enabled_at=_now())
                _write(ctx, "auto.json", config)
                (Path(ctx["root"]) / "auto-pause.json").unlink(missing_ok=True)
    return _snapshot(ctx)


def disable():
    """Persist disabled first, so even an OS deregistration failure is safe."""
    from . import update_scheduler
    ctx = installation._context()
    with _lock(ctx, "auto-control.lock"):
        with _lock(ctx):
            if (Path(ctx["root"]) / "auto.json").exists():
                config = _config(ctx)
                config.update(enabled=False, policy_id=uuid.uuid4().hex, disabled_at=_now())
                _write(ctx, "auto.json", config)
        update_scheduler.remove(ctx)
    return _snapshot(ctx)


def _allowed(ctx, policy_id):
    config = _config(ctx)
    return config["enabled"] and config.get("policy_id") == policy_id and _pause(ctx) is None


def _error(exc):
    return " ".join(str(exc).split())[:500]


def _finish(ctx, attempt, outcome, *, version=None, error=None, retry=False,
            quarantine=None, append=True):
    with _lock(ctx):
        state, config = _state(ctx), _config(ctx)
        pending = state.get("in_progress")
        result = {"status": outcome}
        if version is not None:
            result["version"] = version
        if error is not None:
            result["error"] = _error(error)
        # Explicit re-enable creates a fresh policy while an old tick may still
        # be downloading. It owns the new state and cannot be overwritten here.
        if not pending or pending.get("attempt_id") != attempt["attempt_id"]:
            return result
        now = _now()
        failures = min(state["failure_count"] + 1, 100) if retry else 0
        delay = min(config["interval_seconds"], MIN_INTERVAL * 2 ** min(failures - 1, 12)) if retry else config["interval_seconds"]
        state.update(in_progress=None, failure_count=failures, next_due=now + delay)
        if quarantine is not None:
            state["quarantine"] = quarantine
        if outcome in {"updated", "recovered_updated", "up_to_date"}:
            state["quarantine"] = None
        if append:
            entry = dict(result, time=now)
            current = installation.status().get("current") or {}
            entry["current_version"] = current.get("version")
            state.update(last_attempt=attempt["started_at"], last_outcome=entry)
            state["history"] = (state["history"] + [entry])[-MAX_HISTORY:]
        _write(ctx, "auto-state.json", state)
        result["next_due"] = state["next_due"]
        return result


@contextmanager
def _activation_guard(ctx, attempt):
    # installation.apply_release holds update.lock around this context. Keep
    # auto.lock through the pointer write so disable cannot race activation.
    with _lock(ctx):
        allowed = _allowed(ctx, attempt["policy_id"])
        yield allowed
        if allowed:
            state = _state(ctx)
            pending = state.get("in_progress")
            if pending and pending.get("attempt_id") == attempt["attempt_id"]:
                current = installation.status().get("current") or {}
                state["approved_generation"] = current.get("generation")
                _write(ctx, "auto-state.json", state)


def _recovery(ctx, pending):
    # Recovery reports committed outcomes, so serialize against manual changes
    # just like activation and re-read the journal after both locks.
    # The snapshot taken before acquiring them is never sufficient evidence.
    try:
        with installation._locked(ctx, blocking=False):
            with _lock(ctx):
                state = _state(ctx)
                fresh = state.get("in_progress")
                if (not fresh or fresh.get("attempt_id") != pending["attempt_id"]
                        or not _allowed(ctx, pending["policy_id"])):
                    return {"status": "cancelled"}
                local = installation.status()
                if not local.get("supported"):
                    return {"status": "unsupported", "reason": local.get("reason")}
                current, pending = local["current"], fresh
                version = pending.get("version")
                changed = current.get("generation") != pending.get("expected_generation")
                verified = (pending["phase"] == "applying" and version == current.get("version")
                            and pending.get("wheel_sha256") == current.get("wheel_sha256"))
                # Matching bytes alone do not prove this tick activated
                # them: a manual update could install the same wheel after
                # interruption. Only the activation guard's durable approval
                # distinguishes that commit; ambiguous changes stay paused.
                if changed and verified and state.get("approved_generation") == current["generation"]:
                    outcome = "recovered_updated"
                elif changed:
                    _write(ctx, "auto-pause.json", {"format_version": 1, "reason": "installation_changed",
                           "time": _now(), "generation": current.get("generation")})
                    outcome = "superseded"
                else:
                    outcome = "interrupted_candidate" if pending["phase"] == "applying" else "interrupted_check"
    except installation._InstallationBusy:
        return {"status": "busy"}
    quarantine = ({"version": version, "wheel_sha256": pending.get("wheel_sha256"),
                   "reason": "Installation was interrupted before activation"}
                  if outcome == "interrupted_candidate" else None)
    return _finish(ctx, pending, outcome, version=version, quarantine=quarantine,
                   retry=outcome == "interrupted_check")


def run():
    """Perform at most one due attempt; no workspace, config or database access."""
    ctx = installation._context()
    config, state = _config(ctx), _state(ctx)
    if not config["enabled"]:
        return {"status": "disabled"}
    if _pause(ctx):
        return {"status": "paused", "pause": _pause(ctx)}
    if state.get("in_progress") is None and _now() < state["next_due"]:
        return {"status": "not_due", "next_due": state["next_due"]}
    with _lock(ctx, "auto-run.lock", blocking=False) as acquired:
        if not acquired:
            return {"status": "busy"}
        with _lock(ctx):
            config, state = _config(ctx), _state(ctx)
            if not config["enabled"]:
                return {"status": "disabled"}
            if _pause(ctx):
                return {"status": "paused", "pause": _pause(ctx)}
            local = installation.status()
            if not local.get("supported"):
                return {"status": "unsupported", "reason": local.get("reason")}
            current = local["current"]
            pending = state.get("in_progress")
            if pending is None and current["generation"] != state.get("approved_generation"):
                pause = {"format_version": 1, "reason": "installation_changed",
                         "time": _now(), "generation": current["generation"]}
                _write(ctx, "auto-pause.json", pause)
                return {"status": "paused", "pause": pause}
            if pending is None and _now() < state["next_due"]:
                return {"status": "not_due", "next_due": state["next_due"]}
            if pending is None:
                pending = {"attempt_id": uuid.uuid4().hex, "policy_id": config["policy_id"],
                           "started_at": _now(), "phase": "checking",
                           "expected_generation": current["generation"]}
                state["in_progress"] = pending
                _write(ctx, "auto-state.json", state)
                recovery = False
            else:
                recovery = True
        if recovery:
            return _recovery(ctx, pending)
        try:
            release = releases.fetch_release()
        except (ConfigError, OSError) as exc:
            return _finish(ctx, pending, "check_failed", error=exc, retry=True)
        if not _allowed(ctx, pending["policy_id"]):
            return _finish(ctx, pending, "cancelled")
        if release is None:
            return _finish(ctx, pending, "no_release")
        version = release["version"]
        if releases.version_tuple(version) < releases.version_tuple(current["version"]):
            return _finish(ctx, pending, "older_release", version=version)
        checksum = release["manifest"]["wheel"]["sha256"]
        if version == current["version"] and checksum == current.get("wheel_sha256"):
            return _finish(ctx, pending, "up_to_date", version=version)
        with _lock(ctx):
            state = _state(ctx)
            quarantined = state.get("quarantine")
            if quarantined and releases.version_tuple(version) <= releases.version_tuple(quarantined["version"]):
                blocked = True
            else:
                blocked = False
                active = state.get("in_progress")
                if active and active.get("attempt_id") == pending["attempt_id"]:
                    pending.update(phase="applying", version=version, wheel_sha256=checksum)
                    state["in_progress"] = pending
                    _write(ctx, "auto-state.json", state)
        if blocked:
            return _finish(ctx, pending, "quarantined", version=version)
        try:
            result = installation.apply_release(release, expected_generation=pending["expected_generation"],
                        blocking=False, before_apply=lambda: _allowed(ctx, pending["policy_id"]),
                        activation_guard=lambda: _activation_guard(ctx, pending), require_auto=True)
        except releases.TransientReleaseError as exc:
            return _finish(ctx, pending, "download_failed", version=version, error=exc, retry=True)
        except (ConfigError, OSError) as exc:
            return _finish(ctx, pending, "candidate_failed", version=version, error=exc,
                           quarantine={"version": version, "wheel_sha256": checksum, "reason": _error(exc)})
        outcome = result["status"]
        return _finish(ctx, pending, "up_to_date" if outcome == "already_current" else outcome,
                       version=version, retry=outcome == "busy", append=outcome != "busy")
