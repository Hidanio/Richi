"""Names, limits and outcomes shared by everything that handles running maps."""
from enum import Enum

import richi_bootstrap as bootstrap

# The protocol between this package and the runtime (richi.serve.MAPS_API).
# Change it whenever a function or constant the runtime uses changes; a
# runtime speaking another version treats this package as absent.
API_VERSION = 2
APPLICATION = "project-memory-map"
# Maps from this release onwards follow the installation's current generation by
# themselves and re-execute through the stable entry point on this signal.
HANDOFF_CAPABILITY = "installation_handoff"
# Not SIGHUP: a foreground map must still end when its terminal closes.
HANDOFF_SIGNAL = "SIGUSR1"
PORT_RANGE = 11
DEFAULT_PORT = 8765
STOP_TIMEOUT = 20
EXIT_TIMEOUT = 10
# Without an explicit request, a map waits this long after seeing another
# current generation, so an updater that hands it off directly goes first.
HANDOFF_GRACE = 5
# How long the entry point may take to start before a map replaces itself.
HANDOFF_CHECK_TIMEOUT = 15
# How long to wait for a map asked to hand off: it notices the request within
# a second, runs that check, finishes its requests and re-executes; the new
# code takes over its listening socket or, before this release, binds the port.
HANDOFF_TIMEOUT = HANDOFF_CHECK_TIMEOUT + 10
# How long richi map waits for a registered map that is re-executing.
RESTART_TIMEOUT = 15
# A restarting mark this old belongs to a process that never came back.
RESTART_STALE = 120
REGISTRY = "maps"
MARKER = "handoff.json"

# Set by the installation's bootstrap for every process it starts.
LEASE_ENV, ROOT_ENV, GENERATION_ENV = bootstrap.LEASE_ENV, bootstrap.ROOT_ENV, bootstrap.GENERATION_ENV
# Tests set this to decide deterministically who hands a map off.
GRACE_ENV = "RICHI_MAP_HANDOFF_GRACE"
# Set for the entry point check a map runs before it hands itself off: that
# command must not look for maps, including the one waiting for it.
PASSIVE_ENV = "RICHI_MAPS_PASSIVE"
# Also set for that check. A runtime that can take over the map's listening
# socket answers with PROBE_ANSWER followed by its features on standard error
# (richi.serve.HANDOFF_FEATURES); older runtimes say nothing.
PROBE_ENV = "RICHI_MAP_PROBE"
PROBE_ANSWER = "richi-map-handoff:"
# The feature, and the variable naming the inherited descriptor (richi.serve).
LISTEN_FD = "listen_fd"
LISTEN_FD_ENV = "RICHI_MAP_LISTEN_FD"


class Status(Enum):
    """What happened to a running map an updater or a command found."""
    CURRENT = "current"                    # It already runs the current generation.
    RESTARTED = "restarted"                # It re-executed on the current generation.
    REQUESTED = "requested"                # It was asked to and nobody waited.
    REPLACING = "replacing"                # A background richi map is replacing it.
    RESTART_REQUIRED = "restart_required"  # It cannot follow; someone has to restart it.
    PENDING = "pending"                    # It was asked to and did not switch in time.
    FAILED = "failed"                      # Asking or replacing it did not work.


class Reason(Enum):
    """Why a map was not signalled or stopped."""
    NOT_RICHI_MAP = "not_richi_map"
    UNVERIFIED_PROCESS = "unverified_process"
    FOREIGN_USER = "foreign_user"
    NOT_PERMITTED = "not_permitted"
    PROCESS_EXITED = "process_exited"
    CHANGED = "changed"
    TIMEOUT = "timeout"

    @property
    def message(self):
        return _MESSAGES[self]


_MESSAGES = {
    Reason.NOT_RICHI_MAP: "The process on this port is not a Richi map; it was not stopped",
    Reason.UNVERIFIED_PROCESS: "The process reported by the map could not be verified as Richi's map; "
                               "it was not stopped",
    Reason.FOREIGN_USER: "The map process belongs to another user; it was not stopped",
    Reason.NOT_PERMITTED: "This environment may not signal the map process (for example a sandbox); "
                          "it was not stopped",
    Reason.PROCESS_EXITED: "The map process exited while it was being verified",
    Reason.CHANGED: "The map changed while it was being checked; run the command again",
    Reason.TIMEOUT: "The map did not release its port in time; it may still be finishing requests",
}

# A map that hands itself off or exits between verification and the signal:
# looking again finds what replaced it.
RETRYABLE = frozenset({Reason.CHANGED, Reason.PROCESS_EXITED})
