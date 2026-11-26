"""Synthetic terminal environment for CLI fixtures that do not test chats."""
import os
from pathlib import Path
import random
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

# richi map uses a port and the next ones.
from richi_launcher import maps


SCRUBBED_AGENT_VARS = frozenset({
    "CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDECODE", "CURSOR_AGENT",
    "ANTIGRAVITY_AGENT", "ANTIGRAVITY_CONVERSATION_ID", "ANTIGRAVITY_APP_DATA_DIR",
    "CODEX_HOME", "CLAUDE_CONFIG_DIR",
})


def _ephemeral_start():
    """The first port the kernel may give to an outgoing connection."""
    try:
        return int(Path("/proc/sys/net/ipv4/ip_local_port_range").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        pass
    if sys.platform == "darwin":
        try:
            result = subprocess.run(["/usr/sbin/sysctl", "-n", "net.inet.ip.portrange.first"],
                                    capture_output=True, text=True, timeout=5)
            return int(result.stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return 32768


def _bindable(port):
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def free_port(span=maps.PORT_RANGE):
    """An unused loopback port followed by span - 1 unused ones, below the ephemeral range.

    Map lookups then never reach a developer's real map. Inside the ephemeral
    range the kernel hands ports to outgoing connections, and a client probing a
    free port there can be given that very port and connect to itself; a map
    starting on it meanwhile cannot listen.
    """
    ceiling = min(_ephemeral_start(), 32768) - span
    for _attempt in range(200):
        if ceiling <= 20000:
            break
        first = random.randrange(20000, ceiling)
        if all(_bindable(port) for port in range(first, first + span)):
            return first
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return reservation.getsockname()[1]


def synthetic_port():
    """free_port() as an environment value, such as RICHI_PORT."""
    return str(free_port())


def stop_test_map(started, database):
    """Wait for a detached map to finish writing its disposable store before removing the directory."""
    pid = started["pid"]
    database = str(Path(database).resolve())
    port = urlsplit(started["url"]).port
    health = maps.read_health(port)
    if maps.is_map(health) and health["pid"] == pid and health.get("database") == database:
        if database not in (maps.process_arguments(pid) or ()):
            raise AssertionError("Cannot verify the test store in the map's process arguments")
        # stop verifies the runner, its owner and its listening socket before signalling.
        if maps.stop(port, health).exited:
            return
    # A map already draining can have closed its listener and still be cleaning its files.
    # A zombie no longer has an argument vector and cannot write anything.
    deadline = time.monotonic() + 20
    while database in (maps.process_arguments(pid) or ()):
        if time.monotonic() >= deadline:
            raise AssertionError("Test map %d is still running for %s" % (pid, database))
        time.sleep(0.05)


def cli_environment(directory, **overrides):
    """Avoid the caller's chat, workspace registry, configuration, runtime and agent profiles."""
    root = Path(directory).resolve() / "cli-home"
    # Claude Code exports CLAUDE_CONFIG_DIR and Antigravity exports ANTIGRAVITY_APP_DATA_DIR
    # into their shells; keeping them would aim setup/catalog fixtures at real profiles.
    # CLAUDECODE, CODEX_THREAD_ID, CURSOR_AGENT and ANTIGRAVITY_* would mark an agent shell.
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("RICHI_")
                   and key not in SCRUBBED_AGENT_VARS
                   and key != "ZDOTDIR"}
    environment.update(HOME=str(root), XDG_CONFIG_HOME=str(root / ".config"),
                       XDG_DATA_HOME=str(root / ".local" / "share"))
    environment.update(overrides)
    return environment
