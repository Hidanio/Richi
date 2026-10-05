"""Synthetic terminal environment for CLI fixtures that do not test chats."""
import os
from pathlib import Path


def cli_environment(directory, **overrides):
    """Avoid the caller's chat, workspace registry, configuration and runtime."""
    root = Path(directory).resolve() / "cli-home"
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("RICHI_") and key != "CODEX_THREAD_ID"}
    environment.update(HOME=str(root), XDG_CONFIG_HOME=str(root / ".config"),
                       XDG_DATA_HOME=str(root / ".local" / "share"))
    environment.update(overrides)
    return environment
