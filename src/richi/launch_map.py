#!/usr/bin/env python3
"""Start, reuse, stop or restart the loopback map without a persistent terminal."""

import argparse
from contextlib import contextmanager
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
# maps is None when the installed launcher has no map protocol or another one.
from .serve import (GENERATION_ENV, compatible_health, config_arguments, database_schema, maps,
                    read_health, url, worker_environment)


BASE = Path(__file__).resolve().parent
# richi map uses the configured port and the next ten (richi_launcher.maps.PORT_RANGE).
PORT_RANGE = 11
RESTARTING = ("Карта на %s перезапускается дольше обычного: она завершает запросы или новая версия "
              "ещё стартует. Повторите команду через несколько секунд.")
NO_MAP_CONTROL = ("Установленный richi_launcher не может останавливать карты: у него нет протокола карты "
                  "этой версии Richi или другая его версия. Обновите ту сторону, что старше (установку "
                  "через richi update apply или checkout), либо выполните richi config set dev false.")


class _NoMapControl(Exception):
    """Stands in for maps.Refused without the map protocol, where no map is signalled."""


# A map that was not signalled or stopped; its as_dict() is the command's answer.
MapRefused = maps.Refused if maps is not None else _NoMapControl


def _ports(first_port):
    return range(first_port, min(first_port + PORT_RANGE, 65536))


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
    # Maps before this release do not report their launcher; the package check below covers them.
    if health.get("launcher") is not None and health["launcher"] != str(LAUNCHER_ROOT):
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
        written = log.tell()
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
            return {"url": url(port), "status": "started", "pid": child.pid}
        time.sleep(0.1)
    exited = child.poll() is not None
    if not exited:
        child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
    # A map that handed itself off concurrently may own the port now.
    if healthy(port, database, runtime_selection, config_file):
        return {"url": url(port), "status": "existing"}
    # Only a map that exited by itself wrote why; one stopped here may have started but never answered.
    reason = _reason(log_path, written) if exited else ": она не ответила за 8 секунд"
    raise RuntimeError("Карта не запустилась" + reason + ". Подробности: " + str(log_path))


def _reason(log_path, offset):
    """The last line this map wrote to the shared log, such as why it could not listen."""
    try:
        with log_path.open("rb") as log:
            log.seek(offset)
            lines = [line.strip() for line in log.read(65536).decode("utf-8", "replace").splitlines()]
    except OSError:
        return ""
    lines = [line for line in lines if line]
    return ": " + lines[-1][:500] if lines else ""


def _can_listen():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            probe.listen(1)
        return True
    except OSError:
        return False


def _hand_off_in_place(port, health, database, config_file, runtime_selection):
    """Ask a map of this installation to re-execute on the current version, keeping its PID.

    Returns the result, or None when the map cannot do it here: then it is
    replaced instead. A map that is already handing itself off is rescanned
    rather than stopped.
    """
    if (maps is None or maps.HANDOFF_CAPABILITY not in health.get("capabilities", [])
            or not maps.running_current_generation()):
        return None
    verified, _unverified = maps.inspect(health)
    if verified is None or not maps.installation_owns(verified.launcher):
        return None
    if maps.handoff_problem(database_schema(database)) is not None:
        # The map would refuse to follow; replacing it checks the runtime first.
        return None
    reason = maps.request_handoff(port, health)
    if reason in maps.RETRYABLE:
        raise MapRefused(reason, port, health["pid"])
    if reason is not None:
        return None
    # The map checks the new release before it switches; wait as long as that may take.
    deadline, current = time.monotonic() + maps.HANDOFF_TIMEOUT, None
    while time.monotonic() < deadline:
        current = read_health(port)
        if compatible_health(current, database, runtime_selection, config_file):
            return {"url": url(port), "status": "restarted", "pid": current["pid"],
                    "previous": {"pid": health["pid"], "version": health["runtime"].get("version"),
                                 "exited": current["pid"] != health["pid"]}}
        time.sleep(0.1)
    if maps.is_map(current) and all(current.get(key) == health.get(key)
                                    for key in ("pid", "runtime", "installation_generation")):
        # It kept its version (its log says why): replacing it checks the runtime first.
        return None
    # It is still switching, or switched to something else: look again.
    raise MapRefused(maps.Reason.CHANGED, port, health["pid"])


def _settle(port, timeout=5):
    """After a map changed under us, wait until its port serves a map again or stays free."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if read_health(port) is not None:
            return
        time.sleep(0.1)


def _replace(port, health, database, config_file, runtime_selection, config_required, workspace, runtime_dir):
    """Stop a verified Richi map of this storage and start this runtime on its URL."""
    if maps is None:
        raise RuntimeError(NO_MAP_CONTROL)
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
    result = _start(port, database, config_file, runtime_selection, config_required, workspace, runtime_dir)
    result["status"] = "restarted"
    result["previous"] = {"pid": stopped.pid, "version": stopped.version, "exited": stopped.exited}
    return result


def launch(database, first_port, config_file=None, runtime=None, config_required=False, workspace=None,
           restart=False):
    runtime_selection = runtime or current_runtime()
    with _launch_lock(database) as runtime_dir:
        for attempt in range(3):
            try:
                return _launch_locked(database, first_port, config_file, runtime_selection, config_required,
                                      workspace, restart, runtime_dir)
            except MapRefused as exc:
                if not exc.retryable or attempt == 2:
                    raise
                _settle(exc.port)


def _launch_locked(database, first_port, config_file, runtime_selection, config_required, workspace,
                   restart, runtime_dir):
    ports = _ports(first_port)
    # A map re-executing on a new version has no health for a moment.
    restarting = maps.wait_for_restarts(ports) if maps is not None else []
    transitioning, stale = [], None
    for port in ports:
        health = read_health(port)
        if not restart and compatible_health(health, database, runtime_selection, config_file):
            return {"url": url(port), "status": "existing"}
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
                    return {"url": url(port), "status": "existing"}
            time.sleep(0.1)
        raise RuntimeError("Карта ещё использует другой runtime. Проверьте конфигурацию и "
                           + str(runtime_dir / "map-server.log"))
    if stale is not None:
        # A map of this installation switches in place when asked. An older
        # map or another installation environment is replaced; either way the
        # port and URL stay with this storage.
        if not restart:
            handed_off = _hand_off_in_place(stale[0], stale[1], database, config_file, runtime_selection)
            if handed_off is not None:
                return handed_off
        return _replace(stale[0], stale[1], database, config_file, runtime_selection,
                        config_required, workspace, runtime_dir)
    if restarting:
        # Not back yet and its storage is unknown: it may be this one, so no second map.
        raise RuntimeError(RESTARTING % url(restarting[0]))
    if maps is not None and maps.wait_for_restarts(ports, timeout=0):
        # A map began to restart while this scan looked at its port: look again.
        raise MapRefused(maps.Reason.CHANGED, first_port)
    port = next((port for port in ports if available(port)), None)
    if port is None:
        raise RuntimeError("Не найден свободный локальный порт для карты.")
    return _start(port, database, config_file, runtime_selection, config_required, workspace, runtime_dir)


def stop(database, first_port, config_file=None):
    """Stop this storage's map; only a process verified through its health is signalled."""
    if maps is None:
        raise RuntimeError(NO_MAP_CONTROL)
    with _launch_lock(database):
        # Otherwise a map re-executing right now would be reported as not running.
        restarting = maps.wait_for_restarts(_ports(first_port))
        for port in _ports(first_port):
            health = read_health(port)
            if managed_health(health, database, config_file):
                return maps.stop(port, health).as_dict()
        if restarting:
            raise RuntimeError(RESTARTING % url(restarting[0]))
        result = {"status": "not_running", "url": url(first_port)}
        if maps.is_map(read_health(first_port)):
            result["note"] = "The map on this port serves another workspace; select that workspace to stop it"
        elif not available(first_port):
            result["note"] = "Another program uses this port; it was not touched"
        return result


def _main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=("stop", "restart"))
    parser.add_argument("--db", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--port", type=int)
    parser.add_argument("--no-open", action="store_true", help="Check startup without opening a browser")
    parser.add_argument("--open", action="store_true", help="Open the browser, as restart does by default")
    args = parser.parse_args(argv)
    # The only validation of these flags; richi map serve rejects --no-open itself.
    if args.action == "stop" and (args.open or args.no_open):
        parser.error("--open and --no-open do not apply to richi map stop")
    if args.open and args.no_open:
        parser.error("Use either --open or --no-open")
    if args.open and args.action != "restart":
        parser.error("--open is only supported by richi map serve and richi map restart; richi map opens by default")
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
            print(json.dumps(stop(database, args.port, settings.config_file), ensure_ascii=False))
            return 0
        result = launch(database, args.port, settings.config_file, current_runtime(),
                        config_required=args.config is not None or "RICHI_CONFIG" in os.environ,
                        workspace=settings.workspace, restart=args.action == "restart")
        if not args.no_open:
            result["browser_opened"] = webbrowser.open(result["url"])
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except MapRefused as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False), file=sys.stderr)
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
