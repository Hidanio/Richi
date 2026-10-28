"""First installation from an official wheel, usable from a stdlib-only zipapp."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

from .config import ConfigError
from . import releases, setup_files

MARKER = ".richi-first-install.json"


def _run(command):
    try:
        result = subprocess.run([str(part) for part in command], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError("Installation command failed: " + str(exc)) from exc
    if result.returncode:
        raise ConfigError("Installation command failed: " + (result.stderr or result.stdout)[-3000:].strip())
    return result.stdout


def _write(path, value):
    # Reuse the same fsync/replace implementation as the release manager.
    from .installation import _atomic_json
    _atomic_json(path, value)


def _owner(prefix):
    path = prefix / MARKER
    try:
        raw = setup_files.contents(setup_files.snapshot(path))
        value = json.loads(raw)
        if (value.get("format_version") != 1 or value.get("prefix") != str(prefix)
                or value.get("phase") not in {"creating", "installed", "configured"}):
            raise ValueError("invalid owner")
        return value
    except (ValueError, TypeError, OSError) as exc:
        raise ConfigError("Existing environment was not created by this installer; preserved it: " + str(prefix)
                          + ". Use its richi setup command or choose an empty --prefix") from exc


def _confirm(plan, yes):
    print(json.dumps(plan, indent=2), file=sys.stderr)
    if yes:
        return True
    try:
        with open("/dev/tty", "r+") as terminal:
            terminal.write("Install Richi and connect it to all local Codex chats? [y/N] ")
            terminal.flush()
            return terminal.readline().strip().lower() in {"y", "yes"}
    except OSError as exc:
        raise ConfigError("Noninteractive installation requires --yes. Use --dry-run to inspect paths") from exc


def install(args):
    if bool(args.wheel) != bool(args.manifest) or (args.wheel and args.version):
        raise ConfigError("Use --wheel with --manifest, or --version for an official release")
    if sys.platform not in {"darwin", "linux"} or sys.version_info < (3, 9):
        raise ConfigError("Richi requires macOS/Linux and Python 3.9 or later")
    try:
        import sqlite3
        import venv
        import ensurepip
    except ImportError as exc:
        raise ConfigError("Install Python with sqlite3, venv and ensurepip first (on Debian/Ubuntu: python3-venv)") from exc
    prefix = setup_files.safe_path(args.prefix or Path.home().resolve() / ".local/share/richi/venv")
    if prefix.is_symlink():
        raise ConfigError("Installation prefix must not be a symlink")
    if prefix.exists() and not prefix.is_dir():
        raise ConfigError("Installation prefix is not a directory")
    owner = _owner(prefix) if prefix.exists() else None
    profile = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    setup_plan = {"prefix": str(prefix), "agent": "codex", "scope": "user",
                  "runtime": "Fixed release; existing development settings are preserved",
                  "skill": str(Path.home() / ".agents/skills/project-memory"),
                  "instructions": str(profile / "AGENTS.md"),
                  "instruction_override": "A nonempty AGENTS.override.md is used instead when present",
                  "shell_path": not args.no_path, "replace_existing_skill": args.replace_skill,
                  "database": "Existing stores and chat bindings are preserved; choose a workspace in your first chat"}
    if args.dry_run:
        return dict(setup_plan, status="planned")
    if not _confirm(setup_plan, args.yes):
        return {"status": "cancelled"}
    prefix.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = setup_files.safe_path(prefix.parent / ("." + prefix.name + ".install.lock"))
    if lock.is_symlink():
        raise ConfigError("Installer lock cannot be a symlink")
    descriptor = os.open(str(lock), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ConfigError("Another installer is running for this prefix; retry after it finishes") from exc
        owner = _owner(prefix) if prefix.exists() else None
        setup_args = ["setup", "--yes"]
        if args.replace_skill:
            setup_args.append("--replace-skill")
        if args.no_path:
            setup_args.append("--no-path")
        if owner and owner["phase"] in {"installed", "configured"}:
            console = prefix / "bin/richi"
            if not args.wheel:
                update_args = [console, "update", "apply"]
                if args.version:
                    update_args += ["--version", args.version]
                _run(update_args)
            else:
                manifest = releases.validate_manifest(releases._json(Path(args.manifest).read_bytes()))
                releases.verify_wheel(Path(args.wheel), manifest)
                if owner.get("wheel_sha256") != manifest["wheel"]["sha256"]:
                    raise ConfigError("This prefix already holds a different wheel; use richi update apply or a new prefix")
            result = json.loads(_run([console, *setup_args]))
            _write(prefix / MARKER, dict(owner, phase="configured"))
            return dict(result, prefix=str(prefix), status="configured")
        with tempfile.TemporaryDirectory(prefix="richi-install-") as temporary:
            if args.wheel:
                manifest = releases.validate_manifest(releases._json(Path(args.manifest).read_bytes()))
                wheel = releases.verify_wheel(Path(args.wheel).resolve(), manifest)
            else:
                release = releases.fetch_release(args.version)
                if release is None:
                    raise ConfigError("No stable release is published yet; no environment was installed")
                manifest = release["manifest"]
                wheel = releases.download_wheel(release, Path(temporary))
            from .installation import _compatible
            _compatible(manifest)
            # Venv paths are embedded in shebangs, so never rename a completed
            # venv into place. Interrupted owned attempts are retained as backups.
            if owner:
                backup = prefix.with_name(prefix.name + ".interrupted-" + uuid.uuid4().hex[:12])
                os.rename(prefix, backup)
            prefix.mkdir(mode=0o700)
            owner = {"format_version": 1, "prefix": str(prefix), "phase": "creating",
                     "version": manifest["version"], "wheel_sha256": manifest["wheel"]["sha256"]}
            _write(prefix / MARKER, owner)
            base_python = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
            _run([base_python, "-I", "-m", "venv", str(prefix)])
            python = prefix / "bin/python"
            if not args.wheel:
                _run([python, "-I", "-m", "pip", "--isolated", "install", "--upgrade", "pip"])
            _run([python, "-I", "-m", "pip", "--isolated", "--disable-pip-version-check",
                  "--no-input", "install", "--no-index", "--no-deps", "--no-cache-dir", str(wheel)])
            _run([python, "-I", "-c", "from richi_launcher import agent_setup; "
                  "assert agent_setup.PROTOCOL == 1; "
                  "import richi_bootstrap; assert richi_bootstrap.AGENT_SETUP_PROTOCOL == 1"])
            owner = dict(owner, phase="installed")
            _write(prefix / MARKER, owner)
            result = json.loads(_run([prefix / "bin/richi", *setup_args]))
            _write(prefix / MARKER, dict(owner, phase="configured"))
            return dict(result, prefix=str(prefix), version=manifest["version"], status="configured")
    finally:
        os.close(descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install Richi and connect all local Codex chats (macOS/Linux, Python 3.9+)", allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--yes", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    parser.add_argument("--prefix", type=Path, help="Empty dedicated environment; default ~/.local/share/richi/venv")
    parser.add_argument("--version", help="Official stable X.Y.Z release; default latest")
    parser.add_argument("--replace-skill", action="store_true")
    parser.add_argument("--no-path", action="store_true")
    parser.add_argument("--wheel", type=Path, help="Offline wheel; requires its release manifest")
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args(argv)
    try:
        result = install(args)
        print(json.dumps(result, indent=2))
        return 0
    except (ConfigError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc), "recovery": "Existing instructions and stores are preserved. Resolve the reported conflict and rerun the same command."}), file=sys.stderr)
        return 1
