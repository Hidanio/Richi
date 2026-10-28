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
unchanged. With dev enabled, memory commands keep using source code; `dev false`
selects the installed release. Broken dev sources do not block release checks
or installation.

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
incompatible with the current Python or schemas. An unknown protocol is rejected
when reading the manifest. If the wheel for an already verified installed release
is replaced by another file with the same version number, the result is
`release_changed` and installation is rejected. A corrected build needs a new
version. Background checks without automatic installation report these reasons
as well and do not suggest an installation command for a blocked release.

Updating does not migrate a database. Protocol 1 requires compatibility with
schemas 1 and 2 and map schema 2. Changing this contract requires a separate
update mechanism with backups and data verification. An incompatible updater
protocol is rejected before installation.

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

New CLI invocations use the new version immediately. A running map keeps its
version until restarted, including child operations and dev reloads. After an
update, restart its process and run `richi map`. For a foreground map, press
`Ctrl+C` and start it again. For a background map, stop the Richi-owned process
whose PID is reported by `/api/health`, then run `richi map` again. The updater
does not terminate unrelated or unknown processes.

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
`richi update apply --version X.Y.Z`. System notifications and an install button
in the map are not implemented yet.

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
Restart an open map manually to update its installed runtime. A candidate that
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
