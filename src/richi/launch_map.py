#!/usr/bin/env python3
"""Start, reuse, stop or restart the loopback map without a persistent terminal."""

import argparse
from contextlib import contextmanager
from http.client import HTTPException
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import webbrowser

from richi_launcher.config import resolve_settings, ConfigError
from richi_launcher.runtime import LAUNCHER_ROOT, current_runtime, preflight, runtime_command, lease_fds
from .memory import Parser, MemoryError
from .serve import GENERATION_ENV, compatible_health, config_arguments, worker_environment
from urllib.request import ProxyHandler, build_opener

try:
    from richi_launcher import maps
except ImportError:  # A development runtime paired with an older installed launcher.
    maps = None


BASE = Path(__file__).resolve().parent
HTTP = build_opener(ProxyHandler({}))


class MapRefused(RuntimeError):
    def __init__(self, payload):
        super().__init__(payload["error"])
        self.payload = payload


def read_health(port):
    try:
        with HTTP.open("http://127.0.0.1:%d/api/health" % port, timeout=0.5) as response:
            return json.loads(response.read(8193))
    except (OSError, HTTPException, ValueError, AttributeError, TypeError):
        return None


def healthy(port, database, runtime=None, config_file=None):
    return compatible_health(read_health(port), database, runtime, config_file)


def managed_health(health, database, config_file):
    """Only wait on a server declaring the same configuration; never signal it."""
    return (isinstance(health, dict) and health.get("application") == "project-memory-map"
            and health.get("database") == str(database)
            and health.get("config_file") == (str(config_file) if config_file is not None else None)
            and isinstance(health.get("capabilities"), list)
            and "runtime_reload" in health["capabilities"]
            and "workspace_selection" in health["capabilities"])


def follows_itself(health):
    """A map that will converge on this runtime by itself: same generation and launcher.

    Its watcher resolves the same configuration inside the same installed
    launcher, as for a dev switch. A map of another generation or installation
    environment keeps its code until it is replaced.
    """
    if health.get("installation_generation") != os.environ.get(GENERATION_ENV):
        return False
    runtime = health.get("runtime")
    if not isinstance(runtime, dict):
        return False
    if runtime.get("mode") == "dev":
        return True
    try:
        return Path(runtime["package"]).parent == LAUNCHER_ROOT
    except (KeyError, TypeError):
        return False


def available(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # Match HTTPServer so a stopped map's TIME_WAIT connections do not
        # make a restart drift to the next port. Live listeners still conflict.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _url(port):
    return "http://127.0.0.1:%d/" % port


@contextmanager
def _launch_lock(database):
    try:
        import fcntl
    except ImportError as exc:
        raise RuntimeError("The background map launcher currently requires macOS or Linux") from exc
    runtime = database.parent / "runtime"
    runtime.mkdir(mode=0o700, exist_ok=True)
    # Serialize double-clicks so the launcher never starts duplicate instances.
    with (runtime / "map-launch.lock").open("a") as lock:
        deadline = time.monotonic() + 12
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise RuntimeError("Запуск карты уже выполняется. Повторите через несколько секунд.")
                time.sleep(0.1)
        yield runtime


def _start(port, database, config_file, runtime_selection, config_required, workspace, runtime_dir):
    log_path = runtime_dir / "map-server.log"
    with log_path.open("ab") as log:
        log_path.chmod(0o600)
        child = subprocess.Popen(
            runtime_command(runtime_selection, [*config_arguments(config_file, config_required),
                                                 "--db", str(database), "--port", str(port)],
                            action="serve"),
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            close_fds=True, start_new_session=True, pass_fds=lease_fds(),
            env=worker_environment(config_file, workspace, config_required),
        )
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and child.poll() is None:
        if healthy(port, database, runtime_selection, config_file):
            return {"url": _url(port), "status": "started", "pid": child.pid}
        time.sleep(0.1)
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
    # A map that handed itself off concurrently may own the port now.
    if healthy(port, database, runtime_selection, config_file):
        return {"url": _url(port), "status": "existing"}
    raise RuntimeError("Карта не запустилась. Подробности: " + str(log_path))


def _can_listen():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            probe.listen(1)
        return True
    except OSError:
        return False


def _replace(port, health, database, config_file, runtime_selection, config_required, workspace, runtime_dir):
    """Stop a verified Richi map of this storage and start this runtime on its URL."""
    if maps is None:
        raise RuntimeError("Установленный richi_launcher не умеет останавливать карту; переустановите Richi.")
    if not _can_listen():
        # For example a sandbox without local network: keep the working map.
        raise RuntimeError("Это окружение не может открыть локальный порт; работающая карта не остановлена.")
    try:
        # Check the new runtime before touching a working map.
        preflight(runtime_selection, database,
                  config_file=config_file if config_file is not None and
                  (config_required or Path(config_file).is_file()) else None)
    except ConfigError as exc:
        raise RuntimeError("Новая версия карты не прошла проверку; работающая карта не остановлена: "
                           + str(exc)) from exc
    stopped = maps.stop(port, health)
    if stopped["status"] != "stopped":
        raise MapRefused(stopped)
    result = _start(port, database, config_file, runtime_selection, config_required, workspace, runtime_dir)
    result["status"] = "restarted"
    result["previous"] = {"pid": stopped["pid"], "version": stopped["version"], "exited": stopped["exited"]}
    return result


def launch(database, first_port, config_file=None, runtime=None, config_required=False, workspace=None,
           restart=False):
    runtime_selection = runtime or current_runtime()
    with _launch_lock(database) as runtime_dir:
        ports = range(first_port, min(first_port + 11, 65536))
        transitioning, stale = [], None
        for port in ports:
            health = read_health(port)
            if not restart and compatible_health(health, database, runtime_selection, config_file):
                return {"url": _url(port), "status": "existing"}
            if not managed_health(health, database, config_file):
                continue
            if not restart and follows_itself(health):
                transitioning.append(port)
            elif stale is None:
                stale = (port, health)
        if transitioning:
            # The server watches this config and replaces itself. Waiting avoids
            # duplicate maps during a release/dev switch and cannot kill an
            # unrelated process, even if it reports a misleading health payload.
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                for port in transitioning:
                    if healthy(port, database, runtime_selection, config_file):
                        return {"url": _url(port), "status": "existing"}
                time.sleep(0.1)
            raise RuntimeError("Карта ещё использует другой runtime. Проверьте конфигурацию и "
                               + str(runtime_dir / "map-server.log"))
        if stale is not None:
            # An older generation or another installation environment never
            # switches by itself; the port and URL stay with this storage.
            return _replace(stale[0], stale[1], database, config_file, runtime_selection,
                            config_required, workspace, runtime_dir)
        port = next((port for port in ports if available(port)), None)
        if port is None:
            raise RuntimeError("Не найден свободный локальный порт для карты.")
        return _start(port, database, config_file, runtime_selection, config_required, workspace, runtime_dir)


def stop(database, first_port, config_file=None):
    """Stop this storage's map; only a process verified through its health is signalled."""
    if maps is None:
        raise RuntimeError("Установленный richi_launcher не умеет останавливать карту; переустановите Richi.")
    with _launch_lock(database):
        for port in range(first_port, min(first_port + 11, 65536)):
            health = read_health(port)
            if managed_health(health, database, config_file):
                return maps.stop(port, health)
        health = read_health(first_port)
        if maps.is_map(health):
            return maps.refusal("other_workspace", first_port)
        if not available(first_port):
            return maps.refusal("not_richi_map", first_port)
        return {"status": "not_running", "url": _url(first_port)}


def _main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=("stop", "restart"))
    parser.add_argument("--db", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--port", type=int)
    parser.add_argument("--no-open", action="store_true", help="Check startup without opening a browser")
    parser.add_argument("--open", action="store_true", help="Open the browser (restart; richi map opens by default)")
    args = parser.parse_args(argv)
    if args.action == "stop" and (args.open or args.no_open):
        parser.error("--open and --no-open do not apply to richi map stop")
    if args.open and args.no_open:
        parser.error("Use either --open or --no-open")
    if args.open and args.action != "restart":
        parser.error("--open is only supported by richi map restart and richi map serve; richi map opens by default")
    try:
        settings = resolve_settings(db=args.db, config_file=args.config, port=args.port)
    except ConfigError as exc:
        parser.error(str(exc))
    database, args.port = settings.database, settings.port
    if not database.is_file():
        parser.error("База не найдена: " + str(database))
    if not 1 <= args.port <= 65535:
        parser.error("Port must be 1–65535")
    try:
        if args.action == "stop":
            result = stop(database, args.port, settings.config_file)
            refused = result["status"] == "refused"
            print(json.dumps(result, ensure_ascii=False), file=sys.stderr if refused else sys.stdout)
            return 1 if refused else 0
        result = launch(database, args.port, settings.config_file, current_runtime(),
                        config_required=args.config is not None or "RICHI_CONFIG" in os.environ,
                        workspace=settings.workspace, restart=args.action == "restart")
        if not args.no_open:
            result["browser_opened"] = webbrowser.open(result["url"])
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except MapRefused as exc:
        print(json.dumps(exc.payload, ensure_ascii=False), file=sys.stderr)
        return 1
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


def main(argv=None):
    try:
        return _main(argv) or 0
    except (MemoryError, OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
