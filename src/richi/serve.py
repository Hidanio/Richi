#!/usr/bin/env python3
"""Read-only loopback viewer for project memory. Python standard library only."""

import argparse
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
from urllib.request import urlopen
import webbrowser

from richi_launcher.config import resolve_settings, ConfigError
from richi_launcher.runtime import (bootstrap_command, current_runtime, preflight,
                                    resolve_runtime, runtime_command)
from .memory import Parser, MemoryError


BASE = Path(__file__).resolve().parent
API_VERSION = 4
CAPABILITIES = ["git_source_viewer", "legacy_git_source_viewer", "standalone_runtime",
                "runtime_selection", "runtime_reload", "workspace_selection"]
GIT_QUERY_LIMIT = 8192
GIT_OPERATION_TIMEOUT = 25
GIT_WORKER_TIMEOUT = 30
GIT_WORKER_OUTPUT_LIMIT = 450000


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


def _run_git_request(database, options, runtime=None, config_file=None, workspace=None, config_required=False):
    """Bound worker time and pipes; HTTP threads never retain unbounded Git output."""
    request = json.dumps({"database": str(database), "options": options}).encode("utf-8")
    process = subprocess.Popen(runtime_command(runtime or current_runtime(), [], action="git_worker"),
        cwd=str(BASE), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True, env=worker_environment(config_file, workspace, config_required))
    selector = selectors.DefaultSelector()
    output, size = [], 0
    deadline = time.monotonic() + GIT_WORKER_TIMEOUT
    try:
        process.stdin.write(request)
        process.stdin.close()
        selector.register(process.stdout, selectors.EVENT_READ, True)
        selector.register(process.stderr, selectors.EVENT_READ, False)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Git navigation timed out")
            for key, _ in selector.select(min(remaining, 0.2)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                size += len(chunk)
                if size > GIT_WORKER_OUTPUT_LIMIT:
                    raise ValueError("Git navigation output exceeded the size limit")
                if key.data:
                    output.append(chunk)
        process.wait(timeout=max(0.01, deadline - time.monotonic()))
        if process.returncode:
            raise ValueError("Git navigation worker failed")
        result = json.loads(b"".join(output))
        return result["status"], result["payload"]
    except (OSError, ValueError, KeyError, TimeoutError, subprocess.TimeoutExpired) as exc:
        return 503, {"error": str(exc)[:1000], "code": "timeout" if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)) else "git_unavailable"}
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        selector.close()
        process.stdout.close()
        process.stderr.close()


def compatible_health(health, database, runtime=None, config_file=None):
    return (isinstance(health, dict) and health.get("application") == "project-memory-map"
            and health.get("database") == str(database) and health.get("api_version") == API_VERSION
            and isinstance(health.get("capabilities"), list)
            and all(capability in health["capabilities"]
                    for capability in CAPABILITIES)
            and health.get("runtime") == (runtime or current_runtime()).as_dict()
            and health.get("config_file") == (str(config_file) if config_file is not None else None))


def handler_for(database, config_file=None, runtime=None, config_required=False, workspace=None):
    database = Path(database).expanduser().resolve()
    runtime = runtime or current_runtime()
    export_lock = threading.Lock()
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
                                            "api_version": API_VERSION, "capabilities": CAPABILITIES,
                                            "runtime": runtime.as_dict(), "workspace": workspace,
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
            if url.path != "/api/graph":
                return self.send_data(404, {"error": "Not found"})
            try:
                params = parse_qs(url.query, keep_blank_values=True, strict_parsing=True) if url.query else {}
            except ValueError:
                return self.send_data(400, {"error": "Invalid query"})
            supported = {"include_hypotheses", "include_superseded"}
            if set(params) - supported or any(v not in (["0"], ["1"]) for v in params.values()):
                return self.send_data(400, {"error": "Flags must be 0 or 1"})
            flags = ["--" + key.replace("_", "-") for key, value in params.items() if value == ["1"]]
            if not export_lock.acquire(blocking=False):
                return self.send_data(503, {"error": "Export in progress; try Refresh again"})
            try:
                result = subprocess.run(
                    runtime_command(runtime, [*config_arguments(config_file, config_required),
                                               "--db", str(database), "graph", "export", *flags]),
                    capture_output=True, timeout=20,
                    env=worker_environment(config_file, workspace, config_required),
                )
                if result.returncode:
                    try:
                        reason = json.loads(result.stderr.decode("utf-8"))
                    except (ValueError, UnicodeError):
                        reason = {"error": "Graph export failed; run richi check"}
                    return self.send_data(503, reason)
                return self.send_data(200, result.stdout)
            except subprocess.TimeoutExpired:
                return self.send_data(503, {"error": "Graph export timed out"})
            finally:
                export_lock.release()

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


def watch_runtime(server, runtime, original, database, config_file, config_required,
                  stopped, reload_requested):
    """Switch this server only after its selected runtime passes a read-only check."""
    pending = last_error = None

    def selected():
        settings = resolve_settings(db=database, config_file=config_file,
                                    config_required=config_required)
        target = resolve_runtime(settings)
        fingerprint = source_fingerprint(target.package) if target.mode == "dev" else None
        return target, (target.identity, fingerprint)

    original_key = (runtime.identity, original)
    while not stopped.wait(1):
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
        if stopped.is_set():
            return
        print("Runtime changed; restarting map on the same URL.", flush=True)
        reload_requested.set()
        server.shutdown()
        return


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
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as conn:
        if conn.execute("PRAGMA user_version").fetchone()[0] != 2:
            parser.error("The map requires schema 2; run richi backup and richi migrate")
    runtime = current_runtime()
    config_required = args.config is not None or "RICHI_CONFIG" in os.environ
    initial_source = source_fingerprint(runtime.package) if runtime.mode == "dev" else None
    address = "http://127.0.0.1:%d/" % args.port
    try:
        server = LoopbackHTTPServer(("127.0.0.1", args.port), handler_for(database, settings.config_file, runtime, config_required, settings.workspace))
    except OSError as exc:
        if args.open:
            try:
                with urlopen(address + "api/health", timeout=2) as response:
                    health = json.load(response)
                if compatible_health(health, database, runtime, settings.config_file):
                    webbrowser.open(address)
                    print("Existing map: " + address, flush=True)
                    return
            except (OSError, ValueError):
                pass
        parser.error("Cannot listen on port %d: %s. Choose another --port." % (args.port, exc))
    server.daemon_threads = False
    print("Project memory map: " + address, flush=True)
    print("Local read-only viewer. Press Ctrl+C to stop.", flush=True)
    if args.open:
        webbrowser.open(address)
    stopped, reload_requested = threading.Event(), threading.Event()
    watcher = threading.Thread(target=watch_runtime,
                               args=(server, runtime, initial_source, database,
                                     settings.config_file, config_required, stopped, reload_requested),
                               daemon=False)
    def terminate(_signum, _frame):
        raise KeyboardInterrupt
    previous_sigterm = signal.signal(signal.SIGTERM, terminate)
    watcher.start()
    print("Runtime: " + runtime.mode + " " + str(runtime.package), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        reload_requested.clear()
    finally:
        stopped.set()
        server.server_close()
        # A transition preflight owns a bounded child process (15 seconds).
        # Let it reap that child before this process exits.
        watcher.join(timeout=17)
        signal.signal(signal.SIGTERM, previous_sigterm)
    if reload_requested.is_set():
        # Replace this process, preserving its PID and URL. No supervisor or old
        # server is left running; --open is intentionally not repeated.
        command = bootstrap_command([*config_arguments(settings.config_file, config_required),
                                     "--db", str(database), "map", "serve", "--port", str(args.port)])
        environment = worker_environment(settings.config_file, settings.workspace, config_required)
        environment.pop("RICHI_ACTIVE_RUNTIME", None)
        os.execve(command[0], command, environment)


def main(argv=None):
    try:
        return _main(argv) or 0
    except (MemoryError, OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
