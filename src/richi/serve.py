#!/usr/bin/env python3
"""Read-only loopback viewer for project memory. Python standard library only."""

import argparse
from contextlib import closing
import hashlib
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import selectors
import signal
from socketserver import TCPServer
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import webbrowser

from richi_launcher.config import resolve_settings, ConfigError
from richi_launcher.runtime import (LAUNCHER_ROOT, bootstrap_command, current_runtime, preflight,
                                    resolve_runtime, runtime_command, lease_fds)
from .memory import Parser, MemoryError


BASE = Path(__file__).resolve().parent
API_VERSION = 4
# The richi_launcher.maps protocol this runtime speaks (maps.API_VERSION).
MAPS_API = 1
# Set by the installation's bootstrap for every process it starts.
LEASE_ENV = "RICHI_INSTALLATION_LEASE_FD"
ROOT_ENV = "RICHI_INSTALLATION_ROOT"
GENERATION_ENV = "RICHI_INSTALLATION_GENERATION"


def _launcher_maps():
    """The installed launcher's map module if it speaks this runtime's protocol, else None.

    A development runtime may be paired with an older or a newer installed
    launcher. Without the same protocol a map neither follows the installation
    nor stops or replaces other maps; it is only started and reused, as before.
    """
    try:
        from richi_launcher import maps as module
    except ImportError:
        return None
    return module if getattr(module, "API_VERSION", None) == MAPS_API else None


maps = _launcher_maps()


def handoff_supported():
    """This map can follow its installation: the launcher speaks the protocol and the signal exists."""
    return maps is not None and hasattr(signal, maps.HANDOFF_SIGNAL)


# A map advertises the handoff only when it installs the signal handler: the
# default action of that signal would terminate it.
CAPABILITIES = (["git_source_viewer", "legacy_git_source_viewer", "standalone_runtime",
                 "runtime_selection", "runtime_reload", "workspace_selection", "tool_catalog"]
                + ([maps.HANDOFF_CAPABILITY] if handoff_supported() else []))


def url(port):
    return "http://127.0.0.1:%d/" % port


def read_health(port, timeout=0.5):
    """A loopback map's health answer, or None. Never goes through a proxy.

    richi_launcher.maps has its own reader: this one also works when the
    installed launcher has no map protocol or a different one.
    """
    from http.client import HTTPException
    from urllib.request import ProxyHandler, build_opener
    try:
        with build_opener(ProxyHandler({})).open(url(port) + "api/health", timeout=timeout) as response:
            return json.loads(response.read(65537))
    except (OSError, HTTPException, ValueError, AttributeError, TypeError):
        return None
GIT_QUERY_LIMIT = 8192
GIT_OPERATION_TIMEOUT = 25
GIT_WORKER_TIMEOUT = 30
GIT_WORKER_OUTPUT_LIMIT = 450000
GRAPH_WORKER_TIMEOUT = 20
GRAPH_WORKER_OUTPUT_LIMIT = 32 * 1024 * 1024


class LoopbackHTTPServer(ThreadingHTTPServer):
    """Bind the local viewer without a reverse DNS lookup during startup."""

    def server_bind(self):
        # HTTPServer.server_bind calls getfqdn(), which can block for tens of
        # seconds on macOS. The viewer uses a numeric loopback address only.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class GitRequestError(ValueError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


def _git_options(query):
    """Accept only attached source navigation, never caller-supplied filesystem paths."""
    if len(query) > GIT_QUERY_LIMIT:
        raise GitRequestError(414, "query_too_long", "Git query exceeds the size limit")
    try:
        params = parse_qs(query, keep_blank_values=True, strict_parsing=True,
                          encoding="utf-8", errors="strict", max_num_fields=12)
    except (ValueError, UnicodeError):
        raise GitRequestError(400, "invalid_query", "Invalid Git query")
    required = {"action", "ref", "source", "expected_updated_at"}
    supported = required | {"target", "worktree", "limit", "max_chars", "path"}
    if (not required <= set(params) or set(params) - supported
            or any(len(values) != 1 or not values[0] for values in params.values())):
        raise GitRequestError(400, "invalid_query", "Use one value per supported Git navigation parameter")
    options = {key: values[0] for key, values in params.items()}
    action = options["action"]
    if action not in {"show", "history", "diff", "check", "commit"}:
        raise GitRequestError(400, "invalid_action", "Unsupported Git navigation action")
    if (not re.fullmatch(r"(?:entry|entity|edge):[^\x00-\x1f\x7f]{1,200}", options["ref"])
            or len(options["expected_updated_at"]) > 128):
        raise GitRequestError(400, "invalid_query", "Invalid record reference or version")
    if "target" in options and (len(options["target"]) > 1024
            or options["target"].startswith("-")
            or any(ord(char) < 32 or ord(char) == 127 for char in options["target"])):
        raise GitRequestError(400, "invalid_target", "Invalid local Git revision")
    if "path" in options:
        from . import git_evidence
        try:
            git_evidence._path(options["path"])
        except ValueError as exc:
            raise GitRequestError(400, "invalid_path", str(exc)) from exc
    if ((action in {"show", "commit"} and set(options) & {"target", "worktree", "limit"})
            or (action == "commit" and "path" in options)
            or (action != "history" and "limit" in options)
            or (action == "history" and "worktree" in options)
            or ("worktree" in options and options["worktree"] != "1")
            or {"target", "worktree"} <= set(options)):
        raise GitRequestError(400, "invalid_query", "Parameters do not match the selected Git action")
    for key, default, minimum, maximum in (("source", None, 1, 100000),
            ("limit", "20", 1, 100), ("max_chars", "32000", 2000, 100000)):
        value = options.get(key, default)
        if not re.fullmatch(r"[0-9]{1,6}", value) or not minimum <= int(value) <= maximum:
            raise GitRequestError(400, "invalid_query", "Invalid " + key)
        options[key] = int(value)
    options["worktree"] = options.get("worktree") == "1"
    return options


def _git_response(database, options):
    """Hold one read snapshot so source indices cannot shift during a request."""
    from . import git_evidence
    from . import git_legacy
    from . import git_sources
    from . import memory

    conn = memory.connect(Path(database).resolve(), readonly=True)
    try:
        with memory.transaction(conn, write=False):
            try:
                _, record = git_sources._record(conn, options["ref"], memory)
            except memory.MemoryError as exc:
                raise GitRequestError(404, "record_unavailable", str(exc)) from exc
            try:
                memory.timestamp(options["expected_updated_at"], "expected_updated_at")
            except memory.MemoryError as exc:
                raise GitRequestError(400, "invalid_query", str(exc)) from exc
            if record["updated_at"] != options["expected_updated_at"]:
                raise GitRequestError(409, "stale_record", "The record changed; refresh the map and select the source again")
            index = options["source"]
            if index > len(record["sources"]):
                raise GitRequestError(400, "invalid_source", "Select a Git source by its 1-based index in the full sources array")
            try:
                return git_sources.navigate(conn, SimpleNamespace(**options), memory, database)
            except git_legacy.LegacyGitError as exc:
                raise GitRequestError(exc.status, exc.code, str(exc)) from exc
            except memory.MemoryError as exc:
                raise GitRequestError(400, "invalid_source", str(exc)) from exc
    finally:
        conn.close()


def _git_worker():
    """Private subprocess entry point, with a deadline covering every Git read and diff."""
    def expired(_signum, _frame):
        raise TimeoutError("Git navigation exceeded the time limit")

    try:
        if hasattr(signal, "SIGALRM"):
            signal.signal(signal.SIGALRM, expired)
            signal.setitimer(signal.ITIMER_REAL, GIT_OPERATION_TIMEOUT)
        request = json.loads(sys.stdin.buffer.read(GIT_QUERY_LIMIT * 2 + 1))
        response = _git_response(request["database"], request["options"])
        status, payload = 200, response
    except GitRequestError as exc:
        status, payload = exc.status, {"error": str(exc), "code": exc.code}
    except TimeoutError:
        status, payload = 503, {"error": "Git navigation timed out", "code": "timeout"}
    except (ValueError, OSError, sqlite3.Error) as exc:
        status, payload = 503, {"error": str(exc)[:1000], "code": "git_unavailable"}
    except Exception:
        status, payload = 503, {"error": "Git navigation is unavailable", "code": "git_unavailable"}
    finally:
        if hasattr(signal, "SIGALRM"):
            signal.setitimer(signal.ITIMER_REAL, 0)
    sys.stdout.write(json.dumps({"status": status, "payload": payload}, ensure_ascii=False))


def _read_worker_output(process, *, label, timeout, maximum, request=None):
    """Drain both pipes within one budget and always reap the owned worker."""
    selector = selectors.DefaultSelector()
    output, size = {"stdout": [], "stderr": []}, 0
    deadline = time.monotonic() + timeout
    try:
        if request is not None:
            process.stdin.write(request)
            process.stdin.close()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(label + " timed out")
            for key, _ in selector.select(min(remaining, 0.2)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                size += len(chunk)
                if size > maximum:
                    raise ValueError(label + " output exceeded the size limit")
                output[key.data].append(chunk)
        process.wait(timeout=max(0.01, deadline - time.monotonic()))
        return process.returncode, b"".join(output["stdout"]), b"".join(output["stderr"])
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        selector.close()
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        process.stdout.close()
        process.stderr.close()


def _run_git_request(database, options, runtime=None, config_file=None, workspace=None, config_required=False):
    """Bound worker time and pipes; HTTP threads never retain unbounded Git output."""
    request = json.dumps({"database": str(database), "options": options}).encode("utf-8")
    process = subprocess.Popen(runtime_command(runtime or current_runtime(), [], action="git_worker"),
        cwd=str(BASE), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True, pass_fds=lease_fds(), env=worker_environment(config_file, workspace, config_required))
    try:
        status, output, _error = _read_worker_output(process, label="Git navigation",
            timeout=GIT_WORKER_TIMEOUT, maximum=GIT_WORKER_OUTPUT_LIMIT, request=request)
        if status:
            raise ValueError("Git navigation worker failed")
        result = json.loads(output)
        return result["status"], result["payload"]
    except (OSError, ValueError, KeyError, TimeoutError, subprocess.TimeoutExpired) as exc:
        return 503, {"error": str(exc)[:1000], "code": "timeout" if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)) else "git_unavailable"}


def _run_graph_request(database, flags, runtime, config_file=None, workspace=None, config_required=False,
                       catalog=False, tool_id=None):
    """Node limits do not bound full card payloads; cap bytes before buffering them."""
    try:
        operation = (["tools", "get", *flags, "--", tool_id] if tool_id is not None else
                     ["tools", "list", "--limit", "200", *flags] if catalog else
                     ["graph", "export", *flags])
        process = subprocess.Popen(
            runtime_command(runtime, [*config_arguments(config_file, config_required),
                                      "--db", str(database), *operation]),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            pass_fds=lease_fds(), env=worker_environment(config_file, workspace, config_required))
        status, output, error = _read_worker_output(process, label="Graph export",
            timeout=GRAPH_WORKER_TIMEOUT, maximum=GRAPH_WORKER_OUTPUT_LIMIT)
        if status:
            try:
                payload = json.loads(error.decode("utf-8"))
            except (ValueError, UnicodeError):
                return 503, {"error": "Graph export failed; run richi check"}
            # A missing tool is a stable answer, not a worker failure to retry.
            if tool_id is not None and isinstance(payload, dict) and payload.get("code") == "not_found":
                return 404, payload
            return 503, payload
        return 200, output
    except (TimeoutError, subprocess.TimeoutExpired):
        return 503, {"error": "Graph export timed out"}
    except (OSError, ValueError) as exc:
        return 503, {"error": str(exc)[:1000]}


def compatible_health(health, database, runtime=None, config_file=None):
    return (isinstance(health, dict) and health.get("application") == "project-memory-map"
            and health.get("database") == str(database) and health.get("api_version") == API_VERSION
            and isinstance(health.get("capabilities"), list)
            and all(capability in health["capabilities"]
                    for capability in CAPABILITIES)
            and health.get("runtime") == (runtime or current_runtime()).as_dict()
            # A map on another installation generation or environment holds that
            # lease and launcher even when a development runtime is identical.
            and health.get("installation_generation") == os.environ.get(GENERATION_ENV)
            and health.get("launcher") == str(LAUNCHER_ROOT)
            and health.get("config_file") == (str(config_file) if config_file is not None else None))


def handler_for(database, config_file=None, runtime=None, config_required=False, workspace=None,
                capabilities=None):
    database = Path(database).expanduser().resolve()
    runtime = runtime or current_runtime()
    # A running map may withdraw a capability it can no longer provide.
    capabilities = list(CAPABILITIES) if capabilities is None else capabilities
    export_lock = threading.Lock()
    tools_lock = threading.Lock()
    git_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "ProjectMemoryMap/4"

        def setup(self):
            # Bound draining of idle requests before a runtime transition.
            self.request.settimeout(35)
            super().setup()

        def log_message(self, *_args):
            # Do not log knowledge content or search terms.
            pass

        def send_data(self, status, data, content_type="application/json; charset=utf-8"):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def allowed_origin(self):
            port = self.server.server_address[1]
            hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port}
            if self.headers.get("Host") not in hosts:
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in {"http://" + host for host in hosts}:
                return False
            return self.headers.get("Sec-Fetch-Site") != "cross-site"

        def do_GET(self):
            if not self.allowed_origin():
                return self.send_data(403, {"error": "Only same-origin loopback access is allowed"})
            url = urlsplit(self.path)
            if url.path in {"/", "/map.html"} and not url.query:
                try:
                    return self.send_data(200, (BASE / "map.html").read_bytes(), "text/html; charset=utf-8")
                except OSError:
                    return self.send_data(503, {"error": "map.html is unavailable"})
            if url.path == "/favicon.ico":
                return self.send_data(204, b"", "image/x-icon")
            if url.path == "/api/health" and not url.query:
                return self.send_data(200, {"application": "project-memory-map", "database": str(database),
                                            "api_version": API_VERSION, "capabilities": list(capabilities),
                                            "runtime": runtime.as_dict(), "workspace": workspace, "pid": os.getpid(),
                                            "launcher": str(LAUNCHER_ROOT),
                                            "installation_generation": os.environ.get(GENERATION_ENV),
                                            "config_file": str(config_file) if config_file is not None else None})
            if url.path == "/api/git":
                try:
                    options = _git_options(url.query)
                except GitRequestError as exc:
                    return self.send_data(exc.status, {"error": str(exc), "code": exc.code})
                if not git_lock.acquire(blocking=False):
                    return self.send_data(503, {"error": "Git navigation in progress; try again", "code": "busy"})
                try:
                    status, payload = _run_git_request(database, options, runtime, config_file, workspace, config_required)
                    return self.send_data(status, payload)
                except OSError:
                    return self.send_data(503, {"error": "Git navigation is unavailable", "code": "git_unavailable"})
                finally:
                    git_lock.release()
            if url.path not in {"/api/graph", "/api/tools"}:
                return self.send_data(404, {"error": "Not found"})
            try:
                if len(url.query) > GIT_QUERY_LIMIT:
                    return self.send_data(414, {"error": "Query exceeds the size limit"})
                params = parse_qs(url.query, keep_blank_values=True, strict_parsing=True, max_num_fields=3) if url.query else {}
            except ValueError:
                return self.send_data(400, {"error": "Invalid query"})
            supported = {"include_hypotheses", "include_superseded"}
            if url.path == "/api/tools":
                supported.add("tool")
            tool_id = params.get("tool", [None])[0]
            if (set(params) - supported
                    or any(v not in (["0"], ["1"]) for key, v in params.items() if key != "tool")
                    or ("tool" in params and (len(params["tool"]) != 1 or not tool_id
                        or len(tool_id) > 200 or any(ord(char) < 32 or ord(char) == 127 for char in tool_id)))):
                return self.send_data(400, {"error": "Flags must be 0 or 1"})
            flags = ["--" + key.replace("_", "-") for key, value in params.items() if key != "tool" and value == ["1"]]
            request_lock = tools_lock if url.path == "/api/tools" else export_lock
            if not request_lock.acquire(blocking=False):
                return self.send_data(503, {"error": "Export in progress; try Refresh again", "code": "busy"})
            try:
                if url.path == "/api/tools":
                    status, payload = _run_graph_request(database, flags, runtime, config_file, workspace,
                                                        config_required, catalog=True, tool_id=tool_id)
                else:
                    status, payload = _run_graph_request(database, flags, runtime, config_file, workspace, config_required)
                return self.send_data(status, payload)
            finally:
                request_lock.release()

        def do_HEAD(self):
            self.do_GET()

        def do_POST(self):
            self.send_data(405, {"error": "The map is read-only"})

        do_PUT = do_POST
        do_PATCH = do_POST
        do_DELETE = do_POST
        do_OPTIONS = do_POST

    return Handler


def worker_environment(config_file=None, workspace=None, config_required=False):
    """Keep child storage/labels tied to this map, independently of the registry default."""
    environment = dict(os.environ)
    # These are private descendants of an already selected map invocation.
    # Reload re-enters the stable launcher with the map's explicit config/db;
    # it must not ask for a new choice or follow a later chat rebind.
    environment.pop("RICHI_CHAT_ID", None)
    environment.pop("CODEX_THREAD_ID", None)
    if config_file is not None:
        environment["RICHI_ACTIVE_CONFIG"] = str(config_file)
        if workspace is not None:
            environment["RICHI_ACTIVE_WORKSPACE"] = workspace
        else:
            environment.pop("RICHI_ACTIVE_WORKSPACE", None)
        if config_required or Path(config_file).is_file():
            environment["RICHI_CONFIG"] = str(config_file)
            environment.pop("RICHI_WORKSPACE", None)
        else:
            # Only the implicit default may have no configuration file yet.
            # Pin its name so a later `workspace use` cannot redirect this child.
            environment.pop("RICHI_CONFIG", None)
            environment["RICHI_WORKSPACE"] = "default"
    return environment


def config_arguments(config_file, required=False):
    """Keep a missing default optional, but preserve an explicit configuration."""
    if config_file is not None and (required or Path(config_file).is_file()):
        return ["--config", str(config_file)]
    return []


def source_fingerprint(package=None):
    package = Path(package) if package is not None else BASE
    digest = hashlib.sha256()
    for path in sorted(package.rglob("*")):
        if path.suffix in {".py", ".sql"} and path.is_file():
            digest.update(str(path.relative_to(package)).encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
    return digest.digest()


def database_schema(database):
    """The store's schema, read the same read-only way as at startup; None if unknown."""
    try:
        with closing(sqlite3.connect(Path(database).as_uri() + "?mode=ro", uri=True)) as conn:
            return conn.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.Error:
        return None


HANDOFF_RECHECK = 30


class InstallationFollower:
    """Decide when a map re-executes on its installation's current generation.

    A map follows when another generation becomes current: at once when asked
    (the handoff signal), otherwise after a short grace so that an updater
    asking directly goes first. A request while the map already runs the
    current generation is ignored, so the signal alone cannot restart a map.
    It follows only after the stable entry point started that release with
    this storage selection, and only if the release is complete and can read
    the store; otherwise it keeps serving and logs why.
    """

    def __init__(self, database, selection, arguments, environment, capabilities):
        self.root, self.generation = os.environ.get(ROOT_ENV), os.environ.get(GENERATION_ENV)
        self.database, self.selection, self.arguments = database, selection, arguments
        self.environment, self.capabilities = environment, capabilities
        self.requested = threading.Event()
        self.grace = maps.handoff_grace()
        self.enabled = True
        self.changed_since = self.blocked = self.command = self.reason = None

    def due(self):
        """True once the entry point command for the current generation is prepared."""
        if not self.enabled:
            self.requested.clear()
            return False
        try:
            return self._due()
        except Exception as exc:  # An installed launcher this map does not understand.
            self.enabled = False
            if maps.HANDOFF_CAPABILITY in self.capabilities:
                self.capabilities.remove(maps.HANDOFF_CAPABILITY)
            print("Map no longer follows installation updates (" + str(exc)[:200]
                  + "); run richi map restart after an update.", flush=True)
            return False

    def _due(self):
        current = maps.current_generation(self.root) if self.root and self.generation else None
        requested = self.requested.is_set()
        self.requested.clear()
        if current is None or current == self.generation:
            self.changed_since = self.blocked = None
            if requested:
                print("Restart request ignored: the map already runs the installation's current version.",
                      flush=True)
            return False
        now = time.monotonic()
        if self.changed_since is None:
            self.changed_since = now
        if not requested and now - self.changed_since < self.grace:
            return False
        if self.blocked is not None and self.blocked[0] == current and now - self.blocked[1] < HANDOFF_RECHECK:
            return False
        command, problem = maps.handoff_command(self.selection, self.arguments, self.environment,
                                                database_schema(self.database))
        if problem is not None:
            if self.blocked is None or self.blocked[2] != problem:
                print("Map keeps its version instead of restarting: " + problem, flush=True)
            self.blocked = (current, now, problem)
            return False
        self.command = command
        self.reason = "Restart requested" if requested else "Installation changed"
        return True


def watch_runtime(server, runtime, original, database, config_file, config_required,
                  stopped, reload_requested, follower=None):
    """Switch this server only after its selected runtime passes a read-only check.

    With a ``follower`` the map also follows its installation (see
    InstallationFollower). Before shutting the server down for a restart the
    watcher marks it stopped, so a later SIGTERM ends the map instead.
    """
    pending = last_error = None

    def selected():
        settings = resolve_settings(db=database, config_file=config_file,
                                    config_required=config_required)
        target = resolve_runtime(settings)
        fingerprint = source_fingerprint(target.package) if target.mode == "dev" else None
        return target, (target.identity, fingerprint)

    def restart(message):
        if stopped.is_set():
            return
        print(message + "; restarting map on the same URL.", flush=True)
        reload_requested.set()
        stopped.set()
        server.shutdown()

    original_key = (runtime.identity, original)
    while not stopped.wait(1):
        if follower is not None and follower.due():
            return restart(follower.reason)
        try:
            target, key = selected()
            if key == original_key:
                pending = last_error = None
                continue
            if key != pending:
                pending = key
                continue
            if target.mode == "dev":
                for path in Path(target.package).rglob("*.py"):
                    compile(path.read_bytes(), str(path), "exec")
            preflight(target, database,
                      config_file=config_file if config_file is not None and
                      (config_required or Path(config_file).is_file()) else None)
            if key != selected()[1]:
                continue
        except (ConfigError, RuntimeError, OSError, SyntaxError, ValueError) as exc:
            message = str(exc)
            if message != last_error:
                print("Runtime reload waiting for valid source/configuration: " + message, flush=True)
                last_error = message
            continue
        return restart("Runtime changed")


def _main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--port", type=int)
    parser.add_argument("--open", action="store_true", help="Open in the default browser")
    args = parser.parse_args(argv)
    try:
        settings = resolve_settings(db=args.db, config_file=args.config, port=args.port)
    except ConfigError as exc:
        parser.error(str(exc))
    database, args.port = settings.database, settings.port
    if not database.is_file():
        parser.error("Database does not exist: " + str(database))
    if not 1 <= args.port <= 65535:
        parser.error("Port must be 1–65535")
    schema = database_schema(database)
    if schema is None:
        parser.error("Cannot read the database: " + str(database))
    if schema not in (2, 3):
        parser.error("The map requires schema 2 or 3; run richi backup and richi migrate")
    runtime = current_runtime()
    config_required = args.config is not None or "RICHI_CONFIG" in os.environ
    initial_source = source_fingerprint(runtime.package) if runtime.mode == "dev" else None
    address = url(args.port)
    stopped, reload_requested, terminating = threading.Event(), threading.Event(), threading.Event()
    # A restart re-enters the launcher with this map's explicit storage selection.
    storage = [*config_arguments(settings.config_file, config_required), "--db", str(database)]
    serve_arguments = ["map", "serve", "--port", str(args.port)]
    environment = worker_environment(settings.config_file, settings.workspace, config_required)
    environment.pop("RICHI_ACTIVE_RUNTIME", None)
    capabilities = list(CAPABILITIES)
    follower = handoff_signal = previous_handoff = None
    if handoff_supported():
        follower = InstallationFollower(database, storage, serve_arguments, environment, capabilities)
        handoff_signal = getattr(signal, maps.HANDOFF_SIGNAL)
        # Install before listening: anyone who can read this map's health may
        # ask it to hand off, and the default action would terminate it.
        previous_handoff = signal.signal(handoff_signal, lambda _signum, _frame: follower.requested.set())

    def restore_handoff():
        if handoff_signal is not None:
            signal.signal(handoff_signal, signal.SIG_DFL if previous_handoff is None else previous_handoff)

    try:
        server = LoopbackHTTPServer(("127.0.0.1", args.port),
                                    handler_for(database, settings.config_file, runtime, config_required,
                                                settings.workspace, capabilities))
    except OSError as exc:
        if args.open:
            if compatible_health(read_health(args.port, timeout=2), database, runtime, settings.config_file):
                restore_handoff()
                webbrowser.open(address)
                print("Existing map: " + address, flush=True)
                return
        restore_handoff()
        parser.error("Cannot listen on port %d: %s. Choose another --port." % (args.port, exc))
    server.daemon_threads = False
    registration = maps.register(args.port) if follower is not None else None
    print("Project memory map: " + address, flush=True)
    print("Local read-only viewer. Press Ctrl+C to stop.", flush=True)
    if args.open:
        webbrowser.open(address)
    watcher = threading.Thread(target=watch_runtime,
                               args=(server, runtime, initial_source, database,
                                     settings.config_file, config_required, stopped, reload_requested,
                                     follower),
                               daemon=False)

    def terminate(_signum, _frame):
        # A stop always wins over a restart that is under way.
        terminating.set()
        if not stopped.is_set():
            raise KeyboardInterrupt
    previous_sigterm = signal.signal(signal.SIGTERM, terminate)
    watcher.start()
    print("Runtime: " + runtime.mode + " " + str(runtime.package), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        terminating.set()
    finally:
        stopped.set()
        server.server_close()
        # A transition preflight owns a bounded child process (15 seconds).
        # Let it reap that child before this process exits.
        watcher.join(timeout=17)
        signal.signal(signal.SIGTERM, previous_sigterm)
        restarting = reload_requested.is_set() and not terminating.is_set()
        if not restarting:
            if registration is not None:
                maps.unregister(registration)
            restore_handoff()
    if not restarting:
        return
    # Replace this process, preserving its PID and URL. No supervisor or old
    # server is left running; --open is intentionally not repeated.
    if follower is not None and follower.command is not None:
        # The entry point selects and leases the current generation. Close
        # this generation's lease on exec, when its code stops running.
        command = follower.command
        for key in (LEASE_ENV, ROOT_ENV, GENERATION_ENV):
            environment.pop(key, None)
        for descriptor in lease_fds():
            os.set_inheritable(descriptor, False)
    else:
        command = bootstrap_command([*storage, *serve_arguments])
    if registration is not None:
        # The entry stays while the port is closed, so richi map waits for it.
        maps.mark_restarting(registration)
    if handoff_signal is not None:
        # A late request must not terminate the process before the new map
        # installs its handler; an ignored signal stays ignored across exec.
        signal.signal(handoff_signal, signal.SIG_IGN)
    os.execve(command[0], command, environment)


def main(argv=None):
    try:
        return _main(argv) or 0
    except (MemoryError, OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
