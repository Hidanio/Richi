"""Choose installed code or an explicit checkout before importing Richi."""
from dataclasses import dataclass
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from .config import ConfigError

LAUNCHER_ROOT = Path(__file__).resolve().parent.parent
ACTIVE_RUNTIME = "RICHI_ACTIVE_RUNTIME"


@dataclass(frozen=True)
class Runtime:
    mode: str
    package: Path
    version: str
    identity: str
    source: object = None

    def as_dict(self):
        return {"mode": self.mode, "package": str(self.package), "version": self.version,
                "identity": self.identity, "source": str(self.source) if self.source else None}


def _version(package):
    try:
        tree = ast.parse((package / "__init__.py").read_bytes())
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets):
                version = ast.literal_eval(node.value)
                if isinstance(version, str) and version:
                    return version
    except (OSError, RuntimeError, SyntaxError, ValueError, UnicodeError) as exc:
        raise ConfigError("Cannot read Richi version at " + str(package) + ": " + str(exc)) from exc
    raise ConfigError("Richi source must declare a literal __version__: " + str(package))


def describe_runtime(package, mode="release", source=None):
    try:
        package = Path(package).resolve()
        source = Path(source).resolve() if source is not None else None
    except (OSError, RuntimeError) as exc:
        raise ConfigError("Cannot inspect Richi runtime path: " + str(exc)) from exc
    version = _version(package)
    digest = hashlib.sha256()
    digest.update(json.dumps([mode, str(package), version], ensure_ascii=True).encode())
    if mode == "release":
        # Distinguish builds even if a local wheel reuses a version number.
        try:
            for path in sorted(package.rglob("*")):
                if path.is_file() and path.suffix in {".py", ".sql", ".html"}:
                    digest.update(str(path.relative_to(package)).encode())
                    digest.update(b"\0")
                    digest.update(path.read_bytes())
        except (OSError, RuntimeError) as exc:
            raise ConfigError("Cannot inspect installed Richi: " + str(exc)) from exc
    return Runtime(mode, package, version, digest.hexdigest(), source)


def installed_runtime():
    package = LAUNCHER_ROOT / "richi"
    try:
        available = (package / "memory.py").is_file()
    except (OSError, RuntimeError) as exc:
        raise ConfigError("Cannot inspect installed Richi: " + str(exc)) from exc
    if not available:
        raise ConfigError("A fixed Richi installation is required. Install the package without pip -e, then configure development.source.")
    return describe_runtime(package)


def resolve_runtime(settings):
    if not settings.dev:
        return installed_runtime()
    source = settings.development_source
    if source is None:
        raise ConfigError("dev is true but development.source is not configured; use richi config set development.source PATH or richi config set dev false")
    package = source / "src" / "richi"
    required = (source / "pyproject.toml", package / "__init__.py", package / "memory.py", package / "serve.py")
    try:
        available = all(path.is_file() for path in required)
    except (OSError, RuntimeError) as exc:
        raise ConfigError("Cannot inspect development.source " + str(source) + ": " + str(exc)) from exc
    if not available:
        raise ConfigError("development.source must be a Richi checkout containing pyproject.toml and src/richi: " + str(source))
    return describe_runtime(package, "dev", source)


def current_runtime():
    raw = os.environ.get(ACTIVE_RUNTIME)
    if raw:
        try:
            data = json.loads(raw)
            if data["mode"] not in {"dev", "release"}:
                raise ValueError("unknown mode")
            return Runtime(data["mode"], Path(data["package"]), data["version"], data["identity"],
                           Path(data["source"]) if data.get("source") else None)
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError("Invalid active Richi runtime") from exc
    # Direct library use (including tests) describes the package actually loaded.
    loaded = sys.modules.get("richi")
    if loaded is not None and getattr(loaded, "__file__", None):
        try:
            package = Path(loaded.__file__).resolve().parent
        except (OSError, RuntimeError) as exc:
            raise ConfigError("Cannot inspect loaded Richi: " + str(exc)) from exc
        return describe_runtime(package)
    return installed_runtime()


_RUNNER = r'''
import json, os, pathlib, sys
launcher, payload, action = sys.argv[1:4]
arguments = sys.argv[4:]
runtime = json.loads(payload)
sys.path[:0] = [str(pathlib.Path(runtime["package"]).parent), launcher]
os.environ["RICHI_ACTIVE_RUNTIME"] = payload
try:
    if action == "git_worker":
        from richi.serve import _git_worker
        _git_worker()
        result = 0
    elif action == "status_worker":
        from richi.status import inspect
        print(json.dumps(inspect(**json.loads(arguments[0])), ensure_ascii=False))
        result = 0
    elif action == "preflight":
        from richi import memory, serve
        with memory.connect(pathlib.Path(arguments[0]), readonly=True) as connection:
            memory.backend(connection)
            if connection.execute("PRAGMA user_version").fetchone()[0] != 2:
                raise ValueError("The map requires schema 2; no migration was attempted")
        result = 0
    elif action == "serve":
        from richi.serve import main
        result = main(arguments)
    else:
        from richi.memory import main
        result = main(arguments)
except Exception as exc:
    print(json.dumps({"error": "Cannot run selected Richi runtime: " + str(exc), "mode": runtime["mode"],
                      "source": runtime["source"], "recovery": "richi config set dev false"}), file=sys.stderr)
    sys.exit(1)
sys.exit(result or 0)
'''


def runtime_command(runtime, argv, action="cli"):
    if action not in {"cli", "serve", "git_worker", "preflight", "status_worker"}:
        raise ValueError("Unknown runtime action")
    command = [sys.executable, "-I", "-B"]
    if runtime.mode == "dev":
        # -B alone still reads .pyc. A unique, unwritten prefix avoids stale
        # imports for same-size edits inside a filesystem timestamp tick.
        prefix = str(Path(tempfile.gettempdir()) / ("richi-bytecode-%d-%d" % (os.getpid(), time.monotonic_ns())))
        command += ["-X", "pycache_prefix=" + prefix]
    return command + ["-c", _RUNNER, str(LAUNCHER_ROOT), json.dumps(runtime.as_dict()), action, *map(str, argv)]


def bootstrap_command(argv):
    code = "import sys; sys.path.insert(0, sys.argv[1]); from richi_launcher.cli import main; sys.exit(main(sys.argv[2:]))"
    return [sys.executable, "-I", "-B", "-c", code, str(LAUNCHER_ROOT), *map(str, argv)]


def lease_fds():
    # Legacy in-process library use and isolated fixtures have no lease. Public
    # entry points acquire it in the bootstrap before importing any runtime.
    if "RICHI_INSTALLATION_LEASE_FD" not in os.environ:
        return ()
    from richi_bootstrap import lease_fds as inherited_lease_fds
    return inherited_lease_fds()


def preflight(runtime, database, config_file=None):
    environment = dict(os.environ)
    if config_file is not None:
        environment["RICHI_CONFIG"] = str(config_file)
    try:
        result = subprocess.run(runtime_command(runtime, [database], action="preflight"),
                                capture_output=True, text=True, timeout=15, env=environment,
                                pass_fds=lease_fds())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError("Runtime preflight failed: " + str(exc)) from exc
    if result.returncode:
        raise ConfigError("Runtime preflight failed: " + (result.stderr or result.stdout)[-2000:].strip())
    return True
