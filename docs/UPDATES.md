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

Updating does not migrate a database. Protocol 1 keeps map wire schema 2. From
1.0.2, the manager accepts any release that declares support for every database
schema from 1 to its newest, such as `[1, 2, 3]` or a later `[1, 2, 3, 4]`, provided
it keeps every schema of the current version. One `update apply` can therefore
skip any number of releases. The current runtime
supports all three database schemas; tool cards require an explicit migration
to schema 3. Runtime rollback does not downgrade a migrated database, and older
runtimes may not read it. Preserve the pre-migration backup and use a compatible
runtime for schema 3. An incompatible updater protocol is rejected before
installation. See [graph migration](../skills/project-memory/references/GRAPH.md).

<a id="schema-3-bridge"></a>
Managers in 1.0.1 and earlier accept only `[1, 2]` and cannot install a schema-3
release directly. First install the 1.0.2 bridge release. It carries the new
manager but supports only schemas 1 and 2, so rollback to the old version stays
safe. Its `migrate` stops at schema 2, and it cannot write tool cards. Then update
to the latest release:

```sh
richi update apply --version 1.0.2
richi update apply
```

While 1.0.2 is the latest release, a plain `richi update apply` (or automatic
installation) selects it. The [fresh-environment transition](#schema-3-transition)
below remains an alternative. Reinstalling into the original environment does not replace the managed
current-version pointer: it still selects the old manager, even if its original
seed package has already been collected.

The updated manager records verified database-schema support per installed
generation. Activation and rollback refuse to remove supported schemas, including
a rollback from support for 1/2/3 to 1/2. This conservative check does not open
workspace databases and applies even when no database has been migrated. The
stable bootstrap checks before handing rollback to the previous manager, using
recorded metadata even if the current package is damaged. Rollback between
generations with compatible schema support remains available. A refusal reports
that package rollback does not roll back databases; select a compatible release.

<a id="schema-3-transition"></a>
## Moving an older installation to schema 3 support

Keep the old environments and installation metadata. The replacement uses a new
installation identity with the existing workspace settings, chat bindings and
data. It does not migrate a database. First record the old automatic-update
policy and disable its schedule, using the old command:

```sh
richi update auto status
richi update auto disable
richi config show
```

From a checkout of the schema-3-capable release, install a fixed package into an
**unused** prefix. Do not reinstall over a managed environment or edit its state:

```sh
richi_new_prefix="$HOME/.local/share/richi/venv-schema3"
python3 -m venv "$richi_new_prefix"
"$richi_new_prefix/bin/python" -m pip install --upgrade pip
"$richi_new_prefix/bin/python" -m pip install .
"$richi_new_prefix/bin/python" -I -c 'from richi import memory; assert 3 in memory.SUPPORTED_VERSIONS'
"$richi_new_prefix/bin/richi" config show
"$richi_new_prefix/bin/richi" chat current
```

Check that the configuration/database and any chat binding match the old
command. Existing `dev` settings are preserved; inspect the selected runtime.
If dev still selects an older checkout, select a schema-3-capable checkout or
deliberately run `"$richi_new_prefix/bin/richi" config set dev false` before migration.
For a managed Codex integration, inspect the old `richi setup --status` and
`readlink "$HOME/.local/bin/richi"`. If Claude Code is configured, also save the
old `richi setup --agent claude --status` now. Only after verifying that this is
the old Richi command symlink, move it to an unused backup name and adopt the skill:

```sh
mv -i "$HOME/.local/bin/richi" "$HOME/.local/bin/richi-before-schema3"
"$richi_new_prefix/bin/richi" setup --dry-run --replace-skill
"$richi_new_prefix/bin/richi" setup --yes --replace-skill
"$HOME/.local/bin/richi" setup --status
```

Setup preserves an existing skill outside discovery and retains unchanged
instruction/PATH blocks. Resolve any reported edited-block or ownership conflict
using [setup recovery](SETUP.md#резервные-копии-восстановление-и-удаление); do not
delete installation metadata. Without managed agents, create the replacement
command symlink with `ln -s "$richi_new_prefix/bin/richi" "$HOME/.local/bin/richi"`
after the same verified backup, and update any manually configured agent paths.

For managed Claude Code, use the saved old setup status. Back up its settings
and hook script, remove only the reported `hook_entry` from
`hooks.SessionStart`, and move its `richi/session_start.py` out of that path.
Then run the replacement's `setup --agent claude --dry-run --replace-skill` and
`setup --agent claude --yes --replace-skill`. Preserve unrelated hooks and settings;
the new hook must use the replacement environment's Python.

Open a new terminal/chat and restart maps. Automatic updates in the replacement
start disabled; enable them only if desired using the commands below. Leave the
old schedule disabled and use the replacement for future updates. Only after
verifying the new runtime, back up and explicitly migrate the selected database
as described in [graph migration](../skills/project-memory/references/GRAPH.md).

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
