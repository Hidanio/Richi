<a id="установка-и-конфигурация"></a>
# Installation and configuration

English | [Русский](INSTALLATION.ru.md)

<a id="обычная-установка"></a>
## Standard installation

Richi requires macOS or Linux and Python 3.9+ with the `sqlite3` module. Git must
be in PATH for Git sources. The runtime uses the Python standard library.
Windows support is not claimed for this release.

```sh
git clone https://github.com/Hidanio/Richi.git
cd Richi
python3 -m venv "$HOME/.local/share/richi/venv"
"$HOME/.local/share/richi/venv/bin/python" -m pip install --upgrade pip
"$HOME/.local/share/richi/venv/bin/python" -m pip install .
mkdir -p "$HOME/.local/bin"
ln -s "$HOME/.local/share/richi/venv/bin/richi" "$HOME/.local/bin/richi"
export PATH="$HOME/.local/bin:$PATH"
richi config show
```

These commands install a fixed package copy in a dedicated Python environment.
Normal mode runs that copy: editing, removing or switching branches in the source
checkout does not change the installed version. Installation does not require Codex.
Upgrading pip provides support for modern `pyproject.toml` packaging, including
fresh environments created by older Python distributions.

The `ln -s` command above creates a new command. If `~/.local/bin/richi` already
exists, inspect its target and replace only the previous Richi symlink. Add
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
# Возврат к предыдущей установленной версии:
richi update rollback
# Необязательно: фоновая проверка каждые 6 часов, без установки:
richi update auto enable --interval 6h
richi update auto status
# Только если вы хотите также устанавливать обновления автоматически:
richi update auto enable --interval 6h --install
# Возврат к одной проверке (выключает автоустановку):
richi update auto enable --interval 6h
# Отключение фонового режима:
richi update auto disable
```

The current and previous versions are retained; running processes keep their
code until they finish. Restart the map after an update. Workspace dev settings
and databases remain unchanged. See [UPDATES.md](UPDATES.md) for checks, cleanup
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

`dev` defaults to `false`. This setting applies to every command, agent and map
using this configuration in the selected workspace. Each CLI invocation in dev
uses the current source; edits and branch switches immediately affect subsequent
work. The map automatically reloads its server when Python or SQL files change.
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
# Разовая работа с другой базой без изменения конфигурации:
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
# Сервер в текущем терминале; Ctrl+C завершает его:
richi map serve --open
richi map serve --port 8766 --open
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

<a id="подключение-агента"></a>
## Agent integration

For Codex, use the [one-command installer](SETUP.md), or run `richi setup --yes`
from a supported dedicated installation. It installs the complete skill in
`~/.agents/skills/project-memory` and preserves existing global instructions.
`richi setup --dry-run` previews conflicts; `--replace-skill` explicitly backs
up an existing skill before adoption. The managed skill follows release updates
and rollback. For other agents, copy the complete `skills/project-memory`
directory, including its `references/` files, to the agent's skill directory.

Make sure the agent process can find the `richi` executable and the selected
configuration. First run `richi chat current` and `richi workspace list`. If the
chat is unbound, ask the user whether to use the current CLI workspace or another
one for this chat; save the answer with `chat bind --current` or `chat bind NAME`.
Then check `richi config show` and `richi check`: the mode, source and database
must match expectations. Terminal configuration does not guarantee the same
environment in an IDE or desktop application.

Example project instructions that explicitly authorize local updates:

```markdown
At the start of a new chat, inspect `richi chat current` and `richi workspace list`.
If unbound, ask once whether to use the current CLI workspace or another one for
this chat. An explicit workspace already named by the user is sufficient.
Save the answer with `richi chat bind --current` or `richi chat bind NAME`.
Reuse the binding in later turns and after compaction; do not change the global
CLI default. Rebinding requires the user's explicit request and `--replace`.
Before repository work, verify `richi project check PATH --id PROJECT_ID`, then
use `--project-path PATH` on knowledge operations. Do not infer a workspace from cwd.
Use the project-memory skill to recall prior engineering work and save reusable
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
