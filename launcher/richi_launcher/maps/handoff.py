"""The map's side of a handoff: when it may follow its installation, and how it re-executes."""
import os
from pathlib import Path
from typing import NamedTuple

import richi_bootstrap as bootstrap

from .protocol import (GENERATION_ENV, GRACE_ENV, HANDOFF_CHECK_TIMEOUT, HANDOFF_GRACE, LEASE_ENV,
                       PASSIVE_ENV, PROBE_ANSWER, PROBE_ENV, ROOT_ENV)
from .registry import current_generation

_BOOTSTRAP = ("import sys; sys.path.insert(0, sys.argv[1]); "
              "from richi_bootstrap import main; sys.exit(main(sys.argv[2:]))")


def context():
    # The same seam as the updater, so fixtures can substitute the installation.
    from .. import installation
    return installation._context()


def environments(ctx):
    """Where this installation's code lives: its seed environment and its generations."""
    return Path(ctx["original_prefix"]), Path(ctx["root"]) / "versions"


def within(path, parent):
    try:
        Path(path).relative_to(parent)
        return True
    except ValueError:
        return False


def installation_owns(launcher):
    """The launcher directory belongs to one of this installation's environments."""
    try:
        ctx = context()
    except Exception:
        return False
    return any(within(launcher, path) for path in environments(ctx))


def running_current_generation():
    """This process runs the installation's current generation."""
    root, generation = os.environ.get(ROOT_ENV), os.environ.get(GENERATION_ENV)
    return bool(root and generation) and current_generation(root) == generation


def stable_command(argv):
    """The installation's public entry point, which selects and leases the current generation.

    Returns (command, through_entry_point). Without the installation's entry
    point (a source checkout, an ordinary pip installation, a missing seed) the
    running launcher is used instead, as for an ordinary map reload.
    """
    try:
        ctx = context()
        if (Path(ctx["root"]) / "state.json").exists():
            anchor = Path(bootstrap.read_state(ctx)["seed_bootstrap"])
        else:
            anchor = Path(ctx["bootstrap"])
        python = Path(ctx["original_prefix"]) / "bin" / "python"
        if not python.exists() or not anchor.is_file():
            raise bootstrap.InstallationError("The installation entry point is unavailable")
    except (bootstrap.InstallationError, OSError, KeyError, ValueError):
        from ..runtime import bootstrap_command
        return bootstrap_command(argv), False
    return [str(python), "-I", "-B", "-c", _BOOTSTRAP, str(anchor.parent), *map(str, argv)], True


def handoff_grace():
    try:
        value = float(os.environ.get(GRACE_ENV, HANDOFF_GRACE))
    except ValueError:
        return float(HANDOFF_GRACE)
    return min(max(value, 0.0), 3600.0) if value == value else float(HANDOFF_GRACE)


def handoff_problem(schema=None):
    """Why a map must stay on its version instead of following the current one, or None.

    Unlike the per-second check, this validates the state as the stable entry
    point will: no setup journal may wait for recovery, and the current release
    must be complete and able to read the store. It only reads files, so a map
    it blocked runs it again on each request (richi.serve.InstallationFollower).
    """
    from ..setup_files import JOURNAL
    try:
        ctx = context()
        if not (Path(ctx["root"]) / "state.json").exists():
            return None
        if (Path(ctx["root"]) / JOURNAL).exists():
            # The map's own command recovers it before it serves, and fails if
            # it cannot; the --version check skips recovery, as all help does.
            # A knowledge command recovers it and asks this installation's maps
            # (once per generation and port range), and a map waiting for that
            # follows at once; richi map recovers it and switches only its own
            # workspace's map. Other maps follow at their next recheck or at
            # the next knowledge command.
            return "a setup or activation is waiting for recovery; run a command such as richi status to recover it"
        state = bootstrap.read_state(ctx)
        record = state["generations"][state["current"]]
        site = Path(record["site"])
        if not (site / "richi_launcher" / "cli.py").is_file() or not (site / "richi" / "memory.py").is_file():
            return "release {} is incomplete; repair the installation".format(record.get("version"))
        if schema is not None and schema not in bootstrap.generation_schemas(record):
            return "release {} cannot read database schema {}".format(record.get("version"), schema)
    except (bootstrap.InstallationError, OSError, KeyError, ValueError, TypeError) as exc:
        return "the installation state cannot be read: " + str(exc)
    return None


class Handoff(NamedTuple):
    """How a map re-executes on the current generation."""
    command: list         # The stable entry point running the map with its own storage selection.
    features: frozenset   # What that runtime declared it takes over from this process (LISTEN_FD).


def declared_features(output):
    """Features a runtime announced in its answer to the entry point check."""
    features = set()
    for line in output.splitlines():
        if line.startswith(PROBE_ANSWER):
            features.update(line[len(PROBE_ANSWER):].split())
    return frozenset(features)


def handoff_command(selection_arguments, arguments, environment, schema=None,
                    timeout=HANDOFF_CHECK_TIMEOUT):
    """(Handoff, None) to run ``arguments`` on the current generation, or (None, problem).

    A working map is replaced only after the installation's stable entry point
    resolved the same storage selection and runtime once with that release's
    own code (``richi --version``); the release must also be complete and
    declare support for the store's schema, and no setup journal may wait for
    recovery. That run also tells which features the runtime that will serve
    the map takes over.
    """
    import subprocess
    problem = handoff_problem(schema)
    if problem is not None:
        return None, problem
    command, through_entry_point = stable_command([*selection_arguments, *arguments])
    if not through_entry_point:
        # The running launcher would only start this same generation again.
        return None, "the installation entry point is unavailable; repair the installation"
    check, _through_entry_point = stable_command([*selection_arguments, "--version"])
    child = dict(environment)
    for key in ("RICHI_ACTIVE_RUNTIME", LEASE_ENV, ROOT_ENV, GENERATION_ENV):
        child.pop(key, None)
    child[PASSIVE_ENV] = child[PROBE_ENV] = "1"
    try:
        result = subprocess.run(check, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                timeout=timeout, env=child, close_fds=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "the current release did not start: " + str(exc)[:300]
    if result.returncode:
        return None, "the current release did not start: " + (result.stderr or result.stdout).strip()[-300:]
    return Handoff(command, declared_features(result.stderr)), None
