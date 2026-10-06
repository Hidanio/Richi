#!/usr/bin/env python3
"""Start or reuse the loopback map, then open it without a persistent terminal."""

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import webbrowser

from richi_launcher.config import resolve_settings, ConfigError
from richi_launcher.runtime import current_runtime, runtime_command, lease_fds
from .memory import Parser, MemoryError
from .serve import compatible_health, config_arguments, worker_environment
from urllib.request import ProxyHandler, build_opener


BASE = Path(__file__).resolve().parent
HTTP = build_opener(ProxyHandler({}))


def read_health(port):
    try:
        with HTTP.open("http://127.0.0.1:%d/api/health" % port, timeout=0.5) as response:
            return json.loads(response.read(8193))
    except (OSError, ValueError, AttributeError, TypeError):
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


def launch(database, first_port, config_file=None, runtime=None, config_required=False, workspace=None):
    runtime_selection = runtime or current_runtime()
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
        ports = range(first_port, min(first_port + 11, 65536))
        transitioning = []
        for port in ports:
            health = read_health(port)
            if compatible_health(health, database, runtime_selection, config_file):
                return {"url": "http://127.0.0.1:%d/" % port, "status": "existing"}
            if managed_health(health, database, config_file):
                transitioning.append(port)
        if transitioning:
            # The server watches this config and replaces itself. Waiting avoids
            # duplicate maps during a release/dev switch and cannot kill an
            # unrelated process, even if it reports a misleading health payload.
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                for port in transitioning:
                    if healthy(port, database, runtime_selection, config_file):
                        return {"url": "http://127.0.0.1:%d/" % port, "status": "existing"}
                time.sleep(0.1)
            raise RuntimeError("Карта ещё использует другой runtime. Проверьте конфигурацию и "
                               + str(runtime / "map-server.log"))
        port = next((port for port in ports if available(port)), None)
        if port is None:
            raise RuntimeError("Не найден свободный локальный порт для карты.")
        log_path = runtime / "map-server.log"
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
                return {"url": "http://127.0.0.1:%d/" % port, "status": "started", "pid": child.pid}
            time.sleep(0.1)
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        raise RuntimeError("Карта не запустилась. Подробности: " + str(log_path))


def _main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--port", type=int)
    parser.add_argument("--no-open", action="store_true", help="Check startup without opening a browser")
    args = parser.parse_args(argv)
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
        result = launch(database, args.port, settings.config_file, current_runtime(),
                        config_required=args.config is not None or "RICHI_CONFIG" in os.environ,
                        workspace=settings.workspace)
        if not args.no_open:
            result["browser_opened"] = webbrowser.open(result["url"])
        print(json.dumps(result, ensure_ascii=False))
        return 0
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
