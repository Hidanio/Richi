"""Atomic release activation and leased garbage collection for dedicated venvs.

No operation in this module resolves a workspace configuration or opens a
database. Only the current and previous runtime are retained, except retired
generations still leased by running processes.
"""
from contextlib import contextmanager, nullcontext
import ast
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

import richi_bootstrap as bootstrap
from .config import ConfigError


def _context():
    return bootstrap.context()


def _version(site):
    tree = ast.parse((Path(site) / "richi" / "__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, str) and value:
                return value
    raise ConfigError("Installed Richi must declare its version")


def _validate_layout(ctx):
    prefix, site = Path(ctx["prefix"]), Path(ctx["site"])
    cfg = prefix / "pyvenv.cfg"
    if not cfg.is_file() or not (prefix / "bin" / "python").is_file():
        raise ConfigError("Managed updates require a dedicated virtual environment")
    values = dict(line.split("=", 1) for line in cfg.read_text(encoding="utf-8").splitlines() if "=" in line)
    values = {key.strip().lower(): value.strip().lower() for key, value in values.items()}
    if values.get("include-system-site-packages") != "false":
        raise ConfigError("Managed updates refuse virtual environments that expose system packages")
    if not (site / "richi" / "memory.py").is_file() or not (site / "richi_launcher" / "cli.py").is_file():
        raise ConfigError("Managed updates require a non-editable, fixed Richi installation")
    for name in ("richi", "richi_launcher"):
        package = site / name
        if package.is_symlink() or any(path.is_symlink() for path in package.rglob("*")):
            raise ConfigError("Managed updates refuse linked Richi package files")
    distributions = list(importlib.metadata.distributions(path=[str(site)]))
    names = {dist.metadata.get("Name", "").lower().replace("_", "-") for dist in distributions}
    if "richi" not in names or names - {"richi", "pip", "setuptools", "wheel"}:
        raise ConfigError("Managed updates require an environment dedicated to Richi (only Richi, pip, setuptools and wheel are allowed)")
    for dist in distributions:
        if dist.metadata.get("Name", "").lower() == "richi":
            raw = dist.read_text("direct_url.json")
            if raw:
                direct = json.loads(raw)
                if not isinstance(direct, dict) or not isinstance(direct.get("dir_info", {}), dict):
                    raise ConfigError("Invalid installed Richi direct_url metadata")
                if direct.get("dir_info", {}).get("editable"):
                    raise ConfigError("Managed updates refuse editable Richi installations")


def _seed_state(ctx):
    if ctx["prefix"] != ctx["original_prefix"]:
        raise ConfigError("Managed release is missing its installation state")
    return {"format_version": 1, "updater_protocol": 1,
            "original_prefix": ctx["original_prefix"], "seed_bootstrap": ctx["bootstrap"],
            "current": "seed", "previous": None,
            "generations": {"seed": {"kind": "seed", "prefix": ctx["original_prefix"],
                                      "site": ctx["site"], "version": _version(ctx["site"])}}}


def _state(ctx):
    return bootstrap.read_state(ctx) if (Path(ctx["root"]) / "state.json").exists() else _seed_state(ctx)


def _atomic_json(path, value):
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _owner(ctx, generation):
    return {"format_version": 1, "original_prefix": ctx["original_prefix"], "root": ctx["root"],
            "generation": generation, "kind": "seed" if generation == "seed" else "release"}


def _verify_owner(ctx, generation, prefix):
    marker = prefix / bootstrap.OWNER_FILE
    if prefix.is_symlink() or marker.is_symlink() or bootstrap._object(marker) != _owner(ctx, generation):
        raise ConfigError("Refusing to remove an environment without matching Richi ownership: " + str(prefix))
    expected = Path(ctx["original_prefix"]) if generation == "seed" else Path(ctx["root"]) / "versions" / generation
    if prefix != expected or prefix.resolve() != expected:
        raise ConfigError("Refusing to remove an environment outside the managed installation")


class _InstallationBusy(Exception):
    """Another process owns the installation mutation lock."""


@contextmanager
def _locked(ctx, state=False, blocking=True):
    import fcntl
    root = Path(ctx["root"])
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise ConfigError("Managed installation directory cannot be a symlink")
    # Downloads and pip serialize through update.lock. Bootstrap only reads
    # state.lock, which is held briefly for pointer changes and collection.
    descriptor = os.open(str(root / ("state.lock" if state else "update.lock")), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise _InstallationBusy() from exc
        yield
    finally:
        os.close(descriptor)


def _summary(ctx, state):
    records = state["generations"]
    current, previous = state["current"], state["previous"]
    return {"supported": True, "managed": (Path(ctx["root"]) / "state.json").is_file(),
            "root": ctx["root"], "original_prefix": ctx["original_prefix"],
            "updater_protocol": bootstrap.BOOTSTRAP_PROTOCOL,
            "current": dict(records[current], generation=current),
            "previous": dict(records[previous], generation=previous) if previous is not None else None,
            "retired": [dict(record, generation=key) for key, record in records.items() if key not in {current, previous}],
            "running_generation": os.environ.get(bootstrap.GENERATION_ENV),
            "restart_maps_required": True}


def status():
    """Inspect installation metadata; never create directories, lock files or DBs."""
    try:
        ctx = _context()
        _validate_layout(ctx)
        return _summary(ctx, _state(ctx))
    except (ConfigError, bootstrap.InstallationError, OSError, ValueError, SyntaxError) as exc:
        return {"supported": False, "managed": False, "reason": str(exc),
                "updater_protocol": bootstrap.BOOTSTRAP_PROTOCOL}


def _compatible(manifest):
    if not isinstance(manifest, dict) or manifest.get("format_version") != 1:
        raise ConfigError("Unsupported release manifest format")
    if manifest.get("updater_protocol") != bootstrap.BOOTSTRAP_PROTOCOL:
        raise ConfigError("Release needs a different updater protocol; reinstall the stable launcher")
    minimum = manifest.get("python_min")
    if (not isinstance(minimum, list) or len(minimum) != 2
            or any(type(part) is not int for part in minimum) or tuple(minimum) > sys.version_info[:2]):
        raise ConfigError("Release is incompatible with this Python interpreter")
    if manifest.get("database_schemas") != [1, 2] or manifest.get("map_schema") != 2:
        raise ConfigError("Release schema compatibility does not match updater protocol 1; no database was changed")


_CHECK = r'''
import json, pathlib, sysconfig
import richi, richi_bootstrap
from richi import memory, serve
from richi_launcher import cli
print(json.dumps({"version": richi.__version__, "updater_protocol": richi_bootstrap.BOOTSTRAP_PROTOCOL,
                  "map_schema": memory.VERSION, "site": str(pathlib.Path(sysconfig.get_paths()["purelib"]).resolve())}))
'''


def _run(command, label):
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                timeout=180, pass_fds=bootstrap.lease_fds())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError(label + " failed: " + str(exc)) from exc
    if result.returncode:
        raise ConfigError(label + " failed: " + (result.stderr or result.stdout)[-3000:].strip())
    return result.stdout


def _stage(ctx, generation, wheel, manifest):
    prefix = Path(ctx["root"]) / "versions" / generation
    prefix.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if prefix.parent.resolve() != prefix.parent:
        raise ConfigError("Managed versions directory cannot contain symlinks")
    prefix.mkdir(mode=0o700)
    _atomic_json(prefix / bootstrap.OWNER_FILE, _owner(ctx, generation))
    # Virtual environments embed absolute paths and must be built at their
    # final, unique location. Only the state pointer is renamed at activation.
    # Python 3.9 can report the current venv's bin/python as _base_executable.
    # EnvBuilder called in-process would link to that collectible generation.
    # Always create from the permanent seed interpreter's resolved base instead.
    stable_python = (Path(ctx["original_prefix"]) / "bin" / "python").resolve(strict=True)
    if stable_python.is_relative_to(Path(ctx["root"]) / "versions"):
        raise ConfigError("Original Python interpreter cannot belong to a collectible Richi release")
    _run([str(stable_python), "-I", "-B", "-m", "venv", "--symlinks", str(prefix)], "Release environment creation")
    python = str(prefix / "bin" / "python")
    _run([python, "-I", "-B", "-m", "pip", "--isolated", "--disable-pip-version-check", "--no-input",
          "install", "--no-index", "--no-deps", "--no-cache-dir", "--no-compile", str(wheel)], "Release installation")
    try:
        report = json.loads(_run([python, "-I", "-B", "-c", _CHECK], "Release self-check"))
    except ValueError as exc:
        raise ConfigError("Release self-check returned invalid metadata") from exc
    for key in ("version", "updater_protocol", "map_schema"):
        if report.get(key) != manifest.get(key):
            raise ConfigError("Release self-check disagrees with manifest: " + key)
    site = Path(report["site"])
    if not site.is_relative_to(prefix):
        raise ConfigError("Release self-check returned a package directory outside its environment")
    return {"kind": "release", "prefix": str(prefix), "site": str(site), "version": manifest["version"],
            "wheel_sha256": manifest["wheel"]["sha256"], "installed_at": int(time.time())}


def _remove_seed(ctx, state, record):
    prefix = Path(record["prefix"])
    _verify_owner(ctx, "seed", prefix)
    site = Path(record["site"])
    anchor = Path(state["seed_bootstrap"])
    if anchor.parent != site or site.resolve() != site or not site.is_relative_to(prefix):
        raise ConfigError("Refusing to prune seed code with inconsistent ownership")
    # The original Python, console entry point, bootstrap and distribution
    # metadata are permanent. A tiny namespace stub preserves `python -m richi`.
    for name in ("richi", "richi_launcher"):
        package = site / name
        if package.is_symlink():
            raise ConfigError("Refusing to prune linked seed code")
        if package.exists():
            shutil.rmtree(package)
    package = site / "richi"
    package.mkdir()
    (package / "__main__.py").write_text("from richi_bootstrap import main\nraise SystemExit(main())\n", encoding="utf-8")


def _collect(ctx, state):
    import fcntl
    root = Path(ctx["root"])
    removed, deferred, failures = [], [], []
    active = {state["current"], state["previous"]}
    records = state["generations"]
    # Interrupted staging can leave an owned candidate outside the state. Never
    # touch unknown directories, even if their names resemble release versions.
    versions = root / "versions"
    if versions.is_dir():
        for prefix in versions.iterdir():
            if prefix.name in records or not prefix.is_dir() or prefix.is_symlink():
                continue
            try:
                _verify_owner(ctx, prefix.name, prefix)
            except (ConfigError, bootstrap.InstallationError, OSError):
                continue
            records[prefix.name] = {"kind": "release", "prefix": str(prefix), "site": str(prefix), "version": "interrupted"}
    for generation, record in list(records.items()):
        if generation in active:
            continue
        lease = Path(state["seed_bootstrap"]) if generation == "seed" else root / "leases" / (generation + ".lock")
        descriptor = None
        try:
            if generation != "seed" and not Path(record["prefix"]).exists():
                # A prior collection may have removed the tree just before a
                # failed metadata save. Removing its record is safe to retry.
                del records[generation]
                removed.append(generation)
                continue
            _verify_owner(ctx, generation, Path(record["prefix"]))
            if generation != "seed":
                lease.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(str(lease), os.O_RDONLY if generation == "seed" else os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                deferred.append(generation)
                continue
            if generation == "seed":
                _remove_seed(ctx, state, record)
            else:
                shutil.rmtree(record["prefix"])
            del records[generation]
            removed.append(generation)
        except (ConfigError, bootstrap.InstallationError, OSError) as exc:
            failures.append({"generation": generation, "error": str(exc)})
        finally:
            if descriptor is not None:
                os.close(descriptor)
    return {"removed": removed, "deferred": deferred, "failures": failures}


def _activate(ctx, state, action):
    with _locked(ctx, state=True):
        _atomic_json(Path(ctx["root"]) / "state.json", state)
        collection = _collect(ctx, state)
        _atomic_json(Path(ctx["root"]) / "state.json", state)
        return dict(_summary(ctx, state), status=action, cleanup=collection)


def _candidate_referenced(ctx, generation):
    """A directory fsync can fail after replace has already activated a release."""
    path = Path(ctx["root"]) / "state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return generation in state["generations"]
    except FileNotFoundError:
        return False
    except (OSError, ValueError, TypeError, KeyError):
        # If metadata cannot be inspected, retaining an orphan is safer than
        # deleting a possibly live runtime. Explicit cleanup can retry later.
        return True


def apply_release(release, *, expected_generation=None, blocking=True,
                  before_apply=None, activation_guard=None, require_auto=False):
    """Stage a wheel; optional background guards also protect the pointer commit.

    Lock order is update.lock, caller's activation guard, then state.lock.
    The guard factory yields a boolean and remains entered through activation.
    """
    candidate = None
    committed = False
    try:
        from . import releases
        manifest = release["manifest"]
        _compatible(manifest)
        ctx = _context()
        _validate_layout(ctx)
        with _locked(ctx, blocking=blocking):
            state = _state(ctx)
            if expected_generation is not None and state["current"] != expected_generation:
                return dict(_summary(ctx, state), status="superseded")
            if before_apply is not None and not before_apply():
                return dict(_summary(ctx, state), status="cancelled")
            current = state["generations"][state["current"]]
            if current["version"] == manifest["version"] and current.get("wheel_sha256") == manifest["wheel"]["sha256"]:
                with _locked(ctx, state=True):
                    collection = _collect(ctx, state)
                    if (Path(ctx["root"]) / "state.json").exists():
                        _atomic_json(Path(ctx["root"]) / "state.json", state)
                    return dict(_summary(ctx, state), status="already_current", cleanup=collection)
            if state["generations"][state["current"]]["version"] != "interrupted":
                if releases.version_tuple(manifest["version"]) < releases.version_tuple(state["generations"][state["current"]]["version"]):
                    raise ConfigError("Updates cannot downgrade; use richi update rollback for the previous release")
            original_marker = Path(ctx["original_prefix"]) / bootstrap.OWNER_FILE
            if original_marker.exists():
                _verify_owner(ctx, "seed", Path(ctx["original_prefix"]))
            else:
                _atomic_json(original_marker, _owner(ctx, "seed"))
            generation = "v" + manifest["version"] + "-" + uuid.uuid4().hex[:12]
            if not bootstrap._generation_id(generation):
                raise ConfigError("Invalid release version")
            candidate = Path(ctx["root"]) / "versions" / generation
            with tempfile.TemporaryDirectory(prefix="download-", dir=ctx["root"]) as temporary:
                wheel = releases.download_wheel(release, Path(temporary))
                releases.verify_wheel(wheel, manifest)
                record = _stage(ctx, generation, wheel, manifest)
            if require_auto:
                _run([str(candidate / "bin" / "python"), "-I", "-B", "-c",
                      "from richi_launcher import auto_update, update; "
                      "assert all(callable(getattr(auto_update, name, None)) for name in "
                      "('run', 'status', 'enable', 'disable')); "
                      "update.command(['auto', 'run', '--help'])"], "Automatic release self-check")
            guard = activation_guard() if activation_guard is not None else nullcontext(True)
            with guard as allowed:
                if not allowed:
                    _verify_owner(ctx, generation, candidate)
                    shutil.rmtree(candidate)
                    return dict(_summary(ctx, state), status="cancelled")
                leases = Path(ctx["root"]) / "leases"
                leases.mkdir(parents=True, exist_ok=True, mode=0o700)
                if leases.resolve() != leases:
                    raise ConfigError("Managed leases directory cannot contain symlinks")
                fd = os.open(str(leases / (generation + ".lock")), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                os.close(fd)
                state["generations"][generation] = record
                state["previous"], state["current"] = state["current"], generation
                result = _activate(ctx, state, "updated")
                committed = True
                return result
    except _InstallationBusy:
        return {"status": "busy", "message": "Another installation update is in progress"}
    except (ConfigError, bootstrap.InstallationError, OSError, ValueError, TypeError, KeyError, SyntaxError,
            RuntimeError, subprocess.SubprocessError) as exc:
        if candidate is not None and not committed and candidate.exists() and not _candidate_referenced(ctx, generation):
            try:
                _verify_owner(ctx, generation, candidate)
                shutil.rmtree(candidate)
            except (ConfigError, bootstrap.InstallationError, OSError):
                pass
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError("Cannot update Richi: " + str(exc)) from exc


def rollback():
    """Swap current and previous; retain running generations until unleased."""
    try:
        ctx = _context()
        _validate_layout(ctx)
        with _locked(ctx):
            state = _state(ctx)
            if state["previous"] is None:
                raise ConfigError("No previous Richi release is available for rollback")
            previous = state["generations"][state["previous"]]
            _verify_owner(ctx, state["previous"], Path(previous["prefix"]))
            if not (Path(previous["site"]) / "richi" / "memory.py").is_file():
                raise ConfigError("Previous Richi release is missing; rollback was not applied")
            from . import auto_update
            auto_update.pause_for_rollback(ctx, state["previous"])
            state["current"], state["previous"] = state["previous"], state["current"]
            return _activate(ctx, state, "rolled_back")
    except (bootstrap.InstallationError, OSError, ValueError, SyntaxError) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError("Cannot roll back Richi: " + str(exc)) from exc


def cleanup():
    """Retry removal of retired releases, without selecting or migrating a DB."""
    try:
        ctx = _context()
        _validate_layout(ctx)
        with _locked(ctx):
            state = _state(ctx)
            with _locked(ctx, state=True):
                collection = _collect(ctx, state)
                if (Path(ctx["root"]) / "state.json").exists():
                    _atomic_json(Path(ctx["root"]) / "state.json", state)
                return dict(_summary(ctx, state), status="cleaned", cleanup=collection)
    except (bootstrap.InstallationError, OSError, ValueError, SyntaxError) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError("Cannot clean up Richi releases: " + str(exc)) from exc
