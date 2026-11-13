"""Find, verify, stop and hand off running Richi maps.

How a running map follows its installation
==========================================

Identity. A map is identified only through its loopback ``/api/health``
answer: application name, PID, runtime payload, storage selection,
installation generation, launcher and capabilities. Before any signal, and
before its storage selection is used to start a replacement, the process with
that PID must belong to this user, run Richi's runtime runner with exactly that
payload and hold the socket listening on the port that answered
(``control.inspect``), so an unrelated listener, a forged answer, one naming
another map's PID or a reused PID is never signalled or followed; ``richi map``
also reuses a map only after this check (``richi.serve.foreign_answer``). Only
SIGTERM (stop) and the handoff signal are ever sent. Nothing here opens a
knowledge store or reads a workspace configuration.

The map (``richi.serve``). A map advertises HANDOFF_CAPABILITY only when it
installed the HANDOFF_SIGNAL handler, which needs this package at the
runtime's API_VERSION (``richi.serve.MAPS_API``). Its follower watches
``state.json``; when another generation is current, at once on the signal or
after HANDOFF_GRACE otherwise, it runs ``handoff.handoff_command``: no setup
or activation may wait for recovery, the release must be complete and read the
store's schema, and the stable entry point must
start it once with the map's own storage selection (``richi --version``, with
PASSIVE_ENV and PROBE_ENV). The map then re-executes through that entry point,
keeping its PID and URL; its old lease closes on exec. If the runtime that
answered the check declared LISTEN_FD, the listening socket stays open across
the exec (LISTEN_FD_ENV names it) and connections wait instead of failing;
older runtimes bind the port again themselves. A map that cannot follow keeps
serving and logs why; a stop always wins over a restart in progress.

Registry (``registry``). In a managed installation a running map has
``<root>/maps/<port>.json``. From the moment a map decides to re-execute until
the new code answers, the entry is marked ``restarting``: while it finishes its
requests and starts again its port is closed or silent, and ``richi map``
waits for it (``wait_for_restarts``) instead of taking that port or moving to
the next one.

Who asks (``reconcile``). ``scan`` finds this installation's maps in the
registry and on given ports; callers then act explicitly:

- the updater (``after_activation``, after apply, rollback or automatic
  installation) asks maps that follow and waits for them, and reports older
  maps, on the registry and the default ports;
- the first command of a generation (``first_invocation``) covers activations
  by updaters up to 1.1.0, which cannot do it: once per port range it asks
  maps that follow without waiting, and a terminal command without a chat
  identity replaces older maps in the background through ``richi map``; other
  commands leave them pending in ``<root>/maps/handoff.json``;
- ``richi map`` (``richi.launch_map``) asks a map of this installation to
  switch in place and replaces an older one or one of another environment.

Maps started by 1.1.0 or earlier cannot follow. The updater and commands of
agents or scripts only report them; ``richi map`` replaces them on their URL,
and so does the next terminal command unless such a map runs in a terminal of
its own, which is left to its user.
"""
from . import control, handoff, processes, protocol, reconcile, registry
from .control import (Refused, Stopped, Verified, inspect, is_map, port_free, read_health, request_handoff, stop,
                      url, verify)
from .handoff import (Handoff, handoff_command, handoff_grace, handoff_problem, installation_owns,
                      running_current_generation, stable_command)
from .processes import listens, process_arguments, process_owner, process_uid, runner_launcher
from .protocol import (API_VERSION, APPLICATION, DEFAULT_PORT, GENERATION_ENV, GRACE_ENV, HANDOFF_CAPABILITY,
                       HANDOFF_CHECK_TIMEOUT, HANDOFF_GRACE, HANDOFF_SIGNAL, HANDOFF_TIMEOUT, LEASE_ENV,
                       LISTEN_FD, LISTEN_FD_ENV, MARKER, PASSIVE_ENV, PORT_RANGE, PROBE_ANSWER, PROBE_ENV,
                       REGISTRY, RESTART_TIMEOUT, RETRYABLE, ROOT_ENV, Reason, Status)
from .reconcile import after_activation, first_invocation
from .registry import current_generation, mark_restarting, register, unregister, wait_for_restarts
