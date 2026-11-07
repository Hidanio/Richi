"""Synthetic terminal environment for CLI fixtures that do not test chats."""
import os
from pathlib import Path
import socket


def synthetic_port():
    """An unused loopback port, so map lookups never reach a developer's real map."""
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return str(reservation.getsockname()[1])


def cli_environment(directory, **overrides):
    """Avoid the caller's chat, workspace registry, configuration, runtime and agent profiles."""
    root = Path(directory).resolve() / "cli-home"
    # Claude Code exports CLAUDE_CONFIG_DIR into its shells; keeping it would aim
    # Claude setup fixtures at the developer's real profile.
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("RICHI_")
                   and key not in {"CODEX_THREAD_ID", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "ZDOTDIR"}}
    environment.update(HOME=str(root), XDG_CONFIG_HOME=str(root / ".config"),
                       XDG_DATA_HOME=str(root / ".local" / "share"))
    environment.update(overrides)
    return environment
