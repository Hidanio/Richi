<a id="установка-и-конфигурация"></a>
# Installation and configuration

English | [Русский](INSTALLATION.ru.md)

<a id="обычная-установка"></a>
<a id="standard-installation"></a>
## Install from source

Richi requires macOS or Linux and Python 3.9+ with the `sqlite3` module. Git must
be in PATH for Git sources. The runtime uses the Python standard library.
Windows support is not claimed for this release.

For a published release, use the [installer](SETUP.md). To build the default branch, first get its sources.

```sh
git clone https://github.com/Hidanio/Richi.git
cd Richi
```

To test a different branch or pull request, start the next block in the root
of that checkout. The commands assume a fresh installation with unused
environment and command paths. If an existing installation owns those paths, a separate OS user provides
a clean home for a side-by-side agent trial.

```sh
python3 -m venv "$HOME/.local/share/richi/venv"
"$HOME/.local/share/richi/venv/bin/python" -m pip install --upgrade pip
"$HOME/.local/share/richi/venv/bin/python" -m pip install .
mkdir -p "$HOME/.local/bin"
ln -s "$HOME/.local/share/richi/venv/bin/richi" "$HOME/.local/bin/richi"
export PATH="$HOME/.local/bin:$PATH"
richi config show
```

For agent integration, run `richi agents` in this terminal and choose the client.

These commands install a fixed package copy in a dedicated Python environment.
Normal mode runs that copy: editing, removing or switching branches in the source
checkout does not change the installed version. Installation does not require Codex.
Upgrading pip provides support for modern `pyproject.toml` packaging, including
fresh environments created by older Python distributions.

An existing Richi command may belong to a managed installation. Inspect its
target with `ls -l ~/.local/bin/richi` before choosing an installation path. Add
`~/.local/bin` to your shell's PATH for new terminals. You do not need to activate
the environment afterward. If an agent does not inherit the shell's PATH,
configure that directory or use the command's absolute path in local instructions.
Do not put a personal path into the portable skill.

For module invocation, use the installed environment's Python in isolated mode
to exclude the current directory and `PYTHONPATH`:

```sh
"$HOME/.local/share/richi/venv/bin/python" -I -m richi config show
```

For updates from published stable GitHub Releases, use:

```sh
richi update check
richi update apply
# Return to the previously installed release.
richi update rollback
# Optional background checks every six hours.
richi update auto enable --interval 6h
richi update auto status
# To enable automatic installation too.
richi update auto enable --interval 6h --install
# Return to checks only.
richi update auto enable --interval 6h
# Disable background updates.
richi update auto disable
```

The current and previous versions are retained; running processes keep their
code until they finish. A running map moves to the new version on the same URL
without manual steps; a map started by 1.1.0 or earlier is replaced by the next
knowledge command from a terminal or by `richi map` (see [UPDATES.md](UPDATES.md)).
`richi map stop` and `richi map restart` manage the map explicitly. Workspace
dev settings and databases remain unchanged. See [UPDATES.md](UPDATES.md) for checks, cleanup
and the initial transition from an older installation.

<a id="режим-разработки"></a>
## Development mode

Keep a normal installation. To work directly from source, select the checkout
root containing `src/richi/` and enable dev in the configuration:

```sh
richi config set development.source /path/to/Richi
richi config set dev true
richi config show
richi map
```

The equivalent JSON settings are:

```json
{
  "dev": true,
  "development": {"source": "/path/to/Richi"}
}
```

`dev` defaults to `false`. When enabled, knowledge commands run the current
checkout's runtime for the selected workspace, including calls from agents.
The installed launcher still handles commands such as `setup` and `update`.
Changing dev mode also moves the managed agent skills
([what dev switches](#what-dev-switches)). The map automatically reloads its server when Python or SQL files change.
Refresh the browser page to load interface changes.

```sh
richi config set dev false
richi config show
```

This command returns to the installed version. The database, captured Git bytes,
history and other settings remain unchanged; the two modes do not create two
copies of knowledge. The installed launcher handles `config show` and
`config set dev false`, so they remain available when dev sources are missing
or broken. New CLI invocations report broken dev sources instead of silently
falling back to the installed version.

Ordinary edits under `src/richi/` do not require reinstallation. Changes to the
installed launcher under `launcher/` require rebuilding and installing the package.
Use a separate test environment to develop the launcher itself; update a managed
release installation through `richi update apply`. Dev uses the same Python and
installed dependencies. The runtime currently uses the standard library; any
future dependencies must be installed in that environment.

`config set` changes settings without opening the database. The invoked CLI
operation and the map check schema compatibility before replacing the runtime;
there are no automatic migrations. For schema experiments, use a separate copy
of the database and its associated `artifacts/git/` directory.

<a id="что-переключает-dev"></a>
### What dev switches

Dev switches two things: the **runtime** (`src/richi/` for every CLI call,
agent call and the map) and the **managed Richi skill link of every
agent connected with `richi setup`**. With dev on, links normally point to
`<development.source>/skills/richi`. They use `skills/project-memory` while the
rollback release needs that directory or a pinned checkout supplies only the
legacy skill. When the directory changes, the launcher updates the name in
the managed instructions. An agent connected later
follows the agents already connected; the first one starts on the release skill
until the next `config set dev true`. The installed release supplies the launcher (`launcher/`, including
`setup`, `update` and `config`), the instruction templates, hooks and PATH blocks. In an agent chat, help pages (`richi help`, `--help`) and
`--version` also come from the installed release; check help edits with
`python scripts/check_cli_docs.py`, the tests, or a terminal without a chat ID.

Switching happens automatically. `richi config set dev true` or `false`
re-points every managed link in one recoverable, journaled transaction and
reports it in `agent_skills`:

```json
"agent_skills": {"status": "changed", "mode": "dev",
                 "source": "/path/to/Richi",
                 "target": "/path/to/Richi/skills/richi",
                 "changes": ["/home/me/.agents/skills/richi",
                             "/home/me/.claude/skills/richi"]}
```

`status` is `changed`, `unchanged`, `not_configured` (no `richi setup` yet),
`skipped` (see below), `unavailable` (not a managed installation), `conflict` (Richi refused: a
managed file was changed by hand or its metadata needs repair), `error` or `busy`
(an update or setup holds the installation); in the
last three cases no link changed. `config set development.source` re-points the
links while dev is on. `config show` reports where the links point and `matches_config`;
agents state the mode at the start of each chat and point out a mismatch.
Open a new agent chat after switching; running chats may keep the loaded skill.

`dev` belongs to a workspace, but skill links are user-wide: the latest
`config set dev` in any registered workspace decides where they point. A
configuration file outside the workspace registry, selected ad hoc with
`--config` or `RICHI_CONFIG`, changes only its own settings: `agent_skills`
reports `skipped` and the links stay where they are. If `config show`
reports `"matches_config": false`, rerun `richi config set dev true` (or `false`)
in the workspace you work in.

- Links follow a checkout only if it passes the same check as the dev runtime
  (`pyproject.toml` and `src/richi/`) and contains a valid `SKILL.md` in the directory selected above.
  Otherwise, and for dev without `development.source`, they point to the
  installed release; `reason` explains why. Restoring release links with
  `config set dev false` never depends on the checkout, even a missing or
  broken one.
- Skill edits in the checkout are development work, not drift.
- A release skill edited through its link, or a managed link replaced by hand,
  blocks the switch (`"status": "conflict"`) and is preserved. The configuration
  is saved anyway; reconcile the reported path and rerun the command.
- Skills not created by `richi setup` are never changed.
- `richi update apply` and `richi update rollback` keep the links on the checkout
  while dev is on. If the checkout lost its skill, activation restores the
  release skill. Releases up to 1.1.0 cannot maintain checkout links: run
  `richi config set dev false` before rolling back to one of them, otherwise it
  refuses the rollback and changes nothing. `config set dev true` warns in
  `agent_skills.rollback` when the previous release is one of them.

## Workspaces

Use [workspaces](WORKSPACES.md) for independent sets of related projects.
`richi use NAME` changes the default workspace, `richi use` opens a menu, and
`richi -w NAME ...` pins the workspace for one command. The existing database
remains in `default`; new workspaces receive separate databases and captured Git
artifact directories. Dev settings belong to the selected workspace and are
copied when a workspace is created.

The current CLI settings are a suggestion for a new chat. After the user's
answer, the agent saves the choice with `richi chat bind --current` or
`richi chat bind NAME`. The chat keeps its workspace after the CLI selection
changes. Codex IDs are detected automatically; other clients provide a stable
`RICHI_CHAT_ID` or `--chat ID`. See the [workspace guide](WORKSPACES.md).

<a id="выбор-хранилища"></a>
## Selecting storage

The direct database settings below are intended for a normal terminal. A detected
chat uses a registered workspace and rejects direct overrides through `--config`,
`--db`, `RICHI_CONFIG`, `RICHI_DB` and `RICHI_DATA_DIR`.

`richi config show` displays the resolved settings and selected runtime; reading
settings does not create a database. For a new installation, run `richi init`,
then `richi check`. For an existing database, set its path and run `check` before
writing data.

Settings can be supplied in a JSON file:

```json
{
  "database": "/path/to/existing/memory.sqlite3",
  "data_dir": "/path/to/richi-data",
  "port": 8765,
  "dev": false,
  "development": {"source": "/path/to/Richi"}
}
```

These absolute paths are synthetic. To connect existing memory, `database` must
point to the actual database. When `database` is specified, associated Git
artifacts, runtime files and backups remain beside that database. `data_dir`
determines the default database location when a separate database path is not
specified. Configuration does not move files.

```sh
richi --config /path/to/config.json config show
richi --config /path/to/config.json check
```

Settings use this precedence: explicit CLI argument, environment variable, JSON
configuration, platform default. Use `RICHI_DATA_DIR` for the data directory,
`RICHI_DB` for the database and `RICHI_PORT` for the port. `RICHI_CONFIG` selects
a JSON file; `--config PATH` takes precedence over it. Place `--db PATH` before
the subcommand; `--port` applies to map commands.

```sh
export RICHI_CONFIG=/path/to/config.json
richi config show
richi project list
# Use another database for this command.
richi --db /path/to/another.sqlite3 check
```

Platform defaults:

| System | Configuration | Data directory |
|---|---|---|
| macOS | `~/Library/Application Support/Richi/config.json` | `~/Library/Application Support/Richi/` |
| Linux | `${XDG_CONFIG_HOME:-~/.config}/richi/config.json` | `${XDG_DATA_HOME:-~/.local/share}/richi/` |

Without an explicit `database`, the file is named `memory.sqlite3` inside the
selected data directory. Relative JSON paths are resolved against that JSON
file's directory; relative CLI and environment paths are resolved against the
current working directory. Absolute paths are convenient for shared memory
accessed from different directories. Paths support `~`. An explicitly selected
missing or invalid configuration produces an error instead of silently choosing
another database.

By default, Richi uses the operating system's user data directory, separate from
the checkout. Check actual paths with `config show`, especially when a terminal,
IDE and agent have different environment variables. SQLite, `artifacts/git/`,
`backups/` and `runtime/` remain local. Do not put them in Git. Changing the
configuration path does not copy data or automatically update registered
project paths.

<a id="карта"></a>
## Map

```sh
richi map
# Run in the foreground. Press Ctrl+C to stop.
richi map serve --open
richi map serve --port 8766 --open
# Stop or restart the background map.
richi map stop
richi map restart
```

Both commands follow the shared `dev` configuration setting; the map needs no
separate mode flag. `richi map` starts or reuses the selected database's server
and opens a browser. The map is available only on loopback and displays the
graph, definitions, sources and Git navigation. Write knowledge through the CLI.
The map does not import conversations or execute procedures stored in records.

A running map notices `dev` switches and, after checking the new runtime, switches
to it while preserving its PID and URL. In dev it also watches Python and SQL
source files. Changing an SQL file does not migrate the database. Refresh the
browser page to load an updated interface.

If new sources fail syntax, import or schema compatibility checks, the map keeps
the previous running process and records diagnostics. Child graph and Git
operations read sources again, so broken dev sources can temporarily break those
operations even in the retained server. Fix the source or disable dev;
configuration management commands remain available. For a background map, the
log is `runtime/map-server.log` beside the selected database; for `map serve`,
it is in the terminal.

The checkout and the installed launcher agree on a map protocol version. If the
installed release is older or newer than the checkout in this respect, maps are
still started and reused, but they do not follow updates and `richi map` cannot
stop or replace them. Update whichever side is older (the installation with
`richi update apply`, or the checkout), or run `richi config set dev false`.

<a id="подключение-агента"></a>
## Agent integration

After installing the package, run `richi agents --status` to see which
clients it supports. Follow [setup for Codex, Claude Code and Cursor](SETUP.md)
to connect the client you use.

For other agents, copy the complete `skills/richi`
directory, including its `references/` files, to the agent's skill directory.

Make sure the agent process can find the `richi` executable and the selected
configuration. In Cursor, insert the `--chat` argument from this chat's
`sessionStart` hook after `richi` on every call. First run `richi chat current` and `richi workspace list`. If the
chat is unbound, ask the user whether to use the current CLI workspace or another
one for this chat; save the answer with `chat bind --current` or `chat bind NAME`.
Then check `richi config show` and `richi check`: the mode, source and database
must match expectations. Terminal configuration does not guarantee the same
environment in an IDE or desktop application.

Example project instructions that explicitly authorize local updates:

```markdown
In Cursor, add this chat's exact `--chat` argument from the `sessionStart` hook
after `richi` on every call. If no ID is available yet, defer memory work.
At the start of a new chat, inspect `richi chat current` and `richi workspace list`.
If unbound, ask once whether to use the current CLI workspace or another one for
this chat. An explicit workspace already named by the user is sufficient.
Save the answer with `richi chat bind --current` or `richi chat bind NAME`.
Reuse the binding in later turns and after compaction; do not change the global
CLI default. Rebinding requires the user's explicit request and `--replace`.
After binding, load the workspace's standing rules with `richi rules show`.
For repository work, verify `richi project check PATH --id PROJECT_ID`, then use
`--project-path PATH` on the rules check and knowledge operations. In Cursor,
request rules with `rules show --agent cursor`. Do not infer a workspace from cwd.
Use the `richi` skill to recall prior engineering work and save reusable
findings, decisions, failed experiments, and task outcomes. Use the configured
Richi store; inspect `richi config show` if its location is unclear. Local memory
updates are authorized during this project's work. Search before creating a new
record, preserve sources and scope, and use expected_updated_at for updates.
Code, task trackers, and pull requests remain authoritative for current state.
This authorization does not permit external messages or unrelated changes.
```

This is an example of a project owner choosing a mode of operation. The skill
itself does not imply permission to write arbitrary databases or change external
systems. MCP is optional: the agent invokes the installed CLI through process
execution tools. The CLI provides the `richi use` workspace menu; a separate MCP
integration can be added later.
