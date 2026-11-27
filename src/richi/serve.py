#!/usr/bin/env python3
"""Loopback viewer for project memory. Python standard library only.

Knowledge is served read-only. The only changes the map accepts are to the
user's standing rules and to the user's agent settings (which agents there
are and where each finds skills), and only from a page opened by `richi map`
(map_access).

Like the knowledge itself, the rules, the agents with their skill folders (as
the user wrote them, under the home directory as ~/...) and the names and
descriptions of this user's installed skills are readable by any client of the
loopback port. Where the agent settings are kept, and why they could not be
read, is told only to a page that may edit. The edit key ends with the map
process (map_access.forget_secret), unless it hands itself over to new code.
"""

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
import socket
from socketserver import TCPServer
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from typing import NamedTuple
from urllib.parse import parse_qs, urlsplit

from richi_launcher.config import resolve_settings, ConfigError
from richi_launcher.runtime import (LAUNCHER_ROOT, bootstrap_command, current_runtime, preflight,
                                    resolve_runtime, runtime_command, lease_fds)
from .memory import Parser, MemoryError
from . import agent_commands, map_access, memory, rules, scenario_format, scenario_runs, scenarios, skill_catalog


BASE = Path(__file__).resolve().parent
API_VERSION = 4
# The richi_launcher.maps protocol this runtime speaks (maps.API_VERSION).
MAPS_API = 3
# Set by the installation's bootstrap for every process it starts.
LEASE_ENV = "RICHI_INSTALLATION_LEASE_FD"
ROOT_ENV = "RICHI_INSTALLATION_ROOT"
GENERATION_ENV = "RICHI_INSTALLATION_GENERATION"
# Between two map processes when one hands itself off (richi_launcher.maps).
# Answering the check the old map runs first (richi --version), the runtime
# that will serve names what it takes over; given LISTEN_FD, the old map keeps
# its listening socket open across exec and names it in LISTEN_FD_ENV.
PROBE_ANSWER = "richi-map-handoff:"
LISTEN_FD = "listen_fd"
LISTEN_FD_ENV = "RICHI_MAP_LISTEN_FD"
# Set by a map for the process that replaces it (a reload or a hand-off): that one keeps the edit
# key, and any other map process that starts for the store ends it.
RESTARTED_ENV = "RICHI_MAP_RESTARTED"
HANDOFF_FEATURES = (LISTEN_FD,)


def answer_probe():
    print(PROBE_ANSWER, *HANDOFF_FEATURES, file=sys.stderr, flush=True)


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
                 "runtime_selection", "runtime_reload", "workspace_selection", "tool_catalog", "rules_editor",
                 "agents_editor", "scenarios_editor"]
                + ([maps.HANDOFF_CAPABILITY] if handoff_supported() else []))


def url(port):
    return "http://127.0.0.1:%d/" % port


def read_health(port, timeout=0.5):
    """A loopback map's health answer, or None. Never goes through a proxy.

    richi_launcher.maps has its own reader: this one also works when the
    installed launcher has no map protocol or a different one.
    """
    from http.client import HTTPException
    from urllib.error import HTTPError
    from urllib.request import ProxyHandler, build_opener
    try:
        with build_opener(ProxyHandler({})).open(url(port) + "api/health", timeout=timeout) as response:
            return json.loads(response.read(65537))
    except HTTPError as exc:
        # Another service answered with an error page. Close its response
        # explicitly; the exception's finalizer warns about it on Python 3.14.
        if exc.fp is not None:
            exc.close()
        return None
    except (OSError, HTTPException, ValueError, AttributeError, TypeError):
        return None


def foreign_answer(port, health):
    """Why this map answer on the port may not be taken for this user's map serving it, or None.

    Another local user can copy a map's health answer, PID included, and serve
    it from a port of their own, so a matching answer is reused or opened only
    when its process belongs to this user, runs Richi's map server and listens
    on that port itself. Where that cannot be checked, for example in a
    sandbox, the copy cannot be told apart and the answer is refused as well
    (not_permitted); a map that began to re-execute is CHANGED. Without the
    installed launcher's map control nothing can be checked or stopped, and
    the answer is taken as it is.
    """
    if maps is None:
        return None
    _verified, reason = maps.verify(port, health)
    return reason


GIT_QUERY_LIMIT = 8192
GIT_OPERATION_TIMEOUT = 25
GIT_WORKER_TIMEOUT = 30
GIT_WORKER_OUTPUT_LIMIT = 450000
GRAPH_WORKER_TIMEOUT = 20
GRAPH_WORKER_OUTPUT_LIMIT = 32 * 1024 * 1024
RULES_BODY_LIMIT = 64 * 1024
SCENARIOS_BODY_LIMIT = scenarios.MAX_CHANGE + 16 * 1024
RULES_WORKER_TIMEOUT = 20
RULES_WORKER_OUTPUT_LIMIT = 1024 * 1024
SCENARIOS_LIST_OUTPUT_LIMIT = 8 * 1024 * 1024
# Reading the whole workspace can legitimately be much larger than applying one change.
RULES_LIST_OUTPUT_LIMIT = GRAPH_WORKER_OUTPUT_LIMIT
# Error codes of a refused change and the HTTP status the map answers with; any other code is 400.
CHANGE_ERROR_STATUS = {
    "not_found": 404, "conflict": 409, "exists": 409, "settings": 409, "busy": 503,
    "invalid_state": 409, "schema_upgrade_required": 409,
}
# How long a request waits for its turn: reading for another reading, a change for another change.
TURN_TIMEOUT = RULES_WORKER_TIMEOUT + 5
RULES_SOURCE = "richi map"
CHANGES_ONLY = "The map changes only standing rules, scenarios and agent settings"
# What a page that may not edit is told about agent settings that cannot be used: the full text names the file.
SETTINGS_PROBLEM = "The agent settings cannot be used now"
# No change the page sends nests deeper; a deeper body is refused before anything else reads it.
BODY_DEPTH = 16
SCENARIOS_BODY_DEPTH = scenario_format.MAX_DEPTH + 4
# parse_change's answer for a body it refused, apart from any JSON value (null included).
INVALID = object()


def _shallow(value, limit):
    """Whether no list or object in the value nests deeper than limit."""
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, (dict, list)):
            if depth >= limit:
                return False
            stack.extend((child, depth + 1) for child in (item.values() if isinstance(item, dict) else item))
    return True


class LoopbackHTTPServer(ThreadingHTTPServer):
    """Bind the local viewer without a reverse DNS lookup during startup.

    With ``listener`` it serves a socket that is already bound and listening:
    the one the previous process of this map left open for it.
    """

    # Connections that arrive while a map re-executes wait here.
    request_queue_size = 64

    def __init__(self, address, handler, listener=None):
        self.listener = listener
        super().__init__(address, handler)

    def handle_error(self, request, client_address):
        # A client that gave up, for example while the map re-executed and its
        # connection waited in the backlog, is not an error of the map.
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)

    def server_bind(self):
        if self.listener is not None:
            self.socket.close()
            self.socket = self.listener
            self.server_address = self.socket.getsockname()
        else:
            # HTTPServer.server_bind calls getfqdn(), which can block for tens of
            # seconds on macOS. The viewer uses a numeric loopback address only.
            TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def inherited_listener(port):
    """The listening socket the previous process of this map left open, or None.

    Only a loopback TCP socket listening on this port is taken over; anything
    else under that descriptor is left alone and the port is bound as usual.
    """
    value = os.environ.pop(LISTEN_FD_ENV, None)
    if value is None or not value.isdigit():
        return None
    try:
        listener = socket.socket(fileno=int(value))
    except OSError:
        return None  # Nothing was inherited, for example a launcher closed it.
    try:
        if (listener.family == socket.AF_INET and listener.type == socket.SOCK_STREAM
                and listener.getsockname() == ("127.0.0.1", port) and _accepting(listener)):
            listener.set_inheritable(False)
            return listener
    except OSError:
        pass
    listener.detach()
    return None


def _accepting(sock):
    """The socket listens for connections (or, where that is unknown, is not connected)."""
    try:
        return bool(sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN))
    except (AttributeError, OSError):
        pass  # macOS does not report SO_ACCEPTCONN.
    try:
        sock.getpeername()
    except OSError:
        return True
    return False


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


class WorkerOutputLimit(ValueError):
    """The child produced more output than the caller can buffer."""


def _read_worker_output(process, *, label, timeout, maximum, request=None):
    """Feed the request and drain both pipes within one budget, and always reap the owned worker."""
    selector = selectors.DefaultSelector()
    output, size = {"stdout": [], "stderr": []}, 0
    deadline = time.monotonic() + timeout
    pending = memoryview(request) if request else None
    try:
        if pending is not None:
            # Written as the worker reads it. A request larger than the pipe must neither block this
            # thread outside the budget nor, when the worker stops reading, hide the worker's answer.
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        elif request is not None:
            process.stdin.close()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(label + " timed out")
            for key, _ in selector.select(min(remaining, 0.2)):
                if key.data == "stdin":
                    try:
                        pending = pending[os.write(key.fileobj.fileno(), pending[:65536]):]
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        pending = pending[:0]
                    if not pending:
                        selector.unregister(key.fileobj)
                        try:
                            process.stdin.close()
                        except OSError:
                            pass
                    continue
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                size += len(chunk)
                if size > maximum:
                    raise WorkerOutputLimit(label + " output exceeded the size limit")
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


def rules_change(body):
    """The change object the rules command applies for one map change.

    `rules apply` validates it, exactly as it validates the command's own
    options, so the map adds nothing here but where the rule came from. The
    object travels on stdin: no value of it is ever a command argument.
    """
    if isinstance(body, dict):
        # The page never chooses the source: a rule added here says so, and an edit keeps what it had.
        body = {key: value for key, value in body.items() if key != "source"}
        if body.get("action") == "add":
            body["source"] = RULES_SOURCE
    # ASCII escapes: a lone surrogate in the text cannot be encoded as UTF-8.
    return json.dumps(body).encode("ascii")


def agent_change(body):
    """Apply one map change to the user's agent settings; its answer and HTTP status.

    The same function as `richi skill-sources` validates and writes, so the map
    accepts exactly what the command does.
    """
    if not isinstance(body, dict):
        return 400, {"error": "Send an object with action add, update or remove", "code": "invalid_request"}
    options = {key: value for key, value in body.items() if key not in {"action", "id"}}
    try:
        # full: the page shows every agent and the skill lists, read once after the change.
        return 200, public_agents(agent_commands.change(body.get("action"), body.get("id"), options, full=True),
                                  editing=True)
    except agent_commands.AgentError as exc:
        payload = {"error": str(exc), "code": exc.code}
        if exc.subject is not None:
            payload["subject"] = exc.subject
        return CHANGE_ERROR_STATUS.get(exc.code, 400), payload
    except OSError as exc:
        return 503, {"error": "Agent settings could not be saved: " + str(exc), "code": "unavailable"}


def public_agents(payload, editing):
    """An agents answer as the map gives it: without folder internals, and for a page that
    only reads without the place of the settings file."""
    listing = payload.get("listing", payload)
    if "catalog" in listing:
        listing["catalog"] = {key: value for key, value in listing["catalog"].items() if key != "roots"}
    if not editing:
        listing.pop("settings_file", None)
        if listing.get("problem"):
            # The full text names the file, and the user with it.
            listing["problem"] = SETTINGS_PROBLEM
    return payload


def _rules_list_worker(database, workspace):
    """Private map read on the selected runtime and pinned database. It shares validation with the CLI."""
    from . import memory

    try:
        with closing(memory.connect(Path(database), readonly=True)) as conn, memory.transaction(conn, write=False):
            memory.backend(conn)
            payload = rules.listing(conn, memory, workspace, fields=rules.KNOWN)
    except (OSError, sqlite3.Error, memory.MemoryError) as exc:
        print(json.dumps({"code": "busy" if memory._store_busy(exc) else "unavailable"}), file=sys.stderr)
        return 1
    sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return 0


def _run_rules_request(database, operation, runtime, config_file=None, workspace=None, config_required=False,
                       stdin=None):
    """Run a rules command for the map; its answer, or its error with a matching HTTP status."""
    environment = worker_environment(config_file, workspace, config_required)
    if stdin is not None:
        # The change log names the map as the one who made the change.
        environment["RICHI_RULES_ACTOR"] = RULES_SOURCE
    listing = operation == ["rules", "list"]
    if listing:
        # The stable launcher already dispatches the selected runtime's serve entry point.
        # The map's internal operation never needs a new public CLI flag or a newer launcher.
        arguments = ["_rules_list", str(database), workspace or ""]
    else:
        arguments = ["--compact-json", *config_arguments(config_file, config_required), "--db", str(database), *operation]
    try:
        process = subprocess.Popen(
            runtime_command(runtime, arguments, action="serve" if listing else "cli"),
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            pass_fds=lease_fds(), env=environment)
        status, output, error = _read_worker_output(process, label="Rules command",
            timeout=RULES_WORKER_TIMEOUT, maximum=RULES_LIST_OUTPUT_LIMIT if listing else RULES_WORKER_OUTPUT_LIMIT,
            request=stdin)
    except (TimeoutError, subprocess.TimeoutExpired):
        return 503, {"error": "The rules command timed out", "code": "timeout"}
    except WorkerOutputLimit as exc:
        if listing:
            return 503, {"error": "The rule list is too large for the map; use richi rules list --global or "
                                  "richi rules list --project ID", "code": "output_limit"}
        return 503, {"error": str(exc), "code": "unavailable"}
    except (OSError, ValueError) as exc:
        if listing:
            return 503, {"error": "The map could not run the rule reader", "code": "unavailable"}
        return 503, {"error": str(exc)[:1000], "code": "unavailable"}
    try:
        payload = json.loads((error if status else output).decode("utf-8"))
    except (ValueError, UnicodeError):
        payload = None
    if not isinstance(payload, dict):
        return 503, {"error": "The rules command failed; run richi check", "code": "unavailable"}
    if status:
        # A failed runner can include installation and configuration paths. Only its public status is needed.
        if listing:
            if payload.get("code") == "busy":
                return 503, {"error": "The store is busy", "code": "busy"}
            return 503, {"error": "The rules could not be read; run richi check", "code": "unavailable"}
        if "error" not in payload:
            return 503, {"error": "The rules command failed; run richi check", "code": "unavailable"}
        return CHANGE_ERROR_STATUS.get(payload.get("code"), 400), payload
    if isinstance(payload.get("rule"), dict):
        payload["rule"] = {field: payload["rule"][field] for field in rules.KNOWN}
    return 200, payload


def scenarios_change(body):
    """The change object `scenario apply` receives from the map UI (stripped source, ASCII-safe JSON)."""
    if isinstance(body, dict):
        body = {key: value for key, value in body.items() if key != "source"}
        if body.get("action") == "add":
            body["source"] = RULES_SOURCE
    return json.dumps(body).encode("ascii")


def _scenarios_list_worker(database, workspace):
    """Private map read of scenario definitions, templates, runs and unified journal."""
    try:
        with closing(memory.connect(Path(database), readonly=True)) as conn, memory.transaction(conn, write=False):
            memory.backend(conn)
            payload = scenarios.map_snapshot(conn, memory, workspace=workspace)
    except (OSError, sqlite3.Error, memory.MemoryError) as exc:
        print(json.dumps({"code": "busy" if memory._store_busy(exc) else "unavailable"}), file=sys.stderr)
        return 1
    sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return 0


def _scenario_run_worker(database, run_id):
    """Private map read of one run's detailed step state and event journal."""
    try:
        with closing(memory.connect(Path(database), readonly=True)) as conn, memory.transaction(conn, write=False):
            memory.backend(conn)
            run_data = scenario_runs.run_status(conn, memory, run_id)
            row = conn.execute("SELECT definition_json FROM scenario_runs WHERE id = ?", (run_id,)).fetchone()
            if row is not None and row[0]:
                definition = json.loads(row[0])
                run_data["scenario_title"] = definition.get("title") or run_data["scenario_id"]
                by_id = {step["id"]: step for step in definition.get("steps", []) if isinstance(step, dict)}
                for step in run_data.get("steps", []):
                    spec = by_id.get(step.get("id"))
                    if not spec:
                        continue
                    step["title"] = spec.get("title") or step["id"]
                    params = spec.get("params") or {}
                    if spec.get("type") == "gate":
                        step["gate"] = {
                            "question": params.get("question", ""),
                            "options": list(params.get("options") or []),
                            "allow_comment": params.get("allow_comment") is not False,
                        }
                    elif spec.get("type") == "checklist":
                        step["checklist"] = {"items": list(params.get("items") or [])}
            events = scenarios.log(conn, run_id=run_id, entries=100)
            payload = {"run": run_data, "log": events["entries"], "log_more": events["more"]}
    except memory.NotFoundError as exc:
        print(json.dumps({"error": str(exc), "code": "not_found"}), file=sys.stderr)
        return 1
    except memory.MemoryError as exc:
        code = getattr(exc, "code", None) or ("busy" if memory._store_busy(exc) else "unavailable")
        if code in ("busy", "unavailable"):
            print(json.dumps({"code": code}), file=sys.stderr)
        else:
            print(json.dumps({"error": str(exc), "code": code}), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error) as exc:
        print(json.dumps({"code": "busy" if memory._store_busy(exc) else "unavailable"}), file=sys.stderr)
        return 1
    sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return 0


def _run_scenarios_request(database, mode, runtime, config_file=None, workspace=None, config_required=False,
                           *, run_id=None, stdin=None):
    """Run a scenario list/run-detail/apply worker for the map."""
    environment = worker_environment(config_file, workspace, config_required)
    if stdin is not None:
        environment["RICHI_RULES_ACTOR"] = RULES_SOURCE
    if mode == "list":
        arguments = ["_scenarios_list", str(database), workspace or ""]
        action = "serve"
        maximum = SCENARIOS_LIST_OUTPUT_LIMIT
    elif mode == "run":
        arguments = ["_scenario_run", str(database), run_id]
        action = "serve"
        maximum = SCENARIOS_LIST_OUTPUT_LIMIT
    else:
        arguments = ["--compact-json", *config_arguments(config_file, config_required), "--db", str(database),
                     "scenario", "apply", "--json", "-"]
        action = "cli"
        maximum = RULES_WORKER_OUTPUT_LIMIT
    try:
        process = subprocess.Popen(
            runtime_command(runtime, arguments, action=action),
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            pass_fds=lease_fds(), env=environment)
        status, output, error = _read_worker_output(
            process, label="Scenarios command", timeout=RULES_WORKER_TIMEOUT, maximum=maximum, request=stdin)
    except (TimeoutError, subprocess.TimeoutExpired):
        return 503, {"error": "The scenarios command timed out", "code": "timeout"}
    except WorkerOutputLimit as exc:
        return 503, {"error": str(exc), "code": "output_limit"}
    except (OSError, ValueError) as exc:
        return 503, {"error": str(exc)[:1000], "code": "unavailable"}
    try:
        payload = json.loads((error if status else output).decode("utf-8"))
    except (ValueError, UnicodeError):
        payload = None
    if not isinstance(payload, dict):
        return 503, {"error": "The scenarios command failed; run richi check", "code": "unavailable"}
    if status:
        if payload.get("code") == "busy":
            return 503, {"error": "The store is busy", "code": "busy"}
        if payload.get("code") == "unavailable" or mode == "list":
            return 503, {"error": "Scenarios could not be read; run richi check", "code": "unavailable"}
        if "error" not in payload:
            return 503, {"error": "The scenarios command failed; run richi check", "code": "unavailable"}
        return CHANGE_ERROR_STATUS.get(payload.get("code"), 400), payload
    return 200, payload


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
                capabilities=None, store=None):
    database = Path(database).expanduser().resolve()
    # Without a resolved answer, the file itself is the one selector that cannot name another store.
    store = list(store) if store is not None else store_arguments(database, config_file, None, config_required)
    runtime = runtime or current_runtime()
    # A running map may withdraw a capability it can no longer provide.
    capabilities = list(CAPABILITIES) if capabilities is None else capabilities
    export_lock = threading.Lock()
    tools_lock = threading.Lock()
    git_lock = threading.Lock()
    # Readings take turns among themselves and changes among themselves. Any client of the port
    # may read, so a change, which only the user's page makes, never waits behind readings.
    rules_read_lock, rules_lock = threading.Lock(), threading.Lock()
    scenarios_read_lock, scenarios_lock = threading.Lock(), threading.Lock()
    agents_read_lock, agents_lock = threading.Lock(), threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "ProjectMemoryMap/4"
        answered = False

        def setup(self):
            # Bound draining of idle requests before a runtime transition.
            self.request.settimeout(35)
            super().setup()

        def log_message(self, *_args):
            # Do not log knowledge content or search terms.
            pass

        def answer(self, method):
            """Run one request. A failure nobody foresaw is answered too, not left as a closed connection."""
            self.answered = False
            try:
                return method()
            except (ConnectionError, socket.timeout):
                # The client is gone or silent: there is nobody to answer.
                raise
            except Exception as exc:
                if not self.answered:
                    try:
                        self.send_data(500, {"error": "The map could not answer: " + type(exc).__name__,
                                             "code": "internal"})
                    except (ConnectionError, socket.timeout):
                        # The client left meanwhile; the failure to log is still the one above.
                        pass
                # Still raised: the server's log keeps the traceback.
                raise

        def edit_key(self):
            """Whether the request carries this map's edit key; None after answering that the key cannot be read now."""
            try:
                return map_access.authorized(database, self.headers.get(map_access.HEADER))
            except OSError as exc:
                # Not "no key": the page would drop a key that still works.
                self.send_data(503, {"error": "The edit key could not be read: " + (exc.strerror or str(exc)),
                                     "code": "unavailable"})
                return None

        def require_edit_key(self):
            editing = self.edit_key()
            if editing is False:
                self.send_data(403, {"error": "Open the map with richi map to change rules, scenarios or agents here",
                                     "code": "not_editing"})
            return editing is True

        def send_data(self, status, data, content_type="application/json; charset=utf-8"):
            if not isinstance(data, bytes):
                # backslashreplace: half of a surrogate pair, which a request may carry and a message
                # may quote, is not UTF-8; as \udXXX it is still the same JSON string.
                data = json.dumps(data, ensure_ascii=False).encode("utf-8", "backslashreplace")
            self.answered = True
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
            return self.answer(self.get)

        def get(self):
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
                                            "config_file": str(config_file) if config_file is not None else None,
                                            "store_arguments": store})
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
            if url.path == "/api/rules" and not url.query:
                editing = self.edit_key()
                if editing is None:
                    return None
                # One reading worker at a time: a burst of requests waits instead of forking.
                if not rules_read_lock.acquire(timeout=TURN_TIMEOUT):
                    return self.send_data(503, {"error": "The rules are being read; try again", "code": "busy"})
                try:
                    status, payload = _run_rules_request(database, ["rules", "list"], runtime, config_file, workspace,
                                                         config_required)
                finally:
                    rules_read_lock.release()
                if status == 200:
                    payload["editing"] = editing
                    payload["limit_text"] = rules.MAX_TEXT
                    if not editing and isinstance(payload.get("agent_settings_problem"), dict):
                        payload["agent_settings_problem"]["error"] = SETTINGS_PROBLEM
                return self.send_data(status, payload)
            if url.path == "/api/scenarios" and not url.query:
                editing = self.edit_key()
                if editing is None:
                    return None
                if not scenarios_read_lock.acquire(timeout=TURN_TIMEOUT):
                    return self.send_data(503, {"error": "Scenarios are being read; try again", "code": "busy"})
                try:
                    status, payload = _run_scenarios_request(database, "list", runtime, config_file, workspace,
                                                             config_required)
                finally:
                    scenarios_read_lock.release()
                if status == 200:
                    payload["editing"] = editing
                return self.send_data(status, payload)
            if url.path == "/api/scenario-run":
                try:
                    if len(url.query) > 256:
                        return self.send_data(414, {"error": "Query exceeds the size limit"})
                    params = parse_qs(url.query, keep_blank_values=True, strict_parsing=True, max_num_fields=1) if url.query else {}
                except ValueError:
                    return self.send_data(400, {"error": "Invalid query"})
                run_id = params.get("id", [None])[0]
                if (set(params) != {"id"} or len(params["id"]) != 1 or not run_id
                        or len(run_id) > 120 or any(ord(c) < 32 or ord(c) == 127 for c in run_id)):
                    return self.send_data(400, {"error": "Pass a valid run id"})
                if not scenarios_read_lock.acquire(timeout=TURN_TIMEOUT):
                    return self.send_data(503, {"error": "Scenarios are being read; try again", "code": "busy"})
                try:
                    status, payload = _run_scenarios_request(database, "run", runtime, config_file, workspace,
                                                             config_required, run_id=run_id)
                finally:
                    scenarios_read_lock.release()
                return self.send_data(status, payload)
            if url.path in {"/api/agents", "/api/skills"} and not url.query:
                editing = self.edit_key() if url.path == "/api/agents" else False
                if editing is None:
                    return None
                # Both scan this user's skill folders: one such reading at a time.
                if not agents_read_lock.acquire(timeout=TURN_TIMEOUT):
                    return self.send_data(503, {"error": "Skills are being read; try again", "code": "busy"})
                try:
                    if url.path == "/api/skills":
                        catalog = skill_catalog.catalog()
                        return self.send_data(200, {key: value for key, value in catalog.items() if key != "roots"})
                    payload = public_agents(agent_commands.listing(catalog=True), editing)
                    payload["editing"] = editing
                    return self.send_data(200, payload)
                except OSError:
                    return self.send_data(503, {"error": "Agents and skills are unavailable"})
                finally:
                    agents_read_lock.release()
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
            return self.answer(self.get)

        def read_change(self, max_bytes=RULES_BODY_LIMIT):
            """The body of a same-origin change request as bytes, or None after answering it."""
            port = self.server.server_address[1]
            if (not self.allowed_origin()
                    or self.headers.get("Origin") not in {"http://127.0.0.1:%d" % port, "http://localhost:%d" % port}
                    or self.headers.get("Sec-Fetch-Site") not in (None, "same-origin")):
                self.send_data(403, {"error": "Only same-origin pages of this map may change rules, scenarios or agents"})
                return None
            if self.headers.get_content_type() != "application/json":
                self.send_data(415, {"error": "Send application/json"})
                return None
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                length = -1
            if not 0 <= length <= max_bytes:
                self.send_data(413, {"error": "The request body must have a length of at most %d bytes" % max_bytes})
                return None
            return self.rfile.read(length)

        def parse_change(self, raw, max_depth=BODY_DEPTH):
            """The JSON value of a request body, or INVALID after answering 400; reached only by an entitled request."""
            try:
                value = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeError, RecursionError):
                # RecursionError: brackets nested deeper than the parser goes.
                value = INVALID
            # Some Pythons parse brackets far deeper than they can write back; no change nests that deep.
            if value is INVALID or not _shallow(value, max_depth):
                self.send_data(400, {"error": "Invalid JSON", "code": "invalid_request"})
                return INVALID
            return value

        def do_POST(self):
            return self.answer(self.post)

        def post(self):
            path = urlsplit(self.path)
            if path.path not in {"/api/session", "/api/rules", "/api/agents", "/api/scenarios"} or path.query:
                return self.send_data(405, {"error": CHANGES_ONLY})
            scenario_request = path.path == "/api/scenarios"
            raw = self.read_change(max_bytes=SCENARIOS_BODY_LIMIT if scenario_request else RULES_BODY_LIMIT)
            if raw is None:
                return None
            if path.path == "/api/session":
                # A ticket is 64 hexadecimal characters: nothing longer is parsed without the secret.
                body = self.parse_change(raw) if len(raw) <= 256 else {}
                if body is INVALID:
                    return None
                try:
                    value = map_access.redeem(database, body.get("ticket") if isinstance(body, dict) else None)
                except OSError as exc:
                    # Not the ticket's fault: the files beside the store cannot be written.
                    return self.send_data(503, {"error": "The edit key could not be issued: " + (exc.strerror or str(exc)),
                                                "code": "unavailable"})
                if value is None:
                    return self.send_data(403, {"error": "This edit ticket is invalid, expired or already used; open the map again with richi map",
                                                "code": "ticket_invalid"})
                return self.send_data(200, {"secret": value})
            if not self.require_edit_key():
                return None
            body = self.parse_change(raw, max_depth=SCENARIOS_BODY_DEPTH if scenario_request else BODY_DEPTH)
            if body is INVALID:
                return None
            agent_request = path.path == "/api/agents"
            if scenario_request:
                change = scenarios_change(body)
                if len(change) > scenarios.MAX_CHANGE:
                    return self.send_data(413, {"error": "The scenario change is too large", "code": "limit"})
            elif not agent_request:
                change = rules_change(body)
                if len(change) > rules.MAX_CHANGE:
                    # Escaping non-ASCII text can exceed what the rules command reads.
                    return self.send_data(413, {"error": "The rule change is too large", "code": "limit"})
            lock = agents_lock if agent_request else (scenarios_lock if scenario_request else rules_lock)
            if not lock.acquire(timeout=TURN_TIMEOUT):
                subject = "change of the agents" if agent_request else ("scenario change" if scenario_request else "rule change")
                return self.send_data(503, {"error": "Another " + subject + " is in progress; try again", "code": "busy"})
            try:
                # The page may have lost its editing session while waiting behind another save.
                if not self.require_edit_key():
                    return None
                if agent_request:
                    status, payload = agent_change(body)
                elif scenario_request:
                    status, payload = _run_scenarios_request(database, "apply", runtime,
                                                             config_file, workspace, config_required, stdin=change)
                else:
                    status, payload = _run_rules_request(database, ["rules", "apply", "--json", "-"], runtime,
                                                         config_file, workspace, config_required, stdin=change)
            finally:
                lock.release()
            return self.send_data(status, payload)

        def refuse(self):
            self.send_data(405, {"error": CHANGES_ONLY})

        do_PUT = refuse
        do_PATCH = refuse
        do_DELETE = refuse
        do_OPTIONS = refuse

    return Handler


def worker_environment(config_file=None, workspace=None, config_required=False):
    """Keep child storage/labels tied to this map, independently of the registry default."""
    environment = dict(os.environ)
    # These are private descendants of an already selected map invocation.
    # Reload re-enters the stable launcher with the map's explicit config/db;
    # it must not ask for a new choice or follow a later chat rebind.
    environment.pop("RICHI_CHAT_ID", None)
    environment.pop("CODEX_THREAD_ID", None)
    environment.pop("CLAUDECODE", None)
    environment.pop("CURSOR_AGENT", None)
    environment.pop("ANTIGRAVITY_AGENT", None)
    environment.pop("ANTIGRAVITY_CONVERSATION_ID", None)
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


def store_arguments(database, config_file=None, workspace=None, config_required=False):
    """The global options with which a `richi` command reaches the store this map shows.

    The page puts them into every command it composes. A workspace name is
    enough only when, in a terminal that selects no storage of its own, it
    leads to this very database: a map started for an explicit --db or --config
    may carry a workspace's name and still show another file, and a command for
    it names that file. The name is therefore resolved without this process's
    own storage variables.
    """
    database = Path(database)
    # A named workspace has isolated storage. The default allows RICHI_DB, RICHI_DATA_DIR
    # and later config changes, so its name alone cannot pin the write to this map.
    if workspace not in (None, "default"):
        try:
            if resolve_settings(workspace=workspace, environ={}).database == database:
                return ["--workspace", workspace]
        except (ConfigError, TypeError):
            # TypeError: an installed launcher older than this runtime cannot resolve a name
            # apart from the process environment, so the command names the file.
            pass
    selected = config_arguments(config_file, config_required)
    # Without a configuration file this is the default workspace with another database. Naming
    # it keeps the command valid where the terminal's current workspace is one that forbids --db.
    return [*(selected or ["--workspace", "default"]), "--db", str(database)]


def source_fingerprint(package=None):
    package = Path(package) if package is not None else BASE
    digest = hashlib.sha256()
    for path in sorted(package.rglob("*")):
        if path.suffix in {".py", ".sql", ".json"} and path.is_file():
            digest.update(str(path.relative_to(package)).encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
    return digest.digest()


def database_schema(database):
    """The store's schema, read the same read-only way as at startup; None if unknown."""
    from . import memory
    try:
        with closing(memory.connect(Path(database), readonly=True, timeout=5)) as conn:
            return conn.execute("PRAGMA user_version").fetchone()[0]
    except (sqlite3.Error, MemoryError):
        return None


HANDOFF_RECHECK = 30


class Blocked(NamedTuple):
    """Why a map keeps its version for now (InstallationFollower)."""
    generation: str  # The current generation it did not follow.
    checked: float   # When the check that found the problem ran (time.monotonic()).
    problem: str
    files: bool      # The file checks found it, not the entry point check.


class InstallationFollower:
    """Decide when a map re-executes on its installation's current generation.

    A map follows when another generation becomes current: at once when asked
    (the handoff signal), otherwise after a short grace so that an updater
    asking directly goes first. A request while the map already runs the
    current generation is ignored, so the signal alone cannot restart a map.
    It follows only after the stable entry point started that release with
    this storage selection, and only if the release is complete, can read the
    store and no setup or activation waits for recovery; otherwise it keeps
    serving, logs why and checks again later. A knowledge command recovers a
    setup journal and asks this installation's maps (once per generation and
    port range), and a map waiting for that follows at once; richi map
    recovers it and switches only its own workspace's map. Other maps follow
    at their next recheck or at the next knowledge command.
    """

    def __init__(self, database, selection, arguments, environment, capabilities):
        self.root, self.generation = os.environ.get(ROOT_ENV), os.environ.get(GENERATION_ENV)
        self.database, self.selection, self.arguments = database, selection, arguments
        self.environment, self.capabilities = environment, capabilities
        self.requested = threading.Event()
        self.grace = maps.handoff_grace()
        self.enabled = True
        self.changed_since = self.blocked = self.handoff = self.target = self.reason = None

    def takes_listener(self):
        """The runtime this map hands itself off to takes over its listening socket.

        That runtime answered the check for the generation that is still
        current; after another change the entry point would start a runtime
        nobody asked, which may not know the socket and could not bind its port.
        """
        return (self.handoff is not None and LISTEN_FD in self.handoff.features
                and maps.current_generation(self.root) == self.target)

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
        # The check starts the entry point, so a blocked map repeats it only
        # every HANDOFF_RECHECK seconds. When the file checks, which start
        # nothing, blocked it, a request goes ahead as soon as they pass, as
        # when the command that recovered a setup journal asks the maps.
        if (self.blocked is not None and self.blocked.generation == current
                and now - self.blocked.checked < HANDOFF_RECHECK
                and not (requested and self.blocked.files
                         and maps.handoff_problem(database_schema(self.database)) is None)):
            return False
        # The file checks run here first, so a block records which check found
        # the problem; handoff_command repeats them before the entry point.
        schema = database_schema(self.database)
        problem = maps.handoff_problem(schema)
        files = problem is not None
        if not files:
            handoff, problem = maps.handoff_command(self.selection, self.arguments, self.environment, schema)
        if problem is not None:
            if self.blocked is None or self.blocked.problem != problem:
                print("Map keeps its version instead of restarting: " + problem, flush=True)
            self.blocked = Blocked(current, now, problem, files)
            return False
        self.handoff, self.target = handoff, current
        self.reason = "Restart requested" if requested else "Installation changed"
        return True


def watch_runtime(server, runtime, original, database, config_file, config_required,
                  stopped, reload_requested, follower=None, on_restart=None):
    """Switch this server only after its selected runtime passes a read-only check.

    With a ``follower`` the map also follows its installation (see
    InstallationFollower). Before shutting the server down for a restart the
    watcher calls ``on_restart`` and marks it stopped, so a later SIGTERM ends
    the map instead.
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
        if on_restart is not None:
            on_restart()
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


def _keep_listening(server):
    """A descriptor that keeps the server's socket listening after server_close(), or None."""
    try:
        return os.dup(server.fileno())
    except OSError:
        return None


def _forget_secret(database, *, own_only=False):
    try:
        map_access.forget_secret(database, own_only=own_only)
    except OSError as exc:
        print("The edit key could not be removed: " + (exc.strerror or str(exc)), flush=True)


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
    if schema not in memory.SUPPORTED_VERSIONS or schema < 2:
        parser.error("The map requires schema 2 or newer; run richi migrate, which backs up the store first")
    # Taken before anything copies the environment: no child may see them.
    listener = inherited_listener(args.port)
    restarted = os.environ.pop(RESTARTED_ENV, None) == "1"
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
    store = store_arguments(database, settings.config_file, settings.workspace, config_required)
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
                                                settings.workspace, capabilities, store),
                                    listener=listener)
    except OSError as exc:
        problem = str(exc)
        if args.open:
            existing = read_health(args.port, timeout=2)
            if compatible_health(existing, database, runtime, settings.config_file):
                refused = foreign_answer(args.port, existing)
                if refused is None:
                    restore_handoff()
                    map_access.open_browser(address, database)
                    print("Existing map: " + address, flush=True)
                    return
                problem = ("the map answering there could not be verified as this user's map ("
                           + refused.value + "); it was left alone")
        restore_handoff()
        parser.error("Cannot listen on port %d: %s. Choose another --port." % (args.port, problem))
    if restarted:
        try:
            map_access.resume_secret(database, args.port)
        except OSError as exc:
            print("The previous map edit key could not be resumed: " + (exc.strerror or str(exc)), flush=True)
    else:
        # A key from an earlier map process ends here: pages still holding it compose commands, and
        # the page this start opens gets a new one.
        _forget_secret(database)
    # Where richi map finds this map, also outside its range of ports and without a map registry.
    try:
        map_access.record_port(database, args.port)
    except OSError as exc:
        print("The map's port could not be recorded beside the store: " + (exc.strerror or str(exc)), flush=True)
    server.daemon_threads = False

    def register_map():
        if hasattr(maps, "register_store"):
            return maps.register_store(args.port, database)
        return maps.register(args.port)

    registration = register_map() if follower is not None else None

    def announce_restart():
        # Before the port goes silent: from here until the new code answers,
        # richi map waits for this map instead of starting another one.
        nonlocal registration
        if follower is None:
            return
        if registration is None:
            # Started before the installation became managed: no entry yet.
            registration = register_map()
        maps.mark_restarting(registration)
    print("Project memory map: " + address, flush=True)
    if listener is not None:
        print("Took over the listening socket of the previous map process.", flush=True)
    print("Local viewer: knowledge is read-only; standing rules and agent settings change only from a page "
          "opened by richi map. Press Ctrl+C to stop.", flush=True)
    if args.open:
        # In a thread: the page can redeem its edit ticket only once the server below answers.
        threading.Thread(target=map_access.open_browser, args=(address, database), daemon=True).start()
    watcher = threading.Thread(target=watch_runtime,
                               args=(server, runtime, initial_source, database,
                                     settings.config_file, config_required, stopped, reload_requested,
                                     follower, announce_restart),
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
        # The port keeps listening across exec, so connections wait for the new
        # code; only a map richi map knows to wait for (its entry) does that.
        kept = (_keep_listening(server) if reload_requested.is_set() and not terminating.is_set()
                and registration is not None and follower.takes_listener() else None)
        server.server_close()
        # A transition preflight owns a bounded child process (15 seconds).
        # Let it reap that child before this process exits.
        watcher.join(timeout=17)
        signal.signal(signal.SIGTERM, previous_sigterm)
        restarting = reload_requested.is_set() and not terminating.is_set()
        if not restarting:
            if kept is not None:
                os.close(kept)
            if registration is not None:
                maps.unregister(registration)
            restore_handoff()
            # Whatever answers on this port next must not be handed a key that still works.
            # server_close has drained every accepted request, including the last ticket redemption.
            _forget_secret(database, own_only=True)
            map_access.clear_port(database, args.port)
    if not restarting:
        return
    # Replace this process, preserving its PID and URL. No supervisor or old
    # server is left running; --open is intentionally not repeated.
    if follower is not None and follower.handoff is not None:
        # The entry point selects and leases the current generation. Close
        # this generation's lease on exec, when its code stops running.
        command = follower.handoff.command
        for key in (LEASE_ENV, ROOT_ENV, GENERATION_ENV):
            environment.pop(key, None)
        for descriptor in lease_fds():
            os.set_inheritable(descriptor, False)
        if kept is not None and follower.takes_listener():
            os.set_inheritable(kept, True)
            environment[LISTEN_FD_ENV] = str(kept)
        elif kept is not None:
            os.close(kept)  # The installation changed again while requests finished.
    else:
        command = bootstrap_command([*storage, *serve_arguments])
    if handoff_signal is not None:
        # A late request must not terminate the process before the new map
        # installs its handler; an ignored signal stays ignored across exec.
        signal.signal(handoff_signal, signal.SIG_IGN)
    # The same map goes on: its pages keep their edit key.
    environment[RESTARTED_ENV] = "1"
    os.execve(command[0], command, environment)


def main(argv=None):
    try:
        if argv and argv[0] == "_rules_list" and len(argv) == 3:
            return _rules_list_worker(argv[1], argv[2] or None)
        if argv and argv[0] == "_scenarios_list" and len(argv) == 3:
            return _scenarios_list_worker(argv[1], argv[2] or None)
        if argv and argv[0] == "_scenario_run" and len(argv) == 3:
            return _scenario_run_worker(argv[1], argv[2])
        return _main(argv) or 0
    except (MemoryError, OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
