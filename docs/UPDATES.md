<a id="обновления-из-github-releases"></a>
# Updates from GitHub Releases

English | [Русский](UPDATES.ru.md)

Richi is installed in a dedicated Python environment. After the initial
installation, obtain published stable versions with:

```sh
richi update check
richi update apply
richi update rollback
```

`check` only queries the official `Hidanio/Richi` repository and shows the current
and previous installed versions. `apply` downloads the latest stable release.
To select a specific release, use `--version X.Y.Z` with `check` or `apply`.
Drafts and prereleases are not installed. When no releases exist, the result is
`no_release`; code being present on the main branch does not mean a release exists.
Network failures do not change the installed code.

Updates affect the **entire installation**, across every workspace using it.
Update commands do not accept `-w`, `--config`, `--db` or `--project-path`, do not
read databases, and do not require workspace selection in a new chat. Existing
chat bindings, `dev` settings, source paths, SQLite and captured Git bytes remain
unchanged. With dev enabled, memory commands keep using source code and managed
skill links stay on the checkout; `dev false` selects the installed release and
its skill. Broken dev sources do not block release checks or installation.
Releases up to 1.1.0 cannot maintain checkout skill links: run
`richi config set dev false` before rolling back to one of them
([what dev switches](INSTALLATION.md#what-dev-switches)).

<a id="установка-и-откат"></a>
## Installation and rollback

The updater checks the version, launcher protocol, supported schemas, wheel size
and SHA256, then creates a separate environment for the candidate. Installation
uses only the verified wheel, without downloading dependencies from PyPI. A
separate process checks candidate imports and version before activation. After
successful checks, the current-version pointer changes atomically. A failed
candidate is removed; the current version stays in place.

`rollback` switches to the previous version without network access. It swaps the
two versions, so another rollback restores the version that was current before
the first rollback. `apply` cannot downgrade; use explicit rollback for that.
An installation from a checkout has no verified published-wheel identity, so a
release with the same version number can be installed once. A matching version
number alone does not prove identical builds.

`check` returns `incompatible_release` and `can_apply: false` if a release is
incompatible with the current Python, updater protocol or protocol-1 manifest
fields. If the wheel for an already verified installed release is replaced by
another file with the same version number, the result is `release_changed` and
installation is rejected. A corrected build needs a new version. Background checks
without automatic installation report these reasons as well and do not suggest an
installation command for a blocked release. The database schemas a release
supports are known only from its self-check, after download: a release that would
drop a supported schema is refused at installation and its staged copy removed.

Updating does not migrate a database. Every protocol-1 release keeps the manifest
fields that updaters up to 1.0.1 accept: `database_schemas` is always `[1, 2]`, and
`map_schema` and the release's `memory.VERSION` are always 2. The schemas a release
actually supports come from its own self-check (`memory.SUPPORTED_VERSIONS`) and
are recorded for each installed generation. Any managed installation, including
one on 1.0.1, therefore reaches the latest protocol-1 release with one
`richi update apply` (or automatic installation), however many releases it
skipped. Maintainers: see the [release contract](RELEASING.md#protocol-1-release-contract).

The current runtime supports database schemas 1, 2 and 3. New stores are created
at schema 3; existing stores need an explicit migration for tool cards. Runtime
rollback does not downgrade a store, and older runtimes may not read it. Preserve
the pre-migration backup. See [graph migration](../skills/project-memory/references/GRAPH.md).

<a id="schema-support"></a>
The manager records verified database-schema support per installed generation.
Activation and rollback refuse to remove supported schemas, including a rollback
from support for 1/2/3 to 1/2. This conservative check does not open workspace
databases and applies even when no database has been migrated. A bootstrap
installed by 1.1.0 or later also checks before handing rollback to the previous
manager, using recorded metadata even if the current package is damaged;
installations that started on 1.0.x keep their original bootstrap, which does not.
Rollback between generations with compatible schema support remains available. A
refusal reports that package rollback does not roll back databases; select a
compatible release.

Updaters up to 1.0.1 perform their own rollback without this check. If a store is
at schema 3 (migrated, or created by `init` on 1.1.0 or later) and you roll back to
1.0.1, it refuses to open that store and changes nothing in it; run
`richi update apply` to return to the latest release. Once Claude Code is
connected, 1.0.1 refuses the rollback itself, because it cannot maintain that
integration. If automatic installation was enabled, that refused rollback leaves it
paused; resume it with `richi update auto enable --install`. Later releases restore
the previous pause state when a rollback fails.

<a id="updater-protocols"></a>
## Updater protocols

Every release so far uses updater protocol 1. A release that needs a newer
protocol is reported as `incompatible_release` (or rejected when its manifest is
read) and nothing is installed or changed; its release notes describe how to move
to it. Maintainers keep protocol-1 installations updatable as described in the
[release contract](RELEASING.md#protocol-1-release-contract).

<a id="сколько-версий-хранится"></a>
## Version retention

The current version and one previous version are retained. A candidate exists
temporarily during installation. Every running command, map and child process
holds a lease on its version; cleanup does not delete code still in use. A
retired but leased version may temporarily increase the number of directories.
After those processes finish, cleanup runs on the next update, rollback or
explicit invocation:

```sh
richi update cleanup
```

The result lists removed and deferred versions and cleanup errors. `check` does
not delete anything. The original environment retains Python and a small
permanent bootstrap module; once the original version is retired, its full code
is removed too. This is launch infrastructure, not a third complete Richi version.

State lives under `installations/` in Richi's platform data directory, separately
for each original Python environment. `update check` shows the paths. Workspace
settings do not determine installed code locations. Do not manually move managed
environments or edit version pointers.

<a id="работающая-карта-и-первоначальный-переход"></a>
## Running maps and the initial transition

New CLI invocations use the new version immediately. A running map follows the
installation as well; no manual step is needed after `update apply`, automatic
installation or `update rollback`. Refresh an open browser tab: the URL does not
change.

- A map started by this release or later re-executes itself on the newly current
  version, keeping its process and URL; a foreground `richi map serve` stays in
  its terminal. Right after activation the updater asks the maps it finds to do
  so: those registered by a managed installation and those on the default ports
  (8765 and the next ten, or from `RICHI_PORT`). It waits for them and reports
  them in its JSON result as `maps`: one entry per map with `url`, `status`
  (`restarted`; `restart_required` for an older map, see below; `pending` or
  `failed` with an `error`), `from_version` and `version`. Activation results
  also carry `restart_maps_required`, which is `true` while a found map still
  runs an older version. If an older manager changes the current version, for
  example a rollback performed by 1.1.0 or earlier, the map notices within a few
  seconds and follows by itself.
- A map stays on its version, and logs the reason once, if the newly current
  release is incomplete or cannot read the map's database schema. This happens,
  for example, after 1.0.1 rolls itself back while the store is at schema 3:
  1.0.1 cannot open that store, so the newer map keeps serving it. The map
  follows as soon as the installation changes again.
- A map started by 1.1.0 or earlier cannot follow. After an update by such a
  manager, the first `richi` command from a terminal that runs the new version
  replaces it once: it stops that map and starts the new version on the same
  port. It checks the maps the installation has registered and the ports
  `richi map` would use for that command's workspace (the configured port and
  the next ten). A command with a chat identity comes from an agent and may run
  in a sandbox, so it only asks newer maps to switch and never starts a server;
  the next command from a terminal, or `richi map`, replaces an older map.
- `richi map` keeps its workspace's map current on the same URL. It asks a map of
  the same installation to switch in place; it replaces a map started by 1.1.0 or
  earlier, or one from another installation environment. A desktop shortcut
  that runs `richi map` therefore always opens the current version. The new
  version is checked first, and a map is never stopped where no local port can
  be opened. If two installations serve the same store, the one whose
  `richi map` ran last owns the map.

To stop or restart the selected workspace's map explicitly:

```sh
richi map stop
richi map restart
richi map restart --no-open
```

`stop` reports `stopped` (with `pid`, `version` and whether the process has
`exited`), `not_running`, or, with exit code 1, `refused` with a `reason`:
`not_richi_map` (another program uses the port), `other_workspace`,
`unverified_process`, `foreign_user`, `process_exited`, `changed` (the map
changed meanwhile; run the command again) or `timeout`. `restart` stops the map and
starts the current version on the same URL, or starts one if none is running;
like `richi map` it opens the browser unless `--no-open` is given.

A map is identified only through its `/api/health` answer: the application, the
process ID it reports and its runtime. Before any signal, Richi checks that this
process belongs to the current user and runs Richi's map server with exactly that
runtime. Unrelated programs, unverifiable processes and other workspaces' maps are
never stopped. Stopping sends `SIGTERM` only: the map closes its port and exits.
A map keeps the lease on its old version until that code stops running, so
cleanup never removes code still in use; versions released by a handoff are
removed by the updater right away or by the next `update cleanup`. A handoff
reads no workspace data beyond what the map itself reads (to check schema
support, the map reads its store's schema number); the updater still opens no
database and reads no workspace configuration. The registry of running maps
lives in `maps/` beside the installation state and exists only once the
installation is managed.

To transition from an older installation without this updater, install the new
wheel once in the original dedicated Python environment and restart all old
Richi processes. They do not yet hold version leases. Subsequent updates use the
CLI. In a managed installation, use `update apply` instead of installing over
the original environment with pip.

The updater refuses to modify system Python, editable installations and
environments containing unrelated packages. Create a dedicated environment using
the [installation guide](INSTALLATION.md). Release checks remain available even
when managed installation is unsupported.

The publication procedure is described in [RELEASING.md](RELEASING.md).

<a id="фоновая-проверка-и-выбор-установки"></a>
## Background checks and installation choice

Background checks and automatic installation are initially disabled. To check
releases on a schedule without installing, register a user task through a macOS
LaunchAgent or Linux systemd --user:

```sh
richi update auto enable --interval 6h
richi update auto status
```

This mode retrieves release information without downloading the wheel or changing
the installed version. `auto status` shows an available release and its installation
command. The user decides when to run `richi update apply` or
`richi update apply --version X.Y.Z`. `richi chat current` also reports a newer
release found by the last check as `update`, read from the stored result without
network access. Agents that follow Project Memory run it at every chat start,
mention the update once and never install it themselves. System notifications and
an install button in the map are not implemented yet.

Automatic installation is a separate option, **disabled by default**. Enable it
only if you want compatible releases installed without confirming each version:

```sh
richi update auto enable --interval 6h --install
```

Without `--install`, `enable` always selects checking only, even if automatic
installation was previously enabled. Older settings without an explicit
`auto_install: true` also permit checking only. `auto status` reports `enabled`
(the schedule) and `auto_install` (permission to install) separately.

```sh
# Выключить автоустановку, оставить проверку:
richi update auto enable --interval 6h
# Выключить весь фоновый режим и снять системное задание:
richi update auto disable
```

The default interval is 6 hours. Whole minutes, hours and days from `15m` through
`7d` are accepted. The system task wakes every 15 minutes; Python checks the next
attempt's due time and exits if it is too early. `enable` resets that time and
allows the next check. A powered-off or sleeping computer cannot check releases;
checks resume when the user's session returns. No persistent process, Codex,
LLM or tokens are required.

To make one attempt under the same due-time and selected-mode rules:

```sh
richi update auto run
```

This command does not bypass a disabled policy or require workspace selection.
The schedule starts the original installation's permanent Python with a cleaned
environment. Background mode does not change SQLite, workspace settings or dev
sources.

A transient network failure schedules a retry after 15 minutes, then 30 minutes,
doubling up to the chosen interval. No published releases is a normal `no_release`
result.

<a id="защита-при-включённой-автоустановке"></a>
## Safeguards for automatic installation

`--install` uses the same verified wheel and atomic installation as `update apply`.
Running maps follow the installed version as after `update apply`; they restart
themselves, so the background job never becomes their parent. A candidate that
fails verification is blocked until a newer version or an explicit re-enable.
Concurrent attempts are serialized; a manual operation has priority over a
waiting background installation.

`disable` first persists the disabled policy and revokes permission to install,
then removes the system task. If a download has started, the final guard cancels
activation and removes the candidate. An update that completed before disabling
remains installed. If removing the OS task fails, the policy stays disabled and
the command's error explains what needs repair.

Manual rollback pauses automatic installation so it cannot immediately reinstall
the reverted version. An unexpected change of the current installation generation,
including a manual update or rollback through an older launcher, also pauses
automatic installation. Repeating `enable --install` confirms the current state
and resumes it. Plain `enable` returns to checking without installation. Checks
without automatic installation continue after manual version changes.

`status` reports actual OS task registration, the next due time, last outcome,
pause reason and last 20 events. State, policy and journal files live beside the
installation metadata in `installations/`; background process output does not
accumulate in an unbounded log. macOS requires a user GUI launchd session; Linux
requires a running systemd user manager. If such a scheduler is unavailable,
enabling returns an actionable error; explicit `update check` and `update apply`
remain available.
